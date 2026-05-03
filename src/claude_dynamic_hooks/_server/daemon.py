"""Router daemon — lifecycle.

Owns: process lock, signal handlers, werkzeug WSGI server start/stop,
config-file watcher for hot reload of handler list / defaults.

Handlers are statically configured in `~/.config/cdh/config.toml`; the
router does not probe them. If a handler is down, the chain step fails
(typically ECONNREFUSED, ~1ms on loopback) and the chain continues per
`router_chain` semantics.

Hot reload: the watcher fires when the config file changes (`config_watcher`),
re-parses the TOML, and atomically swaps the handler list, hook defaults,
per-call wire timeout / byte cap, and reload-status fields by replacing
``self._snapshot``. On parse failure the prior chain is preserved and the
snapshot is replaced with one that flips ``last_reload_ok`` to False so
``/health`` can surface the failure to operators. The listening port and
Flask `MAX_CONTENT_LENGTH` are set at startup and not hot-reloadable —
changing them requires a daemon restart.

Signal handling: SIGTERM/SIGINT write a single byte to a pipe (the only
async-signal-safe action we need). A dedicated monitor thread blocks on
that pipe and calls ``server.shutdown()`` when woken. This avoids
spawning threads from the signal handler itself.
"""
from __future__ import annotations

import contextlib
import dataclasses
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import TracebackType
from typing import Any

from werkzeug.serving import make_server

from .. import config as config_mod
from ..paths import DaemonPaths, config_dir
from . import config_watcher, process_lock
from .transport.app import Snapshot, build_app

_BIND_HOST = "127.0.0.1"  # loopback only; never expose router off-host
_WATCHER_JOIN_TIMEOUT_S = 2.0
_LOG_MAX_BYTES = 10 * 1024 * 1024
_LOG_BACKUP_COUNT = 3

log = logging.getLogger("cdh.daemon")


def _snapshot_from(
    cfg: config_mod.Config, *, ok: bool, err: str | None,
) -> Snapshot:
    return Snapshot(
        handlers=tuple(cfg.handlers),
        defaults=cfg.defaults,
        request_timeout_s=cfg.daemon.request_timeout_s,
        outbound_max_bytes=cfg.daemon.wire_max_bytes,
        last_reload_at=time.time(),
        last_reload_ok=ok,
        last_reload_error=err,
    )


class Daemon:
    def __init__(self, paths: DaemonPaths) -> None:
        self._paths = paths
        self._server: Any = None
        self._observer: Any = None
        self._pool: ThreadPoolExecutor | None = None
        self._shutdown_pipe_r, self._shutdown_pipe_w = os.pipe()

        cfg = config_mod.load()
        self._port = cfg.daemon.port
        self._startup_max_body_bytes = cfg.daemon.wire_max_bytes
        self._max_workers = cfg.daemon.max_workers
        self._snapshot = _snapshot_from(cfg, ok=True, err=None)
        self._app = build_app(
            lambda: self._snapshot, max_body_bytes=self._startup_max_body_bytes,
        )

    def _reload(self) -> None:
        """Re-read config and swap the live state.

        On parse failure: keep prior handlers/defaults but flip the snapshot's
        reload-status fields so /health surfaces the failure.
        """
        try:
            cfg = config_mod.load()
        except Exception as e:
            log.warning("config reload failed; keeping prior chain: %r", e)
            self._snapshot = dataclasses.replace(
                self._snapshot,
                last_reload_at=time.time(),
                last_reload_ok=False,
                last_reload_error=repr(e),
            )
            return
        if cfg.daemon.port != self._port:
            log.warning(
                "config reload: [daemon].port change ignored "
                "(%d → %d); restart required", self._port, cfg.daemon.port,
            )
        if cfg.daemon.wire_max_bytes != self._startup_max_body_bytes:
            log.warning(
                "config reload: [daemon].wire_max_bytes change applied to "
                "router→handler hop only; inbound request cap stays at "
                "%d until restart", self._startup_max_body_bytes,
            )
        self._snapshot = _snapshot_from(cfg, ok=True, err=None)
        log.info("config reload: %d handler(s) loaded", len(cfg.handlers))

    def run(self) -> None:
        self._paths.ensure()
        lock_fd = process_lock.acquire(self._paths.pid_file)

        threading.Thread(
            target=self._shutdown_monitor, name="cdh-sigwait", daemon=True,
        ).start()
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

        self._observer = config_watcher.start(
            config_dir() / "config.toml", self._reload,
        )

        self._server = make_server(_BIND_HOST, self._port, self._app, threaded=True)
        if self._max_workers > 0:
            self._pool = ThreadPoolExecutor(
                max_workers=self._max_workers, thread_name_prefix="cdh-req",
            )
            self._install_bounded_pool(self._server, self._pool)
        try:
            self._server.serve_forever()
        finally:
            self._server.server_close()
            if self._observer is not None:
                self._observer.stop()
                self._observer.join(timeout=_WATCHER_JOIN_TIMEOUT_S)
            if self._pool is not None:
                self._pool.shutdown(wait=True, cancel_futures=True)
            for fd in (self._shutdown_pipe_r, self._shutdown_pipe_w):
                with contextlib.suppress(OSError):
                    os.close(fd)
            process_lock.release(lock_fd, self._paths.pid_file)

    @staticmethod
    def _install_bounded_pool(server: Any, pool: ThreadPoolExecutor) -> None:
        """Replace the server's per-request thread spawn with executor submit.

        ``threaded=True`` returns a server using `socketserver.ThreadingMixIn`,
        which spawns one unbounded daemon thread per request. We override
        ``process_request`` to submit to a fixed-size pool so a flood of
        inbound requests queues instead of spawning unbounded threads.
        """
        original_thread_target = server.process_request_thread

        def _process_request(request, client_address):
            pool.submit(original_thread_target, request, client_address)

        server.process_request = _process_request

    def _on_signal(self, *_: Any) -> None:
        # os.write is async-signal-safe; thread/lock allocation is not.
        # Keep bare try/except — contextlib.suppress allocates a manager
        # object, which we want to avoid in signal context.
        try:  # noqa: SIM105
            os.write(self._shutdown_pipe_w, b"x")
        except OSError:
            pass

    def _shutdown_monitor(self) -> None:
        try:
            os.read(self._shutdown_pipe_r, 1)
        except OSError:
            return
        if self._server is not None:
            self._server.shutdown()


def _configure_logging(log_path: Path) -> None:
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUP_COUNT,
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)


def _log_uncaught(
    et: type[BaseException],
    ev: BaseException,
    tb: TracebackType | None,
) -> None:
    log.critical("uncaught exception", exc_info=(et, ev, tb))


def main() -> None:
    paths = DaemonPaths.default()
    paths.ensure()
    _configure_logging(paths.stderr_log)

    sys.excepthook = _log_uncaught

    try:
        Daemon(paths).run()
    except SystemExit:
        raise
    except Exception:
        log.exception("daemon crashed")
        sys.exit(1)


if __name__ == "__main__":
    main()

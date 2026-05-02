"""Router daemon — lifecycle.

Owns: process lock, signal handlers, werkzeug WSGI server start/stop,
config-file watcher for hot reload of handler list / defaults.

Handlers are statically configured in `~/.config/cdh/config.toml`; the
router does not probe them. If a handler is down, the chain step fails
(typically ECONNREFUSED, ~1ms on loopback) and the chain continues per
`router_chain` semantics.

Hot reload: the watcher fires when the config file changes (`config_watcher`),
re-parses the TOML, and atomically swaps the handler list, hook defaults,
and per-call wire timeout / byte cap by replacing ``self._snapshot``. The
listening port and Flask `MAX_CONTENT_LENGTH` are set at startup and not
hot-reloadable — changing them requires a daemon restart.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from werkzeug.serving import make_server

from .. import config as config_mod
from ..paths import DaemonPaths, config_dir
from . import config_watcher, process_lock
from .transport.app import Snapshot, build_app

_BIND_HOST = "127.0.0.1"  # loopback only; never expose router off-host
_WATCHER_JOIN_TIMEOUT_S = 2.0


def _snapshot_from(cfg: config_mod.Config) -> Snapshot:
    return Snapshot(
        handlers=tuple(cfg.handlers),
        defaults=cfg.defaults,
        request_timeout_s=cfg.daemon.request_timeout_s,
        max_body_bytes=cfg.daemon.wire_max_bytes,
    )


class Daemon:
    def __init__(self, paths: DaemonPaths) -> None:
        self._paths = paths
        self._server: Any = None
        self._observer: Any = None
        self._pool: ThreadPoolExecutor | None = None

        cfg = config_mod.load()
        self._port = cfg.daemon.port
        self._startup_max_body_bytes = cfg.daemon.wire_max_bytes
        self._max_workers = cfg.daemon.max_workers
        self._snapshot = _snapshot_from(cfg)
        self._app = build_app(
            lambda: self._snapshot, max_body_bytes=self._startup_max_body_bytes,
        )

    def _reload(self) -> None:
        """Re-read config and swap the live state. Logs and skips on parse error."""
        try:
            cfg = config_mod.load()
        except Exception as e:
            sys.stderr.write(f"config reload failed; keeping prior chain: {e!r}\n")
            sys.stderr.flush()
            return
        if cfg.daemon.port != self._port:
            sys.stderr.write(
                f"config reload: [daemon].port change ignored "
                f"({self._port} → {cfg.daemon.port}); restart required\n"
            )
        if cfg.daemon.wire_max_bytes != self._startup_max_body_bytes:
            sys.stderr.write(
                f"config reload: [daemon].wire_max_bytes change applied to "
                f"router→handler hop only; inbound request cap stays at "
                f"{self._startup_max_body_bytes} until restart\n"
            )
        self._snapshot = _snapshot_from(cfg)
        sys.stderr.write(
            f"config reload: {len(cfg.handlers)} handler(s) loaded\n"
        )
        sys.stderr.flush()

    def run(self) -> None:
        self._paths.ensure()
        lock_fd = process_lock.acquire(self._paths.pid_file)

        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

        # Silence werkzeug's per-request log; transport.app emits our own format.
        logging.getLogger("werkzeug").setLevel(logging.ERROR)

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
        if self._server is not None:
            threading.Thread(target=self._server.shutdown, daemon=True).start()


def main() -> None:
    paths = DaemonPaths.default()
    paths.ensure()
    log_fd = os.open(str(paths.stderr_log), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(log_fd, 2)
    os.close(log_fd)

    try:
        Daemon(paths).run()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

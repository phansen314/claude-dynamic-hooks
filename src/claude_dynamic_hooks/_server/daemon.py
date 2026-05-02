"""Router daemon — lifecycle.

Owns: process lock, signal handlers, werkzeug WSGI server start/stop.
HTTP routes live in `transport/app.py`.

Handlers are statically configured in `~/.config/cdh/config.toml`; the
router does not probe them. If a handler is down, the chain step fails
(typically ECONNREFUSED, ~1ms on loopback) and the chain continues per
`router_chain` semantics.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import traceback
from typing import Any

from werkzeug.serving import make_server

from .. import config as config_mod
from ..config import HandlerEntry, HookDefaults
from ..paths import DaemonPaths
from . import process_lock
from .transport.app import build_app

_BIND_HOST = "127.0.0.1"  # loopback only; never expose router off-host


class Daemon:
    def __init__(
        self,
        paths: DaemonPaths,
        port: int,
        max_body_bytes: int,
        request_timeout_s: float,
        handlers: list[HandlerEntry],
        defaults: HookDefaults,
    ) -> None:
        self._paths = paths
        self._port = port
        self._server: Any = None
        self._app = build_app(
            handlers=list(handlers),
            defaults=defaults,
            request_timeout_s=request_timeout_s,
            max_body_bytes=max_body_bytes,
        )

    def run(self) -> None:
        self._paths.ensure()
        lock_fd = process_lock.acquire(self._paths.pid_file)

        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

        # Silence werkzeug's per-request log; transport.app emits our own format.
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        self._server = make_server(_BIND_HOST, self._port, self._app, threaded=True)
        try:
            self._server.serve_forever()
        finally:
            self._server.server_close()
            process_lock.release(lock_fd, self._paths.pid_file)

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
        cfg = config_mod.load()
    except Exception:
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)

    Daemon(
        paths=paths,
        port=cfg.daemon.port,
        max_body_bytes=cfg.daemon.wire_max_bytes,
        request_timeout_s=cfg.daemon.request_timeout_s,
        handlers=list(cfg.handlers),
        defaults=cfg.defaults,
    ).run()


if __name__ == "__main__":
    main()

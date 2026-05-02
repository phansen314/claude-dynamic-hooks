"""HTTP client — round-trip, timeout, oversize, non-2xx, malformed."""
from __future__ import annotations

import http.server
import json
import threading
import time
from collections.abc import Callable
from typing import Any

from claude_dynamic_hooks._server.transport.client import post


class _StubServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


Dispatch = Callable[[str, str, dict | None], tuple[int, Any]]


def _make_handler(dispatch: Dispatch):
    class _H(http.server.BaseHTTPRequestHandler):
        def _do(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                body = None
            status, payload = dispatch(method, self.path, body)
            data = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if data:
                self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            self._do("GET")

        def do_POST(self):  # noqa: N802
            self._do("POST")

        def log_message(self, *a, **kw):  # noqa: A002
            return

    return _H


def _start(dispatch: Dispatch) -> tuple[_StubServer, threading.Thread]:
    server = _StubServer(("127.0.0.1", 0), _make_handler(dispatch))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, t


def _stop(server: _StubServer, t: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    t.join(timeout=2)


def _port(server: _StubServer) -> int:
    return server.server_address[1]


def test_post_round_trip():
    def dispatch(method, path, body):
        assert method == "POST"
        return 200, {"echo": body}

    server, t = _start(dispatch)
    try:
        url = f"http://127.0.0.1:{_port(server)}/x"
        out = post(url, {"k": 1}, timeout_s=2.0, max_response_bytes=8192)
        assert out == {"echo": {"k": 1}}
    finally:
        _stop(server, t)


def test_post_returns_none_on_connection_refused():
    out = post("http://127.0.0.1:1/x", {}, timeout_s=0.5, max_response_bytes=8192)
    assert out is None


def test_post_returns_none_on_non_2xx():
    server, t = _start(lambda m, p, b: (500, {"error": "boom"}))
    try:
        url = f"http://127.0.0.1:{_port(server)}/x"
        out = post(url, {}, timeout_s=2.0, max_response_bytes=8192)
        assert out is None
    finally:
        _stop(server, t)


def test_post_returns_none_on_oversize_response():
    big = {"x": "y" * 1000}
    server, t = _start(lambda m, p, b: (200, big))
    try:
        url = f"http://127.0.0.1:{_port(server)}/x"
        out = post(url, {}, timeout_s=2.0, max_response_bytes=100)
        assert out is None
    finally:
        _stop(server, t)


def test_post_returns_none_on_timeout():
    def slow(method, path, body):
        time.sleep(2.0)
        return 200, {}

    server, t = _start(slow)
    try:
        url = f"http://127.0.0.1:{_port(server)}/x"
        out = post(url, {}, timeout_s=0.3, max_response_bytes=8192)
        assert out is None
    finally:
        _stop(server, t)


def test_post_returns_none_on_empty_body():
    server, t = _start(lambda m, p, b: (200, None))
    try:
        url = f"http://127.0.0.1:{_port(server)}/x"
        out = post(url, {}, timeout_s=2.0, max_response_bytes=8192)
        assert out is None
    finally:
        _stop(server, t)



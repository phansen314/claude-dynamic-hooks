"""Router chain — sequential, terminal, last-non-null, error coercion."""
from __future__ import annotations

import http.server
import json
import threading

from claude_dynamic_hooks._server.router_chain import run as chain_run
from claude_dynamic_hooks.config import ChainOverride, HandlerEntry
from claude_dynamic_hooks.events import EventType


def _wrap(envelope: dict | None) -> dict:
    """Build the wire response shape: {'envelope': str|null}."""
    return {"envelope": json.dumps(envelope) if envelope is not None else None}


class _StubServer(http.server.ThreadingHTTPServer):
    """Tiny stdlib stub HTTP server, returns canned (status, body) for every request."""
    daemon_threads = True
    allow_reuse_address = True


def _make_handler_class(status: int, body: dict, captured: dict | None = None):
    payload = json.dumps(body).encode()
    class _H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if captured is not None:
                req = json.loads(raw)
                captured["payload_str"] = req.get("payload")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *a, **kw): pass  # noqa: A002
    return _H


def _start_handler(
    envelope: dict | None,
    *,
    status: int = 200,
    raw: dict | None = None,
    captured: dict | None = None,
):
    """Spawn a stub handler that returns *envelope* wrapped per CDHP wire.

    *raw* lets a test inject an arbitrary response body to exercise malformed
    cases (e.g. missing the `envelope` key). *captured*, if given, receives
    the raw `payload` string the handler saw under key `payload_str`.
    """
    body = raw if raw is not None else _wrap(envelope)
    server = _StubServer(
        ("127.0.0.1", 0), _make_handler_class(status, body, captured),
    )
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, t


def _stop(server: _StubServer, t: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    t.join(timeout=2)


def _handler(name: str, base_url: str, *, terminal: bool = False) -> HandlerEntry:
    override = ChainOverride(terminal=terminal) if terminal else ChainOverride()
    return HandlerEntry(
        name=name,
        url=base_url,
        events={EventType.PRE_TOOL_USE: override},
    )


def test_chain_returns_last_non_null():
    s1, t1 = _start_handler({"a": 1})
    s2, t2 = _start_handler({"b": 2})
    try:
        handlers = [
            _handler("h1", f"http://127.0.0.1:{s1.server_address[1]}"),
            _handler("h2", f"http://127.0.0.1:{s2.server_address[1]}"),
        ]
        result = chain_run(
            event=EventType.PRE_TOOL_USE,
            params={},
            handlers=handlers,
            default_timeout_s=2.0,
            default_max_bytes=8192,
        )
        assert result == {"b": 2}
    finally:
        _stop(s1, t1)
        _stop(s2, t2)


def test_terminal_short_circuits():
    s1, t1 = _start_handler({"a": 1})
    s2, t2 = _start_handler({"b": 2})
    try:
        handlers = [
            _handler("h1", f"http://127.0.0.1:{s1.server_address[1]}", terminal=True),
            _handler("h2", f"http://127.0.0.1:{s2.server_address[1]}"),
        ]
        result = chain_run(
            event=EventType.PRE_TOOL_USE,
            params={},
            handlers=handlers,
            default_timeout_s=2.0,
            default_max_bytes=8192,
        )
        assert result == {"a": 1}
    finally:
        _stop(s1, t1)
        _stop(s2, t2)


def test_null_envelope_treated_as_abstain():
    s1, t1 = _start_handler(None)
    s2, t2 = _start_handler({"b": 2})
    try:
        handlers = [
            _handler("h1", f"http://127.0.0.1:{s1.server_address[1]}"),
            _handler("h2", f"http://127.0.0.1:{s2.server_address[1]}"),
        ]
        result = chain_run(
            event=EventType.PRE_TOOL_USE,
            params={},
            handlers=handlers,
            default_timeout_s=2.0,
            default_max_bytes=8192,
        )
        assert result == {"b": 2}
    finally:
        _stop(s1, t1)
        _stop(s2, t2)


def test_malformed_response_treated_as_null():
    """Handler returns body without `envelope` key — chain logs + continues."""
    s1, t1 = _start_handler(None, raw={"oops": "wrong shape"})
    s2, t2 = _start_handler({"b": 2})
    try:
        handlers = [
            _handler("h1", f"http://127.0.0.1:{s1.server_address[1]}"),
            _handler("h2", f"http://127.0.0.1:{s2.server_address[1]}"),
        ]
        result = chain_run(
            event=EventType.PRE_TOOL_USE,
            params={},
            handlers=handlers,
            default_timeout_s=2.0,
            default_max_bytes=8192,
        )
        assert result == {"b": 2}
    finally:
        _stop(s1, t1)
        _stop(s2, t2)


def test_error_coerces_to_null_chain_continues():
    s1, t1 = _start_handler({"err": "x"}, status=500)
    s2, t2 = _start_handler({"b": 2})
    try:
        handlers = [
            _handler("h1", f"http://127.0.0.1:{s1.server_address[1]}"),
            _handler("h2", f"http://127.0.0.1:{s2.server_address[1]}"),
        ]
        result = chain_run(
            event=EventType.PRE_TOOL_USE,
            params={},
            handlers=handlers,
            default_timeout_s=2.0,
            default_max_bytes=8192,
        )
        assert result == {"b": 2}
    finally:
        _stop(s1, t1)
        _stop(s2, t2)


def test_unreachable_handler_skipped():
    """Handler whose URL is unreachable → chain returns None, no crash.

    Uses an ephemeral port we briefly bind then close, rather than a magic
    low number — works regardless of firewall config and avoids hanging to
    the timeout in environments where DROP rules block low ports.
    """
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    handler = HandlerEntry(
        name="dead", url=f"http://127.0.0.1:{port}",
        events={EventType.PRE_TOOL_USE: ChainOverride()},
    )
    result = chain_run(
        event=EventType.PRE_TOOL_USE,
        params={},
        handlers=[handler],
        default_timeout_s=0.3,
        default_max_bytes=8192,
    )
    assert result is None


def test_all_null_returns_none():
    s1, t1 = _start_handler(None)
    s2, t2 = _start_handler(None)
    try:
        handlers = [
            _handler("h1", f"http://127.0.0.1:{s1.server_address[1]}"),
            _handler("h2", f"http://127.0.0.1:{s2.server_address[1]}"),
        ]
        result = chain_run(
            event=EventType.PRE_TOOL_USE,
            params={},
            handlers=handlers,
            default_timeout_s=2.0,
            default_max_bytes=8192,
        )
        assert result is None
    finally:
        _stop(s1, t1)
        _stop(s2, t2)


def test_payload_roundtrips_byte_identical_dict():
    """The opaque-string wrap must preserve arbitrary Claude payload bytes.

    Mirrors `pre_tool_use_read` example in docs/openapi.yaml plus Unicode,
    embedded quotes/newlines, nested objects, and explicit nulls — the bits
    a naive json.dumps tweak (e.g. ensure_ascii=True) would corrupt.
    """
    captured: dict = {}
    s, t = _start_handler(None, captured=captured)
    try:
        payload = {
            "session_id": "abc123",
            "tool_name": "Read",
            "tool_input": {
                "file_path": "/tmp/résumé\n\"weird\".txt",
                "offset": None,
                "extra": {"nested": ["a", "b", 1, None]},
            },
        }
        chain_run(
            event=EventType.PRE_TOOL_USE,
            params=payload,
            handlers=[_handler("h", f"http://127.0.0.1:{s.server_address[1]}")],
            default_timeout_s=2.0,
            default_max_bytes=8192,
        )
        assert "payload_str" in captured, "handler did not receive payload"
        assert isinstance(captured["payload_str"], str)
        assert json.loads(captured["payload_str"]) == payload
    finally:
        _stop(s, t)


def test_empty_handlers_returns_none():
    """Caller filters to handlers declaring the event; empty list → no work."""
    result = chain_run(
        event=EventType.PRE_TOOL_USE,
        params={},
        handlers=[],
        default_timeout_s=2.0,
        default_max_bytes=8192,
    )
    assert result is None

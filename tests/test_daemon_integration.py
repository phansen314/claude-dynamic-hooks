"""Integration: spawn router daemon, drive HTTP, verify probe + chain."""
from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from textwrap import dedent

import pytest

# Standalone CDHP handler-daemon, no cdh dep. Response controlled via
# CDH_TEST_RESPONSE env var (JSON object envelope, or "null" for abstain).
# Wire per docs/openapi.yaml: request {"payload": "<str>"}, response
# {"envelope": "<str>"|null}.
#
# CDH_TEST_MODE selects abnormal behavior for failure-mode tests:
#   echo            (default) — emit RESPONSE as envelope
#   raise_500       — return 500
#   hang            — sleep forever (let router timeout fire)
#   text_plain      — return 200 + text/plain body
#   missing_envelope — return 200 + JSON missing the `envelope` key
#   record          — write request count to CDH_TEST_RECORD_FILE then echo
#   echo_payload    — return 200 + {"envelope": "<received payload string>"} so the
#                     router unwraps it and the test sees the inbound payload dict
_HANDLER_TEMPLATE = '''
import http.server, json, os, signal, sys, threading, time

NAME = os.environ.get("CDH_HANDLER_NAME", "test")
EVENTS = ["preToolUse"]
RESPONSE = json.loads(os.environ.get("CDH_TEST_RESPONSE") or "null")
MODE = os.environ.get("CDH_TEST_MODE") or "echo"
RECORD_FILE = os.environ.get("CDH_TEST_RECORD_FILE")
started = time.monotonic()
_count_lock = threading.Lock()

def _record():
    if not RECORD_FILE:
        return
    with _count_lock:
        try:
            n = int(open(RECORD_FILE).read().strip() or "0")
        except (OSError, ValueError):
            n = 0
        with open(RECORD_FILE, "w") as f:
            f.write(str(n + 1))

class H(http.server.BaseHTTPRequestHandler):
    def _send(self, status, body, *, content_type="application/json"):
        if isinstance(body, (dict, list)) or body is None:
            data = b"" if body is None else json.dumps(body).encode()
        else:
            data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data: self.wfile.write(data)
    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"name": NAME, "protocol_version": "1.0",
                             "events": EVENTS, "uptime_s": int(time.monotonic() - started)})
        else:
            self._send(404, {"error": "not found"})
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.path.startswith("/hooks/"):
            event = self.path[len("/hooks/"):]
            if event not in EVENTS:
                self._send(404, {"error": "not declared"}); return
            _record()
            if MODE == "raise_500":
                self._send(500, {"error": "boom"}); return
            if MODE == "hang":
                # Block forever; router must time out and chain continues.
                while True:
                    time.sleep(1)
            if MODE == "text_plain":
                self._send(200, "hello", content_type="text/plain"); return
            if MODE == "missing_envelope":
                self._send(200, {"foo": "bar"}); return
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                self._send(400, {"error": "bad json"}); return
            payload_str = body.get("payload") if isinstance(body, dict) else None
            if not isinstance(payload_str, str):
                self._send(400, {"error": "missing payload"}); return
            if MODE == "echo_payload":
                self._send(200, {"envelope": payload_str}); return
            envelope = json.dumps(RESPONSE) if RESPONSE is not None else None
            self._send(200, {"envelope": envelope})
            return
        self._send(404, {"error": "not found"})
    def log_message(self, *a, **kw): pass

bind = os.environ["CDH_BIND_ADDR"]
host, _, port = bind.rpartition(":")
class S(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
server = S((host, int(port)), H)

def shutdown(*_):
    threading.Thread(target=server.shutdown, daemon=True).start()
signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)
try:
    server.serve_forever()
finally:
    server.server_close()
'''


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def _spawn_handler(
    tmp: Path,
    name: str,
    *,
    response: object = None,
    raise_500: bool = False,
    mode: str | None = None,
    record_file: Path | None = None,
) -> tuple[subprocess.Popen, str, int]:
    """Spawn a standalone handler. Returns (proc, base_url, port).

    ``mode`` overrides default echo behavior (see ``_HANDLER_TEMPLATE`` for
    supported values). ``raise_500=True`` is a legacy shortcut for
    ``mode="raise_500"``.
    """
    if raise_500 and mode is None:
        mode = "raise_500"
    handler_dir = tmp / "handlers" / name
    _write(handler_dir / "handler.py", _HANDLER_TEMPLATE)
    port = _free_port()
    addr = f"127.0.0.1:{port}"
    env = os.environ.copy()
    env["CDH_BIND_ADDR"] = addr
    env["CDH_HANDLER_NAME"] = name
    env["CDH_TEST_RESPONSE"] = json.dumps(response) if response is not None else "null"
    env["CDH_TEST_MODE"] = mode or "echo"
    if record_file is not None:
        env["CDH_TEST_RECORD_FILE"] = str(record_file)
    proc = subprocess.Popen(
        [sys.executable, str(handler_dir / "handler.py")],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    base = f"http://{addr}"
    for _ in range(60):
        time.sleep(0.05)
        try:
            with urllib.request.urlopen(base + "/health", timeout=0.3) as r:
                if r.status == 200:
                    return proc, base, port
        except (urllib.error.URLError, OSError):
            continue
    proc.kill()
    stderr = proc.stderr.read().decode() if proc.stderr else ""
    pytest.fail(f"handler {name} did not come up; stderr: {stderr}")


def _stop_handler(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


def _make_user_config(tmp: Path, body: str) -> Path:
    user_dir = tmp / "user"
    user_dir.mkdir(parents=True, exist_ok=True)
    _write(user_dir / "config.toml", dedent(body))
    return user_dir


def _spawn_router(
    tmp: Path, *, user_config_dir: Path, port: int, ready_timeout: float = 6.0,
) -> subprocess.Popen:
    env = os.environ.copy()
    env["CDH_STATE_DIR"] = str(tmp / "state")
    env["CDH_CONFIG_DIR"] = str(user_config_dir)
    proc = subprocess.Popen(
        [sys.executable, "-m", "claude_dynamic_hooks._server.daemon"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + ready_timeout
    while time.monotonic() < deadline:
        time.sleep(0.05)
        try:
            with urllib.request.urlopen(base + "/health", timeout=0.3) as r:
                if r.status == 200:
                    return proc
        except (urllib.error.URLError, OSError):
            continue
    proc.kill()
    stderr = proc.stderr.read().decode() if proc.stderr else ""
    pytest.fail(f"router did not come up; stderr: {stderr}")


def _shutdown(proc: subprocess.Popen) -> bytes:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
    return proc.stderr.read() if proc.stderr else b""


def _post(url: str, obj: dict, timeout: float = 3.0) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else {})


def _get(url: str, timeout: float = 2.0) -> tuple[int, dict]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read())


_DENY_RESPONSE = {"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "test deny",
}}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_no_handlers_pretooluse_returns_empty(tmp_path):
    """No handlers + no user-configured default → router returns `{}` so
    Claude Code applies its normal permission flow."""
    port = _free_port()
    user_dir = _make_user_config(tmp_path, f"""
        [daemon]
        port = {port}
    """)
    proc = _spawn_router(tmp_path, user_config_dir=user_dir, port=port)
    try:
        status, body = _post(f"http://127.0.0.1:{port}/hooks/preToolUse", {})
        assert status == 200
        assert body == {}
    finally:
        stderr = _shutdown(proc)
    assert b"Traceback" not in stderr, stderr.decode()


def test_user_can_opt_into_ask_default(tmp_path):
    """Built-in default is empty; user opts into `ask` via `[hook_defaults]`."""
    port = _free_port()
    user_dir = _make_user_config(tmp_path, f"""
        [daemon]
        port = {port}

        [hook_defaults]
        preToolUse = "ask"
    """)
    proc = _spawn_router(tmp_path, user_config_dir=user_dir, port=port)
    try:
        status, body = _post(f"http://127.0.0.1:{port}/hooks/preToolUse", {})
        assert status == 200
        assert body["hookSpecificOutput"]["permissionDecision"] == "ask"
    finally:
        _shutdown(proc)


def test_no_handlers_post_tool_use_returns_empty(tmp_path):
    port = _free_port()
    user_dir = _make_user_config(tmp_path, f"""
        [daemon]
        port = {port}
    """)
    proc = _spawn_router(tmp_path, user_config_dir=user_dir, port=port)
    try:
        status, body = _post(f"http://127.0.0.1:{port}/hooks/postToolUse", {})
        assert status == 200
        assert body == {}
    finally:
        _shutdown(proc)


def test_handler_returns_envelope(tmp_path):
    port = _free_port()
    h_proc, h_url, h_port = _spawn_handler(tmp_path, "deny_h", response=_DENY_RESPONSE)
    try:
        user_dir = _make_user_config(tmp_path, f"""
            [daemon]
            port = {port}

            [[handler]]
            name = "deny_h"
            url = "{h_url}"
            events = ["preToolUse"]
        """)
        proc = _spawn_router(tmp_path, user_config_dir=user_dir, port=port)
        try:
            status, body = _post(f"http://127.0.0.1:{port}/hooks/preToolUse", {})
            assert status == 200
            assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
        finally:
            _shutdown(proc)
    finally:
        _stop_handler(h_proc)


def test_handler_returning_none_falls_to_empty(tmp_path):
    """Handler abstains, no user default → router returns `{}`."""
    port = _free_port()
    h_proc, h_url, _ = _spawn_handler(tmp_path, "noop_h")
    try:
        user_dir = _make_user_config(tmp_path, f"""
            [daemon]
            port = {port}

            [[handler]]
            name = "noop_h"
            url = "{h_url}"
            events = ["preToolUse"]
        """)
        proc = _spawn_router(tmp_path, user_config_dir=user_dir, port=port)
        try:
            status, body = _post(f"http://127.0.0.1:{port}/hooks/preToolUse", {})
            assert status == 200
            assert body == {}
        finally:
            _shutdown(proc)
    finally:
        _stop_handler(h_proc)


def test_unknown_event_returns_404(tmp_path):
    port = _free_port()
    user_dir = _make_user_config(tmp_path, f"""
        [daemon]
        port = {port}
    """)
    proc = _spawn_router(tmp_path, user_config_dir=user_dir, port=port)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/hooks/notAnEvent",
            data=b"{}", headers={"Content-Type": "application/json"}, method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=2.0)
        assert exc_info.value.code == 404
    finally:
        _shutdown(proc)


def test_router_health_lists_handlers(tmp_path):
    port = _free_port()
    h_proc, h_url, _ = _spawn_handler(tmp_path, "noop_h")
    try:
        user_dir = _make_user_config(tmp_path, f"""
            [daemon]
            port = {port}

            [[handler]]
            name = "noop_h"
            url = "{h_url}"
            events = ["preToolUse"]
        """)
        proc = _spawn_router(tmp_path, user_config_dir=user_dir, port=port)
        try:
            status, body = _get(f"http://127.0.0.1:{port}/health")
            assert status == 200
            assert body["ok"] is True
            assert body["handlers"][0]["name"] == "noop_h"
            assert "preToolUse" in body["handlers"][0]["events"]
        finally:
            _shutdown(proc)
    finally:
        _stop_handler(h_proc)


def test_handler_500_coerces_to_default(tmp_path):
    port = _free_port()
    h_proc, h_url, _ = _spawn_handler(tmp_path, "boom_h", raise_500=True)
    try:
        user_dir = _make_user_config(tmp_path, f"""
            [daemon]
            port = {port}

            [[handler]]
            name = "boom_h"
            url = "{h_url}"
            events = ["preToolUse"]
        """)
        proc = _spawn_router(tmp_path, user_config_dir=user_dir, port=port)
        try:
            status, body = _post(f"http://127.0.0.1:{port}/hooks/preToolUse", {})
            assert status == 200
            assert body == {}
        finally:
            _shutdown(proc)
    finally:
        _stop_handler(h_proc)


def test_router_starts_with_no_handler_running(tmp_path):
    """Configure a handler URL with nothing running there. Router still starts;
    chain step fails (ECONNREFUSED) and falls through to empty default."""
    port = _free_port()
    dead_port = _free_port()
    user_dir = _make_user_config(tmp_path, f"""
        [daemon]
        port = {port}

        [[handler]]
        name = "ghost"
        url = "http://127.0.0.1:{dead_port}"
        events = ["preToolUse"]
    """)
    proc = _spawn_router(tmp_path, user_config_dir=user_dir, port=port)
    try:
        status, body = _post(f"http://127.0.0.1:{port}/hooks/preToolUse", {})
        assert status == 200
        assert body == {}
    finally:
        _shutdown(proc)


def test_handler_started_after_router_works_immediately(tmp_path):
    """Start router with handler URL configured, no handler yet. Start handler.
    First request after handler is up routes correctly — no probe latency."""
    port = _free_port()
    h_port = _free_port()
    h_url = f"http://127.0.0.1:{h_port}"
    user_dir = _make_user_config(tmp_path, f"""
        [daemon]
        port = {port}

        [[handler]]
        name = "late_h"
        url = "{h_url}"
        events = ["preToolUse"]
    """)
    proc = _spawn_router(tmp_path, user_config_dir=user_dir, port=port)
    try:
        # Pre-handler request: ECONNREFUSED → empty default fires.
        _, body = _post(f"http://127.0.0.1:{port}/hooks/preToolUse", {})
        assert body == {}

        # Now start the handler.
        handler_dir = tmp_path / "handlers" / "late_h"
        _write(handler_dir / "handler.py", _HANDLER_TEMPLATE)
        env = os.environ.copy()
        env["CDH_BIND_ADDR"] = f"127.0.0.1:{h_port}"
        env["CDH_HANDLER_NAME"] = "late_h"
        env["CDH_TEST_RESPONSE"] = json.dumps(_DENY_RESPONSE)
        env["CDH_TEST_RAISE"] = "0"
        h_proc = subprocess.Popen(
            [sys.executable, str(handler_dir / "handler.py")],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            # Wait briefly for handler bind, then verify chain reaches it.
            deadline = time.monotonic() + 3.0
            decision = None
            while time.monotonic() < deadline:
                time.sleep(0.1)
                try:
                    _, body = _post(f"http://127.0.0.1:{port}/hooks/preToolUse", {})
                    decision = body["hookSpecificOutput"]["permissionDecision"]
                    if decision == "deny":
                        break
                except OSError:
                    continue
            assert decision == "deny", f"chain never reached handler; got {decision}"
        finally:
            _stop_handler(h_proc)
    finally:
        _shutdown(proc)


def test_sigterm_graceful_shutdown(tmp_path):
    port = _free_port()
    user_dir = _make_user_config(tmp_path, f"""
        [daemon]
        port = {port}
    """)
    proc = _spawn_router(tmp_path, user_config_dir=user_dir, port=port)
    t0 = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    ret = proc.wait(timeout=5.0)
    elapsed = time.monotonic() - t0
    assert ret == 0
    assert elapsed < 5.0


def _daemon_log(tmp_path: Path) -> str:
    log = tmp_path / "state" / "daemon.log"
    return log.read_text() if log.exists() else ""


def test_handler_hang_times_out_chain_continues(tmp_path):
    """Handler accepts TCP but never responds. Router timeout fires; chain
    coerces step to null; empty envelope returned to caller."""
    port = _free_port()
    h_proc, h_url, _ = _spawn_handler(tmp_path, "hang_h", mode="hang")
    try:
        user_dir = _make_user_config(tmp_path, f"""
            [daemon]
            port = {port}
            request_timeout_s = 0.5

            [[handler]]
            name = "hang_h"
            url = "{h_url}"
            events = ["preToolUse"]
        """)
        proc = _spawn_router(tmp_path, user_config_dir=user_dir, port=port)
        try:
            t0 = time.monotonic()
            status, body = _post(
                f"http://127.0.0.1:{port}/hooks/preToolUse", {}, timeout=5.0
            )
            elapsed = time.monotonic() - t0
            assert status == 200
            assert body == {}
            assert elapsed < 3.0, f"router didn't honor 0.5s timeout; took {elapsed:.2f}s"
        finally:
            _shutdown(proc)
        log = _daemon_log(tmp_path)
        assert "error/unreachable" in log, (
            f"expected timeout/unreachable log; got: {log!r}"
        )
    finally:
        _stop_handler(h_proc)


def test_handler_text_plain_response_coerces_to_null(tmp_path):
    """Handler returns 200 + text/plain body. Chain treats as null; empty default."""
    port = _free_port()
    h_proc, h_url, _ = _spawn_handler(tmp_path, "text_h", mode="text_plain")
    try:
        user_dir = _make_user_config(tmp_path, f"""
            [daemon]
            port = {port}

            [[handler]]
            name = "text_h"
            url = "{h_url}"
            events = ["preToolUse"]
        """)
        proc = _spawn_router(tmp_path, user_config_dir=user_dir, port=port)
        try:
            status, body = _post(f"http://127.0.0.1:{port}/hooks/preToolUse", {})
            assert status == 200
            assert body == {}
        finally:
            _shutdown(proc)
    finally:
        _stop_handler(h_proc)


def test_handler_missing_envelope_key_logged_as_malformed(tmp_path):
    """Handler returns 200 + JSON object missing `envelope` key. Chain treats
    as malformed envelope; logs and falls through to empty default."""
    port = _free_port()
    h_proc, h_url, _ = _spawn_handler(tmp_path, "missing_h", mode="missing_envelope")
    try:
        user_dir = _make_user_config(tmp_path, f"""
            [daemon]
            port = {port}

            [[handler]]
            name = "missing_h"
            url = "{h_url}"
            events = ["preToolUse"]
        """)
        proc = _spawn_router(tmp_path, user_config_dir=user_dir, port=port)
        try:
            status, body = _post(f"http://127.0.0.1:{port}/hooks/preToolUse", {})
            assert status == 200
            assert body == {}
        finally:
            _shutdown(proc)
        log = _daemon_log(tmp_path)
        assert "malformed envelope" in log, (
            f"expected malformed-envelope log; got: {log!r}"
        )
    finally:
        _stop_handler(h_proc)


def test_chain_recovers_after_malformed_handler(tmp_path):
    """First handler returns malformed envelope; second returns valid deny.
    Chain logs malformed for first, then second handler's envelope wins."""
    port = _free_port()
    bad_proc, bad_url, _ = _spawn_handler(tmp_path, "bad_h", mode="missing_envelope")
    good_proc, good_url, _ = _spawn_handler(tmp_path, "good_h", response=_DENY_RESPONSE)
    try:
        user_dir = _make_user_config(tmp_path, f"""
            [daemon]
            port = {port}

            [[handler]]
            name = "bad_h"
            url = "{bad_url}"
            events = ["preToolUse"]

            [[handler]]
            name = "good_h"
            url = "{good_url}"
            events = ["preToolUse"]
        """)
        proc = _spawn_router(tmp_path, user_config_dir=user_dir, port=port)
        try:
            status, body = _post(f"http://127.0.0.1:{port}/hooks/preToolUse", {})
            assert status == 200
            assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
        finally:
            _shutdown(proc)
    finally:
        _stop_handler(good_proc)
        _stop_handler(bad_proc)


def test_terminal_short_circuits_via_http(tmp_path):
    """First handler with terminal=true returns non-null; second handler must
    never receive a request. Verified via a record file the second handler
    increments per request."""
    port = _free_port()
    record = tmp_path / "second_hits"
    first_proc, first_url, _ = _spawn_handler(
        tmp_path, "first_h", response=_DENY_RESPONSE
    )
    second_proc, second_url, _ = _spawn_handler(
        tmp_path, "second_h", response=_DENY_RESPONSE, record_file=record
    )
    try:
        user_dir = _make_user_config(tmp_path, f"""
            [daemon]
            port = {port}

            [[handler]]
            name = "first_h"
            url = "{first_url}"
            events = ["preToolUse"]
            terminal = true

            [[handler]]
            name = "second_h"
            url = "{second_url}"
            events = ["preToolUse"]
        """)
        proc = _spawn_router(tmp_path, user_config_dir=user_dir, port=port)
        try:
            status, body = _post(f"http://127.0.0.1:{port}/hooks/preToolUse", {})
            assert status == 200
            assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
            time.sleep(0.1)  # give any stray request a chance to land
            assert not record.exists() or record.read_text().strip() in ("", "0"), (
                "second handler received a request despite terminal short-circuit"
            )
        finally:
            _shutdown(proc)
    finally:
        _stop_handler(second_proc)
        _stop_handler(first_proc)


def _await_log(tmp_path: Path, needle: str, timeout_s: float = 5.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        log = _daemon_log(tmp_path)
        if needle in log:
            return log
        time.sleep(0.05)
    return _daemon_log(tmp_path)


def _await_handler_in_health(
    base: str, name: str, *, present: bool, timeout_s: float = 5.0,
) -> dict:
    """Poll /health until ``name`` is present (or absent). Returns last body."""
    deadline = time.monotonic() + timeout_s
    body: dict = {}
    while time.monotonic() < deadline:
        try:
            _, body = _get(f"{base}/health")
        except (urllib.error.URLError, OSError):
            time.sleep(0.05)
            continue
        names = {h["name"] for h in body.get("handlers", [])}
        if (name in names) is present:
            return body
        time.sleep(0.05)
    return body


def test_config_edit_hot_reloads_handler_chain(tmp_path):
    """Edit config.toml mid-flight: new handler appears in /health and serves
    the chain without restarting the daemon."""
    port = _free_port()
    h_proc, h_url, _ = _spawn_handler(tmp_path, "late_added", response=_DENY_RESPONSE)
    try:
        # Start with NO handlers configured.
        user_dir = _make_user_config(tmp_path, f"""
            [daemon]
            port = {port}
        """)
        proc = _spawn_router(tmp_path, user_config_dir=user_dir, port=port)
        try:
            base = f"http://127.0.0.1:{port}"
            _, body = _get(f"{base}/health")
            assert body["handlers"] == []

            # Hot-add the handler via config edit.
            _write(user_dir / "config.toml", dedent(f"""
                [daemon]
                port = {port}

                [[handler]]
                name = "late_added"
                url = "{h_url}"
                events = ["preToolUse"]
            """))
            body = _await_handler_in_health(base, "late_added", present=True)
            assert any(h["name"] == "late_added" for h in body["handlers"]), body

            # New handler now serves the chain.
            status, body = _post(f"{base}/hooks/preToolUse", {})
            assert status == 200
            assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
        finally:
            _shutdown(proc)
        log = _daemon_log(tmp_path)
        assert "Traceback" not in log, log
        assert "config reload: 1 handler(s) loaded" in log, log
    finally:
        _stop_handler(h_proc)


def test_config_broken_toml_keeps_prior_chain(tmp_path):
    """Save broken TOML mid-edit: log records error, prior chain keeps serving.
    Fix the TOML: next save reloads cleanly."""
    port = _free_port()
    h_proc, h_url, _ = _spawn_handler(tmp_path, "stable_h", response=_DENY_RESPONSE)
    try:
        user_dir = _make_user_config(tmp_path, f"""
            [daemon]
            port = {port}

            [[handler]]
            name = "stable_h"
            url = "{h_url}"
            events = ["preToolUse"]
        """)
        proc = _spawn_router(tmp_path, user_config_dir=user_dir, port=port)
        try:
            base = f"http://127.0.0.1:{port}"
            # Sanity: chain works.
            _, body = _post(f"{base}/hooks/preToolUse", {})
            assert body["hookSpecificOutput"]["permissionDecision"] == "deny"

            # /health initially reports a successful reload.
            _, health = _get(f"{base}/health")
            assert health["config"]["last_reload_ok"] is True
            assert health["config"]["last_reload_error"] is None

            # Save broken TOML.
            (user_dir / "config.toml").write_text("this is = not valid = toml [[[\n")
            _await_log(tmp_path, "config reload failed")

            # Prior chain still serves.
            _, body = _post(f"{base}/hooks/preToolUse", {})
            assert body["hookSpecificOutput"]["permissionDecision"] == "deny"

            # /health now surfaces the failure.
            _, health = _get(f"{base}/health")
            assert health["config"]["last_reload_ok"] is False
            assert health["config"]["last_reload_error"]

            # Fix the TOML — recovery.
            _write(user_dir / "config.toml", dedent(f"""
                [daemon]
                port = {port}

                [[handler]]
                name = "stable_h"
                url = "{h_url}"
                events = ["preToolUse"]
            """))
            _await_log(tmp_path, "config reload: 1 handler(s) loaded")
            _, body = _post(f"{base}/hooks/preToolUse", {})
            assert body["hookSpecificOutput"]["permissionDecision"] == "deny"

            # /health flips back to ok on recovery.
            _, health = _get(f"{base}/health")
            assert health["config"]["last_reload_ok"] is True
            assert health["config"]["last_reload_error"] is None
        finally:
            _shutdown(proc)
    finally:
        _stop_handler(h_proc)


def test_chunked_request_body_reaches_handler(tmp_path):
    """Chunked-encoding POST has no Content-Length. Router must still parse the
    body and forward it to the handler — not silently substitute {} (regression
    guard for the `if request.content_length:` gate that was removed in B1)."""
    port = _free_port()
    h_proc, h_url, _ = _spawn_handler(tmp_path, "echo_p", mode="echo_payload")
    try:
        user_dir = _make_user_config(tmp_path, f"""
            [daemon]
            port = {port}

            [[handler]]
            name = "echo_p"
            url = "{h_url}"
            events = ["preToolUse"]
        """)
        proc = _spawn_router(tmp_path, user_config_dir=user_dir, port=port)
        try:
            inbound = {"session_id": "abc", "tool_name": "X"}
            payload = json.dumps(inbound).encode()
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3.0)
            conn.putrequest("POST", "/hooks/preToolUse")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Transfer-Encoding", "chunked")
            conn.endheaders()
            conn.send(f"{len(payload):x}\r\n".encode() + payload + b"\r\n0\r\n\r\n")
            resp = conn.getresponse()
            assert resp.status == 200
            body = json.loads(resp.read())
            # echo_payload handler returns {"envelope": "<payload-string>"}.
            # Router unwraps the envelope; we should see the inbound dict here.
            assert body == inbound
        finally:
            _shutdown(proc)
    finally:
        _stop_handler(h_proc)


def test_bounded_worker_pool_caps_concurrent_handler_calls(tmp_path):
    """With max_workers=2 and 5 inbound requests against a hanging handler,
    only 2 requests reach the handler — the other 3 sit in the router's
    executor queue. Asserts the pool actually bounds in-flight work."""
    import threading
    port = _free_port()
    record = tmp_path / "hits"
    h_proc, h_url, _ = _spawn_handler(
        tmp_path, "hang_h", mode="hang", record_file=record,
    )
    try:
        user_dir = _make_user_config(tmp_path, f"""
            [daemon]
            port = {port}
            max_workers = 2
            request_timeout_s = 30.0
        """ + f"""
            [[handler]]
            name = "hang_h"
            url = "{h_url}"
            events = ["preToolUse"]
        """)
        proc = _spawn_router(tmp_path, user_config_dir=user_dir, port=port)
        try:
            url = f"http://127.0.0.1:{port}/hooks/preToolUse"
            errors: list[Exception] = []

            def fire():
                try:
                    _post(url, {}, timeout=2.0)
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=fire, daemon=True) for _ in range(5)]
            for t in threads:
                t.start()
            # All 5 will time out (handler hangs); we don't care, we just want
            # to observe how many actually reached the handler before timeouts.
            time.sleep(1.0)
            hits = int(record.read_text().strip()) if record.exists() else 0
            assert hits == 2, (
                f"max_workers=2 should cap concurrent handler calls; saw {hits}"
            )
            for t in threads:
                t.join(timeout=5.0)
        finally:
            _shutdown(proc)
    finally:
        _stop_handler(h_proc)


def test_second_router_exits_when_first_running(tmp_path):
    port1 = _free_port()
    user_dir = _make_user_config(tmp_path, f"""
        [daemon]
        port = {port1}
    """)
    proc1 = _spawn_router(tmp_path, user_config_dir=user_dir, port=port1)
    try:
        env = os.environ.copy()
        env["CDH_STATE_DIR"] = str(tmp_path / "state")
        env["CDH_CONFIG_DIR"] = str(user_dir)
        proc2 = subprocess.Popen(
            [sys.executable, "-m", "claude_dynamic_hooks._server.daemon"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        ret = proc2.wait(timeout=5)
        assert ret == 1
    finally:
        _shutdown(proc1)

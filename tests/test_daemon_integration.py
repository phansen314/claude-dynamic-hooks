"""Integration: spawn router daemon, drive HTTP, verify probe + chain."""
from __future__ import annotations

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

def test_no_handlers_pretooluse_returns_ask_default(tmp_path):
    port = _free_port()
    user_dir = _make_user_config(tmp_path, f"""
        [daemon]
        port = {port}
    """)
    proc = _spawn_router(tmp_path, user_config_dir=user_dir, port=port)
    try:
        status, body = _post(f"http://127.0.0.1:{port}/hooks/preToolUse", {})
        assert status == 200
        assert body["hookSpecificOutput"]["permissionDecision"] == "ask"
    finally:
        stderr = _shutdown(proc)
    assert b"Traceback" not in stderr, stderr.decode()


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

            [handler.events.preToolUse]
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


def test_handler_returning_none_falls_to_ask_default(tmp_path):
    port = _free_port()
    h_proc, h_url, _ = _spawn_handler(tmp_path, "noop_h")
    try:
        user_dir = _make_user_config(tmp_path, f"""
            [daemon]
            port = {port}

            [[handler]]
            name = "noop_h"
            url = "{h_url}"

            [handler.events.preToolUse]
        """)
        proc = _spawn_router(tmp_path, user_config_dir=user_dir, port=port)
        try:
            status, body = _post(f"http://127.0.0.1:{port}/hooks/preToolUse", {})
            assert status == 200
            assert body["hookSpecificOutput"]["permissionDecision"] == "ask"
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

            [handler.events.preToolUse]
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

            [handler.events.preToolUse]
        """)
        proc = _spawn_router(tmp_path, user_config_dir=user_dir, port=port)
        try:
            status, body = _post(f"http://127.0.0.1:{port}/hooks/preToolUse", {})
            assert status == 200
            assert body["hookSpecificOutput"]["permissionDecision"] == "ask"
        finally:
            _shutdown(proc)
    finally:
        _stop_handler(h_proc)


def test_router_starts_with_no_handler_running(tmp_path):
    """Configure a handler URL with nothing running there. Router still starts;
    chain step fails (ECONNREFUSED) and falls through to default."""
    port = _free_port()
    dead_port = _free_port()
    user_dir = _make_user_config(tmp_path, f"""
        [daemon]
        port = {port}

        [[handler]]
        name = "ghost"
        url = "http://127.0.0.1:{dead_port}"

        [handler.events.preToolUse]
    """)
    proc = _spawn_router(tmp_path, user_config_dir=user_dir, port=port)
    try:
        status, body = _post(f"http://127.0.0.1:{port}/hooks/preToolUse", {})
        assert status == 200
        assert body["hookSpecificOutput"]["permissionDecision"] == "ask"
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

        [handler.events.preToolUse]
    """)
    proc = _spawn_router(tmp_path, user_config_dir=user_dir, port=port)
    try:
        # Pre-handler request: ECONNREFUSED → default fires.
        _, body = _post(f"http://127.0.0.1:{port}/hooks/preToolUse", {})
        assert body["hookSpecificOutput"]["permissionDecision"] == "ask"

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
    coerces step to null; default ask envelope returned to caller."""
    port = _free_port()
    h_proc, h_url, _ = _spawn_handler(tmp_path, "hang_h", mode="hang")
    try:
        user_dir = _make_user_config(tmp_path, f"""
            [daemon]
            port = {port}

            [[handler]]
            name = "hang_h"
            url = "{h_url}"

            [handler.events.preToolUse]
            timeout_s = 0.5
        """)
        proc = _spawn_router(tmp_path, user_config_dir=user_dir, port=port)
        try:
            t0 = time.monotonic()
            status, body = _post(
                f"http://127.0.0.1:{port}/hooks/preToolUse", {}, timeout=5.0
            )
            elapsed = time.monotonic() - t0
            assert status == 200
            assert body["hookSpecificOutput"]["permissionDecision"] == "ask"
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
    """Handler returns 200 + text/plain body. Chain treats as null; default fires."""
    port = _free_port()
    h_proc, h_url, _ = _spawn_handler(tmp_path, "text_h", mode="text_plain")
    try:
        user_dir = _make_user_config(tmp_path, f"""
            [daemon]
            port = {port}

            [[handler]]
            name = "text_h"
            url = "{h_url}"

            [handler.events.preToolUse]
        """)
        proc = _spawn_router(tmp_path, user_config_dir=user_dir, port=port)
        try:
            status, body = _post(f"http://127.0.0.1:{port}/hooks/preToolUse", {})
            assert status == 200
            assert body["hookSpecificOutput"]["permissionDecision"] == "ask"
        finally:
            _shutdown(proc)
    finally:
        _stop_handler(h_proc)


def test_handler_missing_envelope_key_logged_as_malformed(tmp_path):
    """Handler returns 200 + JSON object missing `envelope` key. Chain treats
    as malformed envelope; logs and falls through to default."""
    port = _free_port()
    h_proc, h_url, _ = _spawn_handler(tmp_path, "missing_h", mode="missing_envelope")
    try:
        user_dir = _make_user_config(tmp_path, f"""
            [daemon]
            port = {port}

            [[handler]]
            name = "missing_h"
            url = "{h_url}"

            [handler.events.preToolUse]
        """)
        proc = _spawn_router(tmp_path, user_config_dir=user_dir, port=port)
        try:
            status, body = _post(f"http://127.0.0.1:{port}/hooks/preToolUse", {})
            assert status == 200
            assert body["hookSpecificOutput"]["permissionDecision"] == "ask"
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

            [handler.events.preToolUse]

            [[handler]]
            name = "good_h"
            url = "{good_url}"

            [handler.events.preToolUse]
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

            [handler.events.preToolUse]
            terminal = true

            [[handler]]
            name = "second_h"
            url = "{second_url}"

            [handler.events.preToolUse]
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

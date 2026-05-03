"""ReadOnce — standalone CDHP handler.

Tracks (session_id, file_path) → mtime_ns per session. On re-read of an
unchanged file within TTL: advises (warn mode) or blocks (deny mode).

You run this process yourself (manual / systemd / container). You declare
which events the router POSTs to /hooks/<event> in ~/.config/cdh/config.toml
via `events = [...]` on the [[handler]] block — the router does not probe
/health to discover events. /health is used by `cdh list-handlers` for
liveness only.

CDHP wire contract (see docs/openapi.yaml) implemented:
    GET  /health              → 200 + {name, protocol_version, events, uptime_s}
    POST /hooks/preToolUse    request:  {"payload": "<json-string-claude-payload>"}
                              response: 200 + {"envelope": "<json-string-envelope>"}
                                        or  200 + {"envelope": null} for "no opinion"

Env (required):
    CDH_BIND_ADDR       "127.0.0.1:<port>" — address to bind

Env (handler-specific, optional):
    READ_ONCE_MODE      "warn" (default) — allow with advisory reason
                        "deny" — block with decision:block
    READ_ONCE_TTL       seconds before cache entry expires (default 1200)
    READ_ONCE_DISABLED  "1" to disable entirely
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from cachetools import TTLCache
from flask import Flask, jsonify, request
from werkzeug.serving import make_server

_NAME = "read_once"
_PROTO = "1.0"
_EVENTS = ["preToolUse"]

_DEFAULT_TTL_S = 1200            # 20 minutes
_CACHE_MAXSIZE = 10_000          # (session_id, file_path) entries
_BYTES_PER_TOKEN_DIVISOR = 4     # rough chars-per-token
_TOKEN_OVERHEAD_FACTOR = 1.7     # adjusts for tokenizer overhead vs naive char count


@dataclass(frozen=True, slots=True)
class _Settings:
    ttl_s: int
    mode: str          # "warn" | "deny"
    disabled: bool


def _read_settings() -> _Settings:
    return _Settings(
        ttl_s=int(os.environ.get("READ_ONCE_TTL") or _DEFAULT_TTL_S),
        mode=os.environ.get("READ_ONCE_MODE") or "warn",
        disabled=os.environ.get("READ_ONCE_DISABLED") == "1",
    )


def _decision_envelope(decision: str, reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }


def _handle_pre_tool_use(
    req: dict, settings: _Settings, cache: TTLCache, lock: threading.Lock,
) -> dict | None:
    if settings.disabled:
        return None
    if req.get("tool_name") != "Read":
        return None

    tool_input = req.get("tool_input") or {}
    file_path = tool_input.get("file_path")
    session_id = req.get("session_id")
    if not file_path or not session_id:
        return None

    if tool_input.get("offset") is not None or tool_input.get("limit") is not None:
        return None

    p = Path(file_path)
    if not p.exists():
        return None

    try:
        current_mtime = p.stat().st_mtime_ns
    except OSError:
        return None

    key = (session_id, file_path)
    with lock:
        cached_mtime = cache.get(key)
        if cached_mtime == current_mtime:
            file_size = p.stat().st_size
            tokens = int(file_size / _BYTES_PER_TOKEN_DIVISOR * _TOKEN_OVERHEAD_FACTOR)
            reason = (
                f"read-once: {p.name} (~{tokens} tokens) already in context "
                f"(unchanged). Re-read allowed after {settings.ttl_s // 60}m or file change."
            )
            decision = "deny" if settings.mode == "deny" else "allow"
            return _decision_envelope(decision, reason)
        cache[key] = current_mtime
    return None


def _build_app(settings: _Settings) -> Flask:
    app = Flask(_NAME)
    cache: TTLCache = TTLCache(maxsize=_CACHE_MAXSIZE, ttl=settings.ttl_s)
    lock = threading.Lock()
    started = time.monotonic()

    @app.get("/health")
    def health():
        return jsonify({
            "name": _NAME, "protocol_version": _PROTO, "events": _EVENTS,
            "uptime_s": int(time.monotonic() - started),
        })

    @app.post("/hooks/preToolUse")
    def hook_pre_tool_use():
        body = request.get_json(silent=True) or {}
        payload_str = body.get("payload") if isinstance(body, dict) else None
        if not isinstance(payload_str, str):
            return jsonify({"error": "missing or invalid 'payload' string"}), 400
        try:
            payload = json.loads(payload_str)
        except json.JSONDecodeError:
            return jsonify({"error": "payload is not valid JSON"}), 400
        if not isinstance(payload, dict):
            return jsonify({"error": "payload must be a JSON object"}), 400
        result = _handle_pre_tool_use(payload, settings, cache, lock)
        envelope = json.dumps(result) if result is not None else None
        return jsonify({"envelope": envelope})

    @app.errorhandler(404)
    def _not_found(_e):
        return jsonify({"error": "not found"}), 404

    @app.after_request
    def _log(resp):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        sys.stderr.write(
            f"{ts} {request.method} {request.path} "
            f"{resp.status_code} {resp.content_length or 0}\n"
        )
        sys.stderr.flush()
        return resp

    return app


def main() -> None:
    bind = os.environ.get("CDH_BIND_ADDR")
    if not bind:
        sys.stderr.write("read_once: CDH_BIND_ADDR not set\n")
        sys.exit(1)
    host, _, port_s = bind.rpartition(":")

    settings = _read_settings()
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    server = make_server(host, int(port_s), _build_app(settings), threaded=True)

    shutdown_r, shutdown_w = os.pipe()

    def _on_signal(*_):
        # os.write is async-signal-safe; thread/lock allocation is not.
        # Keep bare try/except — contextlib.suppress allocates a manager
        # object, which we want to avoid in signal context.
        try:  # noqa: SIM105
            os.write(shutdown_w, b"x")
        except OSError:
            pass

    def _wait_and_shutdown():
        try:
            os.read(shutdown_r, 1)
        except OSError:
            return
        server.shutdown()

    threading.Thread(target=_wait_and_shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        server.serve_forever()
    finally:
        server.server_close()
        for fd in (shutdown_r, shutdown_w):
            with contextlib.suppress(OSError):
                os.close(fd)


if __name__ == "__main__":
    main()

"""Flask app for the router daemon — HTTP routes only.

Owns: `/health`, `/hooks/<event>`, error handler, request log. Lifecycle
(process lock, signals, server start/stop) lives in `daemon.py`.
"""
from __future__ import annotations

import sys
import time

from flask import Flask, jsonify, request

from ...config import HandlerEntry, HookDefaults
from ...events import EventType
from ..router_chain import run as chain_run


def build_app(
    *,
    handlers: list[HandlerEntry],
    defaults: HookDefaults,
    request_timeout_s: float,
    max_body_bytes: int,
) -> Flask:
    app = Flask("cdh")
    app.config["MAX_CONTENT_LENGTH"] = max_body_bytes

    @app.get("/health")
    def health():
        return jsonify({
            "ok": True,
            "handlers": [
                {
                    "name": h.name, "url": h.url,
                    "events": sorted(e.value for e in h.events),
                }
                for h in handlers
            ],
        })

    @app.post("/hooks/<event>")
    def hook(event: str):
        try:
            ev = EventType(event)
        except ValueError:
            return jsonify({"error": f"unknown event {event!r}"}), 404

        if request.content_length:
            body = request.get_json(silent=True)
            if body is None:
                return jsonify({"error": "body is not valid JSON"}), 400
            if not isinstance(body, dict):
                return jsonify({"error": "body must be a JSON object"}), 400
            params: dict = body
        else:
            params = {}

        result = chain_run(
            event=ev, params=params,
            handlers=[h for h in handlers if ev in h.events],
            default_timeout_s=request_timeout_s,
            default_max_bytes=max_body_bytes,
        )

        if result is None:
            result = defaults.get(ev)
        return jsonify(result if result is not None else {})

    @app.errorhandler(404)
    def _not_found(_e):
        return jsonify({"error": "not found"}), 404

    @app.after_request
    def _log_request(resp):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        sys.stderr.write(
            f"{ts} {request.method} {request.path} "
            f"{resp.status_code} {resp.content_length or 0}\n"
        )
        sys.stderr.flush()
        return resp

    return app

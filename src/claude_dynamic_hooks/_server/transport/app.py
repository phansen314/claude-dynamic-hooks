"""Flask app for the router daemon — HTTP routes only.

Owns: `/health`, `/hooks/<event>`, error handler, request log. Lifecycle
(process lock, signals, server start/stop, config watcher) lives in `daemon.py`.

Per-request state (handler list, defaults, timeout, wire cap, last reload
status) is read through a ``get_snapshot`` callable on every request so hot
config reloads (see `config_watcher`) take effect without restart. The
daemon owns the backing attribute and replaces it atomically; ``Snapshot``
itself is immutable, so a reader either sees the prior snapshot
end-to-end or the new one end-to-end.

`MAX_CONTENT_LENGTH` is pinned at the startup `wire_max_bytes` because
werkzeug enforces it before our route runs; changes to that field require
a daemon restart. The hot-reloadable cap on `Snapshot.outbound_max_bytes`
applies only to the router→handler hop.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from flask import Flask, jsonify, request

from ...config import HandlerEntry, HookDefaults
from ...events import EventType
from ..router_chain import run as chain_run

_access_log = logging.getLogger("cdh.access")


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Immutable bundle of per-request router state. Replace, don't mutate."""
    handlers: tuple[HandlerEntry, ...]
    defaults: HookDefaults
    request_timeout_s: float
    outbound_max_bytes: int
    last_reload_at: float           # epoch seconds; 0.0 only if never loaded
    last_reload_ok: bool
    last_reload_error: str | None   # None on success


def build_app(
    get_snapshot: Callable[[], Snapshot], *, max_body_bytes: int,
) -> Flask:
    """Build the router Flask app.

    ``get_snapshot`` is invoked once per request to fetch the current
    immutable :class:`Snapshot`. ``max_body_bytes`` is the startup wire cap
    used to set Flask's ``MAX_CONTENT_LENGTH`` (live updates to the
    snapshot's ``outbound_max_bytes`` only affect the router→handler hop).
    """
    app = Flask("cdh")
    app.config["MAX_CONTENT_LENGTH"] = max_body_bytes

    @app.get("/health")
    def health():
        snap = get_snapshot()
        return jsonify({
            "ok": True,
            "handlers": [
                {
                    "name": h.name, "url": h.url,
                    "events": sorted(e.value for e in h.events),
                }
                for h in snap.handlers
            ],
            "config": {
                "last_reload_at": snap.last_reload_at,
                "last_reload_ok": snap.last_reload_ok,
                "last_reload_error": snap.last_reload_error,
            },
        })

    @app.post("/hooks/<event>")
    def hook(event: str):
        try:
            ev = EventType(event)
        except ValueError:
            return jsonify({"error": f"unknown event {event!r}"}), 404

        body = request.get_json(silent=True)
        if body is None:
            if request.data:
                return jsonify({"error": "body is not valid JSON"}), 400
            params: dict = {}
        elif not isinstance(body, dict):
            return jsonify({"error": "body must be a JSON object"}), 400
        else:
            params = body

        snap = get_snapshot()
        result = chain_run(
            event=ev, params=params,
            handlers=[h for h in snap.handlers if ev in h.events],
            default_timeout_s=snap.request_timeout_s,
            default_max_bytes=snap.outbound_max_bytes,
        )

        if result is None:
            result = snap.defaults.get(ev)
        return jsonify(result if result is not None else {})

    @app.errorhandler(404)
    def _not_found(_e):
        return jsonify({"error": "not found"}), 404

    @app.after_request
    def _log_request(resp):
        _access_log.info(
            "%s %s %s %s",
            request.method, request.path, resp.status_code,
            resp.content_length or 0,
        )
        return resp

    return app

"""Chain execution over CDHP HTTP.

Sequential chain across handlers (in registration order). Each step is
a POST to one handler's `/hooks/<event>`. Semantics: terminal short-circuit,
last-non-null wins, exception or unreachable handler → null + log + continue.

Wire on the router↔handler hop:
    request   {"payload":  "<json-string-of-claude-payload>"}
    response  {"envelope": "<json-string-of-claude-envelope>" | null}

Both fields are opaque JSON strings so the wire stays decoupled from
Claude's hook payload schema. See `docs/openapi.yaml`.
"""
from __future__ import annotations

import json
import sys

from ..config import HandlerEntry
from ..events import EventType
from .transport import client as http_client

_MALFORMED = object()


def run(
    event: EventType,
    params: dict,
    handlers: list[HandlerEntry],
    default_timeout_s: float,
    default_max_bytes: int,
) -> dict | None:
    """Execute the chain for *event* across all handlers declaring it.

    Args:
        event:             The hook event being dispatched.
        params:            The Claude hook payload (serialized as a string for transport).
        handlers:          Pre-filtered handlers that declare *event* in their config.
        default_timeout_s: Per-call timeout when handler config doesn't override.
        default_max_bytes: Wire cap when handler config doesn't override.

    Returns the last non-null result, or None if every handler abstained.
    """
    result: dict | None = None
    wire_request = {"payload": json.dumps(params)}

    for h in handlers:
        override = h.events.get(event)
        timeout_s = (
            override.timeout_s if override and override.timeout_s is not None
            else default_timeout_s
        )
        max_bytes = (
            override.max_bytes if override and override.max_bytes is not None
            else default_max_bytes
        )

        url = f"{h.url}/hooks/{event.value}"
        raw = http_client.post(url, wire_request, timeout_s=timeout_s, max_response_bytes=max_bytes)
        if raw is None:
            _log_step(event, h.name, "error/unreachable")
            continue

        envelope = _extract_envelope(raw)
        if envelope is None:
            _log_step(event, h.name, "null")
            continue
        if envelope is _MALFORMED:
            _log_step(event, h.name, "malformed envelope")
            continue

        terminal = bool(override and override.terminal)
        result = envelope
        _log_step(event, h.name, f"envelope{' [terminal]' if terminal else ''}")
        if terminal:
            return result

    return result


def _extract_envelope(raw: dict):
    if "envelope" not in raw:
        return _MALFORMED
    env = raw["envelope"]
    if env is None:
        return None
    if not isinstance(env, str):
        return _MALFORMED
    try:
        parsed = json.loads(env)
    except json.JSONDecodeError:
        return _MALFORMED
    if not isinstance(parsed, dict):
        return _MALFORMED
    return parsed


def _log_step(event: EventType, name: str, outcome: str) -> None:
    sys.stderr.write(f"chain {event.value} → {name}: {outcome}\n")
    sys.stderr.flush()

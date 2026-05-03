"""Router config (TOML).

Single file at `~/.config/cdh/config.toml` (overridable via `CDH_CONFIG_DIR`).
Contains `[daemon]`, `[hook_defaults]`, and `[[handler]]` entries. Each
`[[handler]]` declares `name`, `url`, an `events` array of event names,
and an optional `terminal` bool (short-circuit the chain on a non-null
response). Wire timeout and byte cap come from `[daemon]` globals
(`request_timeout_s`, `wire_max_bytes`). The router does not probe
handlers to discover events — declaration is static.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .events import EventType
from .paths import config_dir

HookDefaults = dict[EventType, dict | None]


def _pre_tool_use_decision(decision: str, reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }


# ---- Defaults (single source of truth for dataclass fields + TOML fallbacks) ----
_DEFAULT_PORT = 8765
_DEFAULT_REQUEST_TIMEOUT_S = 5.0
_DEFAULT_WIRE_MAX_BYTES = 1 * 1024 * 1024  # 1 MiB
_DEFAULT_MAX_WORKERS = 16

# ---- Bounds applied when user provides their own values ----
_PORT_MIN = 1
_PORT_MAX = 65535
_REQUEST_TIMEOUT_S_FLOOR = 0.1
_REQUEST_TIMEOUT_S_CEILING = 60.0
_WIRE_MAX_BYTES_FLOOR = 1 * 1024            # 1 KiB
_WIRE_MAX_BYTES_CEILING = 64 * 1024 * 1024  # 64 MiB
_MAX_WORKERS_FLOOR = 0                      # 0 = unbounded (back-compat escape hatch)
_MAX_WORKERS_CEILING = 1024


@dataclass(frozen=True, slots=True)
class DaemonConfig:
    port: int = _DEFAULT_PORT                       # router HTTP listen port
    request_timeout_s: float = _DEFAULT_REQUEST_TIMEOUT_S  # per-request wall-clock cap
    wire_max_bytes: int = _DEFAULT_WIRE_MAX_BYTES   # request/response cap
    max_workers: int = _DEFAULT_MAX_WORKERS         # thread pool size; 0 = unbounded


@dataclass(frozen=True, slots=True)
class HandlerEntry:
    """One `[[handler]]` block in the router config."""
    name: str
    url: str
    events: frozenset[EventType] = field(default_factory=frozenset)
    terminal: bool = False


@dataclass(frozen=True, slots=True)
class Config:
    daemon: DaemonConfig = DaemonConfig()
    handlers: tuple[HandlerEntry, ...] = ()
    defaults: HookDefaults = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _user_path() -> Path:
    return config_dir() / "config.toml"


def load() -> Config:
    """Load `~/.config/cdh/config.toml`. Missing file → all defaults."""
    raw = _read(_user_path())
    daemon = _build_daemon(raw.get("daemon", {}) or {})

    defaults: dict[EventType, dict | None] = {}
    _apply_defaults(defaults, raw.get("hook_defaults", {}) or {})

    handlers = list(_extract_handlers(raw))

    return Config(
        daemon=daemon,
        handlers=tuple(handlers),
        defaults=defaults,
    )


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def _bounded(label: str, value, lo, hi):
    if not (lo <= value <= hi):
        raise ValueError(f"{label} must be in [{lo}, {hi}]; got {value!r}")
    return value


def _build_daemon(raw: dict) -> DaemonConfig:
    port = int(raw.get("port", _DEFAULT_PORT))
    timeout = float(raw.get("request_timeout_s", _DEFAULT_REQUEST_TIMEOUT_S))
    max_bytes = int(raw.get("wire_max_bytes", _DEFAULT_WIRE_MAX_BYTES))
    max_workers = int(raw.get("max_workers", _DEFAULT_MAX_WORKERS))
    return DaemonConfig(
        port=_bounded("[daemon].port", port, _PORT_MIN, _PORT_MAX),
        request_timeout_s=_bounded(
            "[daemon].request_timeout_s", timeout,
            _REQUEST_TIMEOUT_S_FLOOR, _REQUEST_TIMEOUT_S_CEILING,
        ),
        wire_max_bytes=_bounded(
            "[daemon].wire_max_bytes", max_bytes,
            _WIRE_MAX_BYTES_FLOOR, _WIRE_MAX_BYTES_CEILING,
        ),
        max_workers=_bounded(
            "[daemon].max_workers", max_workers,
            _MAX_WORKERS_FLOOR, _MAX_WORKERS_CEILING,
        ),
    )


def _extract_handlers(raw: dict):
    """Parse `[[handler]]` entries from the router config."""
    entries = raw.get("handler", [])
    if not entries:
        return
    if not isinstance(entries, list):
        raise ValueError("[[handler]] must be an array of tables")
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"[[handler]][{i}] must be a table")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"[[handler]][{i}] missing 'name'")
        url = entry.get("url")
        if not isinstance(url, str):
            raise ValueError(f"[[handler]][{i}] 'url' missing or not a string")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(
                f"[[handler]][{i}] 'url' must be a valid http(s) URL with host; "
                f"got {url!r}"
            )
        events_raw = entry.get("events", []) or []
        if not isinstance(events_raw, list):
            raise ValueError(
                f"[[handler]][{i}] 'events' must be an array of event names"
            )
        events: set[EventType] = set()
        for event_name in events_raw:
            if not isinstance(event_name, str):
                raise ValueError(
                    f"[[handler]][{i}] 'events' entries must be strings; "
                    f"got {event_name!r}"
                )
            try:
                events.add(EventType(event_name))
            except ValueError as e:
                raise ValueError(
                    f"[[handler]][{i}] unknown event {event_name!r}"
                ) from e
        terminal = bool(entry.get("terminal", False))
        yield HandlerEntry(
            name=name,
            url=url.rstrip("/"),
            events=frozenset(events),
            terminal=terminal,
        )


def _apply_defaults(target: dict[EventType, dict | None], raw: dict) -> None:
    if not isinstance(raw, dict):
        raise ValueError("[hook_defaults] must be a table")
    for event_name, kw in raw.items():
        try:
            event = EventType(event_name)
        except ValueError as e:
            raise ValueError(f"[hook_defaults]: unknown event {event_name!r}") from e
        target[event] = _compile_default(event, kw)


def _compile_default(event: EventType, kw) -> dict | None:
    if kw == "passthrough" or kw is None:
        return None
    if not isinstance(kw, str):
        raise ValueError(f"[hook_defaults.{event.value}] must be a string keyword")
    if event is EventType.PRE_TOOL_USE and kw in ("ask", "allow", "deny"):
        return _pre_tool_use_decision(kw, f"cdh: default ({kw})")
    raise ValueError(
        f"[hook_defaults.{event.value}] = {kw!r} is not supported; "
        f"valid: 'passthrough' (any event), 'ask'/'allow'/'deny' (preToolUse only)"
    )

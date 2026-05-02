"""Daemon config — TOML parsing, clamping, hook_defaults."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from claude_dynamic_hooks import config as config_mod
from claude_dynamic_hooks.events import EventType


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_missing_file_returns_defaults(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CDH_CONFIG_DIR", str(tmp_path))
    cfg = config_mod.load()
    assert cfg.daemon.port == 8765
    assert cfg.daemon.request_timeout_s == 5.0
    assert cfg.daemon.wire_max_bytes == 1 * 1024 * 1024
    assert cfg.handlers == ()


def test_loads_overrides(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CDH_CONFIG_DIR", str(tmp_path))
    _write(tmp_path / "config.toml", dedent("""
        [daemon]
        port = 9999
        request_timeout_s = 10.0
        wire_max_bytes = 524288
    """))
    cfg = config_mod.load()
    assert cfg.daemon.port == 9999
    assert cfg.daemon.request_timeout_s == 10.0
    assert cfg.daemon.wire_max_bytes == 524288


def test_out_of_range_values_raise(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CDH_CONFIG_DIR", str(tmp_path))
    _write(tmp_path / "config.toml", dedent("""
        [daemon]
        request_timeout_s = 9999.0
    """))
    with pytest.raises(ValueError, match=r"\[daemon\]\.request_timeout_s"):
        config_mod.load()


def test_out_of_range_port_raises(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CDH_CONFIG_DIR", str(tmp_path))
    _write(tmp_path / "config.toml", dedent("""
        [daemon]
        port = 99999
    """))
    with pytest.raises(ValueError, match=r"\[daemon\]\.port"):
        config_mod.load()


def test_out_of_range_wire_max_bytes_raises(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CDH_CONFIG_DIR", str(tmp_path))
    _write(tmp_path / "config.toml", dedent("""
        [daemon]
        wire_max_bytes = 999999999999
    """))
    with pytest.raises(ValueError, match=r"\[daemon\]\.wire_max_bytes"):
        config_mod.load()


# ---------------------------------------------------------------------------
# hook_defaults
# ---------------------------------------------------------------------------

def test_pretooluse_default_is_ask_when_unset(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CDH_CONFIG_DIR", str(tmp_path))
    cfg = config_mod.load()
    pre = cfg.defaults.get(EventType.PRE_TOOL_USE)
    assert pre is not None
    assert pre["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_other_events_default_is_passthrough(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CDH_CONFIG_DIR", str(tmp_path))
    cfg = config_mod.load()
    for ev in EventType:
        if ev is EventType.PRE_TOOL_USE:
            continue
        assert cfg.defaults.get(ev) is None


def test_hook_defaults_rejects_invalid_keyword(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CDH_CONFIG_DIR", str(tmp_path))
    _write(tmp_path / "config.toml", dedent("""
        [hook_defaults]
        preToolUse = "frobnicate"
    """))
    with pytest.raises(ValueError):
        config_mod.load()


def test_hook_defaults_ask_only_for_pretooluse(tmp_path: Path, monkeypatch):
    """ASK doesn't make sense for PostToolUse — reject."""
    monkeypatch.setenv("CDH_CONFIG_DIR", str(tmp_path))
    _write(tmp_path / "config.toml", dedent("""
        [hook_defaults]
        postToolUse = "ask"
    """))
    with pytest.raises(ValueError):
        config_mod.load()


def test_hook_defaults_passthrough_works_for_new_catalog_events(tmp_path: Path, monkeypatch):
    """Newly-added events accept passthrough in hook_defaults."""
    monkeypatch.setenv("CDH_CONFIG_DIR", str(tmp_path))
    _write(tmp_path / "config.toml", dedent("""
        [hook_defaults]
        sessionEnd = "passthrough"
        taskCreated = "passthrough"
        worktreeCreate = "passthrough"
    """))
    cfg = config_mod.load()
    assert cfg.defaults.get(EventType.SESSION_END) is None
    assert cfg.defaults.get(EventType.TASK_CREATED) is None
    assert cfg.defaults.get(EventType.WORKTREE_CREATE) is None


def test_handler_override_timeout_out_of_range_raises(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CDH_CONFIG_DIR", str(tmp_path))
    _write(tmp_path / "config.toml", dedent("""
        [[handler]]
        name = "h"
        url = "http://127.0.0.1:9001"

        [handler.events.preToolUse]
        timeout_s = 0.0
    """))
    with pytest.raises(ValueError, match=r"\[\[handler\]\]\[0\]\.events\.preToolUse\.timeout_s"):
        config_mod.load()


def test_handler_override_max_bytes_out_of_range_raises(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CDH_CONFIG_DIR", str(tmp_path))
    _write(tmp_path / "config.toml", dedent("""
        [[handler]]
        name = "h"
        url = "http://127.0.0.1:9001"

        [handler.events.preToolUse]
        max_bytes = 1
    """))
    with pytest.raises(ValueError, match=r"\[\[handler\]\]\[0\]\.events\.preToolUse\.max_bytes"):
        config_mod.load()


def test_handler_events_accept_new_catalog_events(tmp_path: Path, monkeypatch):
    """[[handler.events.<event>]] parses for newly-catalogued events."""
    monkeypatch.setenv("CDH_CONFIG_DIR", str(tmp_path))
    _write(tmp_path / "config.toml", dedent("""
        [[handler]]
        name = "h"
        url = "http://127.0.0.1:9001"

        [handler.events.sessionEnd]
        terminal = true

        [handler.events.permissionRequest]
        timeout_s = 1.0
    """))
    cfg = config_mod.load()
    h = cfg.handlers[0]
    assert EventType.SESSION_END in h.events
    assert h.events[EventType.SESSION_END].terminal is True
    assert h.events[EventType.PERMISSION_REQUEST].timeout_s == 1.0

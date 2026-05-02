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

def test_all_events_default_to_passthrough_when_unset(tmp_path: Path, monkeypatch):
    """No built-in defaults — every event passes through unless user opts in."""
    monkeypatch.setenv("CDH_CONFIG_DIR", str(tmp_path))
    cfg = config_mod.load()
    for ev in EventType:
        assert cfg.defaults.get(ev) is None


def test_pretooluse_user_opt_in_to_ask(tmp_path: Path, monkeypatch):
    """User can re-enable the previous fail-safe ask behavior explicitly."""
    monkeypatch.setenv("CDH_CONFIG_DIR", str(tmp_path))
    _write(tmp_path / "config.toml", dedent("""
        [hook_defaults]
        preToolUse = "ask"
    """))
    cfg = config_mod.load()
    pre = cfg.defaults.get(EventType.PRE_TOOL_USE)
    assert pre is not None
    assert pre["hookSpecificOutput"]["permissionDecision"] == "ask"


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


def test_handler_events_array_form(tmp_path: Path, monkeypatch):
    """`events = [...]` parses; per-handler `terminal` is read."""
    monkeypatch.setenv("CDH_CONFIG_DIR", str(tmp_path))
    _write(tmp_path / "config.toml", dedent("""
        [[handler]]
        name = "h"
        url = "http://127.0.0.1:9001"
        events = ["sessionEnd", "permissionRequest"]
        terminal = true
    """))
    cfg = config_mod.load()
    h = cfg.handlers[0]
    assert EventType.SESSION_END in h.events
    assert EventType.PERMISSION_REQUEST in h.events
    assert h.terminal is True


def test_handler_terminal_defaults_false(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CDH_CONFIG_DIR", str(tmp_path))
    _write(tmp_path / "config.toml", dedent("""
        [[handler]]
        name = "h"
        url = "http://127.0.0.1:9001"
        events = ["preToolUse"]
    """))
    cfg = config_mod.load()
    assert cfg.handlers[0].terminal is False


def test_handler_events_must_be_array(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CDH_CONFIG_DIR", str(tmp_path))
    _write(tmp_path / "config.toml", dedent("""
        [[handler]]
        name = "h"
        url = "http://127.0.0.1:9001"
        events = "preToolUse"
    """))
    with pytest.raises(ValueError, match="must be an array"):
        config_mod.load()


def test_handler_events_reject_unknown(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CDH_CONFIG_DIR", str(tmp_path))
    _write(tmp_path / "config.toml", dedent("""
        [[handler]]
        name = "h"
        url = "http://127.0.0.1:9001"
        events = ["bogusEvent"]
    """))
    with pytest.raises(ValueError, match="unknown event"):
        config_mod.load()

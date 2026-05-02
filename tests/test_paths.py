"""Path resolution — CDH-specific override → XDG → defaults."""
from __future__ import annotations

from pathlib import Path

from claude_dynamic_hooks.paths import DaemonPaths, config_dir


def test_config_dir_prefers_cdh_override(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CDH_CONFIG_DIR", str(tmp_path / "cdh-explicit"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config_dir() == tmp_path / "cdh-explicit"


def test_config_dir_falls_back_to_xdg(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("CDH_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config_dir() == tmp_path / "xdg" / "cdh"


def test_config_dir_default_when_neither_set(monkeypatch):
    monkeypatch.delenv("CDH_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert config_dir() == Path("~/.config/cdh").expanduser()


def test_state_dir_prefers_cdh_override(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CDH_STATE_DIR", str(tmp_path / "cdh-state"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    assert DaemonPaths.default().state_dir == tmp_path / "cdh-state"


def test_state_dir_falls_back_to_xdg(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("CDH_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    assert DaemonPaths.default().state_dir == tmp_path / "xdg-state" / "cdh"


def test_state_dir_default_when_neither_set(monkeypatch):
    monkeypatch.delenv("CDH_STATE_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert DaemonPaths.default().state_dir == Path("~/.local/state/cdh").expanduser()

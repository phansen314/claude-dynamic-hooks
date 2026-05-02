"""Canonical filesystem paths for cdh router state.

Resolution order (first match wins):
    1. `CDH_CONFIG_DIR` / `CDH_STATE_DIR` — explicit cdh-specific override.
    2. `XDG_CONFIG_HOME/cdh` / `XDG_STATE_HOME/cdh` — XDG Base Directory spec.
    3. `~/.config/cdh` / `~/.local/state/cdh` — XDG defaults.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _resolve(cdh_var: str, xdg_var: str, xdg_default: str, subdir: str) -> Path:
    override = os.environ.get(cdh_var)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get(xdg_var)
    if xdg:
        return Path(xdg).expanduser() / subdir
    return Path(xdg_default).expanduser() / subdir


def config_dir() -> Path:
    """Return the cdh config directory."""
    return _resolve("CDH_CONFIG_DIR", "XDG_CONFIG_HOME", "~/.config", "cdh")


def _state_root() -> Path:
    return _resolve("CDH_STATE_DIR", "XDG_STATE_HOME", "~/.local/state", "cdh")


@dataclass(frozen=True, slots=True)
class DaemonPaths:
    """Paths for the router (cdh-daemon) process."""
    state_dir: Path
    pid_file: Path
    stderr_log: Path

    def ensure(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    @classmethod
    def default(cls) -> DaemonPaths:
        root = _state_root()
        return cls(
            state_dir=root,
            pid_file=root / "daemon.pid",
            stderr_log=root / "daemon.log",
        )

"""Integration: `cdh test <handler> <event>` synthetic dispatch."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

from .test_daemon_integration import (
    _DENY_RESPONSE,
    _make_user_config,
    _spawn_handler,
    _stop_handler,
)


def _run_cdh(state_dir: Path, config_dir: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CDH_STATE_DIR"] = str(state_dir)
    env["CDH_CONFIG_DIR"] = str(config_dir)
    return subprocess.run(
        [sys.executable, "-m", "claude_dynamic_hooks._cli.main", *args],
        env=env, capture_output=True, text=True, timeout=10,
    )


def test_cdh_test_returns_envelope(tmp_path):
    h_proc, h_url, _ = _spawn_handler(tmp_path, "deny_h", response=_DENY_RESPONSE)
    try:
        user_dir = _make_user_config(tmp_path, f"""
            [[handler]]
            name = "deny_h"
            url = "{h_url}"
            events = ["preToolUse"]
        """)
        result = _run_cdh(
            tmp_path / "state", user_dir,
            "test", "deny_h", "preToolUse", "--payload", "{}",
        )
        assert result.returncode == 0, result.stderr
        body = json.loads(result.stdout)
        assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
    finally:
        _stop_handler(h_proc)


def test_cdh_test_null_handler_prints_abstain(tmp_path):
    h_proc, h_url, _ = _spawn_handler(tmp_path, "noop_h")
    try:
        user_dir = _make_user_config(tmp_path, f"""
            [[handler]]
            name = "noop_h"
            url = "{h_url}"
            events = ["preToolUse"]
        """)
        result = _run_cdh(
            tmp_path / "state", user_dir,
            "test", "noop_h", "preToolUse",
        )
        assert result.returncode == 0, result.stderr
        assert "null" in result.stdout
        assert "abstained" in result.stdout
    finally:
        _stop_handler(h_proc)


def test_cdh_test_unknown_handler_errors(tmp_path):
    user_dir = _make_user_config(tmp_path, dedent("""
        [[handler]]
        name = "real"
        url = "http://127.0.0.1:1"
        events = ["preToolUse"]
    """))
    result = _run_cdh(
        tmp_path / "state", user_dir,
        "test", "ghost", "preToolUse",
    )
    assert result.returncode == 1
    assert "unknown handler" in result.stderr
    assert "real" in result.stderr


def test_cdh_test_handler_unreachable_errors(tmp_path):
    user_dir = _make_user_config(tmp_path, dedent("""
        [[handler]]
        name = "ghost"
        url = "http://127.0.0.1:1"
        events = ["preToolUse"]
    """))
    result = _run_cdh(
        tmp_path / "state", user_dir,
        "test", "ghost", "preToolUse",
    )
    assert result.returncode == 1
    assert "unreachable" in result.stderr or "connection" in result.stderr.lower()


def test_cdh_test_undeclared_event_warns_but_sends(tmp_path):
    """Handler declares preToolUse only; user tests postToolUse. Handler returns
    404 for the undeclared event; cdh prints a warning then surfaces the error."""
    h_proc, h_url, _ = _spawn_handler(tmp_path, "deny_h", response=_DENY_RESPONSE)
    try:
        user_dir = _make_user_config(tmp_path, f"""
            [[handler]]
            name = "deny_h"
            url = "{h_url}"
            events = ["preToolUse"]
        """)
        result = _run_cdh(
            tmp_path / "state", user_dir,
            "test", "deny_h", "postToolUse",
        )
        # Warning emitted; handler 404s the undeclared event → exit 1.
        assert "warning" in result.stderr.lower()
        assert result.returncode == 1
    finally:
        _stop_handler(h_proc)

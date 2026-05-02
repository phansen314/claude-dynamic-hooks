"""Integration: `cdh logs [-n N] [-f]` reads the daemon log file."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path


def _run_cdh(
    state_dir: Path, *args: str, timeout: float = 5,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CDH_STATE_DIR"] = str(state_dir)
    env["CDH_CONFIG_DIR"] = str(state_dir / "config")
    return subprocess.run(
        [sys.executable, "-m", "claude_dynamic_hooks._cli.main", *args],
        env=env, capture_output=True, text=True, timeout=timeout,
    )


def _seed_log(state_dir: Path, lines: list[str]) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    log = state_dir / "daemon.log"
    log.write_text("\n".join(lines) + "\n")
    return log


def test_cdh_logs_no_file_errors(tmp_path):
    result = _run_cdh(tmp_path / "state", "logs")
    assert result.returncode == 1
    assert "no log file" in result.stderr


def test_cdh_logs_default_tail(tmp_path):
    state = tmp_path / "state"
    _seed_log(state, [f"line {i}" for i in range(120)])
    result = _run_cdh(state, "logs", "-n", "10")
    assert result.returncode == 0, result.stderr
    out = result.stdout.splitlines()
    assert out == [f"line {i}" for i in range(110, 120)]


def test_cdh_logs_n_larger_than_file(tmp_path):
    state = tmp_path / "state"
    _seed_log(state, ["a", "b", "c"])
    result = _run_cdh(state, "logs", "-n", "100")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["a", "b", "c"]


def test_cdh_logs_follow_streams_appends(tmp_path):
    state = tmp_path / "state"
    log = _seed_log(state, ["initial"])
    env = os.environ.copy()
    env["CDH_STATE_DIR"] = str(state)
    env["CDH_CONFIG_DIR"] = str(state / "config")
    proc = subprocess.Popen(
        [sys.executable, "-m", "claude_dynamic_hooks._cli.main", "logs", "-f", "-n", "5"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    captured: list[bytes] = []

    def reader():
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(1024)
            if not chunk:
                return
            captured.append(chunk)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        time.sleep(0.4)
        with log.open("a") as f:
            f.write("appended-1\n")
            f.flush()
        time.sleep(0.5)
        with log.open("a") as f:
            f.write("appended-2\n")
            f.flush()
        time.sleep(0.5)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
    out = b"".join(captured).decode()
    assert "initial" in out
    assert "appended-1" in out
    assert "appended-2" in out

"""Tests for daemon liveness detection via flock probe."""
from __future__ import annotations

import fcntl
import multiprocessing
import os
from pathlib import Path

from claude_dynamic_hooks._server import process_lock
from claude_dynamic_hooks.paths import DaemonPaths


def is_running(paths: DaemonPaths):
    return process_lock.holder_pid(paths.pid_file)


def _hold_lock(path_str: str, ready_w, done_r):
    fd = os.open(path_str, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.write(ready_w, b"1")
    done_r.read(1)
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def _make_paths(tmp_path: Path) -> DaemonPaths:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return DaemonPaths(
        state_dir=state_dir,
        pid_file=state_dir / "daemon.pid",
        stderr_log=state_dir / "daemon.log",
    )


def test_no_pid_file_returns_none(tmp_path):
    paths = _make_paths(tmp_path)
    assert is_running(paths) is None


def test_pid_file_no_lock_holder_returns_none(tmp_path):
    paths = _make_paths(tmp_path)
    paths.pid_file.write_text(str(os.getpid()))
    assert is_running(paths) is None


def test_pid_file_with_lock_held_returns_pid(tmp_path):
    paths = _make_paths(tmp_path)
    pid = os.getpid()
    paths.pid_file.write_text(str(pid))

    ctx = multiprocessing.get_context("fork")
    ready_r, ready_w = os.pipe()
    done_r, done_w = os.pipe()
    proc = ctx.Process(
        target=_hold_lock,
        args=(str(paths.pid_file), ready_w, os.fdopen(done_r, "rb")),
    )
    proc.start()
    os.close(ready_w)
    os.read(ready_r, 1)
    os.close(ready_r)

    try:
        assert is_running(paths) == pid
    finally:
        os.write(done_w, b"x")
        os.close(done_w)
        proc.join(timeout=3)


def test_corrupt_pid_file_returns_none(tmp_path):
    paths = _make_paths(tmp_path)
    paths.pid_file.write_text("not-a-pid")
    assert is_running(paths) is None

"""Exclusive per-process daemon guard: flock on the PID file.

The fd returned by `acquire` is held for the daemon's lifetime so the OS
releases the flock automatically on crash, letting a new daemon start cleanly.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import sys
from pathlib import Path

_PID_BUF_BYTES = 32

LOCKED_UNKNOWN_PID = -1
"""Sentinel: PID file is locked by a real holder, but the bytes do not parse
as an integer (corrupted file, mid-write race, manual edit). Callers should
treat this as 'daemon is running' for safety, but cannot signal it."""


def holder_pid(pid_file: Path) -> int | None:
    """Return the PID of the lock holder, ``LOCKED_UNKNOWN_PID`` if locked but
    PID unreadable, or ``None`` if not locked."""
    if not pid_file.exists():
        return None
    try:
        fd = os.open(str(pid_file), os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return None
        except BlockingIOError:
            try:
                return int(os.read(fd, _PID_BUF_BYTES).strip())
            except (ValueError, OSError):
                return LOCKED_UNKNOWN_PID
    finally:
        os.close(fd)


def acquire(pid_file: Path) -> int:
    """Acquire exclusive flock; return the held fd. Calls sys.exit(1) on conflict."""
    fd = os.open(str(pid_file), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            existing_pid = int(os.read(fd, _PID_BUF_BYTES).strip())
        except Exception:
            existing_pid = "?"
        os.close(fd)
        print(f"cdh daemon already running (pid {existing_pid})", file=sys.stderr)
        sys.exit(1)
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, str(os.getpid()).encode())
    return fd


def release(fd: int, pid_file: Path) -> None:
    """Release the flock and unlink the PID file."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except OSError:
        pass
    with contextlib.suppress(OSError):
        pid_file.unlink()

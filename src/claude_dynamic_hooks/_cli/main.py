"""CLI entry points for cdh."""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

import requests

from .. import config as config_mod
from .._server import process_lock
from ..paths import DaemonPaths

_DAEMON_START_TIMEOUT_S = 3.0
_DAEMON_START_POLL_INTERVAL_S = 0.1
_HEALTH_PROBE_TIMEOUT_S = 0.5


def _spawn_daemon(paths: DaemonPaths) -> None:
    paths.ensure()
    log = paths.stderr_log.open("a")
    try:
        subprocess.Popen(
            [sys.executable, "-m", "claude_dynamic_hooks._server.daemon"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log,
        )
    finally:
        log.close()


def _format_pid(pid: int) -> str:
    return "pid unknown" if pid == process_lock.LOCKED_UNKNOWN_PID else f"pid {pid}"


def _cmd_start() -> None:
    paths = DaemonPaths.default()
    pid = process_lock.holder_pid(paths.pid_file)
    if pid is not None:
        print(f"cdh daemon already running ({_format_pid(pid)})")
        return
    _spawn_daemon(paths)
    iterations = int(_DAEMON_START_TIMEOUT_S / _DAEMON_START_POLL_INTERVAL_S)
    for _ in range(iterations):
        time.sleep(_DAEMON_START_POLL_INTERVAL_S)
        new_pid = process_lock.holder_pid(paths.pid_file)
        if new_pid is not None:
            print(f"cdh daemon started ({_format_pid(new_pid)})")
            return
    print(
        f"cdh daemon did not come up within {_DAEMON_START_TIMEOUT_S}s; "
        f"check log: {paths.stderr_log}",
        file=sys.stderr,
    )
    sys.exit(1)


def _cmd_stop() -> None:
    paths = DaemonPaths.default()
    pid = process_lock.holder_pid(paths.pid_file)
    if pid is None:
        print("cdh daemon not running")
        sys.exit(0)
    if pid == process_lock.LOCKED_UNKNOWN_PID:
        print(
            "cdh daemon running but pid file unreadable; cannot signal",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print("cdh daemon not running (stale pid file)")
        sys.exit(0)
    except PermissionError as e:
        print(f"cdh daemon stop failed: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"cdh daemon stopped (pid {pid})")
    sys.exit(0)


def _cmd_status() -> None:
    paths = DaemonPaths.default()
    pid = process_lock.holder_pid(paths.pid_file)
    if pid is None:
        print("stopped")
        sys.exit(1)
    print(f"running ({_format_pid(pid)})")
    sys.exit(0)


def _probe_health(url: str, timeout: float = _HEALTH_PROBE_TIMEOUT_S) -> str:
    try:
        r = requests.get(f"{url}/health", timeout=timeout)
    except requests.RequestException:
        return "down"
    if not r.ok:
        return f"down ({r.status_code})"
    return "alive"


def _cmd_list_handlers() -> None:
    try:
        cfg = config_mod.load()
    except Exception as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(1)

    if not cfg.handlers:
        print("(no handlers configured)")
        return

    for entry in cfg.handlers:
        events = ",".join(sorted(e.value for e in entry.events)) or "-"
        status = _probe_health(entry.url)
        print(f"{entry.name}\t{entry.url}\t{status}\t{events}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="cdh", description="claude-dynamic-hooks router")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("start", help="Start the router daemon")
    sub.add_parser("stop", help="Stop the router daemon")
    sub.add_parser("status", help="Show router daemon status")
    sub.add_parser("list-handlers", help="List configured handlers")

    args = parser.parse_args()

    if args.cmd == "start":
        _cmd_start()
    elif args.cmd == "stop":
        _cmd_stop()
    elif args.cmd == "status":
        _cmd_status()
    elif args.cmd == "list-handlers":
        _cmd_list_handlers()


if __name__ == "__main__":
    main()

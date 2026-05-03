"""CLI entry points for cdh."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests

from .. import config as config_mod
from .._server import process_lock
from .._server.transport import client as http_client
from ..events import EventType
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
    _print_reload_status()
    sys.exit(0)


def _print_reload_status() -> None:
    """Probe /health for last config reload status; print if reload failed.

    Silent on success and on probe failure (status command must not depend
    on knowing the daemon's port — best-effort only).
    """
    try:
        cfg = config_mod.load()
    except Exception:
        return
    try:
        r = requests.get(
            f"http://127.0.0.1:{cfg.daemon.port}/health",
            timeout=_HEALTH_PROBE_TIMEOUT_S,
        )
    except requests.RequestException:
        return
    if not r.ok:
        return
    try:
        config_block = r.json().get("config") or {}
    except ValueError:
        return
    if config_block.get("last_reload_ok") is False:
        err = config_block.get("last_reload_error") or "(no detail)"
        print(f"CONFIG RELOAD FAILED: {err}", file=sys.stderr)


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


def _resolve_payload(args: argparse.Namespace) -> dict:
    """Materialize the payload dict from --payload / --payload-file / stdin."""
    if args.payload and args.payload_file:
        print("--payload and --payload-file are mutually exclusive", file=sys.stderr)
        sys.exit(2)
    if args.payload_file:
        text = args.payload_file.read_text()
    elif args.payload == "-":
        text = sys.stdin.read()
    elif args.payload:
        text = args.payload
    else:
        return {}
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"payload is not valid JSON: {e}", file=sys.stderr)
        sys.exit(2)
    if not isinstance(obj, dict):
        print("payload must be a JSON object", file=sys.stderr)
        sys.exit(2)
    return obj


def _cmd_test(args: argparse.Namespace) -> None:
    try:
        cfg = config_mod.load()
    except Exception as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(1)

    matches = [h for h in cfg.handlers if h.name == args.handler]
    if not matches:
        names = ", ".join(h.name for h in cfg.handlers) or "(none)"
        print(
            f"unknown handler {args.handler!r}; configured: {names}", file=sys.stderr,
        )
        sys.exit(1)
    handler = matches[0]

    try:
        ev = EventType(args.event)
    except ValueError:
        print(f"unknown event {args.event!r}", file=sys.stderr)
        sys.exit(2)

    if ev not in handler.events:
        declared = ",".join(sorted(e.value for e in handler.events)) or "-"
        print(
            f"warning: handler {handler.name!r} does not declare {args.event!r} "
            f"(declared: {declared}); sending anyway",
            file=sys.stderr,
        )

    params = _resolve_payload(args)
    url = f"{handler.url}/hooks/{ev.value}"
    wire = {"payload": json.dumps(params)}
    raw = http_client.post(
        url, wire,
        timeout_s=cfg.daemon.request_timeout_s,
        max_response_bytes=cfg.daemon.wire_max_bytes,
    )
    if raw is None:
        print("handler error or unreachable (see stderr above)", file=sys.stderr)
        sys.exit(1)
    if "envelope" not in raw:
        print(
            f"malformed response: missing 'envelope' key; got {list(raw)}",
            file=sys.stderr,
        )
        sys.exit(1)
    env = raw["envelope"]
    if env is None:
        print("null (handler abstained)")
        return
    if not isinstance(env, str):
        print(
            f"malformed response: 'envelope' must be a JSON string or null; "
            f"got {type(env).__name__}",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        parsed = json.loads(env)
    except json.JSONDecodeError as e:
        print(f"malformed envelope JSON: {e}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(parsed, indent=2, sort_keys=True))


def _cmd_logs(args: argparse.Namespace) -> None:
    paths = DaemonPaths.default()
    log_path = paths.stderr_log
    if not log_path.exists():
        print(f"no log file at {log_path}", file=sys.stderr)
        sys.exit(1)
    if args.follow:
        _follow_log(log_path, tail_lines=args.lines)
    else:
        _tail_log(log_path, args.lines)


def _tail_log(path, n: int) -> None:
    with path.open("rb") as f:
        try:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            blocksize = 4096
            data = b""
            while size > 0 and data.count(b"\n") <= n:
                step = min(blocksize, size)
                size -= step
                f.seek(size)
                data = f.read(step) + data
            lines = data.splitlines()[-n:]
        except OSError:
            f.seek(0)
            lines = f.read().splitlines()[-n:]
    for line in lines:
        sys.stdout.buffer.write(line + b"\n")
    sys.stdout.flush()


def _follow_log(path, tail_lines: int) -> None:
    _tail_log(path, tail_lines)
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        try:
            while True:
                chunk = f.read(4096)
                if chunk:
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.flush()
                else:
                    time.sleep(0.2)
        except KeyboardInterrupt:
            return


def main() -> None:
    parser = argparse.ArgumentParser(prog="cdh", description="claude-dynamic-hooks router")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("start", help="Start the router daemon")
    sub.add_parser("stop", help="Stop the router daemon")
    sub.add_parser("status", help="Show router daemon status")
    sub.add_parser("list-handlers", help="List configured handlers")

    p_test = sub.add_parser(
        "test",
        help="POST a synthetic event directly to one handler (bypasses chain)",
    )
    p_test.add_argument("handler", help="Handler name from config")
    p_test.add_argument("event", help="Event name (e.g. preToolUse)")
    p_test.add_argument(
        "--payload",
        help="Inline JSON payload, or '-' to read from stdin",
    )
    p_test.add_argument(
        "--payload-file", type=Path,
        help="Path to a JSON file containing the payload",
    )

    p_logs = sub.add_parser("logs", help="Show daemon log")
    p_logs.add_argument(
        "-n", "--lines", type=int, default=50, help="Lines to show (default 50)",
    )
    p_logs.add_argument(
        "-f", "--follow", action="store_true", help="Stream new log lines",
    )

    args = parser.parse_args()

    if args.cmd == "start":
        _cmd_start()
    elif args.cmd == "stop":
        _cmd_stop()
    elif args.cmd == "status":
        _cmd_status()
    elif args.cmd == "list-handlers":
        _cmd_list_handlers()
    elif args.cmd == "test":
        _cmd_test(args)
    elif args.cmd == "logs":
        _cmd_logs(args)


if __name__ == "__main__":
    main()

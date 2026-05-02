"""Filesystem watcher for the router config.

Watches the parent directory of `config.toml` (non-recursive) via
`watchdog`. On any modify/create/move event whose path matches the target
file, schedules a debounced reload callback. Watching the parent dir
(rather than the file directly) catches atomic-rename editor saves
(vim default, most IDEs) without races on inode changes.

Debouncing collapses bursts of events from shell redirects (`cat > file`
fires open+write+close, ~2-3 events) and editor save sequences (write
tmp → fsync → rename → chmod) into a single reload after the file
settles. The debounce window is short enough (50 ms) to feel instant.

Reload errors are logged but never raised — the watcher thread must
stay alive across malformed-TOML edits so the next save can recover.
"""
from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

_DEBOUNCE_S = 0.05


def _log(msg: str) -> None:
    sys.stderr.write(f"config_watcher {msg}\n")
    sys.stderr.flush()


class _Handler(FileSystemEventHandler):
    def __init__(self, target: Path, on_change: Callable[[], None]) -> None:
        self._target = os.path.abspath(os.fspath(target))
        self._on_change = on_change
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def _maybe_fire(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        paths = [event.src_path]
        # `on_moved` events also carry dest_path; check both so editor
        # save-via-rename (write tmp → rename over target) is caught.
        dest = getattr(event, "dest_path", None)
        if dest:
            paths.append(dest)
        if not any(os.path.abspath(p) == self._target for p in paths):
            return
        self._schedule()

    on_modified = _maybe_fire
    on_created = _maybe_fire
    on_moved = _maybe_fire

    def _schedule(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(_DEBOUNCE_S, self._run)
            self._timer.daemon = True
            self._timer.start()

    def _run(self) -> None:
        try:
            self._on_change()
        except Exception as e:
            _log(f"reload failed: {e!r}")


def start(config_path: Path, on_change: Callable[[], None]) -> Any:
    """Start watching ``config_path`` for changes; call ``on_change`` on edits.

    Returns the running observer; caller must invoke ``observer.stop()``
    and ``observer.join(timeout)`` on shutdown.
    """
    parent = config_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    observer = Observer()
    observer.schedule(_Handler(config_path, on_change), str(parent), recursive=False)
    observer.daemon = True
    observer.start()
    return observer

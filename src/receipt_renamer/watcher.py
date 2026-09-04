"""Folder watching with a write-stability debounce.

Auto-document feeders write files incrementally, so a newly created path is not safe
to read until its size and mtime have stopped changing.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .config import Settings
from .images import SUPPORTED_EXTENSIONS

DEFAULT_DEBOUNCE = 2.0
_POLL_INTERVAL = 0.25


def is_stable(path: Path, *, debounce: float = DEFAULT_DEBOUNCE, timeout: float = 120.0) -> bool:
    """Block until a file stops changing for ``debounce`` seconds."""
    deadline = time.monotonic() + timeout
    last: tuple[int, float] | None = None
    steady_since = time.monotonic()
    while time.monotonic() < deadline:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return False
        current = (stat.st_size, stat.st_mtime)
        if current != last:
            last = current
            steady_since = time.monotonic()
        elif stat.st_size > 0 and time.monotonic() - steady_since >= debounce:
            return True
        time.sleep(_POLL_INTERVAL)
    return False


class _ScanHandler(FileSystemEventHandler):
    def __init__(self, settings: Settings, enqueue: Callable[[Path], None]) -> None:
        self._settings = settings
        self._enqueue = enqueue

    def _consider(self, raw_path: str | bytes) -> None:
        path = Path(raw_path.decode() if isinstance(raw_path, bytes) else raw_path)
        if path.name.startswith("."):
            return
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            return
        if self._settings.should_skip(path):
            return
        self._enqueue(path)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._consider(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._consider(event.dest_path)


class FolderWatcher:
    """Watch a folder and invoke ``handler`` once each new file is fully written."""

    def __init__(
        self,
        settings: Settings,
        handler: Callable[[Path], None],
        *,
        debounce: float = DEFAULT_DEBOUNCE,
    ) -> None:
        self._settings = settings
        self._handler = handler
        self._debounce = debounce
        self._seen: set[Path] = set()
        self._lock = threading.Lock()
        self._observer = Observer()

    def _enqueue(self, path: Path) -> None:
        with self._lock:
            if path in self._seen:
                return
            self._seen.add(path)
        thread = threading.Thread(target=self._process_when_stable, args=(path,), daemon=True)
        thread.start()

    def _process_when_stable(self, path: Path) -> None:
        try:
            if is_stable(path, debounce=self._debounce):
                self._handler(path)
        finally:
            with self._lock:
                self._seen.discard(path)

    def run_forever(self) -> None:
        """Start watching and block until interrupted."""
        self._observer.schedule(
            _ScanHandler(self._settings, self._enqueue),
            str(self._settings.input_dir),
            recursive=self._settings.recursive,
        )
        self._observer.start()
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self._observer.stop()
            self._observer.join(timeout=5)

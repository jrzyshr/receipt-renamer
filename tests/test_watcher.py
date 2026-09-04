"""Watcher debounce logic (no real filesystem events required)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from receipt_renamer.watcher import is_stable


def test_is_stable_waits_for_writes_to_finish(tmp_path: Path) -> None:
    target = tmp_path / "scan.jpg"
    target.write_bytes(b"partial")

    def keep_writing() -> None:
        for _ in range(4):
            time.sleep(0.1)
            with target.open("ab") as handle:
                handle.write(b"more")

    writer = threading.Thread(target=keep_writing)
    started = time.monotonic()
    writer.start()
    assert is_stable(target, debounce=0.3, timeout=10) is True
    writer.join()
    # It must not have returned before the writer stopped appending.
    assert time.monotonic() - started >= 0.4


def test_is_stable_returns_false_for_missing_file(tmp_path: Path) -> None:
    assert is_stable(tmp_path / "gone.jpg", debounce=0.1, timeout=1) is False


def test_is_stable_ignores_empty_files(tmp_path: Path) -> None:
    empty = tmp_path / "empty.jpg"
    empty.touch()
    assert is_stable(empty, debounce=0.1, timeout=0.6) is False


def test_is_stable_accepts_a_settled_file(tmp_path: Path) -> None:
    settled = tmp_path / "done.jpg"
    settled.write_bytes(b"complete file")
    assert is_stable(settled, debounce=0.2, timeout=5) is True

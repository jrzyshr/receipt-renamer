"""A file that cannot be moved must not take the rest of the batch down with it.

Locks are common on Windows (antivirus, cloud sync, scanner software) and possible
anywhere; these tests simulate them by making the move fail.
"""

from __future__ import annotations

import errno
import os
import shutil
from datetime import date
from pathlib import Path

import pytest

from receipt_renamer.ledger import Ledger
from receipt_renamer.processor import (
    MoveError,
    _move,
    process_directory,
    process_file,
)
from receipt_renamer.providers.fake import AlwaysFailsProvider, FakeProvider

TODAY = date(2024, 6, 1)


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the retry logic exercised without paying the real backoff."""
    monkeypatch.setattr("receipt_renamer.processor.MOVE_RETRY_DELAY", 0.0)


def payload(**overrides):
    data = {
        "date": "2024-03-14",
        "business": "Blue Bottle Coffee",
        "purpose": "Meals",
        "confidence": 0.95,
    }
    data.update(overrides)
    return data


def lock(monkeypatch: pytest.MonkeyPatch, *, match: str = "", fail_times: int | None = None):
    """Make os.replace/shutil.move fail for paths containing ``match``."""
    real_replace = os.replace
    state = {"calls": 0}

    def fake_replace(src, dst):
        if match in str(src):
            state["calls"] += 1
            if fail_times is None or state["calls"] <= fail_times:
                raise PermissionError(13, "used by another process")
        return real_replace(src, dst)

    def fake_move(src, dst):
        raise PermissionError(13, "used by another process")

    monkeypatch.setattr(os, "replace", fake_replace)
    monkeypatch.setattr(shutil, "move", fake_move)
    return state


def test_locked_file_does_not_abort_the_batch(
    inbox, settings_factory, image_factory, monkeypatch
) -> None:
    for i in range(4):
        image_factory(inbox / f"scan_{i}.jpg")
    lock(monkeypatch, match="scan_1")

    summary = process_directory(
        FakeProvider([payload()] * 4), settings_factory(), today=TODAY
    )

    assert len(summary.results) == 4
    assert summary.renamed == 3
    assert summary.skipped == 1


def test_locked_file_is_reported_not_silently_dropped(
    inbox, settings_factory, image_factory, monkeypatch
) -> None:
    image_factory(inbox / "scan_0.jpg")
    lock(monkeypatch, match="scan_0")

    summary = process_directory(FakeProvider([payload()]), settings_factory(), today=TODAY)

    result = summary.results[0]
    assert result.outcome == "skipped"
    assert "could not move" in (result.reason or "")
    assert "another process" in (result.reason or "")


def test_locked_original_is_left_untouched(
    inbox, settings_factory, image_factory, monkeypatch
) -> None:
    source = image_factory(inbox / "scan_0.jpg")
    before = source.read_bytes()
    lock(monkeypatch, match="scan_0")

    process_directory(FakeProvider([payload()]), settings_factory(), today=TODAY)

    assert source.exists(), "a file we could not move must stay where it was"
    assert source.read_bytes() == before


def test_transient_lock_is_retried_and_succeeds(
    inbox, settings_factory, image_factory, monkeypatch
) -> None:
    """Antivirus and sync clients hold brief locks; a retry should ride them out."""
    image_factory(inbox / "scan_0.jpg")
    lock(monkeypatch, match="scan_0", fail_times=1)

    summary = process_directory(FakeProvider([payload()]), settings_factory(), today=TODAY)

    assert summary.renamed == 1
    assert summary.skipped == 0


def test_needs_review_move_failure_keeps_the_diagnosis(
    inbox, settings_factory, image_factory, monkeypatch
) -> None:
    """Losing the move must not lose *why* the file was being reviewed."""
    image_factory(inbox / "scan_0.jpg")
    lock(monkeypatch, match="scan_0")

    summary = process_directory(AlwaysFailsProvider(), settings_factory(), today=TODAY)

    result = summary.results[0]
    assert result.outcome == "skipped"
    assert "could not move" in (result.reason or "")
    # The original extraction failure is still visible.
    assert result.reason != "could not move file"
    assert len(result.reason or "") > len("could not move file: ")


def test_failed_move_is_not_recorded_as_applied(
    inbox, settings_factory, image_factory, monkeypatch, tmp_path
) -> None:
    """A move that never happened must not look reversible in the ledger."""
    image_factory(inbox / "scan_0.jpg")
    ledger = Ledger(tmp_path / "ledger.jsonl")
    lock(monkeypatch, match="scan_0")

    summary = process_directory(
        FakeProvider([payload()]), settings_factory(), ledger=ledger, today=TODAY
    )

    assert summary.results[0].applied is False
    assert ledger.moves_for_run(summary.run_id) == []


def test_collision_numbering_has_no_gap_after_a_failure(
    inbox, settings_factory, image_factory, monkeypatch
) -> None:
    """A file that failed to move must release the name it had claimed."""
    for i in range(3):
        image_factory(inbox / f"scan_{i}.jpg")
    lock(monkeypatch, match="scan_0")

    settings = settings_factory()
    summary = process_directory(FakeProvider([payload()] * 3), settings, today=TODAY)

    names = sorted(p.name for p in settings.output_dir.iterdir())
    assert names == [
        "2024-03-14 Blue Bottle Coffee - Meals (2).jpg",
        "2024-03-14 Blue Bottle Coffee - Meals.jpg",
    ]
    assert summary.skipped == 1


def test_move_raises_move_error_after_exhausting_attempts(tmp_path, monkeypatch) -> None:
    source = tmp_path / "a.jpg"
    source.write_bytes(b"data")
    lock(monkeypatch, match="a.jpg")

    with pytest.raises(MoveError):
        _move(source, tmp_path / "out" / "b.jpg", attempts=2, delay=0)

    assert source.exists()


def test_cross_device_move_still_falls_back_to_copy(tmp_path, monkeypatch) -> None:
    """EXDEV must use shutil.move rather than being retried pointlessly."""
    source = tmp_path / "a.jpg"
    source.write_bytes(b"data")
    destination = tmp_path / "out" / "b.jpg"
    used = {"shutil": False}

    def exdev(src, dst):
        raise OSError(errno.EXDEV, "cross-device link")

    def fake_move(src, dst):
        used["shutil"] = True
        Path(dst).write_bytes(Path(src).read_bytes())
        Path(src).unlink()

    monkeypatch.setattr(os, "replace", exdev)
    monkeypatch.setattr(shutil, "move", fake_move)

    assert _move(source, destination, delay=0) == destination
    assert used["shutil"] is True
    assert destination.read_bytes() == b"data"


def test_partial_copy_is_cleaned_up_so_it_cannot_look_complete(
    tmp_path, monkeypatch
) -> None:
    source = tmp_path / "a.jpg"
    source.write_bytes(b"data")
    destination = tmp_path / "out" / "b.jpg"

    def exdev(src, dst):
        raise OSError(errno.EXDEV, "cross-device link")

    def half_written_move(src, dst):
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"par")  # truncated copy
        raise OSError(errno.EIO, "disk fell over")

    monkeypatch.setattr(os, "replace", exdev)
    monkeypatch.setattr(shutil, "move", half_written_move)

    with pytest.raises(MoveError):
        _move(source, destination, attempts=1, delay=0)

    assert source.exists(), "original must survive"
    assert not destination.exists(), "a truncated copy must not be left behind"


def test_process_file_never_raises_on_a_locked_file(
    inbox, settings_factory, image_factory, monkeypatch
) -> None:
    """The docstring promise, asserted directly."""
    source = image_factory(inbox / "scan_0.jpg")
    lock(monkeypatch, match="scan_0")

    result = process_file(
        source, FakeProvider([payload()]), settings_factory(), today=TODAY
    )

    assert result.outcome == "skipped"

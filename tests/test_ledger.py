"""Ledger persistence and undo semantics."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from receipt_renamer.ledger import Ledger, LedgerEntry, new_run_id, undo_run
from receipt_renamer.processor import process_directory
from receipt_renamer.providers.fake import AlwaysFailsProvider, FakeProvider

TODAY = date(2024, 6, 1)


def payload(**overrides):
    data = {
        "date": "2024-03-14",
        "business": "Blue Bottle Coffee",
        "purpose": "Meals",
        "confidence": 0.95,
    }
    data.update(overrides)
    return data


def test_entries_round_trip(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "log.jsonl")
    entry = LedgerEntry(
        run_id="r1",
        action="renamed",
        original_path="/a/scan.jpg",
        new_path="/b/nice.jpg",
        confidence=0.9,
    )
    ledger.append(entry)
    (loaded,) = ledger.read_all()
    assert loaded.original_path == entry.original_path
    assert loaded.confidence == 0.9
    assert loaded.version == 1


def test_partial_final_line_is_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    ledger = Ledger(path)
    ledger.append(LedgerEntry(run_id="r1", action="renamed", original_path="/a.jpg", new_path="/b.jpg"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"run_id": "r2", "acti')
    assert len(ledger.read_all()) == 1


def test_last_run_id_picks_the_most_recent(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "log.jsonl")
    ledger.append(LedgerEntry(run_id="r1", action="renamed", original_path="/a.jpg", new_path="/b.jpg"))
    ledger.append(LedgerEntry(run_id="r2", action="renamed", original_path="/c.jpg", new_path="/d.jpg"))
    assert ledger.last_run_id() == "r2"


def test_undo_restores_renamed_and_needs_review_files(
    inbox, settings_factory, image_factory
) -> None:
    image_factory(inbox / "scan_0001.jpg")
    image_factory(inbox / "scan_0002.jpg")
    settings = settings_factory()
    ledger = Ledger(settings.ledger_path)
    summary = process_directory(
        FakeProvider([payload(), payload(confidence=0.1)]), settings, ledger=ledger, today=TODAY
    )
    assert not (inbox / "scan_0001.jpg").exists()

    run_id, results = undo_run(ledger, summary.run_id)
    assert run_id == summary.run_id
    assert {r.status for r in results} == {"restored"}
    assert (inbox / "scan_0001.jpg").exists()
    assert (inbox / "scan_0002.jpg").exists()
    assert not list(settings.output_dir.glob("*.jpg"))


def test_undo_defaults_to_the_last_run(inbox, settings_factory, image_factory) -> None:
    settings = settings_factory()
    ledger = Ledger(settings.ledger_path)

    image_factory(inbox / "first.jpg")
    process_directory(FakeProvider([payload()]), settings, ledger=ledger, today=TODAY)
    image_factory(inbox / "second.jpg")
    second = process_directory(
        FakeProvider([payload(business="Shell", purpose="Fuel")]),
        settings,
        ledger=ledger,
        today=TODAY,
    )

    run_id, results = undo_run(ledger)
    assert run_id == second.run_id
    assert (inbox / "second.jpg").exists()
    assert not (inbox / "first.jpg").exists()  # the earlier run is untouched


def test_undo_is_not_repeated_for_an_already_undone_run(
    inbox, settings_factory, image_factory
) -> None:
    settings = settings_factory()
    ledger = Ledger(settings.ledger_path)
    image_factory(inbox / "first.jpg")
    first = process_directory(FakeProvider([payload()]), settings, ledger=ledger, today=TODAY)
    undo_run(ledger, first.run_id)

    assert ledger.last_run_id() is None


def test_undo_dry_run_moves_nothing(inbox, settings_factory, image_factory) -> None:
    image_factory(inbox / "scan_0001.jpg")
    settings = settings_factory()
    ledger = Ledger(settings.ledger_path)
    summary = process_directory(FakeProvider([payload()]), settings, ledger=ledger, today=TODAY)

    _, results = undo_run(ledger, summary.run_id, dry_run=True)
    assert [r.status for r in results] == ["restored"]
    assert not (inbox / "scan_0001.jpg").exists()
    assert list(settings.output_dir.glob("*.jpg"))


def test_undo_refuses_to_overwrite_a_recreated_original(
    inbox, settings_factory, image_factory
) -> None:
    image_factory(inbox / "scan_0001.jpg")
    settings = settings_factory()
    ledger = Ledger(settings.ledger_path)
    summary = process_directory(FakeProvider([payload()]), settings, ledger=ledger, today=TODAY)
    image_factory(inbox / "scan_0001.jpg")  # a new scan lands with the same name

    _, results = undo_run(ledger, summary.run_id)
    assert [r.status for r in results] == ["blocked"]
    assert list(settings.output_dir.glob("*.jpg"))  # renamed file left in place


def test_undo_reports_missing_files(inbox, settings_factory, image_factory) -> None:
    image_factory(inbox / "scan_0001.jpg")
    settings = settings_factory()
    ledger = Ledger(settings.ledger_path)
    summary = process_directory(FakeProvider([payload()]), settings, ledger=ledger, today=TODAY)
    for path in settings.output_dir.glob("*.jpg"):
        path.unlink()

    _, results = undo_run(ledger, summary.run_id)
    assert [r.status for r in results] == ["missing"]


def test_undo_with_no_ledger_entries(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "log.jsonl")
    assert undo_run(ledger) == (None, [])


def test_undo_skips_entries_that_never_moved(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "log.jsonl")
    run_id = new_run_id()
    ledger.append(
        LedgerEntry(run_id=run_id, action="skipped", original_path=str(tmp_path / "notes.txt"))
    )
    _, results = undo_run(ledger, run_id)
    assert results == []


def test_needs_review_run_can_be_undone(inbox, settings_factory, image_factory) -> None:
    image_factory(inbox / "scan_0001.jpg")
    settings = settings_factory()
    ledger = Ledger(settings.ledger_path)
    summary = process_directory(AlwaysFailsProvider(), settings, ledger=ledger, today=TODAY)

    undo_run(ledger, summary.run_id)
    assert (inbox / "scan_0001.jpg").exists()
    assert not settings.needs_review_dir.exists()

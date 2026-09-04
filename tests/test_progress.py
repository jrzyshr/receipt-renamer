"""Progress reporting: start hooks, the failure circuit breaker, and CLI output."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from typer.testing import CliRunner

from receipt_renamer.cli import app
from receipt_renamer.processor import (
    DEFAULT_MAX_CONSECUTIVE_FAILURES,
    process_paths,
    scan_input,
)
from receipt_renamer.providers.fake import AlwaysFailsProvider, FakeProvider

TODAY = date(2024, 6, 1)
runner = CliRunner()


def payload(**overrides):
    data = {
        "date": "2024-03-14",
        "business": "Blue Bottle Coffee",
        "purpose": "Meals",
        "confidence": 0.95,
    }
    data.update(overrides)
    return data


class FlakyProvider:
    """Fails only on the calls listed in ``fail_on`` (1-based)."""

    model = "flaky"

    def __init__(self, fail_on: set[int]) -> None:
        self.fail_on = fail_on
        self.calls = 0
        self._good = FakeProvider([payload()] * 50)

    def extract(self, image_bytes: bytes, *, mime_type: str):
        self.calls += 1
        if self.calls in self.fail_on:
            from receipt_renamer.providers import ProviderError

            raise ProviderError("429 rate limit exceeded")
        return self._good.extract(image_bytes, mime_type=mime_type)


def test_on_file_start_fires_before_each_result(inbox, settings_factory, image_factory) -> None:
    paths = [image_factory(inbox / f"scan_{i}.jpg") for i in range(3)]
    events: list[tuple[str, str]] = []

    process_paths(
        paths,
        FakeProvider([payload()] * 3),
        settings_factory(dry_run=True),
        on_file_start=lambda p: events.append(("start", p.name)),
        on_result=lambda r: events.append(("result", r.original.name)),
        today=TODAY,
    )

    assert events == [
        ("start", "scan_0.jpg"),
        ("result", "scan_0.jpg"),
        ("start", "scan_1.jpg"),
        ("result", "scan_1.jpg"),
        ("start", "scan_2.jpg"),
        ("result", "scan_2.jpg"),
    ]


def test_batch_aborts_after_consecutive_provider_failures(
    inbox, settings_factory, image_factory
) -> None:
    total = DEFAULT_MAX_CONSECUTIVE_FAILURES + 4
    paths = [image_factory(inbox / f"scan_{i:02d}.jpg") for i in range(total)]

    summary = process_paths(
        paths, AlwaysFailsProvider(), settings_factory(dry_run=True), today=TODAY
    )

    assert summary.aborted
    assert len(summary.results) == DEFAULT_MAX_CONSECUTIVE_FAILURES
    assert "consecutive provider failures" in (summary.aborted_reason or "")


def test_isolated_failures_do_not_abort_the_batch(inbox, settings_factory, image_factory) -> None:
    """A single bad receipt must not look like an outage."""
    paths = [image_factory(inbox / f"scan_{i:02d}.jpg") for i in range(8)]

    summary = process_paths(
        paths,
        FlakyProvider(fail_on={2, 5}),
        settings_factory(dry_run=True),
        today=TODAY,
    )

    assert not summary.aborted
    assert len(summary.results) == 8
    assert summary.needs_review == 2
    assert summary.renamed == 6


def test_low_confidence_does_not_trip_the_breaker(inbox, settings_factory, image_factory) -> None:
    """Unreadable receipts are a data problem, not an infrastructure one."""
    total = DEFAULT_MAX_CONSECUTIVE_FAILURES + 2
    paths = [image_factory(inbox / f"scan_{i:02d}.jpg") for i in range(total)]

    summary = process_paths(
        paths,
        FakeProvider([payload(confidence=0.1)] * total),
        settings_factory(dry_run=True),
        today=TODAY,
    )

    assert not summary.aborted
    assert summary.needs_review == total


def test_breaker_can_be_disabled(inbox, settings_factory, image_factory) -> None:
    total = DEFAULT_MAX_CONSECUTIVE_FAILURES + 3
    paths = [image_factory(inbox / f"scan_{i:02d}.jpg") for i in range(total)]

    summary = process_paths(
        paths,
        AlwaysFailsProvider(),
        settings_factory(dry_run=True),
        max_consecutive_failures=0,
        today=TODAY,
    )

    assert not summary.aborted
    assert len(summary.results) == total


def test_scan_input_separates_unreadable_files(inbox, settings_factory, image_factory) -> None:
    image_factory(inbox / "scan_1.jpg")
    (inbox / "Thumbs.db").write_text("junk")
    (inbox / "notes.txt").write_text("junk")

    candidates, unsupported = scan_input(settings_factory())

    assert [p.name for p in candidates] == ["scan_1.jpg"]
    assert sorted(p.name for p in unsupported) == ["Thumbs.db", "notes.txt"]


def test_run_reports_each_phase(inbox, image_factory) -> None:
    image_factory(inbox / "scan_1.jpg")

    result = runner.invoke(app, ["run", "--input", str(inbox), "--provider", "fake"])

    assert result.exit_code == 0, result.output
    assert "Connected to" in result.output
    assert "Found 1 receipt" in result.output


def test_run_counts_unreadable_files_up_front(inbox, image_factory) -> None:
    image_factory(inbox / "scan_1.jpg")
    (inbox / "Thumbs.db").write_text("junk")

    result = runner.invoke(app, ["run", "--input", str(inbox), "--provider", "fake"])

    assert "1 unreadable file(s) ignored" in result.output


def test_run_streams_problem_files_but_not_successes(inbox, image_factory, monkeypatch) -> None:
    """Renamed files stay quiet; anything needing attention is shown immediately."""
    image_factory(inbox / "good.jpg")
    image_factory(inbox / "bad.jpg")
    provider = FlakyProvider(fail_on={1})  # sorted order puts bad.jpg first
    monkeypatch.setattr("receipt_renamer.cli.get_provider", lambda *a, **k: provider)

    result = runner.invoke(app, ["run", "--input", str(inbox), "--provider", "fake"])

    streamed = [line for line in result.output.splitlines() if "needs review" in line]
    assert streamed, result.output
    assert "429 rate limit" in result.output


def test_run_verbose_streams_every_file(inbox, image_factory) -> None:
    image_factory(inbox / "scan_1.jpg")

    result = runner.invoke(
        app, ["run", "--input", str(inbox), "--provider", "fake", "--verbose"]
    )

    assert "renamed" in result.output


def test_run_exits_nonzero_when_the_batch_aborts(inbox, image_factory, monkeypatch) -> None:
    for i in range(DEFAULT_MAX_CONSECUTIVE_FAILURES + 2):
        image_factory(inbox / f"scan_{i:02d}.jpg")
    monkeypatch.setattr(
        "receipt_renamer.cli.get_provider", lambda *a, **k: AlwaysFailsProvider()
    )

    result = runner.invoke(app, ["run", "--input", str(inbox), "--provider", "fake"])

    assert result.exit_code == 1
    assert "Aborted" in result.output


def test_run_on_empty_folder_says_so(inbox: Path) -> None:
    result = runner.invoke(app, ["run", "--input", str(inbox), "--provider", "fake"])

    assert result.exit_code == 0
    assert "No supported image files found" in result.output


class InterruptingProvider:
    """Raises KeyboardInterrupt part-way through, as Ctrl-C would."""

    model = "interrupting"

    def __init__(self, stop_after: int) -> None:
        self.stop_after = stop_after
        self.calls = 0
        self._good = FakeProvider([payload()] * 50)

    def extract(self, image_bytes: bytes, *, mime_type: str):
        self.calls += 1
        if self.calls > self.stop_after:
            raise KeyboardInterrupt
        return self._good.extract(image_bytes, mime_type=mime_type)


def test_ctrl_c_is_not_swallowed_as_a_bad_receipt(
    inbox, settings_factory, image_factory
) -> None:
    """A cancel must abort the batch, not quietly file receipts under Needs Review."""
    import pytest

    paths = [image_factory(inbox / f"scan_{i}.jpg") for i in range(4)]

    with pytest.raises(KeyboardInterrupt):
        process_paths(
            paths, InterruptingProvider(stop_after=2), settings_factory(dry_run=True), today=TODAY
        )


def test_run_keeps_partial_results_after_ctrl_c(inbox, image_factory, monkeypatch) -> None:
    """Applied moves must stay undoable, so the run id has to survive the interrupt."""
    for i in range(4):
        image_factory(inbox / f"scan_{i}.jpg")
    monkeypatch.setattr(
        "receipt_renamer.cli.get_provider", lambda *a, **k: InterruptingProvider(stop_after=2)
    )

    result = runner.invoke(
        app, ["run", "--input", str(inbox), "--provider", "fake", "--apply"]
    )

    assert "Interrupted" in result.output
    assert "after 2 of 4 file(s)" in result.output
    assert "run id:" in result.output

"""The per-file pipeline: discover, extract, name, and move (or plan) receipts."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import date as date_cls
from pathlib import Path
from typing import Literal

from .config import Settings
from .extractor import ExtractionError, ReceiptData, extract_receipt
from .images import SUPPORTED_EXTENSIONS, unsupported_reason
from .ledger import Ledger, LedgerEntry, new_run_id
from .naming import build_stem, normalize_extension, unique_path
from .providers import VisionProvider

Outcome = Literal["renamed", "needs_review", "skipped"]


@dataclass(slots=True)
class FileResult:
    """What happened (or would happen) to a single file."""

    original: Path
    outcome: Outcome
    destination: Path | None = None
    data: ReceiptData | None = None
    reason: str | None = None
    applied: bool = False

    @property
    def new_name(self) -> str:
        return self.destination.name if self.destination else "-"


@dataclass(slots=True)
class RunSummary:
    """Aggregate result of one run."""

    run_id: str
    results: list[FileResult]
    dry_run: bool

    @property
    def renamed(self) -> int:
        return sum(1 for r in self.results if r.outcome == "renamed")

    @property
    def needs_review(self) -> int:
        return sum(1 for r in self.results if r.outcome == "needs_review")

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.outcome == "skipped")


def iter_candidates(settings: Settings) -> Iterator[Path]:
    """Yield candidate files in the input folder, skipping output folders."""
    pattern = "**/*" if settings.recursive else "*"
    for path in sorted(settings.input_dir.glob(pattern)):
        if not path.is_file() or path.name.startswith("."):
            continue
        if settings.should_skip(path):
            continue
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        yield path


def process_file(
    path: Path,
    provider: VisionProvider,
    settings: Settings,
    *,
    ledger: Ledger | None = None,
    run_id: str | None = None,
    reserved: set[Path] | None = None,
    today: date_cls | None = None,
) -> FileResult:
    """Process one file end to end. Never raises for a single bad receipt."""
    reserved = reserved if reserved is not None else set()
    run_id = run_id or new_run_id()

    reason = unsupported_reason(path)
    if reason:
        result = FileResult(original=path, outcome="skipped", reason=reason)
        _record(ledger, run_id, result, settings)
        return result

    try:
        data = extract_receipt(
            path,
            provider,
            min_confidence=settings.min_confidence,
            max_edge=settings.max_edge,
            quality=settings.jpeg_quality,
            today=today,
        )
    except ExtractionError as exc:
        return _to_needs_review(path, str(exc), settings, ledger, run_id, reserved)

    stem = build_stem(data.date, data.business, data.purpose)
    destination_dir = path.parent if settings.in_place else settings.output_dir
    destination = unique_path(
        destination_dir, stem, normalize_extension(path), reserved=reserved
    )
    reserved.add(destination)

    result = FileResult(original=path, outcome="renamed", destination=destination, data=data)
    if not settings.dry_run:
        if destination != path:
            result.destination = _move(path, destination)
        result.applied = True
    _record(ledger, run_id, result, settings)
    return result


def _to_needs_review(
    path: Path,
    reason: str,
    settings: Settings,
    ledger: Ledger | None,
    run_id: str,
    reserved: set[Path],
) -> FileResult:
    destination = unique_path(
        settings.needs_review_dir, path.stem, normalize_extension(path), reserved=reserved
    )
    reserved.add(destination)
    result = FileResult(
        original=path, outcome="needs_review", destination=destination, reason=reason
    )
    if not settings.dry_run:
        result.destination = _move(path, destination)
        result.applied = True
    _record(ledger, run_id, result, settings)
    return result


def _move(source: Path, destination: Path) -> Path:
    """Move a file, never overwriting an existing one. Returns the final path."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination = unique_path(destination.parent, destination.stem, destination.suffix)
    try:
        os.replace(source, destination)
    except OSError:
        # Cross-device move (e.g. output on another volume): fall back to copy+unlink.
        import shutil

        shutil.move(str(source), str(destination))
    return destination


def _record(
    ledger: Ledger | None, run_id: str, result: FileResult, settings: Settings
) -> None:
    if ledger is None or settings.dry_run:
        return
    data = result.data
    ledger.append(
        LedgerEntry(
            run_id=run_id,
            action=result.outcome,
            original_path=str(result.original),
            new_path=str(result.destination) if result.destination else None,
            date=data.date if data else None,
            business=data.business if data else None,
            purpose=data.purpose if data else None,
            confidence=data.confidence if data else None,
            provider=data.provider if data else settings.provider_name,
            model=data.model if data else settings.model,
            reason=result.reason,
        )
    )


def process_paths(
    paths: Iterable[Path],
    provider: VisionProvider,
    settings: Settings,
    *,
    ledger: Ledger | None = None,
    run_id: str | None = None,
    on_result: Callable[[FileResult], None] | None = None,
    today: date_cls | None = None,
) -> RunSummary:
    """Process a batch of files, reserving planned names so dry runs stay collision-free."""
    run_id = run_id or new_run_id()
    reserved: set[Path] = set()
    results: list[FileResult] = []
    for path in paths:
        result = process_file(
            path,
            provider,
            settings,
            ledger=ledger,
            run_id=run_id,
            reserved=reserved,
            today=today,
        )
        results.append(result)
        if on_result is not None:
            on_result(result)
    return RunSummary(run_id=run_id, results=results, dry_run=settings.dry_run)


def process_directory(
    provider: VisionProvider,
    settings: Settings,
    *,
    ledger: Ledger | None = None,
    on_result: Callable[[FileResult], None] | None = None,
    today: date_cls | None = None,
) -> RunSummary:
    """Process every candidate file in ``settings.input_dir``."""
    return process_paths(
        list(iter_candidates(settings)),
        provider,
        settings,
        ledger=ledger,
        on_result=on_result,
        today=today,
    )

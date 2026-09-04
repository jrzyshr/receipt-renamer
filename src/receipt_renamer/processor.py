"""The per-file pipeline: discover, extract, name, and move (or plan) receipts."""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
import time
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

#: Consecutive provider failures tolerated before a batch gives up.
DEFAULT_MAX_CONSECUTIVE_FAILURES = 5

#: How many times to attempt a move before giving up on a file.
MOVE_ATTEMPTS = 3
#: Base delay between move attempts; doubles each retry.
MOVE_RETRY_DELAY = 0.25


class MoveError(OSError):
    """A file could not be moved after retries, e.g. it is locked by another process."""


@dataclass(slots=True)
class FileResult:
    """What happened (or would happen) to a single file."""

    original: Path
    outcome: Outcome
    destination: Path | None = None
    data: ReceiptData | None = None
    reason: str | None = None
    applied: bool = False
    provider_failure: bool = False

    @property
    def new_name(self) -> str:
        return self.destination.name if self.destination else "-"


@dataclass(slots=True)
class RunSummary:
    """Aggregate result of one run."""

    run_id: str
    results: list[FileResult]
    dry_run: bool
    aborted_reason: str | None = None

    @property
    def aborted(self) -> bool:
        return self.aborted_reason is not None

    @property
    def renamed(self) -> int:
        return sum(1 for r in self.results if r.outcome == "renamed")

    @property
    def needs_review(self) -> int:
        return sum(1 for r in self.results if r.outcome == "needs_review")

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.outcome == "skipped")


def _iter_files(settings: Settings) -> Iterator[Path]:
    """Yield every real input file, ignoring hidden files and our own output folders."""
    pattern = "**/*" if settings.recursive else "*"
    for path in sorted(settings.input_dir.glob(pattern)):
        if not path.is_file() or path.name.startswith("."):
            continue
        if settings.should_skip(path):
            continue
        yield path


def iter_candidates(settings: Settings) -> Iterator[Path]:
    """Yield files this tool can actually read."""
    for path in _iter_files(settings):
        if path.suffix.lower() in SUPPORTED_EXTENSIONS:
            yield path


def scan_input(settings: Settings) -> tuple[list[Path], list[Path]]:
    """Split the input folder into processable files and unreadable ones.

    Returning the rejects lets the CLI say up front how many files it is ignoring,
    rather than leaving the user to wonder why a count looks short.
    """
    candidates: list[Path] = []
    unsupported: list[Path] = []
    for path in _iter_files(settings):
        if path.suffix.lower() in SUPPORTED_EXTENSIONS:
            candidates.append(path)
        else:
            unsupported.append(path)
    return candidates, unsupported


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
    """Process one file end to end.

    Never raises for a single bad receipt: extraction failures route to Needs Review, and
    a file that cannot be moved (locked, read-only) is reported as skipped so the rest of
    the batch still runs.
    """
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
        return _to_needs_review(
            path,
            str(exc),
            settings,
            ledger,
            run_id,
            reserved,
            provider_failure=exc.provider_failure,
        )

    stem = build_stem(data.date, data.business, data.purpose)
    destination_dir = path.parent if settings.in_place else settings.output_dir
    destination = unique_path(
        destination_dir, stem, normalize_extension(path), reserved=reserved
    )
    reserved.add(destination)

    result = FileResult(original=path, outcome="renamed", destination=destination, data=data)
    if not settings.dry_run:
        if destination != path:
            try:
                result.destination = _move(path, destination)
            except MoveError as exc:
                # The file is still where it was, so report it and move on rather than
                # taking the whole batch down with it. Free the name it had claimed so
                # later files do not skip a number.
                reserved.discard(destination)
                result = FileResult(
                    original=path, outcome="skipped", data=data, reason=str(exc)
                )
                _record(ledger, run_id, result, settings)
                return result
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
    provider_failure: bool = False,
) -> FileResult:
    destination = unique_path(
        settings.needs_review_dir, path.stem, normalize_extension(path), reserved=reserved
    )
    reserved.add(destination)
    result = FileResult(
        original=path,
        outcome="needs_review",
        destination=destination,
        reason=reason,
        provider_failure=provider_failure,
    )
    if not settings.dry_run:
        try:
            result.destination = _move(path, destination)
        except MoveError as exc:
            # Keep the original diagnosis; it is why the file was headed for review.
            reserved.discard(destination)
            result = FileResult(
                original=path,
                outcome="skipped",
                reason=f"{reason}; {exc}",
                provider_failure=provider_failure,
            )
            _record(ledger, run_id, result, settings)
            return result
        result.applied = True
    _record(ledger, run_id, result, settings)
    return result


def _move(
    source: Path,
    destination: Path,
    *,
    attempts: int | None = None,
    delay: float | None = None,
) -> Path:
    """Move a file, never overwriting an existing one. Returns the final path.

    Transient failures are retried with a backoff: scanners, antivirus, and cloud sync
    clients all take brief exclusive locks, which is common on Windows and possible
    anywhere. A lock that outlives the retries raises :class:`MoveError` so the caller
    can skip one file instead of losing the rest of the batch.
    """
    # Read the module constants at call time so tests can shorten the backoff.
    attempts = MOVE_ATTEMPTS if attempts is None else attempts
    delay = MOVE_RETRY_DELAY if delay is None else delay

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination = unique_path(destination.parent, destination.stem, destination.suffix)

    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            os.replace(source, destination)
            return destination
        except OSError as exc:
            last_error = exc
            if exc.errno == errno.EXDEV:
                # Output folder is on another volume: copy+unlink instead.
                try:
                    shutil.move(str(source), str(destination))
                    return destination
                except OSError as move_exc:
                    last_error = move_exc
                    # A failed copy can leave a partial file behind; do not let it
                    # masquerade as a completed move.
                    if source.exists() and destination.exists():
                        with contextlib.suppress(OSError):
                            destination.unlink()
        if attempt < attempts - 1:
            time.sleep(delay * (2**attempt))

    raise MoveError(f"could not move file: {last_error}")


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
    on_file_start: Callable[[Path], None] | None = None,
    on_result: Callable[[FileResult], None] | None = None,
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    today: date_cls | None = None,
) -> RunSummary:
    """Process a batch of files, reserving planned names so dry runs stay collision-free.

    ``on_file_start`` fires before each (slow) extraction so callers can show which file
    is in flight; ``on_result`` fires once it finishes. If ``max_consecutive_failures``
    files in a row fail for provider reasons the batch stops early -- an expired token or
    a dead endpoint would otherwise quietly file every remaining receipt under Needs
    Review.
    """
    run_id = run_id or new_run_id()
    reserved: set[Path] = set()
    results: list[FileResult] = []
    consecutive_failures = 0
    aborted_reason: str | None = None
    for path in paths:
        if on_file_start is not None:
            on_file_start(path)
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

        if result.provider_failure:
            consecutive_failures += 1
        else:
            consecutive_failures = 0
        if max_consecutive_failures and consecutive_failures >= max_consecutive_failures:
            aborted_reason = (
                f"stopped after {consecutive_failures} consecutive provider failures: "
                f"{results[-1].reason}"
            )
            break
    return RunSummary(
        run_id=run_id,
        results=results,
        dry_run=settings.dry_run,
        aborted_reason=aborted_reason,
    )


def process_directory(
    provider: VisionProvider,
    settings: Settings,
    *,
    ledger: Ledger | None = None,
    on_file_start: Callable[[Path], None] | None = None,
    on_result: Callable[[FileResult], None] | None = None,
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    today: date_cls | None = None,
) -> RunSummary:
    """Process every candidate file in ``settings.input_dir``."""
    return process_paths(
        list(iter_candidates(settings)),
        provider,
        settings,
        ledger=ledger,
        on_file_start=on_file_start,
        on_result=on_result,
        max_consecutive_failures=max_consecutive_failures,
        today=today,
    )

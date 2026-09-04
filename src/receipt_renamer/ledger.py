"""Append-only JSONL ledger that makes every rename auditable and reversible."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

Action = Literal["renamed", "needs_review", "skipped", "failed", "undone"]

LEDGER_VERSION = 1


def new_run_id() -> str:
    """A sortable, unique identifier for one run."""
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class LedgerEntry:
    """One recorded action against one file."""

    run_id: str
    action: Action
    original_path: str
    new_path: str | None = None
    date: str | None = None
    business: str | None = None
    purpose: str | None = None
    confidence: float | None = None
    provider: str | None = None
    model: str | None = None
    reason: str | None = None
    timestamp: str = field(default_factory=_utc_now)
    version: int = LEDGER_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LedgerEntry:
        known = {f for f in cls.__slots__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


class Ledger:
    """Append-only JSONL log stored next to the receipts."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def append(self, entry: LedgerEntry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(entry.to_json() + "\n")

    def extend(self, entries: Iterable[LedgerEntry]) -> None:
        for entry in entries:
            self.append(entry)

    def read_all(self) -> list[LedgerEntry]:
        if not self.path.exists():
            return []
        entries: list[LedgerEntry] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue  # tolerate a partially written final line
                if isinstance(data, dict) and "run_id" in data:
                    entries.append(LedgerEntry.from_dict(data))
        return entries

    def run_ids(self) -> list[str]:
        """Run ids in first-seen order, excluding runs that were only undos."""
        seen: list[str] = []
        for entry in self.read_all():
            if entry.action == "undone":
                continue
            if entry.run_id not in seen:
                seen.append(entry.run_id)
        return seen

    def undone_run_ids(self) -> set[str]:
        return {
            entry.reason.removeprefix("undo of ")
            for entry in self.read_all()
            if entry.action == "undone" and entry.reason and entry.reason.startswith("undo of ")
        }

    def last_run_id(self, *, include_undone: bool = False) -> str | None:
        undone = set() if include_undone else self.undone_run_ids()
        for run_id in reversed(self.run_ids()):
            if run_id not in undone:
                return run_id
        return None

    def entries_for_run(self, run_id: str) -> list[LedgerEntry]:
        return [entry for entry in self.read_all() if entry.run_id == run_id]

    def moves_for_run(self, run_id: str) -> list[LedgerEntry]:
        """Entries that physically moved a file, newest last."""
        return [
            entry
            for entry in self.entries_for_run(run_id)
            if entry.action in ("renamed", "needs_review") and entry.new_path
        ]


@dataclass(frozen=True, slots=True)
class UndoResult:
    """Outcome of reversing one ledger entry."""

    original_path: Path
    new_path: Path
    status: Literal["restored", "missing", "blocked"]
    reason: str | None = None


def undo_run(
    ledger: Ledger,
    run_id: str | None = None,
    *,
    dry_run: bool = False,
) -> tuple[str | None, list[UndoResult]]:
    """Reverse the moves of a run, newest first. Never overwrites an existing file."""
    target = run_id or ledger.last_run_id()
    if target is None:
        return None, []

    results: list[UndoResult] = []
    undo_entries: list[LedgerEntry] = []
    for entry in reversed(ledger.moves_for_run(target)):
        current = Path(entry.new_path or "")
        original = Path(entry.original_path)
        if not current.exists():
            results.append(
                UndoResult(original, current, "missing", "file is no longer at its new path")
            )
            continue
        if original.exists():
            results.append(
                UndoResult(original, current, "blocked", "a file already exists at the original path")
            )
            continue
        if not dry_run:
            original.parent.mkdir(parents=True, exist_ok=True)
            os.replace(current, original)
            undo_entries.append(
                LedgerEntry(
                    run_id=target,
                    action="undone",
                    original_path=str(current),
                    new_path=str(original),
                    reason=f"undo of {target}",
                )
            )
        results.append(UndoResult(original, current, "restored"))

    if not dry_run and undo_entries:
        ledger.extend(undo_entries)
        _prune_empty_dirs(
            {Path(entry.original_path).parent for entry in undo_entries}, ledger.path.parent
        )
    return target, results


def _prune_empty_dirs(dirs: Iterable[Path], stop_at: Path) -> None:
    """Remove now-empty Renamed/Needs Review folders left behind by an undo."""
    for directory in dirs:
        try:
            if directory != stop_at and directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            pass


def iter_entries(path: Path | str) -> Iterator[LedgerEntry]:
    """Convenience iterator over a ledger file."""
    yield from Ledger(path).read_all()

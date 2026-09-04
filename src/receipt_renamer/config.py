"""Runtime settings shared by the CLI commands."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .images import DEFAULT_JPEG_QUALITY, DEFAULT_MAX_EDGE

DEFAULT_OUTPUT_DIRNAME = "Renamed"
NEEDS_REVIEW_DIRNAME = "Needs Review"
LEDGER_FILENAME = ".receipt-renamer-log.jsonl"
DEFAULT_MIN_CONFIDENCE = 0.7


def load_env(explicit: Path | None = None) -> None:
    """Load a ``.env`` file if python-dotenv is available.

    Existing environment variables always win, so an exported key is never clobbered.
    """
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:  # pragma: no cover - dotenv is a hard dependency
        return
    path = str(explicit) if explicit else find_dotenv(usecwd=True)
    if path:
        load_dotenv(path, override=False)


@dataclass(slots=True)
class Settings:
    """Everything a run/watch needs to know about where files go."""

    input_dir: Path
    output_dir: Path
    needs_review_dir: Path
    ledger_path: Path
    in_place: bool = False
    recursive: bool = False
    dry_run: bool = True
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    max_edge: int = DEFAULT_MAX_EDGE
    jpeg_quality: int = DEFAULT_JPEG_QUALITY
    provider_name: str = "openai"
    model: str | None = None
    skip_dirs: set[Path] = field(default_factory=set)

    @classmethod
    def create(
        cls,
        input_dir: Path,
        *,
        output_dir: Path | None = None,
        in_place: bool = False,
        recursive: bool = False,
        dry_run: bool = True,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        max_edge: int = DEFAULT_MAX_EDGE,
        jpeg_quality: int = DEFAULT_JPEG_QUALITY,
        provider_name: str = "openai",
        model: str | None = None,
        ledger_path: Path | None = None,
    ) -> Settings:
        input_dir = Path(input_dir).expanduser().resolve()
        if in_place:
            resolved_output = input_dir
        elif output_dir is not None:
            resolved_output = Path(output_dir).expanduser().resolve()
        else:
            resolved_output = input_dir / DEFAULT_OUTPUT_DIRNAME
        needs_review = resolved_output / NEEDS_REVIEW_DIRNAME
        ledger = (
            Path(ledger_path).expanduser().resolve()
            if ledger_path is not None
            else input_dir / LEDGER_FILENAME
        )
        return cls(
            input_dir=input_dir,
            output_dir=resolved_output,
            needs_review_dir=needs_review,
            ledger_path=ledger,
            in_place=in_place,
            recursive=recursive,
            dry_run=dry_run,
            min_confidence=min_confidence,
            max_edge=max_edge,
            jpeg_quality=jpeg_quality,
            provider_name=provider_name,
            model=model,
            skip_dirs={resolved_output, needs_review},
        )

    def should_skip(self, path: Path) -> bool:
        """True when ``path`` lives inside an output/needs-review folder."""
        if self.in_place:
            return self.needs_review_dir in path.parents
        return any(skip == path.parent or skip in path.parents for skip in self.skip_dirs)


def env_provider(default: str = "openai") -> str:
    return os.getenv("RECEIPT_RENAMER_PROVIDER", default)

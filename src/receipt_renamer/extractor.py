"""Turn an image file into validated receipt fields."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date as date_cls
from datetime import datetime, timedelta
from pathlib import Path

from .images import DEFAULT_JPEG_QUALITY, DEFAULT_MAX_EDGE, UnsupportedImageError, prepare_image
from .providers import ProviderError, VisionProvider

#: Receipts older than this (or dated in the future) are almost certainly misread.
MIN_YEAR = 1990
FUTURE_TOLERANCE = timedelta(days=2)


class ExtractionError(Exception):
    """Extraction produced no usable result; the file should go to Needs Review."""


@dataclass(frozen=True, slots=True)
class ReceiptData:
    """Validated fields for one receipt."""

    date: str
    business: str
    purpose: str
    confidence: float
    provider: str
    model: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def validate_date(value: str | None, *, today: date_cls | None = None) -> str:
    """Validate an ISO date string and return it normalised as ``YYYY-MM-DD``."""
    if not value:
        raise ExtractionError("no date found on receipt")
    text = str(value).strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ExtractionError(f"date {text!r} is not a valid YYYY-MM-DD date") from exc
    if parsed.year < MIN_YEAR:
        raise ExtractionError(f"date {text} is implausibly old")
    reference = today or date_cls.today()
    if parsed > reference + FUTURE_TOLERANCE:
        raise ExtractionError(f"date {text} is in the future")
    return parsed.isoformat()


def validate(raw, *, min_confidence: float, today: date_cls | None = None) -> ReceiptData:
    """Validate a :class:`~receipt_renamer.providers.base.RawExtraction`.

    Raises :class:`ExtractionError` when the result is unusable, so the caller can
    route the file to Needs Review instead of guessing.
    """
    missing = [
        field
        for field in ("date", "business", "purpose")
        if not getattr(raw, field, None)
    ]
    if missing:
        raise ExtractionError(f"model could not read: {', '.join(missing)}")
    if raw.confidence < min_confidence:
        raise ExtractionError(
            f"confidence {raw.confidence:.2f} is below the {min_confidence:.2f} threshold"
        )
    normalized_date = validate_date(raw.date, today=today)
    return ReceiptData(
        date=normalized_date,
        business=str(raw.business).strip(),
        purpose=str(raw.purpose).strip(),
        confidence=float(raw.confidence),
        provider=raw.provider,
        model=raw.model,
    )


def extract_receipt(
    path: Path | str,
    provider: VisionProvider,
    *,
    min_confidence: float = 0.7,
    max_edge: int = DEFAULT_MAX_EDGE,
    quality: int = DEFAULT_JPEG_QUALITY,
    today: date_cls | None = None,
) -> ReceiptData:
    """Prepare an image, send it to ``provider``, and validate the response."""
    try:
        prepared = prepare_image(path, max_edge=max_edge, quality=quality)
    except UnsupportedImageError as exc:
        raise ExtractionError(str(exc)) from exc

    try:
        raw = provider.extract(prepared.data, mime_type=prepared.mime_type)
    except ProviderError as exc:
        raise ExtractionError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - one bad file must not kill the batch
        raise ExtractionError(f"provider error: {exc}") from exc

    return validate(raw, min_confidence=min_confidence, today=today)

"""Provider-agnostic types, prompt, and JSON parsing for vision extraction."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

SYSTEM_PROMPT = (
    "You are a meticulous bookkeeping assistant that reads photographs and scans of "
    "paper receipts. You reply with strict JSON only."
)

USER_PROMPT = """\
Read this receipt image and extract the following fields.

Return STRICT JSON and nothing else - no prose, no markdown, no code fences:
{
  "date": "YYYY-MM-DD",
  "business": "the merchant/business name as printed",
  "purpose": "a short expense category",
  "confidence": 0.0
}

Rules:
- "date" is the transaction date printed on the receipt, in YYYY-MM-DD format.
- "business" is the merchant name only. Drop store numbers, addresses, slogans and
  legal suffixes unless they are part of the everyday name.
- "purpose" is a SHORT expense category inferred from the line items, ideally one of:
  Meals, Groceries, Fuel, Travel, Lodging, Office Supplies, Software, Hardware,
  Shipping, Utilities, Parking, Entertainment, Medical, Maintenance, Other.
- "confidence" is your overall confidence from 0.0 to 1.0 that every field is correct.
- If a field is not clearly legible, set it to null. DO NOT GUESS and do not invent a
  plausible value. A null field is far better than a wrong one.
- If the image is not a receipt at all, set date, business and purpose to null and
  confidence to 0.
"""


class ProviderError(RuntimeError):
    """Raised when a provider cannot produce a usable response."""


@dataclass(frozen=True, slots=True)
class RawExtraction:
    """The unvalidated result of one provider call."""

    date: str | None
    business: str | None
    purpose: str | None
    confidence: float
    provider: str
    model: str
    raw_response: str = ""


@runtime_checkable
class VisionProvider(Protocol):
    """Common interface every provider implements."""

    name: str
    model: str

    def extract(self, image_bytes: bytes, *, mime_type: str = "image/jpeg") -> RawExtraction:
        """Send one prepared image to the model and return the parsed fields."""


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def parse_json_response(text: str) -> dict[str, Any]:
    """Parse a model response into a dict, tolerating code fences and stray prose."""
    if not text or not text.strip():
        raise ProviderError("model returned an empty response")
    cleaned = _FENCE_RE.sub("", text.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise ProviderError(f"model response was not JSON: {text[:200]!r}") from None
        try:
            data = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ProviderError(f"model response was not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProviderError("model response JSON was not an object")
    return data


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "n/a", "unknown", "unreadable", ""}:
        return None
    return text


def _clean_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    if confidence != confidence:  # NaN
        return 0.0
    return max(0.0, min(1.0, confidence))


def build_extraction(data: dict[str, Any], *, provider: str, model: str, raw: str = "") -> RawExtraction:
    """Normalise a parsed JSON payload into a :class:`RawExtraction`."""
    return RawExtraction(
        date=_clean_str(data.get("date")),
        business=_clean_str(data.get("business")),
        purpose=_clean_str(data.get("purpose")),
        confidence=_clean_confidence(data.get("confidence")),
        provider=provider,
        model=model,
        raw_response=raw,
    )

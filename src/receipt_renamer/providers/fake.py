"""An offline provider used by the test suite and for demos.

It never touches the network. Responses are either scripted by the caller or derived
deterministically from the image bytes.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from .base import ProviderError, RawExtraction, build_extraction

DEFAULT_MODEL = "fake-vision-1"


class FakeProvider:
    """Returns canned extractions in order; repeats the last one when exhausted."""

    name = "fake"

    def __init__(
        self,
        responses: Iterable[dict[str, Any] | Exception] | None = None,
        model: str | None = None,
    ) -> None:
        self.model = model or DEFAULT_MODEL
        self._responses: list[dict[str, Any] | Exception] = list(responses or [])
        self._iter: Iterator[dict[str, Any] | Exception] = iter(self._responses)
        self.calls: list[tuple[int, str]] = []
        self._last: dict[str, Any] | Exception | None = None

    def extract(self, image_bytes: bytes, *, mime_type: str = "image/jpeg") -> RawExtraction:
        self.calls.append((len(image_bytes), mime_type))
        try:
            item: dict[str, Any] | Exception = next(self._iter)
            self._last = item
        except StopIteration:
            if self._last is None:
                item = _default_payload()
            else:
                item = self._last
        if isinstance(item, Exception):
            raise item
        return build_extraction(item, provider=self.name, model=self.model, raw=repr(item))


def _default_payload() -> dict[str, Any]:
    return {
        "date": "2024-01-15",
        "business": "Example Cafe",
        "purpose": "Meals",
        "confidence": 0.95,
    }


class AlwaysFailsProvider:
    """A provider that always raises - handy for exercising the Needs Review path."""

    name = "fake-failing"

    def __init__(self, message: str = "simulated provider failure", model: str = DEFAULT_MODEL) -> None:
        self.model = model
        self._message = message

    def extract(self, image_bytes: bytes, *, mime_type: str = "image/jpeg") -> RawExtraction:
        raise ProviderError(self._message)

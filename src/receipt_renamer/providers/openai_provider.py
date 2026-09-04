"""OpenAI vision provider (gpt-4o-mini by default)."""

from __future__ import annotations

import os

from ._openai_common import chat_extract
from .base import ProviderError, RawExtraction

DEFAULT_MODEL = "gpt-4o-mini"


class OpenAIProvider:
    """Vision extraction via the OpenAI Chat Completions API.

    The endpoint comes from the SDK: ``OPENAI_BASE_URL`` when set, otherwise
    ``https://api.openai.com/v1``. Pass ``base_url`` to target a compatible gateway.
    For Azure-hosted models use the ``azure`` provider instead.
    """

    name = "openai"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = 60.0,
        max_retries: int = 2,
    ) -> None:
        self.model = model or os.getenv("RECEIPT_RENAMER_MODEL") or DEFAULT_MODEL
        key = api_key or os.getenv("OPENAI_API_KEY")
        if not key:
            raise ProviderError(
                "OPENAI_API_KEY is not set. Export it or put it in a .env file."
            )
        try:  # imported lazily so the package works without the SDK installed
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise ProviderError(
                "The openai package is required for --provider openai. "
                "Install it with: pip install 'receipt-renamer[openai]'"
            ) from exc
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL")
        kwargs: dict[str, object] = {
            "api_key": key,
            "timeout": timeout,
            "max_retries": max_retries,
        }
        if self.base_url:
            kwargs["base_url"] = self.base_url
        self._client = OpenAI(**kwargs)  # type: ignore[arg-type]

    def extract(self, image_bytes: bytes, *, mime_type: str = "image/jpeg") -> RawExtraction:
        return chat_extract(
            self._client,
            model=self.model,
            provider_name=self.name,
            image_bytes=image_bytes,
            mime_type=mime_type,
            label="OpenAI",
        )

"""OpenAI vision provider (gpt-4o-mini by default)."""

from __future__ import annotations

import base64
import os

from .base import (
    SYSTEM_PROMPT,
    USER_PROMPT,
    ProviderError,
    RawExtraction,
    build_extraction,
    parse_json_response,
)

DEFAULT_MODEL = "gpt-4o-mini"


class OpenAIProvider:
    """Vision extraction via the OpenAI Chat Completions API."""

    name = "openai"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        *,
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
        self._client = OpenAI(api_key=key, timeout=timeout, max_retries=max_retries)

    def extract(self, image_bytes: bytes, *, mime_type: str = "image/jpeg") -> RawExtraction:
        data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": USER_PROMPT},
                            {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
                        ],
                    },
                ],
            )
        except Exception as exc:  # noqa: BLE001 - surface any SDK/transport failure uniformly
            raise ProviderError(f"OpenAI request failed: {exc}") from exc

        text = (response.choices[0].message.content or "") if response.choices else ""
        return build_extraction(
            parse_json_response(text), provider=self.name, model=self.model, raw=text
        )

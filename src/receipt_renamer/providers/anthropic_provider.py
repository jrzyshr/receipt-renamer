"""Anthropic vision provider (Claude)."""

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

DEFAULT_MODEL = "claude-3-5-sonnet-latest"
SUPPORTED_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})


class AnthropicProvider:
    """Vision extraction via the Anthropic Messages API."""

    name = "anthropic"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float = 60.0,
        max_retries: int = 2,
        max_tokens: int = 512,
    ) -> None:
        self.model = model or os.getenv("RECEIPT_RENAMER_MODEL") or DEFAULT_MODEL
        self.max_tokens = max_tokens
        key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise ProviderError(
                "ANTHROPIC_API_KEY is not set. Export it or put it in a .env file."
            )
        try:  # imported lazily so the package works without the SDK installed
            from anthropic import Anthropic
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise ProviderError(
                "The anthropic package is required for --provider anthropic. "
                "Install it with: pip install 'receipt-renamer[anthropic]'"
            ) from exc
        self._client = Anthropic(api_key=key, timeout=timeout, max_retries=max_retries)

    def extract(self, image_bytes: bytes, *, mime_type: str = "image/jpeg") -> RawExtraction:
        if mime_type not in SUPPORTED_MIME_TYPES:
            mime_type = "image/jpeg"
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=0,
                system=SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": mime_type,
                                    "data": base64.b64encode(image_bytes).decode("ascii"),
                                },
                            },
                            {"type": "text", "text": USER_PROMPT},
                        ],
                    },
                    # Prefill the assistant turn so Claude starts mid-JSON.
                    {"role": "assistant", "content": "{"},
                ],
            )
        except Exception as exc:  # noqa: BLE001 - surface any SDK/transport failure uniformly
            raise ProviderError(f"Anthropic request failed: {exc}") from exc

        parts = [block.text for block in response.content if getattr(block, "type", "") == "text"]
        text = "{" + "".join(parts)
        return build_extraction(
            parse_json_response(text), provider=self.name, model=self.model, raw=text
        )

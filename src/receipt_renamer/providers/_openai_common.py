"""Shared request/response plumbing for OpenAI-compatible chat completions.

Both the public OpenAI provider and the Azure OpenAI provider speak the same
Chat Completions wire format; only client construction differs.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any

from .base import (
    SYSTEM_PROMPT,
    USER_PROMPT,
    ProviderError,
    RawExtraction,
    build_extraction,
    parse_json_response,
)


def build_messages(image_bytes: bytes, mime_type: str) -> list[dict[str, Any]]:
    """Build the two-message vision prompt with the image inlined as a data URL."""
    data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": USER_PROMPT},
                {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
            ],
        },
    ]


def chat_extract(
    client: Any,
    *,
    model: str,
    provider_name: str,
    image_bytes: bytes,
    mime_type: str,
    label: str,
    hint: Callable[[Exception], str | None] | None = None,
) -> RawExtraction:
    """Send one image through a chat-completions client and parse the JSON reply.

    ``label`` is used in error messages so the user can tell OpenAI and Azure apart.
    ``hint`` may turn a specific SDK error into actionable advice.
    """
    try:
        response = client.chat.completions.create(
            model=model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=build_messages(image_bytes, mime_type),
        )
    except Exception as exc:  # noqa: BLE001 - surface any SDK/transport failure uniformly
        message = f"{label} request failed: {exc}"
        extra = hint(exc) if hint is not None else None
        raise ProviderError(f"{message}\n{extra}" if extra else message) from exc

    text = (response.choices[0].message.content or "") if response.choices else ""
    return build_extraction(
        parse_json_response(text), provider=provider_name, model=model, raw=text
    )

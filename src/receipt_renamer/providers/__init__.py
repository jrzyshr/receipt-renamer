"""Provider registry and factory."""

from __future__ import annotations

import os

from .base import (
    ProviderError,
    RawExtraction,
    VisionProvider,
    build_extraction,
    parse_json_response,
)

PROVIDER_NAMES = ("openai", "azure", "anthropic", "fake")
DEFAULT_PROVIDER = "openai"

__all__ = [
    "DEFAULT_PROVIDER",
    "PROVIDER_NAMES",
    "ProviderError",
    "RawExtraction",
    "VisionProvider",
    "build_extraction",
    "get_provider",
    "parse_json_response",
    "resolve_provider_name",
]


def resolve_provider_name(name: str | None = None) -> str:
    """Resolve the provider from an explicit flag, the environment, or the default."""
    resolved = (name or os.getenv("RECEIPT_RENAMER_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    if resolved not in PROVIDER_NAMES:
        raise ProviderError(
            f"Unknown provider {resolved!r}. Choose one of: {', '.join(PROVIDER_NAMES)}."
        )
    return resolved


def get_provider(name: str | None = None, model: str | None = None) -> VisionProvider:
    """Instantiate a provider by name. SDKs are imported lazily inside each provider."""
    resolved = resolve_provider_name(name)
    if resolved == "openai":
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(model=model)
    if resolved == "azure":
        from .azure_provider import AzureOpenAIProvider

        return AzureOpenAIProvider(model=model)
    if resolved == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(model=model)
    from .fake import FakeProvider

    return FakeProvider(model=model)

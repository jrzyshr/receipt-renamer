"""Azure OpenAI vision provider.

Azure differs from the public OpenAI API in three ways that matter here:

* the endpoint is your own resource, e.g. ``https://my-resource.openai.azure.com``
* ``model`` is the **deployment name** you chose in the portal, not ``gpt-4o-mini``
* every request is pinned to an ``api-version``

Authentication is either an API key or Microsoft Entra ID (``azure-identity``).
A key wins when both are available.
"""

from __future__ import annotations

import os
import re

from ._openai_common import chat_extract
from .base import ProviderError, RawExtraction

#: GA api-version that supports vision input and JSON mode.
DEFAULT_API_VERSION = "2024-10-21"

#: Scope requested when authenticating with Microsoft Entra ID.
ENTRA_SCOPE = "https://cognitiveservices.azure.com/.default"

_DEPLOYMENT_PATH_RE = re.compile(r"/openai(/deployments/[^/]+)?/?$", re.IGNORECASE)


def normalize_endpoint(endpoint: str) -> str:
    """Reduce a pasted Azure URL to the resource endpoint the SDK expects.

    Accepts values copied from the portal or from a full request URL, e.g.
    ``https://r.openai.azure.com/openai/deployments/gpt-4o/chat/completions?api-version=…``
    """
    cleaned = endpoint.strip().split("?", 1)[0].rstrip("/")
    cleaned = re.sub(r"/chat/completions$", "", cleaned, flags=re.IGNORECASE)
    cleaned = _DEPLOYMENT_PATH_RE.sub("", cleaned)
    return cleaned.rstrip("/")


class AzureOpenAIProvider:
    """Vision extraction via an Azure OpenAI deployment."""

    name = "azure"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        *,
        endpoint: str | None = None,
        api_version: str | None = None,
        use_entra_id: bool | None = None,
        timeout: float = 60.0,
        max_retries: int = 2,
    ) -> None:
        raw_endpoint = endpoint or os.getenv("AZURE_OPENAI_ENDPOINT")
        if not raw_endpoint:
            raise ProviderError(
                "AZURE_OPENAI_ENDPOINT is not set. It looks like "
                "https://<your-resource>.openai.azure.com"
            )
        self.endpoint = normalize_endpoint(raw_endpoint)

        deployment = (
            model
            or os.getenv("AZURE_OPENAI_DEPLOYMENT")
            or os.getenv("RECEIPT_RENAMER_MODEL")
        )
        if not deployment:
            raise ProviderError(
                "No Azure deployment specified. Set AZURE_OPENAI_DEPLOYMENT to the "
                "deployment name from the Azure portal, or pass --model <deployment>."
            )
        # For Azure the "model" is the deployment name.
        self.model = deployment
        self.api_version = (
            api_version or os.getenv("AZURE_OPENAI_API_VERSION") or DEFAULT_API_VERSION
        )

        try:  # imported lazily so the package works without the SDK installed
            from openai import AzureOpenAI
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise ProviderError(
                "The openai package is required for --provider azure. "
                "Install it with: pip install 'receipt-renamer[azure]'"
            ) from exc

        key = api_key or os.getenv("AZURE_OPENAI_API_KEY")
        wants_entra = use_entra_id if use_entra_id is not None else not key
        common: dict[str, object] = {
            "azure_endpoint": self.endpoint,
            "azure_deployment": self.model,
            "api_version": self.api_version,
            "timeout": timeout,
            "max_retries": max_retries,
        }

        if wants_entra:
            self.auth = "entra-id"
            common["azure_ad_token_provider"] = _entra_token_provider()
        else:
            self.auth = "api-key"
            common["api_key"] = key

        self._client = AzureOpenAI(**common)  # type: ignore[arg-type]

    def extract(self, image_bytes: bytes, *, mime_type: str = "image/jpeg") -> RawExtraction:
        return chat_extract(
            self._client,
            model=self.model,
            provider_name=self.name,
            image_bytes=image_bytes,
            mime_type=mime_type,
            label="Azure OpenAI",
        )


def _entra_token_provider():
    """Build a bearer-token provider using the ambient Azure credential chain."""
    try:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    except ImportError as exc:
        raise ProviderError(
            "No AZURE_OPENAI_API_KEY set, and Microsoft Entra ID auth requires "
            "azure-identity. Either set AZURE_OPENAI_API_KEY, or install it with: "
            "pip install 'receipt-renamer[azure-entra]' (then run 'az login')."
        ) from exc
    try:
        return get_bearer_token_provider(DefaultAzureCredential(), ENTRA_SCOPE)
    except Exception as exc:  # noqa: BLE001 - credential chain failures vary
        raise ProviderError(f"Could not acquire an Entra ID credential: {exc}") from exc

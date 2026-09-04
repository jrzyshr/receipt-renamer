"""Azure OpenAI vision provider.

Azure differs from the public OpenAI API in three ways that matter here:

* the endpoint is your own resource, e.g. ``https://my-resource.openai.azure.com``
* ``model`` is the **deployment name** you chose in the portal, not ``gpt-4o-mini``
* every request is pinned to an ``api-version``

Authentication is either an API key or Microsoft Entra ID (``azure-identity``).
A key wins when both are available.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
from collections.abc import Iterator

from ._openai_common import chat_extract
from .base import ProviderError, RawExtraction

#: GA api-version that supports vision input and JSON mode.
DEFAULT_API_VERSION = "2024-10-21"

#: Scope requested when authenticating with Microsoft Entra ID.
ENTRA_SCOPE = "https://cognitiveservices.azure.com/.default"

# Trailing API path that AzureOpenAI appends itself. Portal and docs hand out several
# shapes -- ".../openai", ".../openai/v1" (the newer v1 surface), and full request
# URLs -- all of which must reduce to the bare resource root or the SDK doubles the
# path and every call 404s.
_DEPLOYMENT_PATH_RE = re.compile(
    r"/openai(?:/v\d+)?(?:/deployments/[^/]+)?/?$", re.IGNORECASE
)


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
            hint=self._hint,
        )

    def _hint(self, exc: Exception) -> str | None:
        """Turn Azure's opaque status codes into something you can act on."""
        text = str(exc)
        if "404" in text or "Resource not found" in text:
            return (
                "  A 404 means the request URL is wrong, not that your receipt is bad. Check:\n"
                f"    - Deployment name (--model / AZURE_OPENAI_DEPLOYMENT) is {self.model!r}; "
                "it must match the deployment name in Azure AI Studio, which is not "
                "necessarily the model name.\n"
                f"    - Endpoint resolved to {self.endpoint} — it should be the resource root.\n"
                f"    - api-version {self.api_version} is supported by that deployment."
            )
        if "401" in text or "PermissionDenied" in text or "403" in text:
            return (
                "  Authentication was rejected. With Entra ID you need the "
                "'Cognitive Services OpenAI User' role on this resource; with a key, check "
                "AZURE_OPENAI_API_KEY belongs to this endpoint."
            )
        if "429" in text:
            return (
                "  Rate limited. Your deployment's tokens-per-minute quota is too low for a "
                "batch this size; raise the quota or run a smaller folder."
            )
        return None


def _entra_token_provider():
    """Build a bearer-token provider using the ambient Azure credential chain.

    The token is acquired eagerly so an auth problem surfaces at startup rather than
    part-way through a batch of receipts.
    """
    try:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    except ImportError as exc:
        raise ProviderError(
            "No AZURE_OPENAI_API_KEY set, and Microsoft Entra ID auth requires "
            "azure-identity. Either set AZURE_OPENAI_API_KEY, or install it with: "
            "pip install 'receipt-renamer[azure-entra]' (then run 'az login')."
        ) from exc

    try:
        credential = DefaultAzureCredential()
        provider = get_bearer_token_provider(credential, ENTRA_SCOPE)
        # azure-identity logs the full credential-chain dump at WARNING; we summarise
        # it ourselves, so keep the probe quiet and let our message stand.
        with _quiet_logger("azure.identity"):
            provider()  # fail fast instead of at the first receipt
    except Exception as exc:  # noqa: BLE001 - the credential chain raises many types
        raise ProviderError(
            f"Could not acquire a Microsoft Entra ID token for {ENTRA_SCOPE}.\n"
            f"  {_summarize_credential_error(exc)}\n"
            "\nFix one of the following:\n"
            f'  1. Re-authenticate for this audience:  az login --scope "{ENTRA_SCOPE}"\n'
            "  2. Ensure you hold the 'Cognitive Services OpenAI User' role on the "
            "Azure OpenAI resource.\n"
            "  3. Or fall back to key auth by setting AZURE_OPENAI_API_KEY."
        ) from exc
    return provider


@contextlib.contextmanager
def _quiet_logger(name: str, level: int = logging.ERROR) -> Iterator[None]:
    """Temporarily raise a logger's threshold, restoring it afterwards."""
    logger = logging.getLogger(name)
    previous = logger.level
    logger.setLevel(level)
    try:
        yield
    finally:
        logger.setLevel(previous)


def _summarize_credential_error(exc: Exception, *, limit: int = 240) -> str:
    """Condense DefaultAzureCredential's multi-credential dump to something readable."""
    text = " ".join(str(exc).split())
    if not text:
        return exc.__class__.__name__
    # Prefer the AAD error code when present; it is the part worth searching for.
    for token in text.split():
        if token.startswith("AADSTS"):
            code = token.rstrip(":.,")
            head = text[: limit // 2].rstrip()
            return f"{code} - {head}..." if len(text) > limit // 2 else f"{code} - {text}"
    return text if len(text) <= limit else text[:limit].rstrip() + "..."

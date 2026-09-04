"""Azure OpenAI provider: endpoint normalization, auth selection, request shape.

The Azure SDK client is stubbed out, so nothing here touches the network.
"""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest

from receipt_renamer.providers import get_provider, resolve_provider_name
from receipt_renamer.providers.azure_provider import (
    DEFAULT_API_VERSION,
    AzureOpenAIProvider,
    normalize_endpoint,
)
from receipt_renamer.providers.base import ProviderError

AZURE_ENV = (
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_DEPLOYMENT",
    "AZURE_OPENAI_API_VERSION",
    "RECEIPT_RENAMER_MODEL",
    "RECEIPT_RENAMER_PROVIDER",
)


@pytest.fixture(autouse=True)
def clean_azure_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in AZURE_ENV:
        monkeypatch.delenv(name, raising=False)


class _StubCompletions:
    def __init__(self, owner: "_StubClient") -> None:
        self._owner = owner

    def create(self, **kwargs: Any):
        self._owner.requests.append(kwargs)
        payload = json.dumps(
            {
                "date": "2024-03-14",
                "business": "Blue Bottle Coffee",
                "purpose": "Meals",
                "confidence": 0.91,
            }
        )
        message = types.SimpleNamespace(content=payload)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


class _StubClient:
    """Stands in for openai.AzureOpenAI and records how it was constructed."""

    instances: list["_StubClient"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs
        self.requests: list[dict[str, Any]] = []
        self.chat = types.SimpleNamespace(completions=_StubCompletions(self))
        _StubClient.instances.append(self)


@pytest.fixture
def stub_azure(monkeypatch: pytest.MonkeyPatch):
    """Install a fake ``openai`` module exposing AzureOpenAI."""
    _StubClient.instances = []
    module = sys.modules.get("openai")
    if module is None:
        module = types.ModuleType("openai")
        monkeypatch.setitem(sys.modules, "openai", module)
    monkeypatch.setattr(module, "AzureOpenAI", _StubClient, raising=False)
    return _StubClient


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://r.openai.azure.com", "https://r.openai.azure.com"),
        ("https://r.openai.azure.com/", "https://r.openai.azure.com"),
        ("  https://r.openai.azure.com/openai  ", "https://r.openai.azure.com"),
        ("https://r.openai.azure.com/openai/deployments/gpt-4o", "https://r.openai.azure.com"),
        (
            "https://r.openai.azure.com/openai/deployments/gpt-4o/chat/completions"
            "?api-version=2024-10-21",
            "https://r.openai.azure.com",
        ),
    ],
)
def test_endpoint_normalization(raw: str, expected: str) -> None:
    assert normalize_endpoint(raw) == expected


def test_missing_endpoint_is_a_clear_error(stub_azure) -> None:
    with pytest.raises(ProviderError, match="AZURE_OPENAI_ENDPOINT"):
        AzureOpenAIProvider(model="my-deployment")


def test_missing_deployment_is_a_clear_error(stub_azure, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://r.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    with pytest.raises(ProviderError, match="deployment"):
        AzureOpenAIProvider()


def test_api_key_auth_is_preferred(stub_azure, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://r.openai.azure.com/")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "secret-key")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "receipts-4o-mini")

    provider = AzureOpenAIProvider()
    assert provider.auth == "api-key"
    assert provider.model == "receipts-4o-mini"
    assert provider.api_version == DEFAULT_API_VERSION

    kwargs = stub_azure.instances[-1].init_kwargs
    assert kwargs["api_key"] == "secret-key"
    assert kwargs["azure_endpoint"] == "https://r.openai.azure.com"
    assert kwargs["azure_deployment"] == "receipts-4o-mini"
    assert kwargs["api_version"] == DEFAULT_API_VERSION
    assert "azure_ad_token_provider" not in kwargs


def test_model_flag_overrides_the_deployment_env(stub_azure, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://r.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "from-env")

    assert AzureOpenAIProvider(model="from-flag").model == "from-flag"


def test_custom_api_version_is_honoured(stub_azure, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://r.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "d")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")

    assert AzureOpenAIProvider().api_version == "2025-01-01-preview"


def test_entra_id_is_used_when_no_key_is_present(
    stub_azure, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://r.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "d")
    sentinel = object()
    monkeypatch.setattr(
        "receipt_renamer.providers.azure_provider._entra_token_provider", lambda: sentinel
    )

    provider = AzureOpenAIProvider()
    assert provider.auth == "entra-id"
    kwargs = stub_azure.instances[-1].init_kwargs
    assert kwargs["azure_ad_token_provider"] is sentinel
    assert "api_key" not in kwargs


def test_missing_azure_identity_gives_actionable_error(
    stub_azure, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://r.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "d")
    monkeypatch.setitem(sys.modules, "azure.identity", None)

    with pytest.raises(ProviderError, match="azure-identity"):
        AzureOpenAIProvider()


def test_extract_sends_the_deployment_and_a_vision_message(
    stub_azure, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://r.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "receipts-4o")

    provider = AzureOpenAIProvider()
    result = provider.extract(b"jpeg-bytes", mime_type="image/jpeg")

    assert result.business == "Blue Bottle Coffee"
    assert result.provider == "azure"
    assert result.model == "receipts-4o"

    request = stub_azure.instances[-1].requests[-1]
    assert request["model"] == "receipts-4o"
    assert request["temperature"] == 0
    assert request["response_format"] == {"type": "json_object"}
    image_part = request["messages"][1]["content"][1]
    assert image_part["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_transport_errors_become_provider_errors(
    stub_azure, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://r.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "d")
    provider = AzureOpenAIProvider()

    def boom(**kwargs: Any):
        raise RuntimeError("404 DeploymentNotFound")

    monkeypatch.setattr(stub_azure.instances[-1].chat.completions, "create", boom)
    with pytest.raises(ProviderError, match="Azure OpenAI request failed"):
        provider.extract(b"x")


def test_registry_exposes_azure(stub_azure, monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_provider_name("azure") == "azure"
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://r.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    provider = get_provider("azure", "my-deployment")
    assert provider.name == "azure"
    assert provider.model == "my-deployment"

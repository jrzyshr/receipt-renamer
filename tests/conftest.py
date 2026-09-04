"""Shared pytest fixtures. No test in this suite makes a network call."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from receipt_renamer.config import Settings
from receipt_renamer.providers.fake import FakeProvider


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard-fail any test that tries to open a socket."""
    import socket

    def _blocked(*args: object, **kwargs: object):
        raise AssertionError("tests must not make network calls")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


def make_image(path: Path, *, size: tuple[int, int] = (600, 900), color: str = "white") -> Path:
    """Write a small JPEG standing in for a scanned receipt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="JPEG", quality=70)
    return path


@pytest.fixture
def image_factory():
    return make_image


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    folder = tmp_path / "Inbox"
    folder.mkdir()
    return folder


@pytest.fixture
def settings_factory(inbox: Path):
    def _make(**overrides) -> Settings:
        params: dict[str, object] = {"dry_run": False, "provider_name": "fake"}
        params.update(overrides)
        return Settings.create(inbox, **params)  # type: ignore[arg-type]

    return _make


@pytest.fixture
def good_payload() -> dict[str, object]:
    return {
        "date": "2024-03-14",
        "business": "Blue Bottle Coffee",
        "purpose": "Meals",
        "confidence": 0.95,
    }


@pytest.fixture
def provider_factory():
    def _make(*responses) -> FakeProvider:
        return FakeProvider(responses)

    return _make

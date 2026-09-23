"""CLI wiring and provider selection, all offline via the fake provider."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from receipt_renamer.cli import app
from receipt_renamer.config import env_name_format
from receipt_renamer.naming import NameFormat
from receipt_renamer.providers import ProviderError, resolve_provider_name

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "receipt-renamer" in result.stdout


def test_run_dry_run_is_the_default(inbox: Path, image_factory) -> None:
    source = image_factory(inbox / "scan_0001.jpg")
    result = runner.invoke(app, ["run", "--input", str(inbox), "--provider", "fake"])

    assert result.exit_code == 0, result.stdout
    assert "dry run" in result.stdout.lower()
    assert source.exists()
    assert not (inbox / "Renamed").exists()


def test_run_apply_moves_files_and_undo_restores(inbox: Path, image_factory) -> None:
    image_factory(inbox / "scan_0001.jpg")
    applied = runner.invoke(app, ["run", "--input", str(inbox), "--provider", "fake", "--apply"])
    assert applied.exit_code == 0, applied.stdout

    renamed = list((inbox / "Renamed").glob("*.jpg"))
    assert len(renamed) == 1
    assert renamed[0].name.startswith("2024-01-15 Example Cafe")

    undone = runner.invoke(app, ["undo", "--input", str(inbox)])
    assert undone.exit_code == 0, undone.stdout
    assert (inbox / "scan_0001.jpg").exists()


def test_run_rejects_missing_input(tmp_path: Path) -> None:
    result = runner.invoke(app, ["run", "--input", str(tmp_path / "nope"), "--provider", "fake"])
    assert result.exit_code == 2


def test_run_rejects_in_place_with_output(inbox: Path, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["run", "--input", str(inbox), "--provider", "fake", "--in-place", "--output", str(tmp_path)],
    )
    assert result.exit_code == 2


def test_run_rejects_out_of_range_confidence(inbox: Path) -> None:
    result = runner.invoke(
        app, ["run", "--input", str(inbox), "--provider", "fake", "--min-confidence", "1.5"]
    )
    assert result.exit_code == 2


def test_undo_without_ledger_fails_cleanly(inbox: Path) -> None:
    result = runner.invoke(app, ["undo", "--input", str(inbox)])
    assert result.exit_code == 1


def test_provider_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RECEIPT_RENAMER_PROVIDER", raising=False)
    assert resolve_provider_name(None) == "openai"
    assert resolve_provider_name("Anthropic") == "anthropic"
    monkeypatch.setenv("RECEIPT_RENAMER_PROVIDER", "fake")
    assert resolve_provider_name(None) == "fake"
    with pytest.raises(ProviderError):
        resolve_provider_name("gemini")


def test_missing_api_key_is_a_clean_error(inbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("receipt_renamer.cli.load_env", lambda *a, **k: None)
    result = runner.invoke(app, ["run", "--input", str(inbox), "--provider", "openai"])
    assert result.exit_code == 2
    assert "OPENAI_API_KEY" in result.stdout + str(result.stderr or "")


def test_run_honours_the_business_first_name_format(inbox: Path, image_factory) -> None:
    image_factory(inbox / "scan_0001.jpg")
    result = runner.invoke(
        app,
        ["run", "--input", str(inbox), "--provider", "fake", "--apply",
         "--name-format", "business-first"],
    )
    assert result.exit_code == 0, result.stdout

    renamed = list((inbox / "Renamed").glob("*.jpg"))
    assert [p.name for p in renamed] == ["Example Cafe - Meals - 01-15-2024.jpg"]


def test_name_format_defaults_to_the_env_var(
    inbox: Path, image_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_factory(inbox / "scan_0001.jpg")
    monkeypatch.setenv("RECEIPT_RENAMER_NAME_FORMAT", "business-first")
    result = runner.invoke(app, ["run", "--input", str(inbox), "--provider", "fake", "--apply"])
    assert result.exit_code == 0, result.stdout

    renamed = list((inbox / "Renamed").glob("*.jpg"))
    assert [p.name for p in renamed] == ["Example Cafe - Meals - 01-15-2024.jpg"]


def test_name_format_flag_beats_the_env_var(
    inbox: Path, image_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_factory(inbox / "scan_0001.jpg")
    monkeypatch.setenv("RECEIPT_RENAMER_NAME_FORMAT", "business-first")
    result = runner.invoke(
        app,
        ["run", "--input", str(inbox), "--provider", "fake", "--apply",
         "--name-format", "date-first"],
    )
    assert result.exit_code == 0, result.stdout

    renamed = list((inbox / "Renamed").glob("*.jpg"))
    assert [p.name for p in renamed] == ["2024-01-15 Example Cafe - Meals.jpg"]


def test_invalid_env_name_format_is_a_clean_error(
    inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECEIPT_RENAMER_NAME_FORMAT", "yyyy-mm-dd")
    result = runner.invoke(app, ["run", "--input", str(inbox), "--provider", "fake"])
    assert result.exit_code == 2
    assert "RECEIPT_RENAMER_NAME_FORMAT" in result.stdout + str(result.stderr or "")


def test_invalid_name_format_flag_is_rejected(inbox: Path) -> None:
    result = runner.invoke(
        app, ["run", "--input", str(inbox), "--provider", "fake", "--name-format", "nope"]
    )
    assert result.exit_code == 2


def test_env_name_format_ignores_a_blank_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECEIPT_RENAMER_NAME_FORMAT", "   ")
    assert env_name_format() is NameFormat.DATE_FIRST
    monkeypatch.delenv("RECEIPT_RENAMER_NAME_FORMAT")
    assert env_name_format() is NameFormat.DATE_FIRST

"""Filename sanitization, title-casing, stem building and collision handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from receipt_renamer.naming import (
    MAX_BUSINESS_LEN,
    build_stem,
    clean_business,
    clean_purpose,
    normalize_extension,
    sanitize_component,
    unique_path,
)


@pytest.mark.parametrize("char", list('/\\:*?"<>|'))
def test_illegal_characters_are_removed(char: str) -> None:
    result = sanitize_component(f"Acme{char}Corp")
    assert char not in result
    assert result == "Acme Corp"


def test_control_characters_and_whitespace_are_collapsed() -> None:
    assert sanitize_component("  Blue\tBottle\n\x07 Coffee  ") == "Blue Bottle Coffee"


def test_leading_and_trailing_punctuation_is_trimmed() -> None:
    assert sanitize_component("..hidden-name-") == "hidden-name"


def test_component_is_truncated_at_a_word_boundary() -> None:
    long_name = "Northwest Regional Office Supply and Stationery Emporium Limited"
    cleaned = sanitize_component(long_name, max_length=MAX_BUSINESS_LEN)
    assert len(cleaned) <= MAX_BUSINESS_LEN
    assert not cleaned.endswith(" ")
    assert long_name.startswith(cleaned)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("blue bottle coffee", "Blue Bottle Coffee"),
        ("THE HOME DEPOT #4501", "The Home Depot #4501"),
        ("IBM", "IBM"),
        ("IBM AND THE DOJ", "IBM and the DOJ"),
        ("McDonalds", "McDonalds"),
        ("o'brien's pub", "O'Brien's Pub"),
        ("jean-luc bistro", "Jean-Luc Bistro"),
        ("the ritz", "The Ritz"),
    ],
)
def test_business_title_casing(raw: str, expected: str) -> None:
    assert clean_business(raw) == expected


def test_purpose_is_cleaned_and_capped() -> None:
    assert clean_purpose("office supplies") == "Office Supplies"
    assert len(clean_purpose("x" * 100)) <= 32


def test_build_stem_format() -> None:
    assert (
        build_stem("2024-03-14", "blue bottle coffee", "meals")
        == "2024-03-14 Blue Bottle Coffee - Meals"
    )


def test_build_stem_sanitizes_embedded_separators() -> None:
    stem = build_stem("2024-03-14", 'ACME/Coffee "Co"', "Meals")
    assert stem == "2024-03-14 ACME Coffee Co - Meals"
    assert not any(c in stem for c in '/\\:*?"<>|')


def test_build_stem_never_returns_empty() -> None:
    assert build_stem("", "", "") == "receipt"


def test_build_stem_avoids_reserved_device_names() -> None:
    assert build_stem("", "con", "") != "Con"


def test_normalize_extension() -> None:
    assert normalize_extension("scan.JPG") == ".jpg"
    assert normalize_extension("scan.PNG") == ".png"
    assert normalize_extension("scan") == ".jpg"


def test_unique_path_appends_counter_on_collision(tmp_path: Path) -> None:
    (tmp_path / "2024-03-14 Cafe - Meals.jpg").touch()
    (tmp_path / "2024-03-14 Cafe - Meals (2).jpg").touch()
    result = unique_path(tmp_path, "2024-03-14 Cafe - Meals", ".jpg")
    assert result.name == "2024-03-14 Cafe - Meals (3).jpg"


def test_unique_path_respects_reserved_names(tmp_path: Path) -> None:
    reserved = {tmp_path / "Receipt.jpg"}
    result = unique_path(tmp_path, "Receipt", ".jpg", reserved=reserved)
    assert result.name == "Receipt (2).jpg"


def test_unique_path_returns_plain_name_when_free(tmp_path: Path) -> None:
    assert unique_path(tmp_path, "Receipt", ".jpg").name == "Receipt.jpg"

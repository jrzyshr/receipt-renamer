"""Date validation, field validation and confidence gating."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from receipt_renamer.extractor import ExtractionError, validate, validate_date
from receipt_renamer.providers.base import build_extraction, parse_json_response

TODAY = date(2024, 6, 1)


def make_raw(**overrides):
    payload = {
        "date": "2024-03-14",
        "business": "Blue Bottle Coffee",
        "purpose": "Meals",
        "confidence": 0.9,
    }
    payload.update(overrides)
    return build_extraction(payload, provider="fake", model="fake-vision-1")


def test_valid_date_passes_through() -> None:
    assert validate_date("2024-03-14", today=TODAY) == "2024-03-14"


@pytest.mark.parametrize("value", ["03/14/2024", "2024-13-01", "2024-02-30", "March 14 2024", "", None])
def test_invalid_dates_are_rejected(value) -> None:
    with pytest.raises(ExtractionError):
        validate_date(value, today=TODAY)


def test_future_dates_are_rejected() -> None:
    future = (TODAY + timedelta(days=10)).isoformat()
    with pytest.raises(ExtractionError, match="future"):
        validate_date(future, today=TODAY)


def test_small_clock_skew_is_tolerated() -> None:
    tomorrow = (TODAY + timedelta(days=1)).isoformat()
    assert validate_date(tomorrow, today=TODAY) == tomorrow


def test_implausibly_old_dates_are_rejected() -> None:
    with pytest.raises(ExtractionError, match="old"):
        validate_date("1970-01-01", today=TODAY)


def test_validate_returns_receipt_data() -> None:
    data = validate(make_raw(), min_confidence=0.7, today=TODAY)
    assert (data.date, data.business, data.purpose) == ("2024-03-14", "Blue Bottle Coffee", "Meals")
    assert data.provider == "fake"


@pytest.mark.parametrize("field", ["date", "business", "purpose"])
def test_null_fields_are_rejected(field: str) -> None:
    with pytest.raises(ExtractionError, match="could not read"):
        validate(make_raw(**{field: None}), min_confidence=0.7, today=TODAY)


def test_low_confidence_is_rejected() -> None:
    with pytest.raises(ExtractionError, match="below"):
        validate(make_raw(confidence=0.4), min_confidence=0.7, today=TODAY)


def test_confidence_exactly_at_threshold_passes() -> None:
    assert validate(make_raw(confidence=0.7), min_confidence=0.7, today=TODAY).confidence == 0.7


def test_placeholder_strings_count_as_null() -> None:
    with pytest.raises(ExtractionError):
        validate(make_raw(business="unknown"), min_confidence=0.7, today=TODAY)


def test_confidence_is_clamped_and_defaults_to_zero() -> None:
    assert build_extraction({"confidence": 5}, provider="p", model="m").confidence == 1.0
    assert build_extraction({"confidence": "bad"}, provider="p", model="m").confidence == 0.0
    assert build_extraction({}, provider="p", model="m").confidence == 0.0


def test_parse_json_tolerates_code_fences_and_prose() -> None:
    fenced = '```json\n{"date": "2024-01-01"}\n```'
    assert parse_json_response(fenced)["date"] == "2024-01-01"
    chatty = 'Sure! Here you go:\n{"date": "2024-01-01"}\nHope that helps.'
    assert parse_json_response(chatty)["date"] == "2024-01-01"


def test_parse_json_rejects_garbage() -> None:
    from receipt_renamer.providers.base import ProviderError

    with pytest.raises(ProviderError):
        parse_json_response("no json at all")
    with pytest.raises(ProviderError):
        parse_json_response("")

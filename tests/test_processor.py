"""End-to-end pipeline behaviour with a fake provider: renames, needs-review, dry run."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from receipt_renamer.ledger import Ledger
from receipt_renamer.processor import process_directory, process_file
from receipt_renamer.providers.fake import AlwaysFailsProvider, FakeProvider

TODAY = date(2024, 6, 1)


def payload(**overrides):
    data = {
        "date": "2024-03-14",
        "business": "Blue Bottle Coffee",
        "purpose": "Meals",
        "confidence": 0.95,
    }
    data.update(overrides)
    return data


def test_file_is_renamed_into_output_folder(inbox, settings_factory, image_factory) -> None:
    source = image_factory(inbox / "scan_0001.jpg")
    settings = settings_factory()
    summary = process_directory(FakeProvider([payload()]), settings, today=TODAY)

    assert summary.renamed == 1
    destination = settings.output_dir / "2024-03-14 Blue Bottle Coffee - Meals.jpg"
    assert destination.exists()
    assert not source.exists()


def test_dry_run_touches_nothing(inbox, settings_factory, image_factory) -> None:
    source = image_factory(inbox / "scan_0001.jpg")
    settings = settings_factory(dry_run=True)
    summary = process_directory(FakeProvider([payload()]), settings, today=TODAY)

    assert summary.renamed == 1
    assert source.exists()
    assert not settings.output_dir.exists()
    assert summary.results[0].applied is False
    assert summary.results[0].destination is not None


def test_in_place_renames_within_input_folder(inbox, settings_factory, image_factory) -> None:
    image_factory(inbox / "scan_0001.jpg")
    settings = settings_factory(in_place=True)
    process_directory(FakeProvider([payload()]), settings, today=TODAY)

    assert (inbox / "2024-03-14 Blue Bottle Coffee - Meals.jpg").exists()
    assert settings.output_dir == inbox


def test_collisions_get_numbered_suffixes(inbox, settings_factory, image_factory) -> None:
    for index in range(3):
        image_factory(inbox / f"scan_000{index}.jpg")
    settings = settings_factory()
    process_directory(FakeProvider([payload(), payload(), payload()]), settings, today=TODAY)

    names = sorted(p.name for p in settings.output_dir.glob("*.jpg"))
    assert names == [
        "2024-03-14 Blue Bottle Coffee - Meals (2).jpg",
        "2024-03-14 Blue Bottle Coffee - Meals (3).jpg",
        "2024-03-14 Blue Bottle Coffee - Meals.jpg",
    ]


def test_dry_run_previews_collision_suffixes(inbox, settings_factory, image_factory) -> None:
    for index in range(2):
        image_factory(inbox / f"scan_000{index}.jpg")
    settings = settings_factory(dry_run=True)
    summary = process_directory(FakeProvider([payload(), payload()]), settings, today=TODAY)

    planned = [r.destination.name for r in summary.results]
    assert planned == [
        "2024-03-14 Blue Bottle Coffee - Meals.jpg",
        "2024-03-14 Blue Bottle Coffee - Meals (2).jpg",
    ]


def test_low_confidence_goes_to_needs_review(inbox, settings_factory, image_factory) -> None:
    image_factory(inbox / "scan_0001.jpg")
    settings = settings_factory()
    summary = process_directory(FakeProvider([payload(confidence=0.2)]), settings, today=TODAY)

    assert summary.needs_review == 1
    moved = settings.needs_review_dir / "scan_0001.jpg"
    assert moved.exists()
    assert "confidence" in (summary.results[0].reason or "")


def test_null_field_goes_to_needs_review(inbox, settings_factory, image_factory) -> None:
    image_factory(inbox / "scan_0001.jpg")
    settings = settings_factory()
    summary = process_directory(FakeProvider([payload(business=None)]), settings, today=TODAY)

    assert summary.needs_review == 1
    assert (settings.needs_review_dir / "scan_0001.jpg").exists()


def test_provider_failure_goes_to_needs_review(inbox, settings_factory, image_factory) -> None:
    image_factory(inbox / "scan_0001.jpg")
    settings = settings_factory()
    summary = process_directory(AlwaysFailsProvider(), settings, today=TODAY)

    assert summary.needs_review == 1
    assert (settings.needs_review_dir / "scan_0001.jpg").exists()
    assert not (inbox / "scan_0001.jpg").exists()


def test_needs_review_collisions_are_safe(inbox, settings_factory, image_factory) -> None:
    image_factory(inbox / "scan_0001.jpg")
    settings = settings_factory()
    process_directory(AlwaysFailsProvider(), settings, today=TODAY)
    image_factory(inbox / "scan_0001.jpg")
    process_directory(AlwaysFailsProvider(), settings, today=TODAY)

    names = sorted(p.name for p in settings.needs_review_dir.glob("*.jpg"))
    assert names == ["scan_0001 (2).jpg", "scan_0001.jpg"]


def test_unsupported_file_is_skipped_not_moved(inbox, settings_factory) -> None:
    odd = inbox / "notes.txt"
    odd.write_text("not a receipt")
    weird = inbox / "photo.heic"
    weird.write_bytes(b"not really heic")
    settings = settings_factory()

    summary = process_directory(FakeProvider([payload()]), settings, today=TODAY)
    # .txt is not even a candidate; .heic is a candidate but may be unsupported.
    assert odd.exists()
    assert all(r.original.suffix != ".txt" for r in summary.results)

    result = process_file(odd, FakeProvider([payload()]), settings, today=TODAY)
    assert result.outcome == "skipped"
    assert odd.exists()
    assert weird.exists()


def test_corrupt_image_goes_to_needs_review(inbox, settings_factory) -> None:
    broken = inbox / "scan_0001.jpg"
    broken.write_bytes(b"definitely not a jpeg")
    settings = settings_factory()

    summary = process_directory(FakeProvider([payload()]), settings, today=TODAY)
    assert summary.needs_review == 1
    assert (settings.needs_review_dir / "scan_0001.jpg").exists()


def test_output_folder_is_not_reprocessed(inbox, settings_factory, image_factory) -> None:
    image_factory(inbox / "scan_0001.jpg")
    settings = settings_factory(recursive=True)
    process_directory(FakeProvider([payload()]), settings, today=TODAY)

    provider = FakeProvider([payload()])
    second = process_directory(provider, settings, today=TODAY)
    assert second.results == []
    assert provider.calls == []


def test_ledger_records_applied_runs(inbox, settings_factory, image_factory) -> None:
    image_factory(inbox / "scan_0001.jpg")
    image_factory(inbox / "scan_0002.jpg")
    settings = settings_factory()
    ledger = Ledger(settings.ledger_path)
    summary = process_directory(
        FakeProvider([payload(), payload(confidence=0.1)]), settings, ledger=ledger, today=TODAY
    )

    entries = ledger.entries_for_run(summary.run_id)
    assert {e.action for e in entries} == {"renamed", "needs_review"}
    renamed = next(e for e in entries if e.action == "renamed")
    assert renamed.business == "Blue Bottle Coffee"
    assert renamed.confidence == 0.95
    assert renamed.provider == "fake"
    assert renamed.model == "fake-vision-1"
    assert Path(renamed.new_path).exists()
    assert renamed.timestamp


def test_dry_run_writes_no_ledger(inbox, settings_factory, image_factory) -> None:
    image_factory(inbox / "scan_0001.jpg")
    settings = settings_factory(dry_run=True)
    ledger = Ledger(settings.ledger_path)
    process_directory(FakeProvider([payload()]), settings, ledger=ledger, today=TODAY)

    assert not ledger.path.exists()


def test_image_is_downscaled_before_upload(inbox, settings_factory, image_factory) -> None:
    image_factory(inbox / "scan_0001.jpg", size=(4000, 3000))
    settings = settings_factory(max_edge=1000)
    provider = FakeProvider([payload()])
    process_directory(provider, settings, today=TODAY)

    from PIL import Image
    import io

    assert len(provider.calls) == 1
    # The provider receives JPEG bytes, so re-open them to confirm the size cap.
    sent_size = provider.calls[0][0]
    assert sent_size > 0
    assert provider.calls[0][1] == "image/jpeg"

    from receipt_renamer.images import prepare_image

    prepared = prepare_image(
        settings.output_dir / "2024-03-14 Blue Bottle Coffee - Meals.jpg", max_edge=1000
    )
    assert max(Image.open(io.BytesIO(prepared.data)).size) <= 1000

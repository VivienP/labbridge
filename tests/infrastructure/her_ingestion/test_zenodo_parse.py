"""Record-envelope parsing: tolerant to unmodelled fields, explicit about missing required ones."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import pytest

from helpers import FIXED_NOW, SYNTHETIC_DOI, build_file_entry, build_payload, digest
from labbridge.infrastructure.her_ingestion.errors import UnsupportedRecordError
from labbridge.infrastructure.her_ingestion.zenodo import (
    parse_checksum,
    parse_record,
    record_api_url,
    safe_filename,
)

EXPECTED_REVISION = 8


def test_parse_record_extracts_every_inventory_field(fixed_clock: Callable[[], datetime]) -> None:
    content = b"a,b\n1,2\n"
    payload = build_payload(
        version_index=3,
        concept_doi="10.5281/zenodo.9999998",
        revision=8,
        title="Distinctive synthetic title",
        publication_date="2098-12-31",
        files=[build_file_entry("alpha_table.csv", content, url="https://zenodo.example/f/alpha")],
    )

    inventory = parse_record(payload, retrieved_at=fixed_clock())

    # Every value below differs from its type default, so a dropped or defaulted field fails here.
    assert inventory.doi == SYNTHETIC_DOI
    assert inventory.record_id == "9999999"
    assert inventory.record_version == "3"
    assert inventory.concept_doi == "10.5281/zenodo.9999998"
    assert inventory.revision == EXPECTED_REVISION
    assert inventory.title == "Distinctive synthetic title"
    assert inventory.published_date == "2098-12-31"
    assert inventory.retrieved_at == FIXED_NOW
    assert len(inventory.files) == 1
    remote = inventory.files[0]
    assert remote.filename == "alpha_table.csv"
    assert remote.byte_size == len(content)
    assert remote.checksum_algorithm == "md5"
    assert remote.checksum_value == digest(content, "md5")
    assert remote.download_url == "https://zenodo.example/f/alpha"


def test_parse_record_tolerates_unknown_payload_fields(fixed_clock: Callable[[], datetime]) -> None:
    """The real API carries many fields this layer does not model; rejecting them would be wrong."""
    payload = build_payload(
        files=[build_file_entry("alpha_table.csv", b"x")],
        extra={"stats": {"downloads": 4}, "owners": [1], "revision": 7},
    )
    payload["metadata"]["unmodelled_key"] = ["anything"]
    payload["files"][0]["bucket_id"] = "abc"

    inventory = parse_record(payload, retrieved_at=fixed_clock())

    assert len(inventory.files) == 1


def test_parse_record_rejects_missing_files_key(fixed_clock: Callable[[], datetime]) -> None:
    payload = build_payload()
    del payload["files"]

    with pytest.raises(UnsupportedRecordError) as caught:
        parse_record(payload, retrieved_at=fixed_clock())

    assert caught.value.field == "files"


def test_parse_record_rejects_missing_record_id(fixed_clock: Callable[[], datetime]) -> None:
    payload = build_payload(files=[build_file_entry("alpha_table.csv", b"x")])
    del payload["id"]

    with pytest.raises(UnsupportedRecordError) as caught:
        parse_record(payload, retrieved_at=fixed_clock())

    assert caught.value.field == "id"


def test_parse_record_rejects_a_file_entry_missing_its_download_link(
    fixed_clock: Callable[[], datetime],
) -> None:
    entry = build_file_entry("alpha_table.csv", b"x")
    del entry["links"]
    payload = build_payload(files=[entry])

    with pytest.raises(UnsupportedRecordError):
        parse_record(payload, retrieved_at=fixed_clock())


def test_parse_record_rejects_unknown_checksum_algorithm(
    fixed_clock: Callable[[], datetime],
) -> None:
    payload = build_payload(
        files=[build_file_entry("alpha_table.csv", b"x", checksum="crc32:deadbeef")]
    )

    with pytest.raises(UnsupportedRecordError):
        parse_record(payload, retrieved_at=fixed_clock())


def test_parse_checksum_rejects_a_bare_digest_without_an_algorithm() -> None:
    """A bare hex digest must not be assumed to be any particular algorithm."""
    with pytest.raises(UnsupportedRecordError):
        parse_checksum("d41d8cd98f00b204e9800998ecf8427e")


def test_parse_checksum_accepts_the_supported_algorithms() -> None:
    assert parse_checksum("md5:ABC123") == ("md5", "abc123")
    assert parse_checksum("sha256:DEF456") == ("sha256", "def456")


def test_licence_stays_unresolved_even_when_the_record_declares_one(
    fixed_clock: Callable[[], datetime],
) -> None:
    """No parser may close the redistribution gate — docs/DATA_STRATEGY.md section 2.3."""
    payload = build_payload(
        licence={"id": "cc-by-4.0"}, files=[build_file_entry("alpha_table.csv", b"x")]
    )

    inventory = parse_record(payload, retrieved_at=fixed_clock())

    assert inventory.licence.raw_value == "cc-by-4.0"
    assert inventory.licence.access_right == "open"
    assert inventory.licence.redistribution == "unresolved"


def test_parse_record_rejects_a_record_with_no_version_relation(
    fixed_clock: Callable[[], datetime],
) -> None:
    """The live record carries no `metadata.version`; the index must not be invented."""
    payload = build_payload(version_index=None, files=[build_file_entry("alpha_table.csv", b"x")])

    with pytest.raises(UnsupportedRecordError) as caught:
        parse_record(payload, retrieved_at=fixed_clock())

    assert caught.value.field == "metadata.relations"


def test_absent_licence_is_recorded_as_none_not_guessed(
    fixed_clock: Callable[[], datetime],
) -> None:
    payload = build_payload(licence=None, files=[build_file_entry("alpha_table.csv", b"x")])

    inventory = parse_record(payload, retrieved_at=fixed_clock())

    assert inventory.licence.raw_value is None
    assert inventory.licence.redistribution == "unresolved"


@pytest.mark.parametrize(
    "unsafe",
    [
        "../escape.csv",
        "nested/alpha.csv",
        "nested\\alpha.csv",
        "/absolute.csv",
        "C:\\windows.csv",
        "..",
        ".",
        "with\x00nul.csv",
        "",
    ],
)
def test_unsafe_remote_filename_is_rejected(unsafe: str) -> None:
    """`filename` arrives from remote JSON and becomes a path — docs/SPEC.md section 14."""
    with pytest.raises(UnsupportedRecordError):
        safe_filename(unsafe)


def test_safe_filename_passes_a_plain_name() -> None:
    assert safe_filename("alpha_table.csv") == "alpha_table.csv"


def test_record_api_url_targets_the_requested_record() -> None:
    assert record_api_url("9999999").endswith("/records/9999999")

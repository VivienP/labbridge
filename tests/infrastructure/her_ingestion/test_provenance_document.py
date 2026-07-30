"""`provenance.json` is canonical and byte-stable, and records the fields section 2.4 requires."""

from __future__ import annotations

import json
from pathlib import Path

from helpers import FIXED_NOW, SYNTHETIC_DOI
from labbridge.infrastructure.her_ingestion.provenance import (
    canonical_json_bytes,
    read_provenance,
    write_document,
)
from labbridge.infrastructure.her_ingestion.records import (
    FetchedFile,
    LicenceStatus,
    ProvenanceDocument,
)

# The exact MUST list of docs/DATA_STRATEGY.md section 2.4, at the level each field belongs to.
REQUIRED_FILE_KEYS = {
    "filename",
    "source_url",
    "byte_size",
    "provided_checksum_algorithm",
    "provided_checksum_value",
    "computed_sha256",
    "landing_path",
    "retrieved_at",
}
REQUIRED_DOCUMENT_KEYS = {"doi", "record_id", "record_version"}


def _document(*, filenames: tuple[str, ...] = ("alpha_table.csv",)) -> ProvenanceDocument:
    return ProvenanceDocument(
        doi=SYNTHETIC_DOI,
        record_id="9999999",
        record_version="1",
        source_licence=LicenceStatus(raw_value="cc-by-4.0"),
        tool_version="0.1.0",
        files=tuple(
            FetchedFile(
                filename=name,
                source_url=f"https://zenodo.example/api/files/{name}",
                byte_size=8,
                provided_checksum_algorithm="md5",
                provided_checksum_value="0" * 32,
                computed_sha256="1" * 64,
                landing_path=name,
                retrieved_at=FIXED_NOW,
            )
            for name in filenames
        ),
        written_at=FIXED_NOW,
    )


def test_provenance_round_trip_is_byte_identical(tmp_path: Path) -> None:
    path = tmp_path / "provenance.json"
    write_document(path, _document())
    first = path.read_bytes()

    write_document(path, read_provenance(path))

    assert path.read_bytes() == first


def test_provenance_records_every_required_field(tmp_path: Path) -> None:
    """Pairs with the round-trip test, which alone would pass against a writer emitting `{}`."""
    path = tmp_path / "provenance.json"
    write_document(path, _document())

    payload = json.loads(path.read_text(encoding="utf-8"))

    assert set(payload) >= REQUIRED_DOCUMENT_KEYS
    assert len(payload["files"]) == 1
    assert set(payload["files"][0]) == REQUIRED_FILE_KEYS
    assert payload["source_licence"]["redistribution"] == "unresolved"


def test_canonical_bytes_are_independent_of_field_insertion_order() -> None:
    """Keys are sorted, so identical content always serialises identically."""
    encoded = canonical_json_bytes(_document())
    keys = list(json.loads(encoded.decode("utf-8")))

    assert keys == sorted(keys)


def test_canonical_bytes_end_with_a_single_lf_and_contain_no_cr() -> None:
    """A text-mode write on Windows would emit CRLF and break `sha256sum -c` on a clone."""
    encoded = canonical_json_bytes(_document())

    assert b"\r" not in encoded
    assert encoded.endswith(b"\n")
    assert not encoded.endswith(b"\n\n")


def test_written_file_bytes_match_the_canonical_encoding(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "provenance.json"
    document = _document()

    write_document(path, document)

    assert path.read_bytes() == canonical_json_bytes(document)


def test_multiple_files_serialise_in_a_stable_order(tmp_path: Path) -> None:
    path = tmp_path / "provenance.json"
    write_document(path, _document(filenames=("beta_signal.txt", "alpha_table.csv")))
    first = path.read_bytes()

    write_document(path, _document(filenames=("beta_signal.txt", "alpha_table.csv")))

    assert path.read_bytes() == first

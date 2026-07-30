"""The immutable landing zone: byte identity, write-once, and F-018 quarantine on mismatch."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from helpers import FIXED_NOW, FakeTransport, digest
from labbridge.infrastructure.her_ingestion.errors import (
    LandingConflictError,
    SourceIntegrityError,
)
from labbridge.infrastructure.her_ingestion.landing import LandingZone, fetch_file
from labbridge.infrastructure.her_ingestion.records import RemoteFile

URL = "https://zenodo.example/api/files/alpha_table.csv"
# An embedded CRLF: any text-mode write or newline translation corrupts this.
CONTENT = b"potential,current\r\n-0.10,1.5\n-0.20,2.5\n"


def _remote(*, algorithm: str = "md5", checksum: str | None = None) -> RemoteFile:
    return RemoteFile(
        filename="alpha_table.csv",
        byte_size=len(CONTENT),
        checksum_algorithm=algorithm,  # type: ignore[arg-type]
        checksum_value=checksum if checksum is not None else digest(CONTENT, algorithm),
        download_url=URL,
    )


def _transport() -> FakeTransport:
    return FakeTransport(payload={}, blobs={URL: CONTENT})


def test_successful_fetch_writes_a_byte_identical_landing_file(landing_root: Path) -> None:
    zone = LandingZone(root=landing_root)

    fetched = fetch_file(_remote(), transport=_transport(), zone=zone, retrieved_at=FIXED_NOW)

    landed = zone.landing_path("alpha_table.csv")
    assert landed.read_bytes() == CONTENT
    assert fetched.computed_sha256 == digest(CONTENT, "sha256")
    assert fetched.byte_size == len(CONTENT)
    assert fetched.landing_path == "alpha_table.csv"
    assert fetched.retrieved_at == FIXED_NOW
    # No reservation is left behind on success.
    assert not zone.reservation_path("alpha_table.csv").exists()


def test_a_sha256_provided_checksum_is_verified_too(landing_root: Path) -> None:
    zone = LandingZone(root=landing_root)

    fetched = fetch_file(
        _remote(algorithm="sha256"), transport=_transport(), zone=zone, retrieved_at=FIXED_NOW
    )

    assert fetched.provided_checksum_algorithm == "sha256"
    assert fetched.provided_checksum_value == digest(CONTENT, "sha256")


def test_checksum_mismatch_quarantines_the_bytes_and_leaves_the_landing_path_clean(
    landing_root: Path,
) -> None:
    """F-018: terminal integrity failure, bytes "retained separately but not accepted"."""
    zone = LandingZone(root=landing_root)
    wrong = "0" * 32

    with pytest.raises(SourceIntegrityError) as caught:
        fetch_file(
            _remote(checksum=wrong), transport=_transport(), zone=zone, retrieved_at=FIXED_NOW
        )

    error = caught.value
    assert error.filename == "alpha_table.csv"
    assert error.expected == wrong
    assert error.computed == digest(CONTENT, "md5")
    assert not zone.landing_path("alpha_table.csv").exists()
    assert not zone.reservation_path("alpha_table.csv").exists()
    # The download really ran and its bytes are retained: "leaves nothing behind" would also pass
    # against an implementation that raised before downloading anything.
    assert error.quarantine_path.read_bytes() == CONTENT


def test_second_fetch_into_an_occupied_landing_path_refuses_and_changes_nothing(
    landing_root: Path,
) -> None:
    """Invariant 11 write-once. Asserting only that it raises would permit clobber-then-raise."""
    zone = LandingZone(root=landing_root)
    fetch_file(_remote(), transport=_transport(), zone=zone, retrieved_at=FIXED_NOW)
    landed = zone.landing_path("alpha_table.csv")
    before = landed.read_bytes()

    other = FakeTransport(payload={}, blobs={URL: b"different bytes entirely"})
    with pytest.raises(LandingConflictError) as caught:
        fetch_file(_remote(), transport=other, zone=zone, retrieved_at=FIXED_NOW)

    assert caught.value.reason == "landing_occupied"
    assert landed.read_bytes() == before
    # It refused before touching the network at all.
    assert other.stream_urls == []


def test_a_stale_reservation_is_neither_reused_nor_clobbered(landing_root: Path) -> None:
    zone = LandingZone(root=landing_root)
    reservation = zone.reservation_path("alpha_table.csv")
    reservation.parent.mkdir(parents=True, exist_ok=True)
    reservation.write_bytes(b"partial from a crashed run")

    with pytest.raises(LandingConflictError) as caught:
        fetch_file(_remote(), transport=_transport(), zone=zone, retrieved_at=FIXED_NOW)

    assert caught.value.reason == "reservation_occupied"
    assert reservation.read_bytes() == b"partial from a crashed run"
    assert not zone.landing_path("alpha_table.csv").exists()


def test_landing_paths_stay_inside_the_root(landing_root: Path) -> None:
    zone = LandingZone(root=landing_root)

    for path in (
        zone.landing_path("alpha_table.csv"),
        zone.reservation_path("alpha_table.csv"),
        zone.quarantine_path("alpha_table.csv", "abc123"),
    ):
        assert landing_root.resolve() in path.resolve().parents or path.parent == landing_root


def test_retrieved_at_is_the_injected_value_not_a_wall_clock(landing_root: Path) -> None:
    zone = LandingZone(root=landing_root)
    stamp = datetime.fromisoformat("2031-07-08T09:10:11+00:00")

    fetched = fetch_file(_remote(), transport=_transport(), zone=zone, retrieved_at=stamp)

    assert fetched.retrieved_at == stamp

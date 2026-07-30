"""Orchestration: dry-run downloads nothing, the DOI is pinned, and provenance follows a fetch."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from helpers import SYNTHETIC_DOI, FakeTransport, build_file_entry, build_payload, digest
from labbridge.infrastructure.her_ingestion.errors import (
    NoFilesRequestedError,
    UnsupportedRecordError,
)
from labbridge.infrastructure.her_ingestion.fetch import (
    DEFAULT_LANDING_ROOT,
    FetchRequest,
    run_fetch,
)
from labbridge.infrastructure.her_ingestion.provenance import (
    INVENTORY_FILENAME,
    PROVENANCE_FILENAME,
    read_provenance,
)

ALPHA = b"potential,current\n-0.1,1.5\n"
BETA = b"0.10 0.20\n"
ALPHA_URL = "https://zenodo.example/api/files/alpha_table.csv"
RECORD_FILE_COUNT = 2
BETA_URL = "https://zenodo.example/api/files/beta_signal.txt"


def _transport() -> FakeTransport:
    payload = build_payload(
        files=[
            build_file_entry("alpha_table.csv", ALPHA, url=ALPHA_URL),
            build_file_entry("beta_signal.txt", BETA, url=BETA_URL),
        ]
    )
    return FakeTransport(payload=payload, blobs={ALPHA_URL: ALPHA, BETA_URL: BETA})


def _request(landing_root: Path, **overrides: object) -> FetchRequest:
    defaults: dict[str, object] = {
        "record_id": "9999999",
        "expected_doi": SYNTHETIC_DOI,
        "landing_root": landing_root,
    }
    defaults.update(overrides)
    return FetchRequest(**defaults)  # type: ignore[arg-type]


def test_dry_run_reads_the_record_once_and_downloads_nothing(
    landing_root: Path, fixed_clock: Callable[[], datetime]
) -> None:
    """Asserting only "no downloads" would also pass against a run_fetch that does nothing."""
    transport = _transport()

    report = run_fetch(
        _request(landing_root, dry_run=True, filenames=("alpha_table.csv",)),
        transport=transport,
        clock=fixed_clock,
        tool_version="0.1.0",
    )

    assert len(transport.get_json_urls) == 1
    assert transport.stream_urls == []
    assert len(report.inventory.files) == RECORD_FILE_COUNT
    assert [remote.filename for remote in report.selected] == ["alpha_table.csv"]
    assert report.fetched == ()
    assert report.provenance_path is None


def test_dry_run_writes_the_inventory_and_leaves_the_landing_zone_empty(
    landing_root: Path, fixed_clock: Callable[[], datetime]
) -> None:
    report = run_fetch(
        _request(landing_root, dry_run=True),
        transport=_transport(),
        clock=fixed_clock,
        tool_version="0.1.0",
    )

    assert report.inventory_path == landing_root / INVENTORY_FILENAME
    assert report.inventory_path.exists()
    assert not (landing_root / "alpha_table.csv").exists()
    assert not (landing_root / PROVENANCE_FILENAME).exists()
    assert sorted(p.name for p in landing_root.iterdir()) == [INVENTORY_FILENAME]


def test_fetch_writes_the_landing_files_and_a_provenance_document(
    landing_root: Path, fixed_clock: Callable[[], datetime]
) -> None:
    report = run_fetch(
        _request(landing_root, filenames=("alpha_table.csv", "beta_signal.txt")),
        transport=_transport(),
        clock=fixed_clock,
        tool_version="0.1.0",
    )

    assert (landing_root / "alpha_table.csv").read_bytes() == ALPHA
    assert (landing_root / "beta_signal.txt").read_bytes() == BETA
    assert report.provenance_path == landing_root / PROVENANCE_FILENAME

    document = read_provenance(landing_root / PROVENANCE_FILENAME)
    assert document.doi == SYNTHETIC_DOI
    assert document.record_version == "0"
    assert document.tool_version == "0.1.0"
    assert {f.filename for f in document.files} == {"alpha_table.csv", "beta_signal.txt"}
    alpha = next(f for f in document.files if f.filename == "alpha_table.csv")
    assert alpha.computed_sha256 == digest(ALPHA, "sha256")
    assert alpha.source_url == ALPHA_URL


def test_a_real_fetch_requires_at_least_one_explicit_filename(
    landing_root: Path, fixed_clock: Callable[[], datetime]
) -> None:
    transport = _transport()

    with pytest.raises(NoFilesRequestedError):
        run_fetch(
            _request(landing_root), transport=transport, clock=fixed_clock, tool_version="0.1.0"
        )

    assert transport.stream_urls == []


def test_a_record_whose_doi_is_not_the_expected_one_is_rejected(
    landing_root: Path, fixed_clock: Callable[[], datetime]
) -> None:
    """Without this, a wrong --record-id silently acquires a different archive."""
    transport = _transport()

    with pytest.raises(UnsupportedRecordError):
        run_fetch(
            _request(
                landing_root,
                expected_doi="10.5281/zenodo.1234567",
                filenames=("alpha_table.csv",),
            ),
            transport=transport,
            clock=fixed_clock,
            tool_version="0.1.0",
        )

    assert transport.stream_urls == []
    assert not landing_root.exists() or list(landing_root.iterdir()) == []


def test_the_default_landing_root_is_under_a_git_ignored_prefix() -> None:
    """Otherwise provenance.json becomes committable while the licence gate is open."""
    assert DEFAULT_LANDING_ROOT.as_posix().startswith("data/her/")


def test_the_default_expected_doi_is_the_pinned_record() -> None:
    assert FetchRequest(record_id="1").expected_doi == "10.5281/zenodo.20439519"

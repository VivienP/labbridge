"""A recorded decision resolves redistribution; parsing a record never does.

Every decision here is synthetic and pinned to `SYNTHETIC_DOI`. The real decision lives in
`data_use.HER_DATA_USE` and is asserted only for the properties that must hold of any decision, so
this suite never becomes a second, drifting copy of it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

import pytest

from helpers import FIXED_NOW, SYNTHETIC_DOI, FakeTransport, build_file_entry, build_payload
from labbridge.infrastructure.her_ingestion.data_use import (
    HER_DATA_USE,
    resolve_redistribution,
)
from labbridge.infrastructure.her_ingestion.fetch import FetchRequest, run_fetch
from labbridge.infrastructure.her_ingestion.provenance import read_provenance
from labbridge.infrastructure.her_ingestion.records import (
    PINNED_DOI,
    DataUseDecision,
    LicenceStatus,
)
from labbridge.infrastructure.her_ingestion.zenodo import parse_record

ALPHA = b"potential,current\n-0.1,1.5\n"
ALPHA_URL = "https://zenodo.example/api/files/alpha_table.csv"

DECISION = DataUseDecision(
    adr="ADR-000",
    doi=SYNTHETIC_DOI,
    licence_id="cc-by-4.0",
    verified_on=date(2026, 1, 2),
    verified_from="https://zenodo.example/api/records/9999999",
    redistribution="permitted_with_attribution",
    attribution="Synthetic record. Licensed CC BY 4.0.",
)


def test_a_matching_decision_resolves_redistribution() -> None:
    licence = LicenceStatus(raw_value="cc-by-4.0", access_right="open")

    resolved = resolve_redistribution(licence, doi=SYNTHETIC_DOI, decision=DECISION)

    assert resolved.redistribution == "permitted_with_attribution"
    assert resolved.raw_value == "cc-by-4.0"


def test_an_upstream_relicensing_reopens_the_gate() -> None:
    """The decision is evidence about one licence. A different licence is not that evidence."""
    licence = LicenceStatus(raw_value="cc-by-nc-4.0", access_right="open")

    resolved = resolve_redistribution(licence, doi=SYNTHETIC_DOI, decision=DECISION)

    assert resolved.redistribution == "unresolved"


def test_a_withdrawn_licence_field_reopens_the_gate() -> None:
    licence = LicenceStatus(raw_value=None)

    resolved = resolve_redistribution(licence, doi=SYNTHETIC_DOI, decision=DECISION)

    assert resolved.redistribution == "unresolved"


def test_a_decision_does_not_carry_to_another_record() -> None:
    licence = LicenceStatus(raw_value="cc-by-4.0")

    resolved = resolve_redistribution(licence, doi="10.5281/zenodo.1234567", decision=DECISION)

    assert resolved.redistribution == "unresolved"


def test_parsing_a_record_never_resolves_redistribution() -> None:
    """The structural guarantee: only a recorded decision moves the gate, never the record."""
    payload = build_payload(
        licence={"id": "cc-by-4.0"}, files=[build_file_entry("alpha_table.csv", b"x")]
    )

    inventory = parse_record(payload, retrieved_at=FIXED_NOW)

    assert inventory.licence.raw_value == "cc-by-4.0"
    assert inventory.licence.redistribution == "unresolved"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("doi", PINNED_DOI),
        ("licence_id", "cc-by-4.0"),
        ("redistribution", "permitted_with_attribution"),
    ],
)
def test_the_recorded_her_decision_states_what_it_rests_on(field: str, value: str) -> None:
    assert getattr(HER_DATA_USE, field) == value


def test_run_fetch_applies_the_decision_and_records_it_in_provenance(
    landing_root: Path, fixed_clock: Callable[[], datetime]
) -> None:
    """The gate closes for the whole run, and provenance.json says on what authority."""
    payload = build_payload(files=[build_file_entry("alpha_table.csv", ALPHA, url=ALPHA_URL)])
    transport = FakeTransport(payload=payload, blobs={ALPHA_URL: ALPHA})

    report = run_fetch(
        FetchRequest(
            record_id="9999999",
            filenames=("alpha_table.csv",),
            expected_doi=SYNTHETIC_DOI,
            landing_root=landing_root,
            data_use=DECISION,
        ),
        transport=transport,
        clock=fixed_clock,
        tool_version="0.1.0",
    )

    assert report.inventory.licence.redistribution == "permitted_with_attribution"
    assert report.provenance_path is not None
    document = read_provenance(report.provenance_path)
    assert document.data_use is not None
    assert document.data_use.adr == "ADR-000"
    assert document.source_licence.redistribution == "permitted_with_attribution"


def test_a_fetch_under_an_unmatched_licence_records_no_decision(
    landing_root: Path, fixed_clock: Callable[[], datetime]
) -> None:
    """Provenance must not imply an authority that did not apply to this fetch."""
    payload = build_payload(
        licence="cc-by-nc-4.0", files=[build_file_entry("alpha_table.csv", ALPHA, url=ALPHA_URL)]
    )
    transport = FakeTransport(payload=payload, blobs={ALPHA_URL: ALPHA})

    report = run_fetch(
        FetchRequest(
            record_id="9999999",
            filenames=("alpha_table.csv",),
            expected_doi=SYNTHETIC_DOI,
            landing_root=landing_root,
            data_use=DECISION,
        ),
        transport=transport,
        clock=fixed_clock,
        tool_version="0.1.0",
    )

    assert report.inventory.licence.redistribution == "unresolved"
    assert report.provenance_path is not None
    assert read_provenance(report.provenance_path).data_use is None


def test_the_recorded_her_decision_carries_an_adr_a_date_and_an_attribution() -> None:
    """A decision without these is not auditable, whatever its redistribution value says."""
    assert HER_DATA_USE.adr.startswith("ADR-")
    assert HER_DATA_USE.verified_from.startswith("https://")
    assert HER_DATA_USE.verified_on <= date.today()
    assert "10.5281/zenodo.20439519" in HER_DATA_USE.attribution
    assert "CC BY 4.0" in HER_DATA_USE.attribution

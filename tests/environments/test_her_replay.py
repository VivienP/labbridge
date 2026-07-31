"""The replay adapter returns what was recorded, or says nothing was.

Every archive here is built by the fixture generator, so the suite runs offline and never touches
the real dataset. The generator is the same code the adapter will meet in Slice 1's demo, which is
what makes these tests about the adapter rather than about a hand-written stand-in.
"""

from __future__ import annotations

import json
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest

from labbridge.domain.candidates import HerCandidate
from labbridge.domain.provenance import MEASURED_SOURCE_TYPES
from labbridge.domain.quantities import Quantity
from labbridge.environments.her_replay import (
    EXPECTED_LSV_HEADER,
    AdapterSuccess,
    AdapterUnavailable,
    AmbiguousRootError,
    HerReplayAdapter,
    UnknownRootError,
    UnknownSourceTypeError,
    UnsupportedSchemaError,
    build_index,
    classify_member,
    resolve_environment,
)
from labbridge.infrastructure.her_ingestion.fixture import (
    FIXTURE_MANIFEST_FILENAME,
    FixtureSpec,
    build_fixture,
)
from labbridge.infrastructure.her_ingestion.provenance import PROVENANCE_FILENAME, write_document

SPEC = FixtureSpec(areas_per_library=8, seccm_areas_per_library=3)
SHA256_HEX_CHARS = 64


@pytest.fixture
def fixture_root(tmp_path: Path) -> Path:
    manifest = build_fixture(tmp_path, spec=SPEC, generator_version="0.1.0")
    write_document(tmp_path / FIXTURE_MANIFEST_FILENAME, manifest)
    return tmp_path


def _candidate(library: str, area: str) -> HerCandidate:
    return HerCandidate(
        library_id=library,
        measurement_area_id=area,
        grid_x=Quantity(value=Decimal("0"), unit="mm"),
        grid_y=Quantity(value=Decimal("0"), unit="mm"),
    )


def test_a_fixture_root_resolves_to_synthetic_replay(fixture_root: Path) -> None:
    """ADR-010, and the reason the adapter reads the disk instead of taking a flag."""
    environment, archive = resolve_environment(fixture_root)

    assert environment.data_origin == "synthetic"
    assert environment.execution_mode == "replay"
    assert archive.name == "SECCM_dataset.zip"


def test_a_root_holding_both_markers_is_ambiguous_and_fails(fixture_root: Path) -> None:
    """Preferring one marker would guess at the origin of the bytes — the conflation invariant 1
    exists to prevent."""
    (fixture_root / PROVENANCE_FILENAME).write_text("{}", encoding="utf-8")

    with pytest.raises(AmbiguousRootError):
        resolve_environment(fixture_root)


def test_a_root_with_no_marker_fails_rather_than_defaulting(tmp_path: Path) -> None:
    with pytest.raises(UnknownRootError):
        resolve_environment(tmp_path)


def test_the_index_covers_every_measured_location(fixture_root: Path) -> None:
    adapter = HerReplayAdapter(fixture_root)

    assert adapter.location_count == len(SPEC.libraries) * SPEC.seccm_areas_per_library


def test_the_fit_table_is_not_reported_as_an_unrecognised_member(fixture_root: Path) -> None:
    """It is a known member with a different role, not a sign of a layout change."""
    _, unmatched = build_index(fixture_root / "SECCM_dataset.zip")

    assert unmatched == ()


def test_an_unexpected_member_is_reported_not_silently_dropped(tmp_path: Path) -> None:
    """A layout change first shows itself as a member nothing recognises."""
    archive = tmp_path / "SECCM_dataset.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("SECCM_dataset/UNEXPECTED_new_format.csv", b"a,b\n1,2\n")

    _, unmatched = build_index(archive)

    assert unmatched == ("SECCM_dataset/UNEXPECTED_new_format.csv",)


async def test_a_measured_location_returns_the_recorded_bytes(fixture_root: Path) -> None:
    adapter = HerReplayAdapter(fixture_root)
    key = adapter.known_locations()[0]

    result = await adapter.execute(_candidate(key.library_id, key.measurement_area_id))

    assert isinstance(result, AdapterSuccess)
    assert result.payload.startswith(",".join(EXPECTED_LSV_HEADER).encode())
    assert result.signal_kind == "lsv"
    assert len(result.source_sha256) == SHA256_HEX_CHARS


async def test_an_unmeasured_location_returns_a_structured_unavailable(
    fixture_root: Path,
) -> None:
    """F-017. The source excludes areas; the adapter must not interpolate or impute one."""
    adapter = HerReplayAdapter(fixture_root)
    measured = {
        k.measurement_area_id for k in adapter.known_locations() if k.library_id == "Au-rich"
    }
    missing = next(
        str(area) for area in range(1, SPEC.areas_per_library + 1) if str(area) not in measured
    )

    result = await adapter.execute(_candidate("Au-rich", missing))

    assert isinstance(result, AdapterUnavailable)
    assert result.failure_code == "source_location_unavailable"
    assert "not measured" in result.reason


async def test_an_unknown_library_is_unavailable_not_an_error(fixture_root: Path) -> None:
    adapter = HerReplayAdapter(fixture_root)

    result = await adapter.execute(_candidate("Pt-rich", "1"))

    assert isinstance(result, AdapterUnavailable)


async def test_repeating_a_query_returns_the_same_source_identity(fixture_root: Path) -> None:
    """docs/DATA_STRATEGY.md §2.6: a repeat is the same observation, never a new measurement."""
    adapter = HerReplayAdapter(fixture_root)
    key = adapter.known_locations()[0]
    candidate = _candidate(key.library_id, key.measurement_area_id)

    first = await adapter.execute(candidate)
    second = await adapter.execute(candidate)

    assert isinstance(first, AdapterSuccess)
    assert isinstance(second, AdapterSuccess)
    assert first.source_sha256 == second.source_sha256
    assert first.source_path == second.source_path


async def test_an_unrecognised_header_fails_loudly_rather_than_being_parsed(
    tmp_path: Path,
) -> None:
    """F-019. Never coerce, never default, never guess a column."""
    archive = tmp_path / "SECCM_dataset.zip"
    member = "SECCM_dataset/Au-Ir-Rh_Au-rich_SECCM_area_1_x=0.00_y=0.00_LSV.csv"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(member, b"Voltage,Current\r\n-0.1,-1.5\r\n")
    (tmp_path / FIXTURE_MANIFEST_FILENAME).write_text(json.dumps({}), encoding="utf-8")

    adapter = HerReplayAdapter(tmp_path)

    with pytest.raises(UnsupportedSchemaError) as caught:
        await adapter.execute(_candidate("Au-rich", "1"))

    assert caught.value.code == "unsupported_schema"
    assert caught.value.found == ("Voltage", "Current")


async def test_the_recorded_grid_coordinates_come_from_the_filename(fixture_root: Path) -> None:
    """Recorded, not checked against the candidate: a disagreement is a scientific finding."""
    adapter = HerReplayAdapter(fixture_root)
    key = adapter.known_locations()[0]

    result = await adapter.execute(_candidate(key.library_id, key.measurement_area_id))

    assert isinstance(result, AdapterSuccess)
    assert isinstance(result.recorded_grid_x, Decimal)


def test_a_fixture_backed_adapter_offers_a_synthetic_root_only(fixture_root: Path) -> None:
    adapter = HerReplayAdapter(fixture_root)

    root = adapter.synthetic_root(generator="fixture", generator_version="0.1.0", seed=SPEC.seed)

    assert root.seed == SPEC.seed
    assert adapter.environment.data_origin == "synthetic"


@pytest.mark.parametrize(
    ("member", "expected"),
    [
        ("XPS_dataset/Au-Ir-Rh_Au-rich_XPS_predicted.csv", "predicted_xps"),
        ("XPS_dataset/Au-Ir-Rh_Au-rich_XPS.csv", "measured_xps"),
        ("EDX_dataset/Au-Ir-Rh_Au-rich_EDX.csv", "measured_edx"),
        ("SECCM_dataset/Au-Ir-Rh_Au-rich_SECCM_area_1_x=0.00_y=0.00_LSV.csv", "measured_lsv"),
        ("SECCM_dataset/LSV_fit_parameters.csv", "source_fitted_parameters"),
    ],
)
def test_each_member_kind_is_classified_from_its_path(member: str, expected: str) -> None:
    """The only defence against F-046: measured EDX and predicted XPS are structurally identical,
    so nothing but the path can separate a measurement from a model output."""
    assert classify_member(member) == expected


def test_predicted_xps_is_never_classified_as_measured() -> None:
    """The suffix order matters: `_XPS_predicted.csv` also ends with the measured stem plus a
    suffix, so a reversed order would label every GP prediction a measurement."""
    predicted = classify_member("XPS_dataset/Au-Ir-Rh_Rh-rich_XPS_predicted.csv")

    assert predicted == "predicted_xps"
    assert predicted not in MEASURED_SOURCE_TYPES


def test_an_unclassifiable_member_is_refused_not_defaulted() -> None:
    with pytest.raises(UnknownSourceTypeError, match="cannot determine the source type"):
        classify_member("SECCM_dataset/something_new.csv")


async def test_the_source_record_carries_the_classified_type(fixture_root: Path) -> None:
    adapter = HerReplayAdapter(fixture_root)
    key = adapter.known_locations()[0]
    result = await adapter.execute(_candidate(key.library_id, key.measurement_area_id))
    assert isinstance(result, AdapterSuccess)

    record = adapter.source_record(result, doi="10.5281/zenodo.9999999", record_version="0")

    assert record.source_type == "measured_lsv"
    assert record.is_measured

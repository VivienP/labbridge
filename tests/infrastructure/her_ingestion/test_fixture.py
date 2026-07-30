"""The fixture reproduces the recorded structure, reproduces no archive value, and is deterministic.

The schema assertions here are written against the four schemas recorded in `dataset_inventory.json`
during Gate 0. They are the one place in the suite that may name a real column, because their whole
purpose is to fail when the fixture stops matching the inspected source.
"""

from __future__ import annotations

import zipfile
from datetime import UTC, datetime
from pathlib import Path

from labbridge.infrastructure.her_ingestion.dataset import TableProfile
from labbridge.infrastructure.her_ingestion.fixture import (
    FixtureSpec,
    build_fixture,
)
from labbridge.infrastructure.her_ingestion.inspect import build_inventory, profile_archive

GRID_HEADER = ["Area", "Au [at.%]", "Ir [at.%]", "Rh [at.%]"]
LSV_HEADER = [
    "Potential vs. RHE [V]",
    "Current density [A/cm^2]",
    "Standard deviation [A/cm^2]",
]
FIT_HEADER = ["Library", "Area", "i_lim [A/cm^2]", "k^0 [cm/s]", "alpha [a.u.]"]
MEASURED_XPS_HEADER = [
    "MA",
    "Au [at.%]",
    "Ir [at.%]",
    "Rh [at.%]",
    "Rh-Native Oxide [at.%]",
    "Rh-Hydroxide [at.%]",
    "O [at.%]",
    "C [at.%]",
]
ARCHIVE_COUNT = 3
COMPOSITION_TOTAL = 100.0


def _build(tmp_path: Path, **overrides: object) -> Path:
    spec = FixtureSpec(**overrides)  # type: ignore[arg-type]
    build_fixture(tmp_path, spec=spec, generator_version="0.1.0")
    return tmp_path


def _tables(root: Path) -> dict[str, TableProfile]:
    return {
        table.member_path: table
        for archive in sorted(root.glob("*.zip"))
        for table in profile_archive(archive).tables
    }


def test_the_same_seed_produces_byte_identical_archives(tmp_path: Path) -> None:
    """Without this, the manifest checksums are decoration."""
    first = build_fixture(tmp_path / "a", spec=FixtureSpec(), generator_version="0.1.0")
    second = build_fixture(tmp_path / "b", spec=FixtureSpec(), generator_version="0.1.0")

    assert [a.sha256 for a in first.archives] == [a.sha256 for a in second.archives]


def test_a_different_seed_produces_different_archives(tmp_path: Path) -> None:
    first = build_fixture(tmp_path / "a", spec=FixtureSpec(seed=1), generator_version="0.1.0")
    second = build_fixture(tmp_path / "b", spec=FixtureSpec(seed=2), generator_version="0.1.0")

    assert [a.sha256 for a in first.archives] != [a.sha256 for a in second.archives]


def test_the_manifest_declares_the_fixture_synthetic(tmp_path: Path) -> None:
    """A fixture-backed run is not an observation, however faithful the schema."""
    manifest = build_fixture(tmp_path, spec=FixtureSpec(), generator_version="0.1.0")

    assert manifest.data_origin == "synthetic"
    assert manifest.seed == FixtureSpec().seed
    assert len(manifest.archives) == ARCHIVE_COUNT


def test_the_four_recorded_schemas_are_reproduced(tmp_path: Path) -> None:
    tables = _tables(_build(tmp_path))
    headers = {path: [c.header for c in t.columns] for path, t in tables.items()}

    assert GRID_HEADER in headers.values()
    assert LSV_HEADER in headers.values()
    assert FIT_HEADER in headers.values()
    assert MEASURED_XPS_HEADER in headers.values()


def test_measured_edx_and_predicted_xps_are_structurally_identical(tmp_path: Path) -> None:
    """The F-046 trap, reproduced on purpose: column validation cannot tell these apart."""
    tables = _tables(_build(tmp_path))
    edx = tables["EDX_dataset/Au-Ir-Rh_Au-rich_EDX.csv"]
    predicted = tables["XPS_dataset/Au-Ir-Rh_Au-rich_XPS_predicted.csv"]

    assert [c.header for c in edx.columns] == [c.header for c in predicted.columns]
    assert edx.row_count == predicted.row_count
    assert edx.line_ending == predicted.line_ending


def test_the_three_line_endings_are_reproduced(tmp_path: Path) -> None:
    """CR for measured XPS, CRLF for LSVs, LF elsewhere. A universal-newline bug surfaces here."""
    tables = _tables(_build(tmp_path))
    endings = {path: t.line_ending for path, t in tables.items()}

    assert endings["XPS_dataset/Au-Ir-Rh_Au-rich_XPS.csv"] == "CR"
    assert endings["EDX_dataset/Au-Ir-Rh_Au-rich_EDX.csv"] == "LF"
    assert endings["SECCM_dataset/LSV_fit_parameters.csv"] == "LF"
    assert {v for k, v in endings.items() if k.endswith("_LSV.csv")} == {"CRLF"}


def test_lsv_row_counts_vary_so_no_adapter_may_assume_a_fixed_length(tmp_path: Path) -> None:
    tables = _tables(_build(tmp_path))
    counts = {t.row_count for p, t in tables.items() if p.endswith("_LSV.csv")}

    assert len(counts) > 1


def test_the_lsv_current_density_is_negative_and_the_deviation_positive(tmp_path: Path) -> None:
    """HER is a reduction. A fixture on the wrong sign convention would train the adapter wrong."""
    tables = _tables(_build(tmp_path))
    lsv = next(t for p, t in tables.items() if p.endswith("_LSV.csv"))

    current = lsv.columns[1]
    deviation = lsv.columns[2]
    assert float(current.maximum) <= 0
    assert float(deviation.minimum) > 0


def test_the_fitted_limiting_current_is_positive_as_the_source_records_it(tmp_path: Path) -> None:
    """The source stores a magnitude here while the raw column is signed. Preserving that mismatch
    is what lets a later parser be caught placing the fit on the wrong side of zero."""
    tables = _tables(_build(tmp_path))
    fit = tables["SECCM_dataset/LSV_fit_parameters.csv"]

    i_lim = fit.columns[2]
    assert float(i_lim.minimum) > 0


def test_compositions_close_to_one_hundred(tmp_path: Path) -> None:
    """Closure is an artifact of normalisation; the fixture exhibits it rather than hiding it."""
    root = _build(tmp_path)
    with zipfile.ZipFile(root / "EDX_dataset.zip") as archive:
        raw = archive.read("EDX_dataset/Au-Ir-Rh_Au-rich_EDX.csv").decode("utf-8")

    for line in raw.splitlines()[1:]:
        cells = line.split(",")
        assert sum(float(v) for v in cells[1:]) == COMPOSITION_TOTAL


def test_grid_areas_without_an_lsv_exist_so_unavailability_is_exercised(tmp_path: Path) -> None:
    """The source excludes areas from SECCM. An adapter must meet a grid position with no LSV."""
    spec = FixtureSpec(areas_per_library=12, seccm_areas_per_library=4)
    root = tmp_path
    build_fixture(root, spec=spec, generator_version="0.1.0")
    tables = _tables(root)

    lsvs = [p for p in tables if p.endswith("_LSV.csv")]
    assert len(lsvs) == len(spec.libraries) * spec.seccm_areas_per_library
    assert spec.seccm_areas_per_library < spec.areas_per_library


def test_measured_xps_covers_a_sparse_subset_of_the_predicted_grid(tmp_path: Path) -> None:
    tables = _tables(_build(tmp_path))
    measured = tables["XPS_dataset/Au-Ir-Rh_Au-rich_XPS.csv"]
    predicted = tables["XPS_dataset/Au-Ir-Rh_Au-rich_XPS_predicted.csv"]

    assert measured.row_count < predicted.row_count


def test_the_inspector_reads_the_fixture_without_special_casing(tmp_path: Path) -> None:
    """The fixture must be inspectable by the same tool that inspected the archive."""
    root = _build(tmp_path)

    inventory = build_inventory(
        root,
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        tool_version="0.1.0",
        provenance_sha256=None,
    )

    assert len(inventory.archives) == ARCHIVE_COUNT
    assert all(a.tables for a in inventory.archives)

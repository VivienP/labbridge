"""Generate an independently produced, schema-compatible HER fixture.

Gate 0 requires an offline fixture that can exercise the replay adapter. ADR-009 now permits copying
archive rows, and this module still copies none: a fixture built from archive values would make the
offline suite depend on a multi-hundred-megabyte download, and would make a schema regression
indistinguishable from a data change.

The structure mirrored here belongs to Thelen F, Kim M, Arruda de Oliveira G, Bürgel JL,
Schuhmann W, Ludwig A, *Dataset — Autonomous scanning electrochemical cell microscopy enables rapid
exploration of large compositionally complex material spaces*, Zenodo 2026,
doi:10.5281/zenodo.20439519, CC BY 4.0.
ADR-009 does not require attribution on structural metadata, only on artifacts carrying archive
values; the citation is here anyway so a reader meeting an archive-shaped file can find the archive.

What is reproduced is **structure**, read from the recorded `dataset_inventory.json` and not from
memory: the four table schemas, their headers and declared units, their delimiters, their three
different line endings, the varying LSV row count, the filename grammar, and the relationships among
libraries, grid areas, measured points, and fitted parameters.

What is *not* reproduced is any archive value. Every number here comes from a seeded generator over
ranges chosen for this fixture. The numeric shapes are arbitrary and carry no kinetic claim: the LSV
curve is a monotone cathodic ramp chosen because it is easy to verify, not a kinetic model, and
nothing here may be cited as evidence about the physical system (`AI_CONTRACT.md` §7).

Two source conventions are preserved deliberately, because an adapter that gets them wrong is wrong:

* the LSV current density sweeps **negative** — HER is a reduction, and the archive records the
  signed cathodic value — while some files still reach a small positive background near onset;
* the fitted limiting current density is **positive** — the source stores it as a magnitude, on the
  opposite sign convention from the raw column it summarises.
"""

from __future__ import annotations

import hashlib
import io
import random
import zipfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

from pydantic import Field

from .records import _Record

FIXTURE_MANIFEST_FILENAME: Final = "fixture_manifest.json"
FIXTURE_SCHEMA_VERSION: Final = "1"
GENERATOR: Final = "labbridge.infrastructure.her_ingestion.fixture"

#: Fixed so the archive bytes, and therefore the manifest checksums, are reproducible.
_ZIP_TIMESTAMP: Final = (1980, 1, 1, 0, 0, 0)

#: Ranges chosen for this fixture. They are deliberately not the archive's recorded extremes.
_POTENTIAL_START: Final = 0.01
_POTENTIAL_END: Final = -0.90
_CURRENT_FLOOR: Final = -5.0
_STDDEV_SCALE: Final = 0.05
#: A small positive background near onset, as some source files show before the cathodic sweep.
_ONSET_BACKGROUND: Final = 0.08


class FixtureArchive(_Record):
    filename: str
    sha256: str
    member_count: int = Field(ge=0)


class FixtureManifest(_Record):
    """What produced this fixture, and the assertion that it is not observed data."""

    schema_version: str = FIXTURE_SCHEMA_VERSION
    generator: str = GENERATOR
    generator_version: str
    seed: int
    #: Records produced from these bytes are synthetic. A fixture-backed run is not an observation,
    #: however faithfully it reproduces the observed schema.
    data_origin: str = "synthetic"
    #: The configuration the generator ran with, so the lineage root can name the *configuration*
    #: rather than a digest of the output it produced. Rerunning from this reproduces the bytes.
    spec: dict[str, object] = Field(default_factory=dict)
    note: str = (
        "Independently generated, schema-compatible fixture. Contains no value from the HER "
        "archive. Not observed data and not a measurement."
    )
    archives: tuple[FixtureArchive, ...]


@dataclass(frozen=True)
class FixtureSpec:
    """How much fixture to build.

    The defaults are far smaller than the archive on purpose — the offline suite must stay fast —
    while keeping every structural relationship the adapter has to navigate.
    """

    seed: int = 20260730
    libraries: tuple[str, ...] = ("Au-rich", "Ir-rich", "Rh-rich")
    #: Grid areas per library. EDX and predicted XPS cover all of them.
    areas_per_library: int = 12
    #: Areas actually measured by SECCM. The remainder stand in for the source's excluded areas,
    #: so an adapter meets a grid position with no LSV and must return a structured unavailable.
    seccm_areas_per_library: int = 4
    #: Measured XPS covers a sparse subset, as in the source; predicted XPS covers the full grid.
    xps_measured_per_library: int = 2
    #: The source's LSV files are 208 or 209 rows. A fixed length would hide that.
    lsv_row_counts: tuple[int, ...] = (208, 209)


def _rng(spec: FixtureSpec, key: str) -> random.Random:
    """A stream from the seed and a stable key, so each member is independently reproducible."""
    return random.Random(f"{spec.seed}:{key}")


def _closed_composition(rng: random.Random, count: int, decimals: int) -> list[str]:
    """Percentages summing to about 100.

    Closure is an artifact of normalisation: at.% values normalised over the reported elements sum
    to about 100 by construction, which is not evidence that no other element is present.

    "About" is the point. Every value is rounded independently, so the row sums land near 100 rather
    than on it. Forcing exact closure by absorbing the residue into the last column would let a
    validation rule be calibrated to `== 100` and then reject real rows, the same way the
    strictly-negative current density did.

    No archive row-sum range is quoted here. An earlier version quoted one; recomputation found it
    wrong, and the number had come from an ad-hoc inspection rather than from
    `dataset_inventory.json`, which records per-column extremes and no row sums at all.
    `AI_CONTRACT.md` §7 does not admit a figure from memory, and this fixture does not need one:
    what matters is that closure is approximate, not what the archive's exact spread is.
    """
    weights = [rng.uniform(1.0, 10.0) for _ in range(count)]
    total = sum(weights)
    return [f"{weight / total * 100:.{decimals}f}" for weight in weights]


def _table(header: Sequence[str], rows: Sequence[Sequence[str]], *, line_ending: str) -> bytes:
    lines = [",".join(header), *(",".join(row) for row in rows)]
    return (line_ending.join(lines) + line_ending).encode("utf-8")


def _grid_table(spec: FixtureSpec, library: str, kind: str) -> bytes:
    """The schema shared by EDX and predicted XPS — identical headers, identical row count.

    They are one schema in the source too. Nothing distinguishes them but the archive and the
    filename, which is why the source-type field must be set from the path (F-046).
    """
    rng = _rng(spec, f"grid:{kind}:{library}")
    header = ["Area", "Au [at.%]", "Ir [at.%]", "Rh [at.%]"]
    rows = [
        [str(area), *_closed_composition(rng, 3, 2)]
        for area in range(1, spec.areas_per_library + 1)
    ]
    return _table(header, rows, line_ending="\n")


def _measured_xps_table(spec: FixtureSpec, library: str) -> bytes:
    """Eight columns, a different identifier name, and CR-only line endings — all as recorded."""
    rng = _rng(spec, f"xps:{library}")
    header = [
        "MA",
        "Au [at.%]",
        "Ir [at.%]",
        "Rh [at.%]",
        "Rh-Native Oxide [at.%]",
        "Rh-Hydroxide [at.%]",
        "O [at.%]",
        "C [at.%]",
    ]
    areas = _measured_areas(spec, library)
    rows = [[str(area), *_closed_composition(rng, 7, 1)] for area in areas]
    return _table(header, rows, line_ending="\r")


def _lsv_table(spec: FixtureSpec, library: str, area: int) -> bytes:
    """A monotone cathodic ramp. An arbitrary synthetic shape, not a kinetic model.

    The signs are the point: potential runs from just above zero down the cathodic direction on the
    RHE scale, the current density sweeps strongly negative, and the standard deviation is positive.
    Some files carry a small positive background near onset, as 46 of the archive's 966 do.
    """
    rng = _rng(spec, f"lsv:{library}:{area}")
    count = spec.lsv_row_counts[area % len(spec.lsv_row_counts)]
    header = [
        "Potential vs. RHE [V]",
        "Current density [A/cm^2]",
        "Standard deviation [A/cm^2]",
    ]
    # 46 of the archive's 966 LSV files reach a positive current density near onset, so the source
    # is *predominantly* cathodic-negative, not negative throughout. A fixture that never went
    # positive would let an ingestion rule be calibrated to reject those 46 real files. The share
    # here is a fixture choice, not a source statistic; what matters is that both kinds occur.
    background = _ONSET_BACKGROUND if _has_positive_onset(spec, library, area) else 0.0
    rows: list[list[str]] = []
    for index in range(count):
        fraction = index / (count - 1)
        potential = _POTENTIAL_START + fraction * (_POTENTIAL_END - _POTENTIAL_START)
        current = _CURRENT_FLOOR * fraction**2 * rng.uniform(0.85, 1.15) + background * (
            1 - fraction
        )
        deviation = abs(current) * _STDDEV_SCALE * rng.uniform(0.5, 1.5) + 1e-4
        rows.append([f"{potential:.16f}", f"{current:.16f}", f"{deviation:.16f}"])
    return _table(header, rows, line_ending="\r\n")


def _has_positive_onset(spec: FixtureSpec, library: str, area: int) -> bool:
    """At least one file per library, so every library exercises both kinds."""
    measured = _seccm_areas(spec, library)
    return bool(measured) and area == measured[0]


def _fit_table(spec: FixtureSpec) -> bytes:
    """One row per LSV, with the limiting current density positive as the source stores it."""
    header = ["Library", "Area", "i_lim [A/cm^2]", "k^0 [cm/s]", "alpha [a.u.]"]
    rows: list[list[str]] = []
    for library in spec.libraries:
        for area in _seccm_areas(spec, library):
            rng = _rng(spec, f"fit:{library}:{area}")
            rows.append(
                [
                    library,
                    str(area),
                    f"{rng.uniform(1.0, 4.0):.16f}",
                    f"{rng.uniform(1e-4, 5e-3):.16f}",
                    f"{rng.uniform(0.20, 0.40):.16f}",
                ]
            )
    return _table(header, rows, line_ending="\n")


def _seccm_areas(spec: FixtureSpec, library: str) -> list[int]:
    """The measured subset. Every other grid area is unavailable, as areas are in the source."""
    rng = _rng(spec, f"seccm-areas:{library}")
    return sorted(rng.sample(range(1, spec.areas_per_library + 1), spec.seccm_areas_per_library))


def _measured_areas(spec: FixtureSpec, library: str) -> list[int]:
    rng = _rng(spec, f"xps-areas:{library}")
    return sorted(rng.sample(range(1, spec.areas_per_library + 1), spec.xps_measured_per_library))


def _coordinate(spec: FixtureSpec, library: str, area: int) -> tuple[float, float]:
    """Stage coordinates for the filename grammar, which encodes them as `x=..._y=...`."""
    rng = _rng(spec, f"xy:{library}:{area}")
    return round(rng.uniform(-40, 40), 2), round(rng.uniform(-40, 40), 2)


def _members(spec: FixtureSpec) -> dict[str, dict[str, bytes]]:
    """Every archive and member, keyed exactly as the recorded filename grammar keys them."""
    edx: dict[str, bytes] = {}
    xps: dict[str, bytes] = {}
    seccm: dict[str, bytes] = {}

    for library in spec.libraries:
        stem = f"Au-Ir-Rh_{library}"
        edx[f"EDX_dataset/{stem}_EDX.csv"] = _grid_table(spec, library, "edx")
        xps[f"XPS_dataset/{stem}_XPS.csv"] = _measured_xps_table(spec, library)
        xps[f"XPS_dataset/{stem}_XPS_predicted.csv"] = _grid_table(spec, library, "predicted")
        for area in _seccm_areas(spec, library):
            x, y = _coordinate(spec, library, area)
            name = f"SECCM_dataset/{stem}_SECCM_area_{area}_x={x:.2f}_y={y:.2f}_LSV.csv"
            seccm[name] = _lsv_table(spec, library, area)
    seccm["SECCM_dataset/LSV_fit_parameters.csv"] = _fit_table(spec)

    return {"EDX_dataset.zip": edx, "SECCM_dataset.zip": seccm, "XPS_dataset.zip": xps}


def _write_archive(path: Path, members: dict[str, bytes]) -> str:
    """Write one zip with fixed member timestamps, so identical input yields identical bytes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, members[name])
    raw = buffer.getvalue()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def build_fixture(root: Path, *, spec: FixtureSpec, generator_version: str) -> FixtureManifest:
    """Write the fixture archives into `root` and return the manifest describing them."""
    root.mkdir(parents=True, exist_ok=True)
    archives = _members(spec)
    written = [
        FixtureArchive(
            filename=name,
            sha256=_write_archive(root / name, members),
            member_count=len(members),
        )
        for name, members in sorted(archives.items())
    ]
    return FixtureManifest(
        generator_version=generator_version,
        seed=spec.seed,
        spec=asdict(spec),
        archives=tuple(written),
    )

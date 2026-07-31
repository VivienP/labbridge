"""One derived metric over a replayed LSV.

**What this deliberately is not.** The standard HER benchmark is the overpotential at a fixed
current density, conventionally 10 mA per square centimetre. It is not implemented here, and the
reason is recorded rather than left as an omission: that benchmark divides a current by an
electrode area, and *which* area the archive's `A/cm^2` refers to - geometric, meniscus/droplet
contact, or ECSA - is not stated in any header and is not in the recorded inventory. Attaching a
benchmark number to an unknown area basis would produce a value that looks comparable across
sources and is not. Closing that needs the preprint or the authors, and until then it is
`Requires domain review`.

What is implemented is descriptive: the most cathodic current density the file actually records, and
the potential at which it occurs. That is a statement about the recorded array, so it survives not
knowing the area basis — but it is *not* an activity benchmark and must never be charted as one.
Two locations' extrema are comparable only if their area bases match, which is exactly what is
unknown, so the metric carries the basis as `unknown` rather than omitting the question.

The analysis is versioned separately from the parser (`docs/SPEC.md` §3.6). Re-running it over the
same observation with the same parameters yields the same `metric_id`, which is what makes
recomputation idempotent rather than duplicative.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from labbridge.domain.canonical import content_id
from labbridge.domain.quantities import UNKNOWN_UNIT, Quantity

#: Bumped whenever the definition below changes. A new version produces new metric ids rather than
#: silently reinterpreting the old ones.
ANALYSIS_VERSION: Final = "1"
ANALYSIS_NAME: Final = "labbridge_lsv_cathodic_extremum"
#: The source's own fitted parameters use a different name, so the two can never merge (§3.6).
SOURCE_FIT_ANALYSIS_NAME: Final = "source_provided_lsv_fit"

METRIC_CURRENT: Final = "cathodic_current_density_extremum"
METRIC_POTENTIAL: Final = "potential_at_cathodic_extremum"

#: Below this the extremum is a statement about noise rather than about a sweep. The archive's LSVs
#: hold 208 or 209 rows, so this rejects a truncated file rather than a short-but-valid one.
MIN_SAMPLES: Final = 50


@dataclass(frozen=True)
class LsvAnalysis:
    """The result of one analysis run, with everything needed to judge whether to trust it."""

    quality_status: str
    quality_reason: str | None
    current_extremum: Quantity | None
    potential_at_extremum: Quantity | None
    sample_count: int
    #: The area the current density is normalised by. Recorded as unknown because the source does
    #: not state it; never guessed, because a wrong basis rescales every value silently.
    area_basis: str = UNKNOWN_UNIT

    @property
    def is_accepted(self) -> bool:
        return self.quality_status == "accepted"


def parameter_hash() -> str:
    """Identity of the parameters this analysis ran with.

    Empty of tunables today, and hashed anyway: when a parameter is added, existing metric ids
    change, which is the behaviour that stops an old result being read as a new one.
    """
    return content_id("params", {"min_samples": MIN_SAMPLES, "version": ANALYSIS_VERSION})


def _decimal(cell: str) -> Decimal | None:
    try:
        value = Decimal(cell)
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def analyse(payload: bytes, *, potential_unit: str, current_unit: str) -> LsvAnalysis:
    """Read an LSV and report its most cathodic recorded point.

    Units are passed in from the observation's recorded descriptors rather than parsed from the
    header here: the header is the ingestion layer's business, and a second place that reads units
    is a second place they can disagree.
    """
    text = payload.decode("utf-8-sig", errors="replace")
    rows = [row for row in csv.reader(io.StringIO(text, newline=None)) if row]
    if len(rows) < 2:  # noqa: PLR2004 - a header and at least one sample
        return _rejected("file holds no data rows", 0)

    samples: list[tuple[Decimal, Decimal]] = []
    malformed = 0
    for row in rows[1:]:
        expected_columns = 2
        if len(row) < expected_columns:
            malformed += 1
            continue
        potential = _decimal(row[0])
        current = _decimal(row[1])
        if potential is None or current is None:
            malformed += 1
            continue
        samples.append((potential, current))

    if len(samples) < MIN_SAMPLES:
        return _rejected(
            f"{len(samples)} usable samples, fewer than the {MIN_SAMPLES} required; "
            f"{malformed} row(s) were malformed",
            len(samples),
        )

    potential, current = min(samples, key=lambda pair: pair[1])
    if current >= 0:
        # A sweep whose most extreme current is not cathodic is not a reduction sweep. Warned
        # rather than rejected: the archive holds files that reach positive values near onset, and
        # discarding them would lose real data (F-023 — a poor but valid signal still succeeded).
        return LsvAnalysis(
            quality_status="warning",
            quality_reason=(
                "the most extreme recorded current density is not cathodic; the sweep may be "
                "truncated before onset"
            ),
            current_extremum=Quantity(value=current, unit=current_unit),
            potential_at_extremum=Quantity(value=potential, unit=potential_unit),
            sample_count=len(samples),
        )

    return LsvAnalysis(
        quality_status="accepted",
        quality_reason=None,
        current_extremum=Quantity(value=current, unit=current_unit),
        potential_at_extremum=Quantity(value=potential, unit=potential_unit),
        sample_count=len(samples),
    )


def _rejected(reason: str, samples: int) -> LsvAnalysis:
    """A rejected metric. The observation it came from stays accepted and retained (F-021)."""
    return LsvAnalysis(
        quality_status="rejected",
        quality_reason=reason,
        current_extremum=None,
        potential_at_extremum=None,
        sample_count=samples,
    )

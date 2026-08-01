"""Invariant 1 and PO-06, enforced rather than documented.

`AI_CONTRACT.md` invariant 1 requires that an adapter *cannot* emit an incompatible origin/mode pair
and that a test prove it. PO-06 requires a record to resolve to exactly one lineage root — enforced
on `Observation` and `DerivedMetric`, since an attempt that read nothing has no root to cite.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from labbridge.domain.identity import ADMISSIBLE_PAIRS, EnvironmentRef
from labbridge.domain.provenance import (
    Provenance,
    RecordRelation,
    SourceRecord,
    SyntheticRoot,
)

ORIGINS = ("observed", "synthetic")
MODES = ("replay", "simulation", "live")
SHA = "a" * 64


def _environment(origin: str = "synthetic", mode: str = "replay") -> EnvironmentRef:
    return EnvironmentRef(
        environment_id="her",
        adapter_version="1",
        data_origin=origin,  # type: ignore[arg-type]
        execution_mode=mode,  # type: ignore[arg-type]
    )


def _source() -> SourceRecord:
    return SourceRecord(
        doi="10.5281/zenodo.9999999",
        record_version="0",
        source_filename="alpha.zip",
        source_sha256=SHA,
        source_path="alpha/table.csv",
        source_type="measured_lsv",
        parsing_version="1",
    )


def _synthetic() -> SyntheticRoot:
    return SyntheticRoot(
        generator="fixture", generator_version="0.1.0", seed=7, config_hash="deadbeef"
    )


@pytest.mark.parametrize(("origin", "mode"), sorted(ADMISSIBLE_PAIRS))
def test_every_admissible_pair_is_constructible(origin: str, mode: str) -> None:
    assert _environment(origin, mode).data_origin == origin


@pytest.mark.parametrize(
    ("origin", "mode"),
    [pair for pair in itertools.product(ORIGINS, MODES) if pair not in ADMISSIBLE_PAIRS],
)
def test_every_inadmissible_pair_is_rejected(origin: str, mode: str) -> None:
    """Enumerated, not sampled: the whole product is covered, so a widening cannot slip through."""
    with pytest.raises(ValidationError, match="inadmissible origin/mode pair"):
        _environment(origin, mode)


def test_observed_simulation_is_the_pair_that_must_never_exist() -> None:
    """Named explicitly because it is the conflation invariant 1 exists to prevent."""
    assert ("observed", "simulation") not in ADMISSIBLE_PAIRS


def test_synthetic_replay_is_admissible_so_a_fixture_run_need_not_lie(  # ADR-010
) -> None:
    assert ("synthetic", "replay") in ADMISSIBLE_PAIRS
    assert _environment("synthetic", "replay").is_synthetic


def test_provenance_with_no_root_is_allowed_only_because_a_failure_read_nothing() -> None:
    """An attempt that timed out, met an unavailable location, or lost its lease has no member to
    cite. Inventing one would put a false path in the record, so the root is required where PO-06
    actually requires it — on an observation and on a derived metric — not on this type."""
    provenance = Provenance(environment=_environment(), code_version="1", config_hash="x")

    assert not provenance.has_root


def test_provenance_with_both_roots_is_rejected() -> None:
    """A record derives from observed data or from generated data, never both (PO-06)."""
    with pytest.raises(ValidationError, match="two roots"):
        Provenance(
            environment=_environment("observed", "replay"),
            source_record=_source(),
            synthetic_root=_synthetic(),
            code_version="1",
            config_hash="x",
        )


def test_a_source_record_root_requires_observed_origin() -> None:
    with pytest.raises(ValidationError, match="requires data_origin=observed"):
        Provenance(
            environment=_environment("synthetic", "replay"),
            source_record=_source(),
            code_version="1",
            config_hash="x",
        )


def test_a_synthetic_root_requires_synthetic_origin() -> None:
    with pytest.raises(ValidationError, match="requires data_origin=synthetic"):
        Provenance(
            environment=_environment("observed", "replay"),
            synthetic_root=_synthetic(),
            code_version="1",
            config_hash="x",
        )


def test_the_fixture_backed_provenance_is_constructible() -> None:
    """The whole point of ADR-010: a fixture run records honestly without a workaround."""
    provenance = Provenance(
        environment=_environment("synthetic", "replay"),
        synthetic_root=_synthetic(),
        code_version="1",
        config_hash="x",
    )

    assert provenance.environment.is_synthetic
    assert provenance.source_record is None


def test_a_relation_cannot_point_at_itself() -> None:
    with pytest.raises(ValidationError, match="to itself"):
        RecordRelation(
            subject_id="obs:1",
            predicate="supersedes",
            object_id="obs:1",
            reason="typo",
            recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_a_relation_requires_a_reason() -> None:
    """An invalidation with no stated reason is not auditable."""
    with pytest.raises(ValidationError):
        RecordRelation(
            subject_id="obs:1",
            predicate="invalidates",
            object_id="obs:2",
            reason="",
            recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

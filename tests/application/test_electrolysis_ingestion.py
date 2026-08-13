from __future__ import annotations

from decimal import Decimal

import pytest

from electrolysis_helpers import (
    auxiliary_source,
    electrolysis_profile,
    electrolysis_profile_with_auxiliary,
    electrolysis_source,
)
from labbridge.application.electrolysis_ingestion import normalise_electrolysis
from labbridge.domain.electrolysis_observations import (
    electrolysis_observation_id,
    electrolysis_series_id,
)

EXPECTED_ROW_COUNT = 3


def test_normalisation_retains_three_electrical_series_and_closed_lineage() -> None:
    result = normalise_electrolysis(
        electrolysis_source(), electrolysis_profile(), producing_version="0.1.0"
    )

    observation = result.observation
    assert observation.technique == "galvanostatic_electrolysis"
    assert observation.row_count == EXPECTED_ROW_COUNT
    assert {series.role for series in observation.series} == {"time", "current", "potential"}
    assert observation.observation_id == electrolysis_observation_id(observation)
    assert result.graph.is_closed is True
    assert (
        observation.provenance.source_artifact_id
        == electrolysis_source().artifact.source_artifact_id
    )
    for series in observation.series:
        assert series.series_id == electrolysis_series_id(
            source_artifact_id=observation.source_artifact_id,
            import_profile_id=observation.import_profile_id,
            schema_version=series.schema_version,
            dtype=series.dtype,
            shape=series.shape,
            source_column=series.source_column,
            role=series.role,
            source_unit=series.source_unit,
            unit=series.unit,
            values=series.values,
        )


def test_normalisation_preserves_explicit_electrical_units_and_values() -> None:
    observation = normalise_electrolysis(
        electrolysis_source(), electrolysis_profile(), producing_version="0.1.0"
    ).observation
    by_role = {series.role: series for series in observation.series}

    assert by_role["time"].unit == "s"
    assert by_role["current"].source_unit == "mA"
    assert by_role["current"].unit == "A"
    assert by_role["current"].values == (Decimal("0.0100"),) * 3
    assert by_role["potential"].unit == "V"
    assert observation.auxiliary_results == ()


def test_structural_findings_do_not_claim_chemical_completeness() -> None:
    result = normalise_electrolysis(
        electrolysis_source(), electrolysis_profile(), producing_version="0.1.0"
    )

    assert {finding.code for finding in result.findings} == {
        "csv.structure.valid",
        "electrolysis.electrical_axes.valid",
        "unit.mapping.valid",
        "lineage.closed",
    }
    assert all("yield" not in finding.message.lower() for finding in result.findings)


def test_auxiliary_result_is_accepted_only_with_matching_retained_source() -> None:
    profile = electrolysis_profile_with_auxiliary()

    with pytest.raises(ValueError, match="retained auxiliary source artifact"):
        normalise_electrolysis(electrolysis_source(), profile, producing_version="0.1.0")

    auxiliary = auxiliary_source()
    result = normalise_electrolysis(
        electrolysis_source(),
        profile,
        producing_version="0.1.0",
        auxiliary_sources={auxiliary.artifact.source_artifact_id: auxiliary},
    )

    assert result.observation.auxiliary_results == profile.auxiliary_results
    assert result.observation.provenance.auxiliary_source_artifact_ids == (
        auxiliary.artifact.source_artifact_id,
    )


def test_auxiliary_result_must_name_the_exact_electrical_source() -> None:
    auxiliary = auxiliary_source()
    profile = electrolysis_profile_with_auxiliary(
        electrical_source_artifact_id="source:another-electrical-record",
        auxiliary=auxiliary,
    )

    with pytest.raises(ValueError, match="electrical source artifact"):
        normalise_electrolysis(
            electrolysis_source(),
            profile,
            producing_version="0.1.0",
            auxiliary_sources={auxiliary.artifact.source_artifact_id: auxiliary},
        )


def test_declared_regular_sampling_must_match_recorded_time_axis() -> None:
    source = electrolysis_source(electrolysis_source().data.replace(b"\n60,", b"\n59,"))

    with pytest.raises(ValueError, match="sampling interval"):
        normalise_electrolysis(source, electrolysis_profile(), producing_version="0.1.0")

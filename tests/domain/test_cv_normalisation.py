from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal

from labbridge.application.cv_ingestion import normalise_cv
from labbridge.application.source_intake import RetrievedSource
from labbridge.domain.cv import (
    ColumnMapping,
    CVImportProfile,
    CVMetadata,
    MetadataValue,
)
from labbridge.domain.source_artifacts import SourceArtifact, source_artifact_id

PAYLOAD = b"sample_index,channel_a,channel_b\n0,-0.240,0.012\n1,0.120,-0.031\n"
EXPECTED_ROWS = 2


def profile(*, potential_role: str = "potential") -> CVImportProfile:
    return CVImportProfile(
        schema_version="1",
        technique="cyclic_voltammetry",
        environment_id="synthetic_cv_fixture",
        encoding="utf-8",
        delimiter=",",
        decimal_convention="point",
        header_row=1,
        missing_value_tokens=("", "NA"),
        columns=(
            ColumnMapping(source_column="sample_index", role="ignored"),
            ColumnMapping(
                source_column="channel_a",
                role=potential_role,
                source_unit="V",
                target_unit="V",
            ),
            ColumnMapping(
                source_column="channel_b",
                role="current",
                source_unit="A",
                target_unit="A",
            ),
        ),
        metadata=CVMetadata(
            reference_scale=MetadataValue(state="unknown"),
            potential_treatment=MetadataValue(state="unknown"),
            current_basis=MetadataValue(state="known", value="current"),
            electrode_role=MetadataValue(state="unknown"),
            geometric_area=MetadataValue(state="unavailable"),
            contact_area=MetadataValue(state="not_applicable"),
            scan_rate=MetadataValue(state="unknown"),
            cycle_information=MetadataValue(state="unavailable"),
        ),
    )


def source() -> RetrievedSource:
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    artifact = SourceArtifact(
        source_artifact_id=source_artifact_id(
            sha256=digest, byte_size=len(PAYLOAD), media_type="text/csv"
        ),
        filename="synthetic-replay-cv-opaque.csv",
        media_type="text/csv",
        byte_size=len(PAYLOAD),
        sha256=digest,
        data_origin="synthetic",
        execution_mode="replay",
        state="committed",
        object_uri=f"s3://test/{digest}",
        created_at=datetime(2026, 8, 12, tzinfo=UTC),
        committed_at=datetime(2026, 8, 12, tzinfo=UTC),
    )
    return RetrievedSource(artifact=artifact, data=PAYLOAD)


def test_normalisation_propagates_source_identity_origin_and_backend_values() -> None:
    result = normalise_cv(source(), profile(), producing_version="0.1.0")

    observation = result.observation
    assert observation.source_artifact_id == source().artifact.source_artifact_id
    assert observation.data_origin == "synthetic"
    assert observation.execution_mode == "replay"
    assert observation.environment_id == "synthetic_cv_fixture"
    assert observation.provenance.environment_id == "synthetic_cv_fixture"
    assert observation.row_count == EXPECTED_ROWS
    assert observation.series[0].role == "potential"
    assert observation.series[0].unit == "V"
    assert observation.series[0].values == (Decimal("-0.240"), Decimal("0.120"))
    assert observation.series[0].schema_version == "1"
    assert observation.series[0].dtype == "decimal"
    assert observation.series[0].shape == (EXPECTED_ROWS,)
    assert observation.series[1].role == "current"
    assert observation.series[1].unit == "A"


def test_transformation_graph_closes_every_value_to_the_source_artifact() -> None:
    result = normalise_cv(source(), profile(), producing_version="0.1.0")

    assert result.graph.source_artifact_id == source().artifact.source_artifact_id
    assert result.graph.observation_id == result.observation.observation_id
    assert result.graph.is_closed
    for series in result.observation.series:
        assert series.source_column
        assert series.source_unit
        assert series.unit
        assert series.transformation_id in result.observation.transformation_ids


def test_same_source_and_canonical_profile_produce_the_same_observation_identity() -> None:
    first = profile()
    reordered = first.model_copy(update={"columns": tuple(reversed(first.columns))})

    first_result = normalise_cv(source(), first, producing_version="0.1.0")
    second_result = normalise_cv(source(), reordered, producing_version="0.1.0")

    assert second_result.observation.observation_id == first_result.observation.observation_id


def test_semantically_unordered_profile_fields_produce_the_same_transformation_graph() -> None:
    first = profile()
    reordered = first.model_copy(
        update={
            "columns": tuple(reversed(first.columns)),
            "missing_value_tokens": tuple(reversed(first.missing_value_tokens)),
        }
    )

    first_result = normalise_cv(source(), first, producing_version="0.1.0")
    second_result = normalise_cv(source(), reordered, producing_version="0.1.0")

    assert second_result.graph == first_result.graph


def test_mapping_change_produces_a_new_observation_for_the_same_source() -> None:
    first = normalise_cv(source(), profile(), producing_version="0.1.0")
    changed_profile = profile().model_copy(
        update={
            "columns": tuple(
                mapping.model_copy(update={"source_unit": "mV"})
                if mapping.source_column == "channel_a"
                else mapping
                for mapping in profile().columns
            )
        }
    )
    changed = normalise_cv(source(), changed_profile, producing_version="0.1.0")

    assert changed.observation.source_artifact_id == first.observation.source_artifact_id
    assert changed.observation.observation_id != first.observation.observation_id
    assert changed.observation.series[0].values == (Decimal("-0.000240"), Decimal("0.000120"))


def test_normalisation_implementation_version_changes_the_observation_identity() -> None:
    first = normalise_cv(source(), profile(), producing_version="0.1.0")
    changed = normalise_cv(source(), profile(), producing_version="0.2.0")

    assert changed.observation.observation_id != first.observation.observation_id


def test_structural_findings_are_bounded_to_phase_2_validity() -> None:
    result = normalise_cv(source(), profile(), producing_version="0.1.0")

    assert [(item.code, item.status) for item in result.findings] == [
        ("csv.structure.valid", "pass"),
        ("cv.axes.valid", "pass"),
        ("unit.mapping.valid", "pass"),
        ("lineage.closed", "pass"),
    ]

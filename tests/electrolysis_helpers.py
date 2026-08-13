from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal

from labbridge.application.source_intake import RetrievedSource
from labbridge.domain.electrolysis import (
    AuxiliaryAnalyticalResult,
    ElectrolysisColumnMapping,
    ElectrolysisImportProfile,
    ElectrolysisMetadata,
    MetadataValue,
    auxiliary_result_id,
)
from labbridge.domain.source_artifacts import SourceArtifact, source_artifact_id

ELECTROLYSIS_PAYLOAD = (
    b"elapsed,applied_current,working_potential\n0,10.0,-0.420\n60,10.0,-0.435\n120,10.0,-0.447\n"
)


def electrolysis_profile() -> ElectrolysisImportProfile:
    return ElectrolysisImportProfile(
        schema_version="1",
        technique="galvanostatic_electrolysis",
        environment_id="synthetic_galvanostatic_fixture",
        encoding="utf-8",
        delimiter=",",
        decimal_convention="point",
        header_row=1,
        missing_value_tokens=("", "NA"),
        columns=(
            ElectrolysisColumnMapping(
                source_column="elapsed", role="time", source_unit="s", target_unit="s"
            ),
            ElectrolysisColumnMapping(
                source_column="applied_current",
                role="current",
                source_unit="mA",
                target_unit="A",
            ),
            ElectrolysisColumnMapping(
                source_column="working_potential",
                role="potential",
                source_unit="V",
                target_unit="V",
            ),
        ),
        metadata=ElectrolysisMetadata(
            current_sign_convention=MetadataValue(state="unknown"),
            current_basis=MetadataValue(state="known", value="total_current"),
            electrode_area=MetadataValue(state="not_applicable"),
            cell_geometry=MetadataValue(state="unknown"),
            reference_scale=MetadataValue(state="unknown"),
            potential_treatment=MetadataValue(state="unknown"),
            sampling_interval=MetadataValue(state="known", value=Decimal("60"), unit="s"),
            interruptions=MetadataValue(state="known", value="none_declared"),
            chemical_analysis=MetadataValue(state="unavailable"),
        ),
        auxiliary_results=(),
    )


def electrolysis_source(payload: bytes = ELECTROLYSIS_PAYLOAD) -> RetrievedSource:
    sha256 = hashlib.sha256(payload).hexdigest()
    artifact = SourceArtifact(
        source_artifact_id=source_artifact_id(
            sha256=sha256,
            byte_size=len(payload),
            media_type="text/csv",
        ),
        filename="synthetic-galvanostatic-electrolysis.csv",
        media_type="text/csv",
        byte_size=len(payload),
        sha256=sha256,
        data_origin="synthetic",
        execution_mode="replay",
        state="committed",
        object_uri=f"s3://test/{sha256}",
        created_at=datetime(2026, 8, 13, tzinfo=UTC),
        committed_at=datetime(2026, 8, 13, tzinfo=UTC),
    )
    return RetrievedSource(artifact=artifact, data=payload)


def auxiliary_source(
    payload: bytes = b"sample,analyte,concentration,unit\nS-01,product_a,0.52,mol/L\n",
) -> RetrievedSource:
    sha256 = hashlib.sha256(payload).hexdigest()
    artifact = SourceArtifact(
        source_artifact_id=source_artifact_id(
            sha256=sha256, byte_size=len(payload), media_type="text/csv"
        ),
        filename="synthetic-auxiliary-qnmr.csv",
        media_type="text/csv",
        byte_size=len(payload),
        sha256=sha256,
        data_origin="synthetic",
        execution_mode="replay",
        state="committed",
        object_uri=f"s3://test/{sha256}",
        created_at=datetime(2026, 8, 13, tzinfo=UTC),
        committed_at=datetime(2026, 8, 13, tzinfo=UTC),
    )
    return RetrievedSource(artifact=artifact, data=payload)


def electrolysis_profile_with_auxiliary(
    *,
    electrical_source_artifact_id: str | None = None,
    auxiliary: RetrievedSource | None = None,
) -> ElectrolysisImportProfile:
    source = auxiliary or auxiliary_source()
    result = AuxiliaryAnalyticalResult(
        result_id="pending",
        electrical_source_artifact_id=(
            electrical_source_artifact_id or electrolysis_source().artifact.source_artifact_id
        ),
        source_artifact_id=source.artifact.source_artifact_id,
        method_name="quantitative NMR",
        method_version="qNMR/1",
        source_location="row:2,column:concentration",
        sample_id="S-01",
        collection_point="post_electrolysis",
        analyte="product_a",
        quantity_kind="concentration",
        value="0.52",
        unit="mol/L",
    )
    result = result.model_copy(update={"result_id": auxiliary_result_id(result)})
    profile = electrolysis_profile().model_dump(mode="python")
    profile["metadata"]["chemical_analysis"] = MetadataValue(
        state="known", value="source_linked_results_declared"
    ).model_dump(mode="python")
    profile["auxiliary_results"] = (result,)
    return ElectrolysisImportProfile.model_validate(profile)

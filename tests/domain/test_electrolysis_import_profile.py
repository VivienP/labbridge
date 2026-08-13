from __future__ import annotations

import pytest

from electrolysis_helpers import electrolysis_profile
from labbridge.domain.electrolysis import (
    AuxiliaryAnalyticalResult,
    ElectrolysisColumnMapping,
    ElectrolysisImportProfile,
    MetadataValue,
    auxiliary_result_id,
    electrolysis_import_profile_id,
)


def test_profile_requires_exact_time_current_and_potential_axes() -> None:
    profile = electrolysis_profile()

    assert {column.role for column in profile.columns} == {"time", "current", "potential"}
    assert electrolysis_import_profile_id(profile).startswith("electrolysis-profile:")

    without_time = profile.model_dump(mode="python")
    without_time["columns"] = tuple(column for column in profile.columns if column.role != "time")
    with pytest.raises(ValueError, match="exactly one time column"):
        ElectrolysisImportProfile.model_validate(without_time)


@pytest.mark.parametrize(
    ("removed_role", "message"),
    [
        ("potential", "exactly one potential column"),
        ("current", "exactly one current or current_density column"),
    ],
)
def test_profile_rejects_each_missing_required_axis(removed_role: str, message: str) -> None:
    profile = electrolysis_profile()
    payload = profile.model_dump(mode="python")
    payload["columns"] = tuple(column for column in profile.columns if column.role != removed_role)

    with pytest.raises(ValueError, match=message):
        ElectrolysisImportProfile.model_validate(payload)


@pytest.mark.parametrize("duplicated_role", ["time", "potential", "current"])
def test_profile_rejects_each_duplicated_required_axis(duplicated_role: str) -> None:
    profile = electrolysis_profile()
    duplicated = next(item for item in profile.columns if item.role == duplicated_role)
    payload = profile.model_dump(mode="python")
    payload["columns"] = (
        *profile.columns,
        duplicated.model_copy(update={"source_column": f"duplicate_{duplicated_role}"}),
    )

    with pytest.raises(ValueError, match="exactly one"):
        ElectrolysisImportProfile.model_validate(payload)


def test_profile_rejects_current_and_current_density_together() -> None:
    profile = electrolysis_profile()
    payload = profile.model_dump(mode="python")
    payload["columns"] = (
        *profile.columns,
        ElectrolysisColumnMapping(
            source_column="current_density",
            role="current_density",
            source_unit="mA/cm^2",
            target_unit="A/m^2",
        ),
    )

    with pytest.raises(ValueError, match="exactly one current or current_density"):
        ElectrolysisImportProfile.model_validate(payload)


def test_scientific_mapping_requires_explicit_source_and_target_units() -> None:
    with pytest.raises(ValueError, match="requires source_unit and target_unit"):
        ElectrolysisColumnMapping(source_column="elapsed", role="time")


def test_auxiliary_result_requires_source_method_version_location_and_unit() -> None:
    result = AuxiliaryAnalyticalResult(
        result_id="pending",
        electrical_source_artifact_id="source-artifact:electrical",
        source_artifact_id="source-artifact:auxiliary",
        method_name="quantitative NMR",
        method_version="qNMR/1",
        source_location="results!B7",
        sample_id="S-01",
        collection_point="post_electrolysis",
        analyte="product_a",
        quantity_kind="concentration",
        value="0.52",
        unit="mol/L",
    )
    result = result.model_copy(update={"result_id": auxiliary_result_id(result)})

    assert result.result_id.startswith("electrolysis-auxiliary-result:")
    with pytest.raises(ValueError):
        AuxiliaryAnalyticalResult.model_validate(
            {**result.model_dump(mode="python"), "method_version": ""}
        )


def test_auxiliary_result_identity_covers_method_and_source() -> None:
    base = AuxiliaryAnalyticalResult(
        result_id="pending",
        electrical_source_artifact_id="source-artifact:electrical",
        source_artifact_id="source-artifact:auxiliary",
        method_name="quantitative NMR",
        method_version="qNMR/1",
        source_location="results!B7",
        sample_id="S-01",
        collection_point="post_electrolysis",
        analyte="product_a",
        quantity_kind="concentration",
        value="0.52",
        unit="mol/L",
    )

    assert auxiliary_result_id(base) != auxiliary_result_id(
        base.model_copy(update={"method_version": "qNMR/2"})
    )
    assert auxiliary_result_id(base) != auxiliary_result_id(
        base.model_copy(update={"source_artifact_id": "source-artifact:other"})
    )


def test_auxiliary_inventory_requires_compatible_chemical_analysis_state() -> None:
    profile = electrolysis_profile()
    result = AuxiliaryAnalyticalResult(
        result_id="pending",
        electrical_source_artifact_id="source-artifact:electrical",
        source_artifact_id="source-artifact:auxiliary",
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

    with pytest.raises(ValueError, match="chemical_analysis"):
        ElectrolysisImportProfile.model_validate(
            {**profile.model_dump(mode="python"), "auxiliary_results": (result,)}
        )


@pytest.mark.parametrize(
    ("role", "source_unit", "target_unit"),
    [
        ("time", "V", "V"),
        ("current", "s", "s"),
        ("current_density", "A", "A"),
        ("potential", "mA", "A"),
    ],
)
def test_scientific_column_roles_reject_dimensionally_wrong_units(
    role: str, source_unit: str, target_unit: str
) -> None:
    with pytest.raises(ValueError, match="units are incompatible with electrolysis role"):
        ElectrolysisColumnMapping(
            source_column="signal",
            role=role,
            source_unit=source_unit,
            target_unit=target_unit,
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("current_sign_convention", "positive_means_better"),
        ("current_basis", "per_magic_area"),
        ("cell_geometry", "usual_cell"),
        ("reference_scale", "standard_reference"),
        ("potential_treatment", "corrected_somehow"),
        ("interruptions", "probably_none"),
        ("chemical_analysis", "complete"),
    ],
)
def test_known_electrolysis_metadata_uses_controlled_values(field_name: str, value: str) -> None:
    profile = electrolysis_profile()
    payload = profile.model_dump(mode="python")
    payload["metadata"][field_name] = MetadataValue(state="known", value=value).model_dump(
        mode="python"
    )

    with pytest.raises(ValueError, match=field_name):
        ElectrolysisImportProfile.model_validate(payload)

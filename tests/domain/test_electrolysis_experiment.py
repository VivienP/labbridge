from __future__ import annotations

import pytest

from electrolysis_helpers import electrolysis_profile, electrolysis_source
from labbridge.application.electrolysis_ingestion import normalise_electrolysis
from labbridge.application.experiments import experiment_from_electrolysis_normalisation
from labbridge.domain.electrolysis import ElectrolysisColumnMapping, MetadataValue
from labbridge.domain.experiments import AssertionValue, add_user_assertion, validate_experiment
from labbridge.evidence.passport import build_passport, render_passport_html


def _experiment():
    result = normalise_electrolysis(
        electrolysis_source(), electrolysis_profile(), producing_version="0.1.0"
    )
    return experiment_from_electrolysis_normalisation(result)


def test_electrolysis_assertions_keep_cv_only_fields_not_applicable() -> None:
    experiment = _experiment()
    resolved = {item.field_name: item for item in experiment.active_assertions}

    assert experiment.technique == "galvanostatic_electrolysis"
    assert resolved["time_axis"].value.state == "known"
    assert resolved["current_axis"].value.unit == "A"
    assert resolved["potential_axis"].value.unit == "V"
    assert resolved["scan_rate"].value.state == "not_applicable"
    assert resolved["cycle_information"].value.state == "not_applicable"


def test_unknown_electrical_context_remains_visible_without_becoming_chemical_blocker() -> None:
    experiment = _experiment()
    validation = validate_experiment(experiment, validation_version="2")

    assert validation.release_decision.status == "eligible"
    assert {finding.field_name for finding in validation.findings} >= {
        "current_sign_convention",
        "cell_geometry",
        "reference_scale",
        "potential_treatment",
        "chemical_analysis",
    }
    assert not any(finding.field_name == "faradaic_efficiency" for finding in validation.findings)


def test_current_density_requires_a_known_area_basis() -> None:
    profile = electrolysis_profile()
    profile = profile.model_copy(
        update={
            "columns": tuple(
                ElectrolysisColumnMapping(
                    source_column=item.source_column,
                    role="current_density",
                    source_unit="mA/cm^2",
                    target_unit="A/m^2",
                )
                if item.role == "current"
                else item
                for item in profile.columns
            ),
            "metadata": profile.metadata.model_copy(
                update={
                    "current_basis": MetadataValue(state="unknown"),
                    "electrode_area": MetadataValue(state="unknown"),
                }
            ),
        }
    )
    result = normalise_electrolysis(electrolysis_source(), profile, producing_version="0.1.0")
    validation = validate_experiment(
        experiment_from_electrolysis_normalisation(result), validation_version="2"
    )

    assert validation.release_decision.status == "blocked"
    finding = next(item for item in validation.findings if item.field_name == "current_basis")
    assert finding.severity == "blocking"
    assert "area basis" in finding.message


def test_current_density_rejects_total_current_and_not_applicable_area() -> None:
    profile = electrolysis_profile()
    payload = profile.model_dump(mode="python")
    payload["columns"] = tuple(
        ElectrolysisColumnMapping(
            source_column=item.source_column,
            role="current_density",
            source_unit="mA/cm^2",
            target_unit="A/m^2",
        )
        if item.role == "current"
        else item
        for item in profile.columns
    )

    with pytest.raises(ValueError, match="current density requires"):
        type(profile).model_validate(payload)


def test_user_assertion_cannot_create_unbacked_auxiliary_result() -> None:
    with pytest.raises(ValueError, match="auxiliary analytical results"):
        add_user_assertion(
            _experiment(),
            expected_version=1,
            field_name="auxiliary_result.unbacked",
            requirement_class="optional",
            transformation="none",
            value=AssertionValue(state="known", value="42", unit="mmol/L"),
            evidence_note="A declaration without a retained analytical source.",
        )


@pytest.mark.parametrize(
    "field_name",
    [
        "source_artifact",
        "observation",
        "time_axis",
        "potential_axis",
        "current_axis",
        "current_quantity_kind",
        "current_sign_convention",
        "current_basis",
        "electrode_area",
        "cell_geometry",
        "reference_scale",
        "potential_treatment",
        "sampling_interval",
        "interruptions",
        "chemical_analysis",
        "scan_rate",
        "cycle_information",
    ],
)
def test_user_assertion_cannot_reclassify_normalised_electrical_semantics(
    field_name: str,
) -> None:
    experiment = _experiment()
    active = next(item for item in experiment.active_assertions if item.field_name == field_name)

    with pytest.raises(ValueError, match="new profile and observation"):
        add_user_assertion(
            experiment,
            expected_version=1,
            field_name=field_name,
            requirement_class=active.requirement_class,
            transformation="none",
            value=AssertionValue(state="known", value="reclassified"),
            evidence_note="An attempted reclassification of retained electrical evidence.",
            supersedes_assertion_id=active.assertion_id,
        )


def test_user_assertion_cannot_shadow_normalised_electrical_semantics() -> None:
    experiment = _experiment()

    with pytest.raises(ValueError, match="new profile and observation"):
        add_user_assertion(
            experiment,
            expected_version=1,
            field_name="current_quantity_kind",
            requirement_class="required",
            transformation="none",
            value=AssertionValue(state="known", value="current_density"),
            evidence_note="An attempted shadow of retained electrical evidence.",
        )


@pytest.mark.parametrize(
    "field_name",
    [
        "faradaic_efficiency",
        "faradaic-efficiency",
        "Faradaic efficiency",
        "conversion",
        "selectivity",
        "yield",
        "product_yield",
    ],
)
def test_unapproved_product_or_efficiency_claim_blocks_release(field_name: str) -> None:
    experiment = add_user_assertion(
        _experiment(),
        expected_version=1,
        field_name=field_name,
        requirement_class="conditional",
        transformation="derived",
        value=AssertionValue(state="known", value="0.90", unit="1"),
        evidence_note="User-provided value without an approved Phase 5 derivation contract.",
    )

    validation = validate_experiment(experiment, validation_version="2")

    assert validation.release_decision.status == "blocked"
    finding = next(item for item in validation.findings if item.field_name == field_name)
    assert finding.severity == "blocking"
    assert "equation" in finding.resolution
    assert "analysis version" in finding.resolution


def test_unapproved_claim_can_be_removed_by_append_only_correction() -> None:
    claimed = add_user_assertion(
        _experiment(),
        expected_version=1,
        field_name="product_yield",
        requirement_class="conditional",
        transformation="derived",
        value=AssertionValue(state="known", value="0.90", unit="1"),
        evidence_note="An unsupported declared product claim.",
    )
    claim = next(item for item in claimed.active_assertions if item.field_name == "product_yield")
    corrected = add_user_assertion(
        claimed,
        expected_version=2,
        field_name="product_yield",
        requirement_class="conditional",
        transformation="none",
        value=AssertionValue(state="unavailable"),
        evidence_note="The unsupported claim is retained in history and no longer active.",
        supersedes_assertion_id=claim.assertion_id,
    )

    validation = validate_experiment(corrected, validation_version="2")

    assert validation.release_decision.status == "eligible"
    assert claim.assertion_id not in corrected.active_assertion_ids
    assert claim.assertion_id in {item.assertion_id for item in corrected.assertions}


def test_passport_separates_electrical_record_from_unavailable_chemical_analysis() -> None:
    experiment = _experiment()
    validation = validate_experiment(experiment, validation_version="2")
    passport = build_passport(
        experiment,
        validation,
        released_at=None,
        release=False,
    )

    report = render_passport_html(passport).decode("utf-8")

    assert "Recorded electrical time series" in report
    assert "Chemical/product quantification: unavailable" in report
    assert "does not report conversion, selectivity, yield, or Faradaic efficiency" in report

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from labbridge.domain.experiments import (
    AssertionValue,
    ConfidenceRepresentation,
    Experiment,
    InferenceDetails,
    MetadataAssertion,
    add_user_assertion,
    create_experiment,
    experiment_id_for_observation,
    make_assertion,
    validate_experiment,
)

SOURCE_ID = "source:97fbafab40e0dde53c902a88f32e5088"
OBSERVATION_ID = "cv-observation:6b9846ff3dfe2a38e2989984c21d450a"
TRANSFORM_ID = "transform:e5c8b9c23624b210ebbd534fc1183fe7"
SUPERSEDING_VERSION = 2
CORRECTED_VERSION = 3
UNKNOWN_FINDING_COUNT = 2


def _source_assertion(
    field_name: str,
    *,
    state: str = "known",
    value: str | Decimal | None = "present",
    unit: str | None = None,
    requirement_class: str = "required",
) -> MetadataAssertion:
    return make_assertion(
        experiment_id=experiment_id_for_observation(OBSERVATION_ID),
        field_name=field_name,
        requirement_class=requirement_class,
        origin="source_file",
        transformation="parsed",
        value=AssertionValue(state=state, value=value, unit=unit),
        evidence_ids=(SOURCE_ID, TRANSFORM_ID),
        evidence_note="Recorded from the retained source through the declared Phase 2 mapping.",
    )


def _experiment(*assertions: MetadataAssertion) -> Experiment:
    return create_experiment(
        observation_id=OBSERVATION_ID,
        source_artifact_id=SOURCE_ID,
        import_profile_id="cv-profile:93b2ab987c861b65a4284876505bd8d5",
        technique="cyclic_voltammetry",
        data_origin="synthetic",
        execution_mode="replay",
        environment_id="synthetic_cv_fixture",
        transformation_ids=(TRANSFORM_ID,),
        assertions=assertions,
    )


def _release_ready_experiment() -> Experiment:
    return _experiment(
        _source_assertion("source_artifact"),
        _source_assertion("observation"),
        _source_assertion("potential_axis", value="channel_a", unit="V"),
        _source_assertion("current_axis", value="channel_b", unit="A"),
        _source_assertion(
            "reference_scale",
            state="unknown",
            value=None,
            requirement_class="conditional",
        ),
        _source_assertion(
            "scan_rate",
            state="unknown",
            value=None,
            requirement_class="recommended",
        ),
    )


def test_assertion_schema_round_trips_without_collapsing_dimensions() -> None:
    assertion = _source_assertion("potential_axis", value="channel_a", unit="V")

    restored = MetadataAssertion.model_validate_json(assertion.model_dump_json())

    assert restored == assertion
    assert restored.origin == "source_file"
    assert restored.transformation == "parsed"
    assert restored.requirement_class == "required"
    assert restored.value.state == "known"


def test_version_one_experiment_and_validation_round_trip_compatibly() -> None:
    experiment = _release_ready_experiment()
    validation = validate_experiment(experiment, validation_version="1")

    restored_experiment = Experiment.model_validate_json(experiment.model_dump_json())
    restored_validation = type(validation).model_validate_json(validation.model_dump_json())

    assert restored_experiment == experiment
    assert restored_validation == validation
    assert restored_experiment.schema_version == "1"
    assert restored_validation.schema_version == "1"


def test_unsupported_future_experiment_schema_fails_closed() -> None:
    payload = _release_ready_experiment().model_dump(mode="json")
    payload["schema_version"] = "2"

    with pytest.raises(ValidationError):
        Experiment.model_validate(payload)


def test_unknown_value_cannot_masquerade_as_known() -> None:
    with pytest.raises(ValidationError, match="unknown metadata carries no value or unit"):
        AssertionValue(state="unknown", value="RHE")


def test_inferred_assertion_requires_method_version_evidence_and_confidence() -> None:
    with pytest.raises(ValidationError, match="inferred assertion requires inference details"):
        make_assertion(
            experiment_id="experiment:one",
            field_name="reference_scale",
            requirement_class="conditional",
            origin="inferred",
            transformation="derived",
            value=AssertionValue(state="known", value="RHE"),
            evidence_ids=(SOURCE_ID,),
            evidence_note="Header convention was inspected.",
        )

    inferred = make_assertion(
        experiment_id="experiment:one",
        field_name="reference_scale",
        requirement_class="conditional",
        origin="inferred",
        transformation="derived",
        value=AssertionValue(state="known", value="RHE"),
        evidence_ids=(SOURCE_ID,),
        evidence_note="Header convention was inspected.",
        inference=InferenceDetails(
            method="declared_header_rule",
            method_version="1",
            evidence="The retained source contains an explicit reference-scale declaration.",
            confidence=ConfidenceRepresentation(kind="probability", value=Decimal("0.95")),
        ),
    )

    assert inferred.inference is not None
    assert inferred.inference.method_version == "1"


@pytest.mark.parametrize("origin", ["source_file", "inferred"])
def test_user_edit_cannot_select_protected_origin(origin: str) -> None:
    experiment = _release_ready_experiment()

    with pytest.raises(ValueError, match="user edits always have origin=user_supplied"):
        add_user_assertion(
            experiment,
            expected_version=1,
            field_name="reference_scale",
            requirement_class="conditional",
            transformation="none",
            value=AssertionValue(state="known", value="RHE"),
            evidence_note="Declared by the operator for this experiment.",
            requested_origin=origin,
            supplements_assertion_id=next(
                item.assertion_id
                for item in experiment.assertions
                if item.field_name == "reference_scale"
            ),
        )


def test_user_supplement_preserves_source_assertion_and_prior_version() -> None:
    initial = _release_ready_experiment()
    source = next(item for item in initial.assertions if item.field_name == "reference_scale")

    supplemented = add_user_assertion(
        initial,
        expected_version=1,
        field_name="reference_scale",
        requirement_class="conditional",
        transformation="none",
        value=AssertionValue(state="known", value="RHE"),
        evidence_note="Declared by the operator for this experiment.",
        supplements_assertion_id=source.assertion_id,
    )

    assert initial.version == 1
    assert initial.assertions == _release_ready_experiment().assertions
    assert supplemented.version == SUPERSEDING_VERSION
    assert source in supplemented.assertions
    user = supplemented.assertions[-1]
    assert user.origin == "user_supplied"
    assert user.supplements_assertion_id == source.assertion_id
    assert user.supersedes_assertion_id is None


def test_user_correction_supersedes_only_the_active_user_assertion() -> None:
    initial = _release_ready_experiment()
    source = next(item for item in initial.assertions if item.field_name == "reference_scale")
    supplemented = add_user_assertion(
        initial,
        expected_version=1,
        field_name="reference_scale",
        requirement_class="conditional",
        transformation="none",
        value=AssertionValue(state="known", value="RHE"),
        evidence_note="Initial operator declaration.",
        supplements_assertion_id=source.assertion_id,
    )
    first_user = supplemented.assertions[-1]

    corrected = add_user_assertion(
        supplemented,
        expected_version=2,
        field_name="reference_scale",
        requirement_class="conditional",
        transformation="none",
        value=AssertionValue(state="known", value="Ag/AgCl"),
        evidence_note="Corrected operator declaration.",
        supplements_assertion_id=source.assertion_id,
        supersedes_assertion_id=first_user.assertion_id,
    )

    assert corrected.version == CORRECTED_VERSION
    assert corrected.assertions[-1].supersedes_assertion_id == first_user.assertion_id
    assert source.assertion_id in corrected.active_assertion_ids
    assert first_user.assertion_id not in corrected.active_assertion_ids
    assert corrected.assertions[-1].assertion_id in corrected.active_assertion_ids


def test_user_correction_cannot_supersede_source_or_use_stale_version() -> None:
    initial = _release_ready_experiment()
    source = next(item for item in initial.assertions if item.field_name == "reference_scale")

    with pytest.raises(ValueError, match="source-file assertions are immutable"):
        add_user_assertion(
            initial,
            expected_version=1,
            field_name="reference_scale",
            requirement_class="conditional",
            transformation="none",
            value=AssertionValue(state="known", value="RHE"),
            evidence_note="Operator declaration.",
            supersedes_assertion_id=source.assertion_id,
        )

    with pytest.raises(ValueError, match="expected experiment version 2, found 1"):
        add_user_assertion(
            initial,
            expected_version=2,
            field_name="reference_scale",
            requirement_class="conditional",
            transformation="none",
            value=AssertionValue(state="known", value="RHE"),
            evidence_note="Operator declaration.",
            supplements_assertion_id=source.assertion_id,
        )


def test_validation_keeps_warnings_and_unknowns_visible_without_scalar_score() -> None:
    validation = validate_experiment(_release_ready_experiment(), validation_version="1")

    assert validation.release_decision.status == "eligible"
    assert validation.release_decision.blocking_count == 0
    assert validation.release_decision.unknown_count == UNKNOWN_FINDING_COUNT
    assert {finding.severity for finding in validation.findings} == {"unknown"}
    assert {finding.field_name for finding in validation.findings} == {
        "reference_scale",
        "scan_rate",
    }
    assert not hasattr(validation.release_decision, "score")


def test_missing_required_assertion_blocks_release_deterministically() -> None:
    experiment = _experiment(
        _source_assertion("source_artifact"),
        _source_assertion("observation"),
        _source_assertion("potential_axis", value="channel_a", unit="V"),
    )

    first = validate_experiment(experiment, validation_version="1")
    second = validate_experiment(experiment, validation_version="1")

    assert first == second
    assert first.release_decision.status == "blocked"
    assert first.release_decision.blocking_count == 1
    assert first.findings[0].field_name == "current_axis"
    assert first.findings[0].severity == "blocking"
    assert "Append a user-supplied current_axis assertion" in first.findings[0].resolution

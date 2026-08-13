from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any, cast

import pytest
from pydantic.warnings import PydanticDeprecatedSince211

from cv_helpers import gamry_dta_profile, gamry_dta_source
from labbridge.application.cv_ingestion import normalise_cv
from labbridge.application.experiments import experiment_from_normalisation
from labbridge.domain.experiments import (
    AssertionValue,
    ConfidenceRepresentation,
    Experiment,
    InferenceDetails,
    add_user_assertion,
    make_assertion,
)
from labbridge.evidence.echemdb_exchange import (
    EchemDBExchangeError,
    build_cv_exchange,
    required_mapping_paths,
    round_trip_series,
    validate_exchange,
    validate_mapping_entries,
)

pytestmark = pytest.mark.filterwarnings(
    f"error::{PydanticDeprecatedSince211.__module__}.{PydanticDeprecatedSince211.__name__}"
)


def _normalisation():
    source = gamry_dta_source()
    result = normalise_cv(
        source,
        gamry_dta_profile(),
        producing_version="0.1.0",
        source_format="gamry_dta",
    )
    return source, result


def _with_exchange_assertions(
    experiment: Experiment,
    *,
    omit: str | None = None,
    unknown: str | None = None,
) -> Experiment:
    values = {
        "exchange.source.citation_key": "labbridge_synthetic_gamry_cv",
        "exchange.source.url": (
            "https://github.com/VivienP/labbridge/blob/"
            "14921d701e512bb70816e55c844293f90e6bb8d0/"
            "fixtures/source/synthetic-gamry-cv.dta"
        ),
        "exchange.system.type": "electrochemical",
        "exchange.electrolyte.type": "aqueous",
        "exchange.figure.type": "simulated",
        "exchange.measurement_type": "CV",
        "exchange.curation.process": "empty",
        "exchange.experimental": "empty",
        "exchange.electrodes": "empty",
    }
    current = experiment
    for field_name, value in values.items():
        if field_name == omit:
            continue
        assertion_value = (
            AssertionValue(state="unknown")
            if field_name == unknown
            else AssertionValue(state="known", value=value)
        )
        current = add_user_assertion(
            current,
            expected_version=current.version,
            field_name=field_name,
            requirement_class="required",
            transformation="none",
            value=assertion_value,
            evidence_note=(
                "Explicit metadata for the project-owned synthetic exchange fixture; it is not "
                "declared by the DTA source."
            ),
        )
    return current


def test_required_echemdb_metadata_cannot_be_defaulted_or_inferred() -> None:
    source, normalisation = _normalisation()
    experiment = experiment_from_normalisation(normalisation)

    with pytest.raises(EchemDBExchangeError) as raised:
        build_cv_exchange(
            experiment=experiment,
            observation=normalisation.observation,
            source_artifact=source.artifact,
        )

    assert raised.value.code == "echemdb_required_assertion_missing"
    assert raised.value.field_name == "exchange.source.citation_key"


def test_required_echemdb_metadata_must_be_known_user_assertions() -> None:
    source, normalisation = _normalisation()
    experiment = _with_exchange_assertions(
        experiment_from_normalisation(normalisation),
        unknown="exchange.electrolyte.type",
    )

    with pytest.raises(EchemDBExchangeError) as raised:
        build_cv_exchange(
            experiment=experiment,
            observation=normalisation.observation,
            source_artifact=source.artifact,
        )

    assert raised.value.code == "echemdb_required_assertion_unknown"
    assert raised.value.field_name == "exchange.electrolyte.type"


def test_required_empty_structures_also_require_explicit_user_assertions() -> None:
    source, normalisation = _normalisation()
    experiment = _with_exchange_assertions(
        experiment_from_normalisation(normalisation),
        omit="exchange.electrodes",
    )

    with pytest.raises(EchemDBExchangeError) as raised:
        build_cv_exchange(
            experiment=experiment,
            observation=normalisation.observation,
            source_artifact=source.artifact,
        )

    assert raised.value.code == "echemdb_required_assertion_missing"
    assert raised.value.field_name == "exchange.electrodes"


def test_required_echemdb_metadata_rejects_non_user_origin() -> None:
    source, normalisation = _normalisation()
    experiment = _with_exchange_assertions(experiment_from_normalisation(normalisation))
    assertion = next(
        item for item in experiment.assertions if item.field_name == "exchange.source.citation_key"
    )
    forged = assertion.model_copy(update={"origin": "source_file"})
    experiment = experiment.model_copy(
        update={
            "assertions": tuple(
                forged if item.assertion_id == assertion.assertion_id else item
                for item in experiment.assertions
            )
        }
    )

    with pytest.raises(EchemDBExchangeError) as raised:
        build_cv_exchange(
            experiment=experiment,
            observation=normalisation.observation,
            source_artifact=source.artifact,
        )

    assert raised.value.code == "echemdb_required_assertion_origin"
    assert raised.value.field_name == "exchange.source.citation_key"


def test_inferred_value_cannot_be_exported_as_external_metadata() -> None:
    source, normalisation = _normalisation()
    experiment = _with_exchange_assertions(experiment_from_normalisation(normalisation))
    user_assertion = next(
        item for item in experiment.assertions if item.field_name == "exchange.measurement_type"
    )
    inferred = make_assertion(
        experiment_id=experiment.experiment_id,
        field_name="exchange.measurement_type",
        requirement_class="optional",
        origin="inferred",
        transformation="derived",
        value=AssertionValue(state="known", value="CV"),
        evidence_ids=(experiment.observation_id,),
        evidence_note="Test-only inference that must not cross the exchange boundary.",
        inference=InferenceDetails(
            method="test-classifier",
            method_version="1",
            evidence=experiment.observation_id,
            confidence=ConfidenceRepresentation(kind="probability", value=Decimal("1")),
        ),
    )
    experiment = experiment.model_copy(
        update={
            "assertions": tuple(
                inferred if item.assertion_id == user_assertion.assertion_id else item
                for item in experiment.assertions
            ),
            "active_assertion_ids": tuple(
                inferred.assertion_id if item == user_assertion.assertion_id else item
                for item in experiment.active_assertion_ids
            ),
        }
    )

    with pytest.raises(EchemDBExchangeError) as raised:
        build_cv_exchange(
            experiment=experiment,
            observation=normalisation.observation,
            source_artifact=source.artifact,
        )

    assert raised.value.code == "echemdb_assertion_not_exportable"
    assert raised.value.field_name == "exchange.measurement_type"


def test_exchange_traces_descriptor_values_and_rows_to_assertions_or_observation() -> None:
    source, normalisation = _normalisation()
    experiment = _with_exchange_assertions(experiment_from_normalisation(normalisation))

    exchange = build_cv_exchange(
        experiment=experiment,
        observation=normalisation.observation,
        source_artifact=source.artifact,
    )

    assert exchange.descriptor["$schema"] == (
        "https://datapackage.org/profiles/2.0/datapackage.json"
    )
    resource = exchange.descriptor["resources"][0]
    echemdb = resource["metadata"]["echemdb"]
    assert echemdb["echemdbSchemaVersion"] == "0.8.3"
    assert echemdb["system"]["electrolyte"]["type"] == "aqueous"
    assert echemdb["figureDescription"]["measurementType"] == "CV"
    assert resource["schema"]["fields"] == [
        {"name": "Vf", "type": "number", "unit": "V"},
        {"name": "Im", "type": "number", "unit": "A"},
        {"name": "T", "type": "number", "unit": "s"},
        {"name": "Cycle", "type": "number", "unit": "1"},
    ]
    assert all(trace.source_kind in {"assertion", "observation"} for trace in exchange.traces)
    assert len({trace.external_path for trace in exchange.traces}) == len(exchange.traces)
    assert exchange.report.untraced_exported_paths == ()
    assert exchange.report.mapping_collisions == ()
    assert b"Vf,Im,T,Cycle" in exchange.csv_bytes


def test_unknown_metadata_is_omitted_and_every_omission_is_machine_visible() -> None:
    source, normalisation = _normalisation()
    exchange = build_cv_exchange(
        experiment=_with_exchange_assertions(experiment_from_normalisation(normalisation)),
        observation=normalisation.observation,
        source_artifact=source.artifact,
    )

    omitted = {
        item.labbridge_path: (item.status, item.value_state)
        for item in exchange.report.entries
        if item.status == "omitted"
    }
    assert omitted == {
        f"observation.metadata.{field_name}": ("omitted", "unknown")
        for field_name in (
            "reference_scale",
            "potential_treatment",
            "current_basis",
            "electrode_role",
            "geometric_area",
            "contact_area",
            "scan_rate",
            "cycle_information",
        )
    }
    echemdb = exchange.descriptor["resources"][0]["metadata"]["echemdb"]
    assert "scanRate" not in echemdb["figureDescription"]
    assert echemdb["system"]["electrodes"] == []


def test_semantic_fixture_classifications_have_machine_visible_review_disposition() -> None:
    source, normalisation = _normalisation()
    exchange = build_cv_exchange(
        experiment=_with_exchange_assertions(experiment_from_normalisation(normalisation)),
        observation=normalisation.observation,
        source_artifact=source.artifact,
    )

    semantic_paths = {
        "experiment.assertions[exchange.system.type]",
        "experiment.assertions[exchange.electrolyte.type]",
        "experiment.assertions[exchange.measurement_type]",
        "experiment.assertions[exchange.electrodes]",
        "observation.data_origin + observation.execution_mode",
    }
    reviewed = {
        item.labbridge_path: (item.semantic_review, item.semantic_review_note)
        for item in exchange.report.entries
        if item.labbridge_path in semantic_paths
    }

    assert set(reviewed) == semantic_paths
    assert all(status == "fixture_declaration" for status, _ in reviewed.values())
    assert all(note and "not" in note for _, note in reviewed.values())


def test_mapping_completeness_rejects_external_path_collisions() -> None:
    source, normalisation = _normalisation()
    exchange = build_cv_exchange(
        experiment=_with_exchange_assertions(experiment_from_normalisation(normalisation)),
        observation=normalisation.observation,
        source_artifact=source.artifact,
    )
    mapped = [item for item in exchange.report.entries if item.external_path is not None]
    colliding = (
        *mapped,
        mapped[0].model_copy(update={"labbridge_path": "forged", "source_id": "forged"}),
    )

    with pytest.raises(EchemDBExchangeError) as raised:
        validate_mapping_entries(colliding)

    assert raised.value.code == "echemdb_mapping_collision"


def test_mapping_completeness_rejects_missing_internal_field() -> None:
    source, normalisation = _normalisation()
    experiment = _with_exchange_assertions(experiment_from_normalisation(normalisation))
    exchange = build_cv_exchange(
        experiment=experiment,
        observation=normalisation.observation,
        source_artifact=source.artifact,
    )
    required = required_mapping_paths(experiment, normalisation.observation)
    incomplete = tuple(
        item
        for item in exchange.report.entries
        if item.labbridge_path != "experiment.schema_version"
    )

    with pytest.raises(EchemDBExchangeError) as raised:
        validate_mapping_entries(incomplete, required_labbridge_paths=required)

    assert raised.value.code == "echemdb_mapping_incomplete"
    assert raised.value.field_name == "experiment.schema_version"


def test_generated_package_validates_against_exact_pinned_external_versions() -> None:
    source, normalisation = _normalisation()
    exchange = build_cv_exchange(
        experiment=_with_exchange_assertions(experiment_from_normalisation(normalisation)),
        observation=normalisation.observation,
        source_artifact=source.artifact,
    )

    validation = validate_exchange(exchange)

    assert validation.valid is True
    assert validation.echemdb_schema_valid is True
    assert validation.data_package_profile_valid is True
    assert validation.frictionless_valid is True
    assert validation.versions == {
        "echemdb_metadata_schema": "0.8.3",
        "data_package_profile": "2.0",
        "frictionless": "5.19.0",
        "jsonschema": "4.26.0",
        "referencing": "0.37.0",
    }
    assert validation.errors == ()


def test_pinned_schema_rejects_invalid_exported_semantics() -> None:
    source, normalisation = _normalisation()
    exchange = build_cv_exchange(
        experiment=_with_exchange_assertions(experiment_from_normalisation(normalisation)),
        observation=normalisation.observation,
        source_artifact=source.artifact,
    )
    descriptor = copy.deepcopy(exchange.descriptor)
    resources = cast(list[dict[str, Any]], descriptor["resources"])
    resources[0]["metadata"]["echemdb"]["figureDescription"]["type"] = "invented"

    validation = validate_exchange(exchange.model_copy(update={"descriptor": descriptor}))

    assert validation.valid is False
    assert validation.echemdb_schema_valid is False
    assert any("invented" in item for item in validation.errors)


def test_round_trip_preserves_series_values_units_roles_and_identities() -> None:
    source, normalisation = _normalisation()
    exchange = build_cv_exchange(
        experiment=_with_exchange_assertions(experiment_from_normalisation(normalisation)),
        observation=normalisation.observation,
        source_artifact=source.artifact,
    )

    restored = round_trip_series(exchange)

    assert [item.series_id for item in restored] == [
        item.series_id for item in normalisation.observation.series
    ]
    assert [item.values for item in restored] == [
        tuple(Decimal(value) for value in item.values) for item in normalisation.observation.series
    ]
    assert [item.unit for item in restored] == [
        item.unit for item in normalisation.observation.series
    ]
    assert [item.role for item in restored] == [
        item.role for item in normalisation.observation.series
    ]


def test_origin_and_execution_mode_projection_is_explicitly_lossy() -> None:
    source, normalisation = _normalisation()
    exchange = build_cv_exchange(
        experiment=_with_exchange_assertions(experiment_from_normalisation(normalisation)),
        observation=normalisation.observation,
        source_artifact=source.artifact,
    )

    lossy = [item for item in exchange.report.entries if item.status == "lossy"]
    assert [(item.labbridge_path, item.external_path) for item in lossy] == [
        (
            "observation.data_origin + observation.execution_mode",
            "/resources/0/metadata/echemdb/figureDescription/type",
        )
    ]
    assert lossy[0].loss_reason == (
        "EchemDB figureDescription.type cannot represent LabBridge data_origin and "
        "execution_mode as independent dimensions."
    )
    assert exchange.provenance["data_origin"] == "synthetic"
    assert exchange.provenance["execution_mode"] == "replay"

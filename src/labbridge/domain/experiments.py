"""Versioned experiments, metadata assertions, and deterministic release validation."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical import content_id
from .identity import DataOrigin, ExecutionMode

AssertionOrigin = Literal["source_file", "user_supplied", "inferred"]
AssertionTransformation = Literal["none", "parsed", "unit_converted", "derived"]
RequirementClass = Literal["required", "conditional", "recommended", "optional"]
ValueState = Literal["known", "unknown", "unavailable", "not_applicable"]
FindingSeverity = Literal["blocking", "warning", "unknown"]
ReleaseStatus = Literal["blocked", "eligible"]
Technique = Literal["cyclic_voltammetry", "galvanostatic_electrolysis"]

EXPERIMENT_SCHEMA_VERSION = "1"
ASSERTION_SCHEMA_VERSION = "1"
VALIDATION_SCHEMA_VERSION = "1"


class ExperimentVersionConflictError(ValueError):
    code = "experiment_version_conflict"

    def __init__(self, expected_version: int, current_version: int) -> None:
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(f"expected experiment version {expected_version}, found {current_version}")


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AssertionValue(_Model):
    """A metadata value whose evidence state remains explicit and queryable."""

    state: ValueState
    value: Decimal | str | bool | int | None = None
    unit: str | None = None

    @model_validator(mode="after")
    def _state_matches_value(self) -> Self:
        if self.state == "known" and self.value is None:
            raise ValueError("known metadata requires a value")
        if self.state != "known" and (self.value is not None or self.unit is not None):
            raise ValueError(f"{self.state} metadata carries no value or unit")
        if self.unit is not None and not self.unit:
            raise ValueError("metadata unit cannot be blank")
        if self.state == "known" and isinstance(self.value, Decimal) and self.unit is None:
            raise ValueError("known numeric metadata requires an explicit unit")
        return self


class ConfidenceRepresentation(_Model):
    """The declared representation used for one inference confidence statement."""

    kind: Literal["probability", "qualitative", "interval"]
    value: Decimal | str

    @model_validator(mode="after")
    def _probability_is_bounded(self) -> Self:
        if self.kind == "probability":
            if not isinstance(self.value, Decimal):
                raise ValueError("probability confidence requires a decimal value")
            if not Decimal("0") <= self.value <= Decimal("1"):
                raise ValueError("probability confidence must be between zero and one")
        return self


class InferenceDetails(_Model):
    """Method and evidence required before an assertion may be called inferred."""

    method: str = Field(min_length=1)
    method_version: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    confidence: ConfidenceRepresentation


class MetadataAssertion(_Model):
    """One immutable claim about one experiment metadata field."""

    assertion_id: str = Field(min_length=1)
    schema_version: Literal["1"]
    experiment_id: str = Field(min_length=1)
    field_name: str = Field(min_length=1)
    requirement_class: RequirementClass
    origin: AssertionOrigin
    transformation: AssertionTransformation
    value: AssertionValue
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    evidence_note: str = Field(min_length=1)
    inference: InferenceDetails | None = None
    supplements_assertion_id: str | None = None
    supersedes_assertion_id: str | None = None

    @model_validator(mode="after")
    def _origin_contract(self) -> Self:
        if self.origin == "inferred" and self.inference is None:
            raise ValueError("inferred assertion requires inference details")
        if self.origin != "inferred" and self.inference is not None:
            raise ValueError("only an inferred assertion may carry inference details")
        if self.assertion_id in {
            self.supplements_assertion_id,
            self.supersedes_assertion_id,
        }:
            raise ValueError("an assertion cannot supplement or supersede itself")
        return self


def experiment_id_for_observation(observation_id: str) -> str:
    """Derive one stable experiment aggregate identity from one Phase 2 observation."""
    return content_id("experiment", {"observation_id": observation_id})


def make_assertion(
    *,
    experiment_id: str,
    field_name: str,
    requirement_class: RequirementClass,
    origin: AssertionOrigin,
    transformation: AssertionTransformation,
    value: AssertionValue,
    evidence_ids: tuple[str, ...],
    evidence_note: str,
    inference: InferenceDetails | None = None,
    supplements_assertion_id: str | None = None,
    supersedes_assertion_id: str | None = None,
) -> MetadataAssertion:
    """Construct an assertion whose identity covers every scientific and lineage dimension."""
    body = {
        "schema_version": ASSERTION_SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "field_name": field_name,
        "requirement_class": requirement_class,
        "origin": origin,
        "transformation": transformation,
        "value": value,
        "evidence_ids": evidence_ids,
        "evidence_note": evidence_note,
        "inference": inference,
        "supplements_assertion_id": supplements_assertion_id,
        "supersedes_assertion_id": supersedes_assertion_id,
    }
    return MetadataAssertion(
        assertion_id=content_id("assertion", body),
        schema_version=ASSERTION_SCHEMA_VERSION,
        experiment_id=experiment_id,
        field_name=field_name,
        requirement_class=requirement_class,
        origin=origin,
        transformation=transformation,
        value=value,
        evidence_ids=evidence_ids,
        evidence_note=evidence_note,
        inference=inference,
        supplements_assertion_id=supplements_assertion_id,
        supersedes_assertion_id=supersedes_assertion_id,
    )


class Experiment(_Model):
    """One immutable version of an experiment aggregate."""

    experiment_id: str = Field(min_length=1)
    schema_version: Literal["1"]
    version: int = Field(ge=1)
    observation_id: str = Field(min_length=1)
    source_artifact_id: str = Field(min_length=1)
    import_profile_id: str = Field(min_length=1)
    technique: Technique
    data_origin: DataOrigin
    execution_mode: ExecutionMode
    environment_id: str = Field(min_length=1)
    transformation_ids: tuple[str, ...] = Field(min_length=1)
    assertions: tuple[MetadataAssertion, ...]
    active_assertion_ids: tuple[str, ...]
    supersedes_version: int | None = None

    @model_validator(mode="after")
    def _assertions_belong_to_experiment(self) -> Self:
        ids = [assertion.assertion_id for assertion in self.assertions]
        if len(ids) != len(set(ids)):
            raise ValueError("experiment contains duplicate assertions")
        if any(assertion.experiment_id != self.experiment_id for assertion in self.assertions):
            raise ValueError("every assertion must belong to its experiment")
        if not set(self.active_assertion_ids).issubset(ids):
            raise ValueError("active assertions must exist in the experiment history")
        if self.version == 1 and self.supersedes_version is not None:
            raise ValueError("the initial experiment version supersedes no version")
        if self.version > 1 and self.supersedes_version != self.version - 1:
            raise ValueError("an experiment version must supersede its immediate predecessor")
        return self

    @property
    def active_assertions(self) -> tuple[MetadataAssertion, ...]:
        active = set(self.active_assertion_ids)
        return tuple(assertion for assertion in self.assertions if assertion.assertion_id in active)


def create_experiment(
    *,
    observation_id: str,
    source_artifact_id: str,
    import_profile_id: str,
    technique: Technique,
    data_origin: DataOrigin,
    execution_mode: ExecutionMode,
    environment_id: str,
    transformation_ids: tuple[str, ...],
    assertions: tuple[MetadataAssertion, ...],
) -> Experiment:
    experiment_id = experiment_id_for_observation(observation_id)
    return Experiment(
        experiment_id=experiment_id,
        schema_version=EXPERIMENT_SCHEMA_VERSION,
        version=1,
        observation_id=observation_id,
        source_artifact_id=source_artifact_id,
        import_profile_id=import_profile_id,
        technique=technique,
        data_origin=data_origin,
        execution_mode=execution_mode,
        environment_id=environment_id,
        transformation_ids=transformation_ids,
        assertions=assertions,
        active_assertion_ids=tuple(assertion.assertion_id for assertion in assertions),
    )


def add_user_assertion(
    experiment: Experiment,
    *,
    expected_version: int,
    field_name: str,
    requirement_class: RequirementClass,
    transformation: AssertionTransformation,
    value: AssertionValue,
    evidence_note: str,
    requested_origin: AssertionOrigin = "user_supplied",
    supplements_assertion_id: str | None = None,
    supersedes_assertion_id: str | None = None,
) -> Experiment:
    """Append a user supplement or correction without rewriting an earlier assertion."""
    if expected_version != experiment.version:
        raise ExperimentVersionConflictError(expected_version, experiment.version)
    if requested_origin != "user_supplied":
        raise ValueError("user edits always have origin=user_supplied")
    if experiment.technique == "galvanostatic_electrolysis" and field_name.startswith(
        "auxiliary_result."
    ):
        raise ValueError(
            "auxiliary analytical results must enter through retained source-linked records"
        )
    if (
        experiment.technique == "galvanostatic_electrolysis"
        and field_name in _ELECTROLYSIS_ASSERTION_FIELDS
    ):
        raise ValueError("electrolysis profile semantics require a new profile and observation")
    by_id = {assertion.assertion_id: assertion for assertion in experiment.assertions}
    if supplements_assertion_id is not None:
        supplemented = by_id.get(supplements_assertion_id)
        if supplemented is None or supplemented.field_name != field_name:
            raise ValueError("supplement must name an assertion for the same field")
    active_ids = list(experiment.active_assertion_ids)
    if supersedes_assertion_id is not None:
        superseded = by_id.get(supersedes_assertion_id)
        if superseded is None or supersedes_assertion_id not in active_ids:
            raise ValueError("a correction must supersede an active assertion")
        if superseded.origin == "source_file":
            raise ValueError("source-file assertions are immutable")
        if superseded.origin != "user_supplied" or superseded.field_name != field_name:
            raise ValueError("a user correction may supersede only a user assertion for the field")
        active_ids.remove(supersedes_assertion_id)
    assertion = make_assertion(
        experiment_id=experiment.experiment_id,
        field_name=field_name,
        requirement_class=requirement_class,
        origin="user_supplied",
        transformation=transformation,
        value=value,
        evidence_ids=(experiment.experiment_id,),
        evidence_note=evidence_note,
        supplements_assertion_id=supplements_assertion_id,
        supersedes_assertion_id=supersedes_assertion_id,
    )
    active_ids.append(assertion.assertion_id)
    return experiment.model_copy(
        update={
            "version": experiment.version + 1,
            "assertions": (*experiment.assertions, assertion),
            "active_assertion_ids": tuple(active_ids),
            "supersedes_version": experiment.version,
        }
    )


class ValidationFinding(_Model):
    """One stable validation result shared by every report representation."""

    finding_id: str = Field(min_length=1)
    schema_version: Literal["1"]
    code: str = Field(min_length=1)
    severity: FindingSeverity
    field_name: str = Field(min_length=1)
    requirement_class: RequirementClass
    assertion_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    message: str = Field(min_length=1)
    resolution: str = Field(min_length=1)


class ReleaseDecision(_Model):
    status: ReleaseStatus
    blocking_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    unknown_count: int = Field(ge=0)
    finding_ids: tuple[str, ...]


class ValidationRun(_Model):
    validation_id: str = Field(min_length=1)
    schema_version: Literal["1"]
    validation_version: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    experiment_version: int = Field(ge=1)
    findings: tuple[ValidationFinding, ...]
    release_decision: ReleaseDecision


_REQUIRED_FIELDS = {
    "cyclic_voltammetry": (
        "source_artifact",
        "observation",
        "potential_axis",
        "current_axis",
    ),
    "galvanostatic_electrolysis": (
        "source_artifact",
        "observation",
        "time_axis",
        "potential_axis",
        "current_axis",
        "current_quantity_kind",
    ),
}
_ELECTROLYSIS_ASSERTION_FIELDS = {
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
}
_ELECTROLYSIS_AREA_BASES = {
    "geometric_area",
    "electrochemically_active_area",
    "contact_or_wetted_area",
}
_ORIGIN_PRIORITY = {"source_file": 0, "inferred": 1, "user_supplied": 2}


def _resolved_by_field(experiment: Experiment) -> dict[str, MetadataAssertion]:
    resolved: dict[str, MetadataAssertion] = {}
    for assertion in experiment.active_assertions:
        current = resolved.get(assertion.field_name)
        if (
            current is None
            or _ORIGIN_PRIORITY[assertion.origin] >= _ORIGIN_PRIORITY[current.origin]
        ):
            resolved[assertion.field_name] = assertion
    return resolved


def _finding(
    *,
    validation_version: str,
    experiment: Experiment,
    field_name: str,
    requirement_class: RequirementClass,
    severity: FindingSeverity,
    assertions: tuple[MetadataAssertion, ...],
    message: str,
    resolution: str,
) -> ValidationFinding:
    assertion_ids = tuple(item.assertion_id for item in assertions)
    evidence_ids = tuple(
        sorted({item for assertion in assertions for item in assertion.evidence_ids})
    )
    body = {
        "schema_version": VALIDATION_SCHEMA_VERSION,
        "validation_version": validation_version,
        "experiment_id": experiment.experiment_id,
        "experiment_version": experiment.version,
        "field_name": field_name,
        "requirement_class": requirement_class,
        "severity": severity,
        "assertion_ids": assertion_ids,
        "evidence_ids": evidence_ids,
        "message": message,
        "resolution": resolution,
    }
    return ValidationFinding(
        finding_id=content_id("validation-finding", body),
        schema_version=VALIDATION_SCHEMA_VERSION,
        code=f"metadata.{field_name}.{severity}",
        severity=severity,
        field_name=field_name,
        requirement_class=requirement_class,
        assertion_ids=assertion_ids,
        evidence_ids=evidence_ids,
        message=message,
        resolution=resolution,
    )


def validate_experiment(experiment: Experiment, *, validation_version: str) -> ValidationRun:
    """Apply technique-aware evidence-completeness rules without judging scientific quality."""
    resolved = _resolved_by_field(experiment)
    findings: list[ValidationFinding] = []
    technique_handled_fields: set[str] = set()
    required_fields = _REQUIRED_FIELDS[experiment.technique]
    for field_name in required_fields:
        assertion = resolved.get(field_name)
        if assertion is None or assertion.value.state != "known":
            assertions = () if assertion is None else (assertion,)
            findings.append(
                _finding(
                    validation_version=validation_version,
                    experiment=experiment,
                    field_name=field_name,
                    requirement_class="required",
                    severity="blocking",
                    assertions=assertions,
                    message=f"Required field {field_name} is not supported by a known value.",
                    resolution=(
                        f"Append a user-supplied {field_name} assertion with retained evidence."
                    ),
                )
            )
    if experiment.technique == "galvanostatic_electrolysis":
        current_kind = resolved.get("current_quantity_kind")
        current_basis = resolved.get("current_basis")
        electrode_area = resolved.get("electrode_area")
        density_context_is_valid = (
            current_basis is not None
            and current_basis.value.state == "known"
            and current_basis.value.value in _ELECTROLYSIS_AREA_BASES
            and electrode_area is not None
            and electrode_area.value.state == "known"
        )
        total_current_context_is_valid = (
            current_basis is not None
            and current_basis.value.state == "known"
            and current_basis.value.value == "total_current"
            and electrode_area is not None
            and electrode_area.value.state == "not_applicable"
        )
        expected_context_is_valid = current_kind is not None and (
            (current_kind.value.value == "current_density" and density_context_is_valid)
            or (current_kind.value.value == "current" and total_current_context_is_valid)
        )
        if not expected_context_is_valid:
            context_assertions = tuple(
                item for item in (current_kind, current_basis, electrode_area) if item is not None
            )
            findings.append(
                _finding(
                    validation_version=validation_version,
                    experiment=experiment,
                    field_name="current_basis",
                    requirement_class="conditional",
                    severity="blocking",
                    assertions=context_assertions,
                    message=(
                        "Current quantity kind, total-current or area basis, and electrode "
                        "area are not dimensionally compatible."
                    ),
                    resolution=(
                        "Use total_current with a total-current series and not_applicable area, "
                        "or use a supported area basis and known area for current density."
                    ),
                )
            )
            technique_handled_fields.add("current_basis")
    for field_name in sorted(set(resolved) - set(required_fields) - technique_handled_fields):
        assertion = resolved[field_name]
        is_auxiliary_result = field_name.startswith("auxiliary_result.")
        if (
            experiment.technique == "galvanostatic_electrolysis"
            and field_name not in _ELECTROLYSIS_ASSERTION_FIELDS
            and not is_auxiliary_result
            and assertion.value.state == "known"
        ):
            findings.append(
                _finding(
                    validation_version=validation_version,
                    experiment=experiment,
                    field_name=field_name,
                    requirement_class="conditional",
                    severity="blocking",
                    assertions=(assertion,),
                    message=(
                        f"{field_name} has no approved galvanostatic-electrolysis derivation "
                        "contract in this release."
                    ),
                    resolution=(
                        "Provide a reviewed equation, every technique-specific input and unit, "
                        "an analysis version, and complete source lineage in a future contract."
                    ),
                )
            )
            continue
        if assertion.value.state in {"known", "not_applicable"}:
            continue
        severity: FindingSeverity = (
            "blocking" if assertion.requirement_class == "required" else "unknown"
        )
        if assertion.value.state == "unavailable" and severity != "blocking":
            severity = "warning"
        findings.append(
            _finding(
                validation_version=validation_version,
                experiment=experiment,
                field_name=field_name,
                requirement_class=assertion.requirement_class,
                severity=severity,
                assertions=(assertion,),
                message=(
                    f"{field_name} remains {assertion.value.state}; the Passport does not infer it."
                ),
                resolution=(
                    f"Append a user-supplied {field_name} assertion if additional retained "
                    "evidence is available; otherwise keep this finding visible."
                ),
            )
        )
    ordered = tuple(sorted(findings, key=lambda item: (item.severity, item.field_name)))
    blocking_count = sum(item.severity == "blocking" for item in ordered)
    warning_count = sum(item.severity == "warning" for item in ordered)
    unknown_count = sum(item.severity == "unknown" for item in ordered)
    decision = ReleaseDecision(
        status="blocked" if blocking_count else "eligible",
        blocking_count=blocking_count,
        warning_count=warning_count,
        unknown_count=unknown_count,
        finding_ids=tuple(item.finding_id for item in ordered),
    )
    body = {
        "schema_version": VALIDATION_SCHEMA_VERSION,
        "validation_version": validation_version,
        "experiment_id": experiment.experiment_id,
        "experiment_version": experiment.version,
        "findings": ordered,
        "release_decision": decision,
    }
    return ValidationRun(
        validation_id=content_id("validation", body),
        schema_version=VALIDATION_SCHEMA_VERSION,
        validation_version=validation_version,
        experiment_id=experiment.experiment_id,
        experiment_version=experiment.version,
        findings=ordered,
        release_decision=decision,
    )


__all__ = [
    "ASSERTION_SCHEMA_VERSION",
    "EXPERIMENT_SCHEMA_VERSION",
    "VALIDATION_SCHEMA_VERSION",
    "AssertionOrigin",
    "AssertionTransformation",
    "AssertionValue",
    "ConfidenceRepresentation",
    "Experiment",
    "ExperimentVersionConflictError",
    "FindingSeverity",
    "InferenceDetails",
    "MetadataAssertion",
    "ReleaseDecision",
    "RequirementClass",
    "Technique",
    "ValidationFinding",
    "ValidationRun",
    "add_user_assertion",
    "create_experiment",
    "experiment_id_for_observation",
    "make_assertion",
    "validate_experiment",
]

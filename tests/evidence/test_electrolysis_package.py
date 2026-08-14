from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime

import pytest

from cv_helpers import cv_profile, cv_source
from electrolysis_helpers import (
    auxiliary_source,
    electrolysis_profile,
    electrolysis_profile_with_auxiliary,
    electrolysis_source,
)
from labbridge.application.cv_ingestion import normalise_cv
from labbridge.application.electrolysis_ingestion import normalise_electrolysis
from labbridge.application.experiments import (
    experiment_from_electrolysis_normalisation,
    experiment_from_normalisation,
)
from labbridge.domain.canonical import content_id
from labbridge.domain.experiments import AssertionValue, make_assertion, validate_experiment
from labbridge.evidence.experiment_package import (
    MANIFEST_MEMBER,
    AuxiliaryPackageSource,
    ExperimentPackageVerificationError,
    PackageInputs,
    _member_entry,
    _zip,
    build_experiment_package,
    verify_experiment_package,
)
from labbridge.evidence.manifest import canonical_json, digest
from labbridge.evidence.passport import build_passport

RELEASED_AT = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def _electrolysis_inputs(*, include_auxiliary: bool = False) -> PackageInputs:
    source = electrolysis_source()
    profile = electrolysis_profile_with_auxiliary() if include_auxiliary else electrolysis_profile()
    auxiliary = auxiliary_source()
    result = normalise_electrolysis(
        source,
        profile,
        producing_version="0.1.0",
        auxiliary_sources=(
            {auxiliary.artifact.source_artifact_id: auxiliary} if include_auxiliary else None
        ),
    )
    experiment = experiment_from_electrolysis_normalisation(result)
    passport = build_passport(
        experiment,
        validate_experiment(experiment, validation_version="2"),
        released_at=RELEASED_AT,
        release=True,
    )
    auxiliary_inputs = (
        (
            AuxiliaryPackageSource(
                source_filename=auxiliary.artifact.filename,
                source_bytes=auxiliary.data,
                source_artifact={
                    "schema_version": "1",
                    **auxiliary.artifact.model_dump(mode="json"),
                },
            ),
        )
        if include_auxiliary
        else ()
    )
    return PackageInputs(
        source_filename=source.artifact.filename,
        source_bytes=source.data,
        source_artifact={"schema_version": "1", **source.artifact.model_dump(mode="json")},
        import_profile={
            "profile_id": result.observation.import_profile_id,
            **profile.model_dump(mode="json"),
        },
        normalised_observation=result.observation.model_dump(mode="json"),
        transformation_graph=result.graph.model_dump(mode="json"),
        auxiliary_sources=auxiliary_inputs,
        passport=passport,
    )


def _cv_inputs() -> PackageInputs:
    source = cv_source()
    profile = cv_profile()
    result = normalise_cv(source, profile, producing_version="0.1.0")
    experiment = experiment_from_normalisation(result)
    passport = build_passport(
        experiment,
        validate_experiment(experiment, validation_version="1"),
        released_at=RELEASED_AT,
        release=True,
    )
    return PackageInputs(
        source_filename=source.artifact.filename,
        source_bytes=source.data,
        source_artifact={"schema_version": "1", **source.artifact.model_dump(mode="json")},
        import_profile={
            "profile_id": result.observation.import_profile_id,
            **profile.model_dump(mode="json"),
        },
        normalised_observation=result.observation.model_dump(mode="json"),
        transformation_graph=result.graph.model_dump(mode="json"),
        passport=passport,
    )


def test_same_verifier_contract_accepts_cv_and_electrolysis_packages() -> None:
    cv_package = build_experiment_package(
        _cv_inputs(),
        producing_versions={"labbridge": "0.1.0", "experiment_package": "1"},
    )
    electrolysis_package = build_experiment_package(
        _electrolysis_inputs(),
        producing_versions={"labbridge": "0.1.0", "experiment_package": "3"},
    )

    cv_verification = verify_experiment_package(cv_package.archive_bytes)
    electrolysis_verification = verify_experiment_package(electrolysis_package.archive_bytes)

    assert cv_package.metadata.schema_version == "1"
    assert electrolysis_package.metadata.schema_version == "3"
    assert cv_verification.verified is electrolysis_verification.verified is True
    assert cv_verification.lineage_closed is electrolysis_verification.lineage_closed is True


def test_auxiliary_analytical_result_closes_to_method_and_source_bytes() -> None:
    package = build_experiment_package(
        _electrolysis_inputs(include_auxiliary=True),
        producing_versions={"labbridge": "0.1.0", "experiment_package": "3"},
    )

    verification = verify_experiment_package(package.archive_bytes)

    assert verification.verified is True
    with zipfile.ZipFile(io.BytesIO(package.archive_bytes), "r") as archive:
        assert "phase1/auxiliary-source-artifacts.json" in archive.namelist()
        assert any(name.startswith("auxiliary-source/") for name in archive.namelist())


def test_auxiliary_result_without_packaged_source_is_rejected() -> None:
    inputs = _electrolysis_inputs(include_auxiliary=True).model_copy(
        update={"auxiliary_sources": ()}
    )

    with pytest.raises(ValueError, match="auxiliary result sources"):
        build_experiment_package(
            inputs,
            producing_versions={"labbridge": "0.1.0", "experiment_package": "3"},
        )


def test_auxiliary_source_byte_tamper_fails_existing_verifier_contract() -> None:
    package = build_experiment_package(
        _electrolysis_inputs(include_auxiliary=True),
        producing_versions={"labbridge": "0.1.0", "experiment_package": "3"},
    )
    source = zipfile.ZipFile(io.BytesIO(package.archive_bytes), "r")
    members = {name: source.read(name) for name in source.namelist()}
    auxiliary_name = next(name for name in members if name.startswith("auxiliary-source/"))
    members[auxiliary_name] += b"tampered"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(members.items()):
            archive.writestr(name, data)

    with pytest.raises(ExperimentPackageVerificationError) as raised:
        verify_experiment_package(output.getvalue())

    assert raised.value.code == "package_member_sha256_mismatch"


def test_independent_verifier_rejects_unbacked_auxiliary_assertion() -> None:
    inputs = _electrolysis_inputs(include_auxiliary=True)
    passport = inputs.passport
    unbacked = make_assertion(
        experiment_id=passport.experiment_id,
        field_name="auxiliary_result.unbacked",
        requirement_class="optional",
        origin="user_supplied",
        transformation="none",
        value=AssertionValue(state="known", value="42", unit="mol/L"),
        evidence_ids=(passport.experiment_id,),
        evidence_note="A declaration with no retained analytical source or method record.",
    )
    package = build_experiment_package(
        inputs.model_copy(
            update={
                "passport": passport.model_copy(
                    update={"assertions": (*passport.assertions, unbacked)}
                )
            }
        ),
        producing_versions={"labbridge": "0.1.0", "experiment_package": "3"},
    )

    with pytest.raises(ExperimentPackageVerificationError) as raised:
        verify_experiment_package(package.archive_bytes)

    assert raised.value.code == "package_lineage_open"


def test_independent_verifier_rejects_reclassified_current_quantity() -> None:
    inputs = _electrolysis_inputs()
    passport = inputs.passport
    assertions = tuple(
        item.model_copy(
            update={
                "value": AssertionValue(state="known", value="current_density"),
            }
        )
        if item.field_name == "current_quantity_kind"
        else item
        for item in passport.assertions
    )
    package = build_experiment_package(
        inputs.model_copy(
            update={"passport": passport.model_copy(update={"assertions": assertions})}
        ),
        producing_versions={"labbridge": "0.1.0", "experiment_package": "3"},
    )

    with pytest.raises(ExperimentPackageVerificationError) as raised:
        verify_experiment_package(package.archive_bytes)

    assert raised.value.code == "package_lineage_open"


@pytest.mark.parametrize("valid_last", [True, False])
def test_independent_verifier_rejects_duplicate_active_electrical_semantics(
    valid_last: bool,
) -> None:
    inputs = _electrolysis_inputs()
    passport = inputs.passport
    original = next(
        item for item in passport.assertions if item.field_name == "current_quantity_kind"
    )
    contradictory = make_assertion(
        experiment_id=passport.experiment_id,
        field_name=original.field_name,
        requirement_class=original.requirement_class,
        origin=original.origin,
        transformation=original.transformation,
        value=AssertionValue(state="known", value="current_density"),
        evidence_ids=original.evidence_ids,
        evidence_note="A contradictory duplicate ordered before the valid assertion.",
    )
    ordered_pair = (contradictory, original) if valid_last else (original, contradictory)
    reordered = (*(item for item in passport.assertions if item is not original), *ordered_pair)
    forged_passport = passport.model_copy(
        update={
            "assertions": reordered,
            "active_assertion_ids": (*passport.active_assertion_ids, contradictory.assertion_id),
        }
    )
    package = build_experiment_package(
        inputs.model_copy(update={"passport": forged_passport}),
        producing_versions={"labbridge": "0.1.0", "experiment_package": "3"},
    )

    with pytest.raises(ExperimentPackageVerificationError) as raised:
        verify_experiment_package(package.archive_bytes)

    assert raised.value.code == "package_lineage_open"


def test_independent_verifier_rejects_forged_electrical_assertion_authority() -> None:
    inputs = _electrolysis_inputs()
    passport = inputs.passport
    original = next(
        item for item in passport.assertions if item.field_name == "current_quantity_kind"
    )
    forged = make_assertion(
        experiment_id=passport.experiment_id,
        field_name=original.field_name,
        requirement_class=original.requirement_class,
        origin="source_file",
        transformation="none",
        value=original.value,
        evidence_ids=(passport.source_artifact_id,),
        evidence_note="A matching value with false source authority.",
    )
    assertions = tuple(forged if item is original else item for item in passport.assertions)
    active_ids = tuple(
        forged.assertion_id if item == original.assertion_id else item
        for item in passport.active_assertion_ids
    )
    package = build_experiment_package(
        inputs.model_copy(
            update={
                "passport": passport.model_copy(
                    update={"assertions": assertions, "active_assertion_ids": active_ids}
                )
            }
        ),
        producing_versions={"labbridge": "0.1.0", "experiment_package": "3"},
    )

    with pytest.raises(ExperimentPackageVerificationError) as raised:
        verify_experiment_package(package.archive_bytes)

    assert raised.value.code == "package_lineage_open"


@pytest.mark.parametrize("field_name", ["yield", "faradaic_efficiency"])
def test_independent_verifier_rejects_active_unsupported_product_claim(
    field_name: str,
) -> None:
    inputs = _electrolysis_inputs()
    passport = inputs.passport
    claim = make_assertion(
        experiment_id=passport.experiment_id,
        field_name=field_name,
        requirement_class="conditional",
        origin="user_supplied",
        transformation="derived",
        value=AssertionValue(state="known", value="0.90", unit="1"),
        evidence_ids=(passport.experiment_id,),
        evidence_note="A forged claim without an approved derivation contract.",
    )
    package = build_experiment_package(
        inputs.model_copy(
            update={
                "passport": passport.model_copy(
                    update={
                        "assertions": (*passport.assertions, claim),
                        "active_assertion_ids": (
                            *passport.active_assertion_ids,
                            claim.assertion_id,
                        ),
                    }
                )
            }
        ),
        producing_versions={"labbridge": "0.1.0", "experiment_package": "3"},
    )

    with pytest.raises(ExperimentPackageVerificationError) as raised:
        verify_experiment_package(package.archive_bytes)

    assert raised.value.code == "package_lineage_open"


def test_independent_verifier_accepts_active_unavailable_unsupported_claim() -> None:
    inputs = _electrolysis_inputs()
    passport = inputs.passport
    correction = make_assertion(
        experiment_id=passport.experiment_id,
        field_name="product_yield",
        requirement_class="conditional",
        origin="user_supplied",
        transformation="none",
        value=AssertionValue(state="unavailable"),
        evidence_ids=(passport.experiment_id,),
        evidence_note="The unsupported claim is explicitly unavailable.",
    )
    package = build_experiment_package(
        inputs.model_copy(
            update={
                "passport": passport.model_copy(
                    update={
                        "assertions": (*passport.assertions, correction),
                        "active_assertion_ids": (
                            *passport.active_assertion_ids,
                            correction.assertion_id,
                        ),
                    }
                )
            }
        ),
        producing_versions={"labbridge": "0.1.0", "experiment_package": "3"},
    )

    assert verify_experiment_package(package.archive_bytes).verified is True


def _reforge(archive_bytes: bytes, *, replace: dict[str, bytes]) -> bytes:
    """Rebuild an archive around changed members with every hash and identity recomputed.

    This is the adversary the member checks cannot catch: entry digests, `members_digest`, and
    `package_id` are all derived exactly as the builder derives them, so the archive is internally
    consistent. Only a semantic check can reject it.
    """
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(members[MANIFEST_MEMBER])
    members.update(replace)
    del members[MANIFEST_MEMBER]
    entries = [_member_entry(name, data) for name, data in sorted(members.items())]
    core = {
        key: value
        for key, value in manifest.items()
        if key not in {"package_id", "members", "members_digest"}
    }
    core["members"] = entries
    core["members_digest"] = digest(canonical_json(entries))
    ordered = {key: core[key] for key in manifest if key in core}
    ordered["package_id"] = content_id("experiment-package", ordered)
    members[MANIFEST_MEMBER] = canonical_json(ordered)
    return _zip(members)


def test_a_rehashed_archive_cannot_swap_technique_to_skip_electrolysis_semantics() -> None:
    """A self-consistent forgery must not reach the CV branch and skip electrolysis checks.

    Branching on the import profile while every other check read the Passport meant a Passport
    declaring `galvanostatic_electrolysis` was inspected under CV rules, so
    `_verify_electrolysis_passport_semantics` never ran. The archive was still refused, but by a
    downstream `package_lineage_open` rather than by the contradiction that caused it. One
    authoritative technique makes the refusal name the real defect.
    """
    inputs = _electrolysis_inputs()
    package = build_experiment_package(
        inputs, producing_versions={"labbridge": "0.1.0", "experiment_package": "3"}
    )
    assert verify_experiment_package(package.archive_bytes).verified is True

    with zipfile.ZipFile(io.BytesIO(package.archive_bytes)) as archive:
        profile = json.loads(archive.read("phase2/import-profile.json"))
    profile["technique"] = "cyclic_voltammetry"
    forged = _reforge(
        package.archive_bytes,
        replace={"phase2/import-profile.json": canonical_json(profile)},
    )

    with pytest.raises(ExperimentPackageVerificationError) as raised:
        verify_experiment_package(forged)

    # Not package_identity_mismatch or a member digest failure: the forgery is self-consistent and
    # defeats every hash check, so this proves the semantic gate is what stops it.
    assert raised.value.code == "package_technique_mismatch"


def test_a_rehashed_cv_archive_cannot_claim_the_electrolysis_profile() -> None:
    """The mismatch is rejected in both directions, not only for the electrolysis Passport."""
    inputs = _cv_inputs()
    package = build_experiment_package(
        inputs, producing_versions={"labbridge": "0.1.0", "experiment_package": "1"}
    )
    assert verify_experiment_package(package.archive_bytes).verified is True

    with zipfile.ZipFile(io.BytesIO(package.archive_bytes)) as archive:
        profile = json.loads(archive.read("phase2/import-profile.json"))
    profile["technique"] = "galvanostatic_electrolysis"
    forged = _reforge(
        package.archive_bytes,
        replace={"phase2/import-profile.json": canonical_json(profile)},
    )

    with pytest.raises(ExperimentPackageVerificationError) as raised:
        verify_experiment_package(forged)

    assert raised.value.code == "package_technique_mismatch"

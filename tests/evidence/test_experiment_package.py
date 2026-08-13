from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime

import pytest

from cv_helpers import (
    cv_profile,
    cv_source,
    gamry_dta_profile,
    gamry_dta_source,
)
from labbridge.application.cv_ingestion import normalise_cv
from labbridge.application.experiments import experiment_from_normalisation
from labbridge.domain.canonical import content_id
from labbridge.domain.experiments import (
    AssertionValue,
    add_user_assertion,
    create_experiment,
    experiment_id_for_observation,
    make_assertion,
    validate_experiment,
)
from labbridge.evidence.experiment_package import (
    ExperimentPackageVerificationError,
    PackageInputs,
    build_experiment_package,
    verify_experiment_package,
)
from labbridge.evidence.manifest import canonical_json, digest
from labbridge.evidence.passport import (
    build_passport,
    render_passport_html,
    render_passport_json,
    stable_passport_payload,
)

NORMALISATION = normalise_cv(cv_source(), cv_profile(), producing_version="0.1.0")
SOURCE_ID = cv_source().artifact.source_artifact_id
OBSERVATION_ID = NORMALISATION.observation.observation_id
TRANSFORM_ID = NORMALISATION.observation.transformation_ids[-1]
RELEASED_AT = datetime(2026, 8, 12, 20, 0, tzinfo=UTC)
SUPERSEDING_VERSION = 2


def _assertion(
    field_name: str,
    value: str,
    *,
    unit: str | None = None,
    requirement_class: str = "required",
):
    return make_assertion(
        experiment_id=experiment_id_for_observation(OBSERVATION_ID),
        field_name=field_name,
        requirement_class=requirement_class,
        origin="source_file",
        transformation="parsed",
        value=AssertionValue(state="known", value=value, unit=unit),
        evidence_ids=(SOURCE_ID, TRANSFORM_ID),
        evidence_note="Retained source evidence.",
    )


def _experiment():
    return create_experiment(
        observation_id=OBSERVATION_ID,
        source_artifact_id=SOURCE_ID,
        import_profile_id=NORMALISATION.observation.import_profile_id,
        technique="cyclic_voltammetry",
        data_origin="synthetic",
        execution_mode="replay",
        environment_id="synthetic_cv_fixture",
        transformation_ids=(TRANSFORM_ID,),
        assertions=(
            _assertion("source_artifact", SOURCE_ID),
            _assertion("observation", OBSERVATION_ID),
            _assertion("potential_axis", "channel_a", unit="V"),
            _assertion("current_axis", "channel_b", unit="A"),
            make_assertion(
                experiment_id=experiment_id_for_observation(OBSERVATION_ID),
                field_name="reference_scale",
                requirement_class="conditional",
                origin="source_file",
                transformation="parsed",
                value=AssertionValue(state="unknown"),
                evidence_ids=(SOURCE_ID, TRANSFORM_ID),
                evidence_note="The retained source does not declare a reference scale.",
            ),
            make_assertion(
                experiment_id=experiment_id_for_observation(OBSERVATION_ID),
                field_name="cycle_information",
                requirement_class="recommended",
                origin="source_file",
                transformation="parsed",
                value=AssertionValue(state="unavailable"),
                evidence_ids=(SOURCE_ID, TRANSFORM_ID),
                evidence_note="Cycle information is unavailable in the retained source.",
            ),
        ),
    )


def _passport(experiment=None, *, supersedes_passport_id: str | None = None):
    current = experiment or _experiment()
    validation = validate_experiment(current, validation_version="1")
    return build_passport(
        current,
        validation,
        released_at=RELEASED_AT,
        release=True,
        supersedes_passport_id=supersedes_passport_id,
    )


def _inputs(passport) -> PackageInputs:
    source = cv_source()
    profile = cv_profile()
    return PackageInputs(
        source_filename=source.artifact.filename,
        source_bytes=source.data,
        source_artifact={
            "schema_version": "1",
            **source.artifact.model_dump(mode="json"),
        },
        import_profile={
            "profile_id": NORMALISATION.observation.import_profile_id,
            **profile.model_dump(mode="json"),
        },
        normalised_observation=NORMALISATION.observation.model_dump(mode="json"),
        transformation_graph=NORMALISATION.graph.model_dump(mode="json"),
        passport=passport,
    )


def _rewrite_member(package_bytes: bytes, name: str, data: bytes | None) -> bytes:
    source = zipfile.ZipFile(io.BytesIO(package_bytes), "r")
    members = {member: source.read(member) for member in source.namelist() if member != name}
    if data is not None:
        members[name] = data
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member in sorted(members):
            archive.writestr(member, members[member])
    return output.getvalue()


def _reclose_package_members(members: dict[str, bytes], manifest: dict[str, object]) -> bytes:
    entries = [
        {"name": name, "sha256": digest(data), "byte_size": len(data)}
        for name, data in sorted(members.items())
    ]
    manifest["members"] = entries
    manifest["members_digest"] = digest(canonical_json(entries))
    core = {key: value for key, value in manifest.items() if key != "package_id"}
    manifest["package_id"] = content_id("experiment-package", core)
    members["manifest.json"] = canonical_json(manifest)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(members.items()):
            archive.writestr(name, data)
    return output.getvalue()


def test_json_and_html_share_findings_and_release_decision() -> None:
    passport = _passport()

    json_report = json.loads(render_passport_json(passport))
    html_report = render_passport_html(passport).decode("utf-8")

    assert json_report["release_decision"]["status"] == "eligible"
    assert "Release decision: eligible" in html_report
    assert {item["finding_id"] for item in json_report["findings"]} == set(
        passport.release_decision.finding_ids
    )
    for finding_id in passport.release_decision.finding_ids:
        assert finding_id in html_report
    assert "synthetic + replay" in html_report
    assert "unknown" in html_report
    assert "warning" in html_report
    for finding in passport.findings:
        assert finding.resolution in html_report


def test_blocking_finding_prevents_released_passport() -> None:
    incomplete = _experiment().model_copy(
        update={
            "assertions": tuple(
                item for item in _experiment().assertions if item.field_name != "current_axis"
            ),
            "active_assertion_ids": tuple(
                item.assertion_id
                for item in _experiment().assertions
                if item.field_name != "current_axis"
            ),
        }
    )
    validation = validate_experiment(incomplete, validation_version="1")

    with pytest.raises(ValueError, match="blocking validation findings prevent release"):
        build_passport(incomplete, validation, released_at=RELEASED_AT, release=True)


def test_rendering_is_deterministic_with_release_metadata_isolated() -> None:
    passport = _passport()
    later = passport.model_copy(update={"released_at": datetime(2026, 8, 13, tzinfo=UTC)})

    assert render_passport_json(passport) == render_passport_json(passport)
    assert render_passport_html(passport) == render_passport_html(passport)
    assert stable_passport_payload(passport) == stable_passport_payload(later)
    assert render_passport_json(passport) != render_passport_json(later)


def test_superseding_passport_keeps_prior_passport_immutable() -> None:
    initial = _passport()
    source = next(item for item in _experiment().assertions if item.field_name == "reference_scale")
    supplemented = add_user_assertion(
        _experiment(),
        expected_version=1,
        field_name="reference_scale",
        requirement_class="conditional",
        transformation="none",
        value=AssertionValue(state="known", value="RHE"),
        evidence_note="Operator declaration retained as a user-supplied assertion.",
        supplements_assertion_id=source.assertion_id,
    )
    superseding = _passport(supplemented, supersedes_passport_id=initial.passport_id)

    assert initial.experiment_version == 1
    assert superseding.experiment_version == SUPERSEDING_VERSION
    assert superseding.supersedes_passport_id == initial.passport_id
    assert render_passport_json(initial) == render_passport_json(_passport())
    assert any(item.origin == "source_file" for item in superseding.assertions)
    assert any(item.origin == "user_supplied" for item in superseding.assertions)


def test_package_verifies_and_closes_every_passport_field_to_retained_evidence() -> None:
    package = build_experiment_package(
        _inputs(_passport()),
        producing_versions={"labbridge": "0.1.0", "experiment_package": "1"},
    )

    verification = verify_experiment_package(package.archive_bytes)

    assert verification.verified is True
    assert verification.package_id == package.package_id
    assert verification.passport_id == package.passport_id
    assert verification.lineage_closed is True
    assert verification.data_origin == "synthetic"
    assert verification.execution_mode == "replay"
    assert verification.environment_id == "synthetic_cv_fixture"


def _dta_inputs() -> tuple[PackageInputs, str]:
    source = gamry_dta_source()
    profile = gamry_dta_profile()
    normalisation = normalise_cv(
        source,
        profile,
        producing_version="0.1.0",
        source_format="gamry_dta",
    )
    assert normalisation.parser_record is not None
    experiment = experiment_from_normalisation(normalisation)
    validation = validate_experiment(experiment, validation_version="1")
    passport = build_passport(
        experiment,
        validation,
        released_at=RELEASED_AT,
        release=True,
        supersedes_passport_id=None,
    )
    return (
        PackageInputs(
            source_filename=source.artifact.filename,
            source_bytes=source.data,
            source_artifact={
                "schema_version": "1",
                **source.artifact.model_dump(mode="json"),
            },
            import_profile={
                "profile_id": normalisation.observation.import_profile_id,
                **profile.model_dump(mode="json"),
            },
            normalised_observation=normalisation.observation.model_dump(mode="json"),
            transformation_graph=normalisation.graph.model_dump(mode="json"),
            parser_record=normalisation.parser_record.model_dump(mode="json"),
            passport=passport,
        ),
        normalisation.parser_record.parser_record_id,
    )


def test_dta_package_v2_verifies_parser_identity_and_passport_evidence() -> None:
    inputs, parser_record_id = _dta_inputs()

    package = build_experiment_package(
        inputs,
        producing_versions={
            "labbridge": "0.1.0",
            "experiment_package": "2",
            "gamry_dta_parser": "gamry-dta/1",
        },
    )
    verification = verify_experiment_package(package.archive_bytes)

    assert package.metadata.schema_version == "2"
    assert verification.lineage_closed is True
    assert any(parser_record_id in item.evidence_ids for item in inputs.passport.assertions)
    with zipfile.ZipFile(io.BytesIO(package.archive_bytes), "r") as archive:
        assert "phase2/parser-record.json" in archive.namelist()


def test_dta_package_rejects_parser_record_content_substitution() -> None:
    inputs, _ = _dta_inputs()
    assert inputs.parser_record is not None
    forged = {
        **inputs.parser_record,
        "row_count": int(inputs.parser_record["row_count"]) + 1,
    }
    package = build_experiment_package(
        inputs.model_copy(update={"parser_record": forged}),
        producing_versions={
            "labbridge": "0.1.0",
            "experiment_package": "2",
            "gamry_dta_parser": "gamry-dta/1",
        },
    )

    with pytest.raises(ExperimentPackageVerificationError) as raised:
        verify_experiment_package(package.archive_bytes)

    assert raised.value.code == "package_lineage_open"


@pytest.mark.parametrize(
    ("omission", "expected_code"),
    [
        ("parser_member", "package_member_invalid"),
        ("parser_version", "package_lineage_open"),
        ("source_link", "package_lineage_open"),
        ("profile_link", "package_lineage_open"),
        ("observation_link", "package_lineage_open"),
        ("graph_link", "package_lineage_open"),
    ],
)
def test_dta_package_rejects_required_parser_evidence_omissions(
    omission: str, expected_code: str
) -> None:
    inputs, parser_record_id = _dta_inputs()
    package = build_experiment_package(
        inputs,
        producing_versions={
            "labbridge": "0.1.0",
            "experiment_package": "2",
            "gamry_dta_parser": "gamry-dta/1",
        },
    )
    with zipfile.ZipFile(io.BytesIO(package.archive_bytes), "r") as archive:
        members = {
            name: archive.read(name) for name in archive.namelist() if name != "manifest.json"
        }
        manifest = json.loads(archive.read("manifest.json"))

    if omission == "parser_member":
        members.pop("phase2/parser-record.json")
    elif omission == "parser_version":
        versions = dict(manifest["producing_versions"])
        versions.pop("gamry_dta_parser")
        manifest["producing_versions"] = versions
    elif omission in {"source_link", "profile_link"}:
        parser_record = json.loads(members["phase2/parser-record.json"])
        parser_record.pop(
            "source_artifact_id" if omission == "source_link" else "import_profile_id"
        )
        members["phase2/parser-record.json"] = canonical_json(parser_record)
    elif omission == "observation_link":
        observation = json.loads(members["phase2/normalised-observation.json"])
        observation.pop("parser_record_id")
        members["phase2/normalised-observation.json"] = canonical_json(observation)
    else:
        graph = json.loads(members["phase2/transformation-graph.json"])
        dta_parse = next(item for item in graph["records"] if item["kind"] == "dta_parse")
        dta_parse["output_ids"] = [
            item for item in dta_parse["output_ids"] if item != parser_record_id
        ]
        members["phase2/transformation-graph.json"] = canonical_json(graph)

    damaged = _reclose_package_members(members, manifest)

    with pytest.raises(ExperimentPackageVerificationError) as raised:
        verify_experiment_package(damaged)

    assert raised.value.code == expected_code


@pytest.mark.parametrize(
    "mutation", ["profile_id", "profile_content", "observation_content", "transformation_content"]
)
def test_package_rejects_phase2_identity_substitution(mutation: str) -> None:
    inputs = _inputs(_passport())
    if mutation == "profile_id":
        inputs = inputs.model_copy(
            update={
                "import_profile": {
                    **inputs.import_profile,
                    "profile_id": "cv-profile:substituted",
                }
            }
        )
    elif mutation == "profile_content":
        metadata = dict(inputs.import_profile["metadata"])
        metadata["reference_scale"] = {"state": "known", "value": "RHE", "unit": None}
        inputs = inputs.model_copy(
            update={
                "import_profile": {
                    **inputs.import_profile,
                    "metadata": metadata,
                }
            }
        )
    elif mutation == "observation_content":
        inputs = inputs.model_copy(
            update={
                "normalised_observation": {
                    **inputs.normalised_observation,
                    "normalisation_version": "forged",
                }
            }
        )
    else:
        records = list(inputs.transformation_graph["records"])
        records[0] = {**records[0], "implementation_version": "forged"}
        inputs = inputs.model_copy(
            update={
                "transformation_graph": {
                    **inputs.transformation_graph,
                    "records": records,
                }
            }
        )
    package = build_experiment_package(
        inputs,
        producing_versions={"labbridge": "0.1.0", "experiment_package": "1"},
    )

    with pytest.raises(ExperimentPackageVerificationError) as raised:
        verify_experiment_package(package.archive_bytes)

    assert raised.value.code == "package_lineage_open"


def test_package_rejects_origin_mode_disagreement_across_evidence_layers() -> None:
    inputs = _inputs(_passport())
    inputs = inputs.model_copy(
        update={
            "source_artifact": {
                **inputs.source_artifact,
                "data_origin": "observed",
            }
        }
    )
    package = build_experiment_package(
        inputs,
        producing_versions={"labbridge": "0.1.0", "experiment_package": "1"},
    )

    with pytest.raises(ExperimentPackageVerificationError) as raised:
        verify_experiment_package(package.archive_bytes)

    assert raised.value.code == "package_origin_mismatch"


def test_package_rejects_user_assertion_with_absent_evidence() -> None:
    initial = _experiment()
    source = next(item for item in initial.assertions if item.field_name == "reference_scale")
    supplemented = add_user_assertion(
        initial,
        expected_version=1,
        field_name="reference_scale",
        requirement_class="conditional",
        transformation="none",
        value=AssertionValue(state="known", value="RHE"),
        evidence_note="Operator declaration.",
        supplements_assertion_id=source.assertion_id,
    )
    passport = _passport(supplemented)
    user = passport.assertions[-1].model_copy(update={"evidence_ids": ("missing:evidence",)})
    passport = passport.model_copy(update={"assertions": (*passport.assertions[:-1], user)})
    package = build_experiment_package(
        _inputs(passport),
        producing_versions={"labbridge": "0.1.0", "experiment_package": "1"},
    )

    with pytest.raises(ExperimentPackageVerificationError) as raised:
        verify_experiment_package(package.archive_bytes)

    assert raised.value.code == "package_lineage_open"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("missing", "package_member_missing"),
        ("modified", "package_member_sha256_mismatch"),
        ("unexpected", "package_member_unexpected"),
        ("manifest", "package_manifest_digest_mismatch"),
    ],
)
def test_package_tampering_fails_with_a_specific_code(mutation: str, expected_code: str) -> None:
    package = build_experiment_package(
        _inputs(_passport()),
        producing_versions={"labbridge": "0.1.0", "experiment_package": "1"},
    )
    if mutation == "missing":
        damaged = _rewrite_member(package.archive_bytes, "passport/passport.html", None)
    elif mutation == "modified":
        damaged = _rewrite_member(package.archive_bytes, "passport/passport.html", b"changed")
    elif mutation == "unexpected":
        damaged = _rewrite_member(package.archive_bytes, "unexpected.txt", b"unexpected")
    else:
        with zipfile.ZipFile(io.BytesIO(package.archive_bytes), "r") as archive:
            manifest = json.loads(archive.read("manifest.json"))
        manifest["members_digest"] = "0" * 64
        damaged = _rewrite_member(
            package.archive_bytes,
            "manifest.json",
            json.dumps(manifest, sort_keys=True).encode("utf-8"),
        )

    with pytest.raises(ExperimentPackageVerificationError) as raised:
        verify_experiment_package(damaged)

    assert raised.value.code == expected_code

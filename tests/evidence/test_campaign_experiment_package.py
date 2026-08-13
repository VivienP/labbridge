from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest

from labbridge.domain.canonical import content_id
from labbridge.domain.provenance import Provenance
from labbridge.domain.results import metric_id, observation_id
from labbridge.evidence.campaign_package import (
    CampaignPackageInputs,
    build_campaign_experiment_package,
)
from labbridge.evidence.experiment_package import (
    ExperimentPackageVerificationError,
    verify_experiment_package,
)
from labbridge.evidence.manifest import canonical_json, digest
from labbridge.infrastructure.objectstore import InMemoryObjectStore


def _events(
    provenance: dict[str, object], observation: dict[str, object]
) -> list[dict[str, object]]:
    campaign_id = "00000000-0000-0000-0000-000000000001"
    attempt_id = str(observation["attempt_id"])
    work_item_id = str(observation["work_item_id"])
    job_id = "00000000-0000-0000-0000-000000000004"
    correlation_id = "00000000-0000-0000-0000-000000000011"
    at = "2026-08-13T00:00:00+00:00"
    reservation_id = "00000000-0000-0000-0000-000000000080"
    consumption_id = "00000000-0000-0000-0000-000000000081"
    candidate = {
        "kind": "her_location",
        "library_id": "library",
        "measurement_area_id": "area",
        "grid_x": {"value": "1", "unit": "mm"},
        "grid_y": {"value": "2", "unit": "mm"},
    }
    job = {
        "work_item_id": work_item_id,
        "state": "available",
        "available_at": at,
        "lease_owner": None,
        "lease_token": None,
        "lease_expires_at": None,
        "heartbeat_at": None,
        "lease_generation": 0,
        "attempt_count": 0,
        "max_attempts": 3,
        "command_version": "1",
        "idempotency_key": "instruction:one",
        "last_failure": None,
        "created_at": at,
        "updated_at": at,
    }
    payloads = [
        (
            "campaign.created",
            "campaign",
            campaign_id,
            1,
            {
                "name": "campaign",
                "environment_id": "her-replay",
                "adapter_version": "1",
                "data_origin": "observed",
                "execution_mode": "replay",
                "declaration": {
                    "name": "campaign",
                    "budget": {"hard_budget": "2", "budget_unit": "credit"},
                },
                "declaration_hash": "e" * 64,
                "state": "active",
            },
        ),
        (
            "work_item.queued",
            "work_item",
            work_item_id,
            1,
            {"candidate_id": "cand:one", "candidate": candidate, "state": "queued"},
        ),
        ("job.enqueued", "job", job_id, 1, job),
        (
            "budget.reserved",
            "budget",
            reservation_id,
            1,
            {
                "entry_id": reservation_id,
                "work_item_id": work_item_id,
                "job_id": job_id,
                "attempt_id": None,
                "lease_generation": 1,
                "reservation_entry_id": None,
                "kind": "reserved",
                "amount": "1",
                "unit": "credit",
                "reason": "attempt reservation",
                "recorded_at": at,
            },
        ),
        (
            "job.leased",
            "job",
            job_id,
            2,
            {
                **job,
                "state": "leased",
                "lease_owner": "worker",
                "lease_token": "00000000-0000-0000-0000-000000000005",
                "lease_expires_at": at,
                "lease_generation": 1,
                "attempt_count": 1,
            },
        ),
        (
            "attempt.started",
            "attempt",
            attempt_id,
            1,
            {
                "work_item_id": work_item_id,
                "job_id": job_id,
                "ordinal": 1,
                "state": "running",
                "started_at": at,
                "created_at": at,
            },
        ),
        (
            "job.started",
            "job",
            job_id,
            3,
            {
                **job,
                "state": "running",
                "lease_owner": "worker",
                "lease_token": "00000000-0000-0000-0000-000000000005",
                "lease_expires_at": at,
                "lease_generation": 1,
                "attempt_count": 1,
            },
        ),
        (
            "observation.accepted",
            "attempt",
            attempt_id,
            2,
            {
                **{key: value for key, value in observation.items() if key != "campaign_id"},
                "attempt_id": attempt_id,
                "work_item_id": work_item_id,
                "received_at": at,
            },
        ),
        (
            "attempt.completed",
            "attempt",
            attempt_id,
            3,
            {
                "work_item_id": work_item_id,
                "job_id": job_id,
                "ordinal": 1,
                "campaign_id": campaign_id,
                "state": "succeeded",
                "status": "succeeded",
                "observation_id": observation["observation_id"],
                "failure": None,
                "cost": {},
                "data_origin": "observed",
                "execution_mode": "replay",
                "provenance": provenance,
                "started_at": at,
                "finished_at": at,
            },
        ),
        (
            "budget.consumed",
            "budget",
            reservation_id,
            2,
            {
                "entry_id": consumption_id,
                "work_item_id": work_item_id,
                "job_id": job_id,
                "attempt_id": attempt_id,
                "lease_generation": 1,
                "reservation_entry_id": reservation_id,
                "kind": "consumed",
                "amount": "1",
                "unit": "credit",
                "reason": "attempt completed",
                "recorded_at": at,
            },
        ),
    ]
    events = []
    for position, (event_type, aggregate_type, aggregate_id, sequence, payload) in enumerate(
        payloads, start=1
    ):
        events.append(
            {
                "event_id": f"00000000-0000-0000-0000-{position:012d}",
                "campaign_id": campaign_id,
                "aggregate_id": aggregate_id,
                "aggregate_type": aggregate_type,
                "sequence": sequence,
                "campaign_position": position,
                "event_type": event_type,
                "schema_version": 1,
                "occurred_at": at,
                "recorded_at": at,
                "correlation_id": correlation_id,
                "causation_id": (
                    None if position == 1 else f"00000000-0000-0000-0000-{position - 1:012d}"
                ),
                "idempotency_key": None,
                "payload": payload,
            }
        )
    return events


def _inputs() -> CampaignPackageInputs:
    provenance = {
        "environment": {
            "environment_id": "her-replay",
            "adapter_version": "1",
            "data_origin": "observed",
            "execution_mode": "replay",
        },
        "source_record": {
            "doi": "10.0000/example",
            "record_version": "1",
            "source_filename": "source.csv",
            "source_sha256": "a" * 64,
            "source_path": "source.csv",
            "source_type": "measured_lsv",
            "parsing_version": "1",
        },
        "synthetic_root": None,
        "code_version": "1",
        "config_hash": "b" * 64,
        "parent_ids": [],
    }
    observation_identity = observation_id(
        sha256="c" * 64,
        schema_version="1",
        signal_kind="lsv",
        quantities=(),
        provenance=Provenance.model_validate(provenance),
    )
    observation = {
        "observation_id": observation_identity,
        "campaign_id": "00000000-0000-0000-0000-000000000001",
        "attempt_id": "00000000-0000-0000-0000-000000000002",
        "work_item_id": "00000000-0000-0000-0000-000000000003",
        "sha256": "c" * 64,
        "byte_size": 3,
        "object_uri": "s3://labbridge/observations/one.bin",
        "media_type": "application/octet-stream",
        "schema_version": "1",
        "signal_kind": "lsv",
        "quantities": [],
        "status": "accepted",
        "status_reason": None,
        "data_origin": "observed",
        "execution_mode": "replay",
        "provenance": provenance,
        "received_at": "2026-08-13T00:00:00+00:00",
    }
    metric_provenance = {**provenance, "parent_ids": [observation_identity]}
    campaign_id = "00000000-0000-0000-0000-000000000001"
    metric_identity = metric_id(
        observation_id=observation_identity,
        attempt_id=str(observation["attempt_id"]),
        name="activity",
        analysis_name="labbridge_activity",
        analysis_version="1",
        parameter_hash="d" * 64,
    )
    metric = {
        "metric_id": metric_identity,
        "observation_id": observation_identity,
        "attempt_id": observation["attempt_id"],
        "name": "activity",
        "value": {"value": "1", "unit": "A"},
        "unit": "A",
        "normalisation_basis": None,
        "uncertainty": None,
        "analysis_name": "labbridge_activity",
        "analysis_version": "1",
        "parameter_hash": "d" * 64,
        "quality_status": "accepted",
        "quality_reason": None,
        "environment_id": "her-replay",
        "data_origin": "observed",
        "execution_mode": "replay",
        "provenance": metric_provenance,
        "created_at": "2026-08-13T00:00:00+00:00",
    }
    metric_relation = {
        "relation_id": content_id(
            "record-relation",
            {
                "subject_id": metric_identity,
                "predicate": "derived_from",
                "object_id": observation_identity,
            },
        ),
        "subject_id": metric_identity,
        "predicate": "derived_from",
        "object_id": observation_identity,
        "reason": "metric derived from observation",
        "recorded_at": metric["created_at"],
    }
    return CampaignPackageInputs(
        campaign_id=campaign_id,
        declaration={
            "name": "campaign",
            "budget": {"hard_budget": "2", "budget_unit": "credit"},
        },
        environment=provenance["environment"],
        events=_events(provenance, observation),
        attempts_outcomes=[
            {
                "attempt_id": observation["attempt_id"],
                "work_item_id": observation["work_item_id"],
                "campaign_id": campaign_id,
                "job_id": "00000000-0000-0000-0000-000000000004",
                "ordinal": 1,
                "attempt_state": "succeeded",
                "status": "succeeded",
                "observation_id": observation_identity,
                "failure": None,
                "cost": {
                    "estimated_duration": None,
                    "actual_duration": None,
                    "consumable_cost": None,
                    "compute_cost": None,
                    "budget_estimate": None,
                    "budget_reserved": None,
                    "budget_incurred": None,
                    "budget_actual": None,
                    "budget_released": None,
                },
                "data_origin": "observed",
                "execution_mode": "replay",
                "provenance": provenance,
                "started_at": "2026-08-13T00:00:00+00:00",
                "created_at": "2026-08-13T00:00:00+00:00",
                "finished_at": "2026-08-13T00:00:00+00:00",
            }
        ],
        observations=[observation],
        derived_metrics=[metric],
        relations=[metric_relation],
        budget_ledger=[
            {
                "entry_id": "00000000-0000-0000-0000-000000000080",
                "campaign_id": campaign_id,
                "work_item_id": observation["work_item_id"],
                "job_id": "00000000-0000-0000-0000-000000000004",
                "attempt_id": None,
                "lease_generation": 1,
                "reservation_entry_id": None,
                "kind": "reserved",
                "amount": "1",
                "unit": "credit",
                "reason": "attempt reservation",
                "recorded_at": "2026-08-13T00:00:00+00:00",
            },
            {
                "entry_id": "00000000-0000-0000-0000-000000000081",
                "campaign_id": campaign_id,
                "work_item_id": observation["work_item_id"],
                "job_id": "00000000-0000-0000-0000-000000000004",
                "attempt_id": observation["attempt_id"],
                "lease_generation": 1,
                "reservation_entry_id": "00000000-0000-0000-0000-000000000080",
                "kind": "consumed",
                "amount": "1",
                "unit": "credit",
                "reason": "attempt completed",
                "recorded_at": "2026-08-13T00:00:00+00:00",
            },
        ],
        failures_recoveries=[],
        source_inventory=[provenance["source_record"]],
        object_inventory=[
            {
                "bucket": "labbridge",
                "key": "observations/one.bin",
                "sha256": "c" * 64,
                "byte_size": 3,
                "media_type": "application/octet-stream",
                "lifecycle_state": "committed",
                "observation_id": observation_identity,
                "attempt_id": observation["attempt_id"],
                "object_uri": "s3://labbridge/observations/one.bin",
            }
        ],
        raw_results=[
            {
                "attempt_id": observation["attempt_id"],
                "outcome_status": "succeeded",
                "data_origin": "observed",
                "execution_mode": "replay",
                "observation": observation,
                "metrics": [metric],
            }
        ],
        report_html=(
            "<!doctype html><html><body>Campaign: 00000000-0000-0000-0000-000000000001; "
            "observed + replay; Attempts: 1; observations: 1; "
            "derived metrics: 1. "
            "No live instrument execution is covered.</body></html>"
        ),
        limitations=["No live instrument execution is covered."],
        producing_versions={"labbridge": "0.1.0", "campaign_package": "1"},
    )


def test_campaign_package_is_closed_and_dispatches_to_campaign_verification() -> None:
    built = build_campaign_experiment_package(_inputs())

    result = verify_experiment_package(built.archive_bytes)

    assert result.verified is True
    assert result.producer_kind == "campaign"
    assert result.lineage_closed is True
    with zipfile.ZipFile(io.BytesIO(built.archive_bytes)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["producer_kind"] == "campaign"
        assert "passport/passport.json" not in archive.namelist()


def test_campaign_package_rejects_missing_required_member() -> None:
    built = build_campaign_experiment_package(_inputs())
    output = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(built.archive_bytes)) as source,
        zipfile.ZipFile(output, "w") as target,
    ):
        for name in source.namelist():
            if name != "campaign/budget-ledger.json":
                target.writestr(name, source.read(name))

    with pytest.raises(ExperimentPackageVerificationError) as caught:
        verify_experiment_package(output.getvalue())

    assert caught.value.code == "package_member_missing"


def test_campaign_package_full_verification_reads_every_stored_object() -> None:
    payload = b"raw"
    sha256 = hashlib.sha256(payload).hexdigest()
    inputs = _inputs()
    provenance = Provenance.model_validate(inputs.observations[0]["provenance"])
    observation_identity = observation_id(
        sha256=sha256,
        schema_version=str(inputs.observations[0]["schema_version"]),
        signal_kind="lsv",
        quantities=(),
        provenance=provenance,
    )
    observation = {
        **inputs.observations[0],
        "observation_id": observation_identity,
        "sha256": sha256,
    }
    metric_provenance = {
        **inputs.derived_metrics[0]["provenance"],
        "parent_ids": [observation_identity],
    }
    metric = {
        **inputs.derived_metrics[0],
        "metric_id": metric_id(
            observation_id=observation_identity,
            attempt_id=str(observation["attempt_id"]),
            name=str(inputs.derived_metrics[0]["name"]),
            analysis_name=str(inputs.derived_metrics[0]["analysis_name"]),
            analysis_version=str(inputs.derived_metrics[0]["analysis_version"]),
            parameter_hash=str(inputs.derived_metrics[0]["parameter_hash"]),
        ),
        "observation_id": observation_identity,
        "provenance": metric_provenance,
    }
    outcome = {**inputs.attempts_outcomes[0], "observation_id": observation_identity}
    inventory = [
        {
            **inputs.object_inventory[0],
            "observation_id": observation_identity,
            "sha256": sha256,
        }
    ]
    relation = {
        **inputs.relations[0],
        "relation_id": content_id(
            "record-relation",
            {
                "subject_id": metric["metric_id"],
                "predicate": "derived_from",
                "object_id": observation_identity,
            },
        ),
        "subject_id": metric["metric_id"],
        "object_id": observation_identity,
    }
    raw = [
        {
            **inputs.raw_results[0],
            "observation": observation,
            "metrics": [metric],
        }
    ]
    built = build_campaign_experiment_package(
        inputs.model_copy(
            update={
                "events": _events(observation["provenance"], observation),
                "attempts_outcomes": [outcome],
                "observations": [observation],
                "derived_metrics": [metric],
                "relations": [relation],
                "object_inventory": inventory,
                "raw_results": raw,
            }
        )
    )
    store = InMemoryObjectStore()
    store.put_and_verify("observations/one.bin", payload, media_type="application/octet-stream")

    result = verify_experiment_package(built.archive_bytes, object_store=store)

    assert result.verification_scope == "full"
    assert result.objects_referenced == 1
    assert result.objects_verified == 1


def test_campaign_package_full_verification_rejects_missing_stored_object() -> None:
    built = build_campaign_experiment_package(_inputs())

    with pytest.raises(ExperimentPackageVerificationError) as caught:
        verify_experiment_package(built.archive_bytes, object_store=InMemoryObjectStore())

    assert caught.value.code == "object_missing"


def test_unfavourable_valid_result_remains_a_successful_observation() -> None:
    inputs = _inputs()
    metric = {**inputs.derived_metrics[0], "value": {"value": "-1", "unit": "A"}}
    raw = [{**inputs.raw_results[0], "metrics": [metric]}]
    built = build_campaign_experiment_package(
        inputs.model_copy(update={"derived_metrics": [metric], "raw_results": raw})
    )

    verification = verify_experiment_package(built.archive_bytes)

    assert verification.verified is True
    assert inputs.attempts_outcomes[0]["status"] == "succeeded"
    assert inputs.observations[0]["status"] == "accepted"


def _reclose_campaign_package(package_bytes: bytes, updates: dict[str, bytes]) -> bytes:
    with zipfile.ZipFile(io.BytesIO(package_bytes)) as archive:
        members = {
            name: archive.read(name) for name in archive.namelist() if name != "manifest.json"
        }
        manifest = json.loads(archive.read("manifest.json"))
    members.update(updates)
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
    with zipfile.ZipFile(output, "w") as archive:
        for name, data in sorted(members.items()):
            archive.writestr(name, data)
    return output.getvalue()


def test_campaign_package_rejects_semantically_invalid_exported_event() -> None:
    inputs = _inputs()
    built = build_campaign_experiment_package(inputs)
    event = {
        "campaign_position": 1,
        "event_id": "00000000-0000-0000-0000-000000000010",
        "campaign_id": inputs.campaign_id,
        "aggregate_id": inputs.campaign_id,
        "aggregate_type": "campaign",
        "sequence": 1,
        "event_type": "campaign.unknown",
        "schema_version": 1,
        "occurred_at": "2026-08-13T00:00:00+00:00",
        "recorded_at": "2026-08-13T00:00:00+00:00",
        "correlation_id": "00000000-0000-0000-0000-000000000011",
        "causation_id": None,
        "idempotency_key": None,
        "payload": {"state": "active"},
    }
    damaged = _reclose_campaign_package(
        built.archive_bytes,
        {
            "campaign/events.jsonl": json.dumps(
                event, sort_keys=True, separators=(",", ":")
            ).encode()
            + b"\n"
        },
    )

    with pytest.raises(ExperimentPackageVerificationError) as caught:
        verify_experiment_package(damaged)

    assert caught.value.code == "package_event_stream_invalid"


def test_campaign_package_rejects_outcome_that_differs_from_replay() -> None:
    inputs = _inputs()
    outcomes = [{**inputs.attempts_outcomes[0], "status": "duplicate_suppressed"}]
    built = build_campaign_experiment_package(
        inputs.model_copy(update={"attempts_outcomes": outcomes})
    )

    with pytest.raises(ExperimentPackageVerificationError) as caught:
        verify_experiment_package(built.archive_bytes)

    assert caught.value.code == "package_projection_mismatch"


def test_campaign_package_rejects_dangling_relation() -> None:
    inputs = _inputs()
    relation = {
        "relation_id": "00000000-0000-0000-0000-000000000099",
        "subject_id": "obs:one",
        "predicate": "invalidates",
        "object_id": "obs:absent",
        "reason": "invalid evidence",
        "recorded_at": "2026-08-13T00:00:00+00:00",
    }
    built = build_campaign_experiment_package(inputs.model_copy(update={"relations": [relation]}))

    with pytest.raises(ExperimentPackageVerificationError) as caught:
        verify_experiment_package(built.archive_bytes)

    assert caught.value.code == "package_lineage_open"


def test_campaign_package_inputs_reject_non_json_values() -> None:
    payload = _inputs().model_dump(mode="python")
    payload["declaration"] = {"unstable": object()}

    with pytest.raises(ValueError, match="JSON-compatible"):
        CampaignPackageInputs.model_validate(payload)


def test_campaign_package_repeated_build_is_byte_identical() -> None:
    inputs = _inputs()

    first = build_campaign_experiment_package(inputs)
    second = build_campaign_experiment_package(inputs)

    assert first.archive_bytes == second.archive_bytes


def test_campaign_package_rejects_report_count_mismatch() -> None:
    inputs = _inputs()
    built = build_campaign_experiment_package(
        inputs.model_copy(
            update={"report_html": inputs.report_html.replace("Attempts: 1", "Attempts: 9")}
        )
    )

    with pytest.raises(ExperimentPackageVerificationError) as caught:
        verify_experiment_package(built.archive_bytes)

    assert caught.value.code == "package_report_mismatch"


def test_campaign_package_rejects_budget_ledger_that_differs_from_replay() -> None:
    inputs = _inputs()
    ledger = [
        {
            "entry_id": "00000000-0000-0000-0000-000000000080",
            "kind": "reserved",
            "amount": "1",
            "unit": "credit",
            "reservation_entry_id": None,
        }
    ]
    built = build_campaign_experiment_package(inputs.model_copy(update={"budget_ledger": ledger}))

    with pytest.raises(ExperimentPackageVerificationError) as caught:
        verify_experiment_package(built.archive_bytes)

    assert caught.value.code == "package_projection_mismatch"


def test_campaign_package_rejects_failure_summary_not_derived_from_records() -> None:
    inputs = _inputs()
    built = build_campaign_experiment_package(
        inputs.model_copy(
            update={
                "failures_recoveries": [
                    {
                        "kind": "failure",
                        "attempt_id": inputs.attempts_outcomes[0]["attempt_id"],
                        "status": "timed_out",
                        "failure": None,
                    }
                ]
            }
        )
    )

    with pytest.raises(ExperimentPackageVerificationError) as caught:
        verify_experiment_package(built.archive_bytes)

    assert caught.value.code == "package_projection_mismatch"


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("campaign_id", "00000000-0000-0000-0000-000000000099"),
        ("job_id", "00000000-0000-0000-0000-000000000099"),
        ("ordinal", 2),
        ("attempt_state", "failed_terminal"),
        ("cost", {"budget_actual": {"value": "9", "unit": "credit"}}),
        ("failure", {"failure_code": "tampered"}),
        ("provenance", None),
        ("started_at", "2026-08-13T00:00:01+00:00"),
        ("created_at", "2026-08-13T00:00:01+00:00"),
        ("finished_at", "2026-08-13T00:00:01+00:00"),
    ],
)
def test_campaign_package_rejects_resigned_attempt_projection_tamper(
    field: str, replacement: object
) -> None:
    inputs = _inputs()
    outcomes = [{**inputs.attempts_outcomes[0], field: replacement}]
    damaged = _reclose_campaign_package(
        build_campaign_experiment_package(inputs).archive_bytes,
        {"campaign/attempts-outcomes.json": canonical_json(outcomes)},
    )

    with pytest.raises(ExperimentPackageVerificationError) as caught:
        verify_experiment_package(damaged)

    assert caught.value.code in {"package_projection_mismatch", "package_lineage_open"}


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("campaign_id", "00000000-0000-0000-0000-000000000099"),
        ("media_type", "text/plain"),
        ("schema_version", "2"),
        ("signal_kind", "open_circuit_potential"),
        ("quantities", [{"name": "current", "unit": "A", "axis": 0}]),
        ("provenance", None),
        ("received_at", "2026-08-13T00:00:01+00:00"),
    ],
)
def test_campaign_package_rejects_resigned_observation_projection_tamper(
    field: str, replacement: object
) -> None:
    inputs = _inputs()
    observations = [{**inputs.observations[0], field: replacement}]
    damaged = _reclose_campaign_package(
        build_campaign_experiment_package(inputs).archive_bytes,
        {"campaign/observations.json": canonical_json(observations)},
    )

    with pytest.raises(ExperimentPackageVerificationError) as caught:
        verify_experiment_package(damaged)

    assert caught.value.code in {
        "package_projection_mismatch",
        "package_lineage_open",
        "package_origin_mismatch",
    }


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("work_item_id", "00000000-0000-0000-0000-000000000099"),
        ("job_id", "00000000-0000-0000-0000-000000000099"),
        ("lease_generation", 9),
        ("reason", "tampered reason"),
        ("recorded_at", "2026-08-13T00:00:01+00:00"),
    ],
)
def test_campaign_package_rejects_resigned_budget_entry_tamper(
    field: str, replacement: object
) -> None:
    inputs = _inputs()
    ledger = [{**inputs.budget_ledger[0], field: replacement}, *inputs.budget_ledger[1:]]
    damaged = _reclose_campaign_package(
        build_campaign_experiment_package(inputs).archive_bytes,
        {"campaign/budget-ledger.json": canonical_json(ledger)},
    )

    with pytest.raises(ExperimentPackageVerificationError) as caught:
        verify_experiment_package(damaged)

    assert caught.value.code == "package_projection_mismatch"


@pytest.mark.parametrize(
    ("field", "replacement"),
    [("metric_id", "metric:tampered"), ("name", ""), ("unit", "V")],
)
def test_campaign_package_rejects_resigned_noncanonical_metric(
    field: str, replacement: object
) -> None:
    inputs = _inputs()
    metrics = [{**inputs.derived_metrics[0], field: replacement}]
    raw = [{**inputs.raw_results[0], "metrics": metrics}]
    damaged = _reclose_campaign_package(
        build_campaign_experiment_package(inputs).archive_bytes,
        {
            "campaign/derived-metrics.json": canonical_json(metrics),
            "campaign/raw-results.json": canonical_json(raw),
        },
    )

    with pytest.raises(ExperimentPackageVerificationError) as caught:
        verify_experiment_package(damaged)

    assert caught.value.code in {"package_projection_mismatch", "package_lineage_open"}


def test_campaign_package_rejects_resigned_duplicate_metric_identity() -> None:
    inputs = _inputs()
    metrics = [inputs.derived_metrics[0], inputs.derived_metrics[0]]
    raw = [{**inputs.raw_results[0], "metrics": metrics}]
    damaged = _reclose_campaign_package(
        build_campaign_experiment_package(inputs).archive_bytes,
        {
            "campaign/derived-metrics.json": canonical_json(metrics),
            "campaign/raw-results.json": canonical_json(raw),
        },
    )

    with pytest.raises(ExperimentPackageVerificationError) as caught:
        verify_experiment_package(damaged)

    assert caught.value.code == "package_lineage_open"


def test_campaign_package_rejects_missing_metric_relation() -> None:
    inputs = _inputs()
    damaged = _reclose_campaign_package(
        build_campaign_experiment_package(inputs).archive_bytes,
        {"campaign/relations.json": canonical_json([])},
    )

    with pytest.raises(ExperimentPackageVerificationError) as caught:
        verify_experiment_package(damaged)

    assert caught.value.code == "package_lineage_open"


def test_campaign_package_rejects_missing_event_derived_relation() -> None:
    inputs = _inputs()
    event = {
        "event_id": "00000000-0000-0000-0000-000000000099",
        "campaign_id": inputs.campaign_id,
        "aggregate_id": inputs.campaign_id,
        "aggregate_type": "campaign",
        "sequence": 2,
        "campaign_position": len(inputs.events) + 1,
        "event_type": "observation.invalidated",
        "schema_version": 1,
        "occurred_at": "2026-08-13T00:00:00+00:00",
        "recorded_at": "2026-08-13T00:00:00+00:00",
        "correlation_id": inputs.events[-1]["correlation_id"],
        "causation_id": inputs.events[-1]["event_id"],
        "idempotency_key": None,
        "payload": {
            "relation_id": "00000000-0000-0000-0000-000000000098",
            "subject_id": inputs.observations[0]["observation_id"],
            "predicate": "invalidates",
            "object_id": inputs.derived_metrics[0]["metric_id"],
            "reason": "invalidated derived interpretation",
            "recorded_at": "2026-08-13T00:00:00+00:00",
        },
    }
    built = build_campaign_experiment_package(
        inputs.model_copy(
            update={
                "events": [*inputs.events, event],
                "relations": [*inputs.relations, event["payload"]],
            }
        )
    )
    damaged = _reclose_campaign_package(
        built.archive_bytes,
        {"campaign/relations.json": canonical_json(inputs.relations)},
    )

    with pytest.raises(ExperimentPackageVerificationError) as caught:
        verify_experiment_package(damaged)

    assert caught.value.code == "package_lineage_open"


def test_campaign_package_rejects_unused_source_root() -> None:
    inputs = _inputs()
    unused = {
        **inputs.source_inventory[0],
        "source_filename": "unused.csv",
        "source_path": "unused.csv",
        "source_sha256": "f" * 64,
    }
    damaged = _reclose_campaign_package(
        build_campaign_experiment_package(inputs).archive_bytes,
        {"campaign/source-inventory.json": canonical_json([*inputs.source_inventory, unused])},
    )

    with pytest.raises(ExperimentPackageVerificationError) as caught:
        verify_experiment_package(damaged)

    assert caught.value.code == "package_lineage_open"

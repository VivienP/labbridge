"""Execute the seeded Phase 7 reliability campaign against PostgreSQL and S3 storage."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, func, select

from labbridge.demo import _candidate, _submit
from labbridge.domain.campaigns import CampaignDeclaration
from labbridge.domain.canonical import canonical_bytes
from labbridge.environments.her_replay import HerReplayAdapter
from labbridge.evidence.campaign_package import (
    build_campaign_experiment_package,
    campaign_package_inputs_from_postgres,
)
from labbridge.infrastructure.objectstore import S3ObjectStore
from labbridge.infrastructure.persistence.tables import (
    attempt_outcomes,
    attempts,
    campaigns,
    events,
    observations,
    storage_objects,
    work_items,
)
from labbridge.reliability.fault_campaign import FaultCampaignResult, FaultPoint
from labbridge.reliability.package_export import publish_verified_campaign_package
from labbridge.runtime.budgets import budget_usage
from labbridge.runtime.events import append_event, current_sequence
from labbridge.runtime.jobs import enqueue
from labbridge.runtime.replay import compare_campaign_projection
from labbridge.runtime.worker import Worker


@dataclass(frozen=True)
class KilledProcess:
    state: dict[str, Any]
    pid: int
    exit_code: int
    started_at: datetime
    checkpoint_at: datetime
    killed_at: datetime
    stdout: str
    stderr: str


@dataclass(frozen=True)
class CampaignExecution:
    result: FaultCampaignResult
    package_path: Path
    audit: dict[str, object]


def _now() -> datetime:
    return datetime.now(UTC)


def _read_state(path: Path) -> dict[str, Any]:
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")))
    except (FileNotFoundError, PermissionError, json.JSONDecodeError):
        return {}


def _kill_at_checkpoint(
    argv: list[str], state_path: Path, *, repo_root: Path, extra_env: dict[str, str]
) -> KilledProcess:
    started = _now()
    process = subprocess.Popen(
        argv,
        cwd=repo_root,
        env={
            **os.environ,
            **extra_env,
            "PYTHONPATH": os.pathsep.join((str(repo_root / "src"), str(repo_root))),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    checkpoint = started
    state: dict[str, Any] = {}
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            state = _read_state(state_path)
            if state.get("reached"):
                checkpoint = _now()
                break
            if process.poll() is not None:
                _, stderr = process.communicate()
                raise RuntimeError(
                    "fault subprocess exited before its checkpoint: "
                    + stderr.decode(errors="replace")
                )
            time.sleep(0.05)
        else:
            raise TimeoutError("fault subprocess did not reach its checkpoint")
    finally:
        killed = _now()
        process.kill()
        stdout, stderr = process.communicate(timeout=30)
    return KilledProcess(
        state=state,
        pid=process.pid,
        exit_code=int(process.returncode),
        started_at=started,
        checkpoint_at=checkpoint,
        killed_at=killed,
        stdout=stdout.decode(errors="replace"),
        stderr=stderr.decode(errors="replace"),
    )


def _declare_campaign(engine: Engine, adapter: HerReplayAdapter, *, name: str) -> uuid.UUID:
    campaign_id = uuid.uuid4()
    declaration = CampaignDeclaration(
        hard_budget=Decimal("3"),
        per_attempt_estimate=Decimal("1"),
        budget_unit="attempt",
        max_attempts=3,
        stopping_rule="hard_budget_exhausted",
    )
    declaration_payload = {
        "fault_campaign": True,
        "budget": declaration.model_dump(mode="json"),
    }
    declaration_hash = hashlib.sha256(canonical_bytes(declaration_payload)).hexdigest()
    environment = adapter.environment
    with engine.begin() as connection:
        correlation_id = uuid.uuid4()
        connection.execute(
            campaigns.insert().values(
                campaign_id=campaign_id,
                name=name,
                environment_id=environment.environment_id,
                adapter_version=environment.adapter_version,
                data_origin=environment.data_origin,
                execution_mode=environment.execution_mode,
                state="active",
                declaration=declaration_payload,
                declaration_hash=declaration_hash,
                hard_budget=declaration.hard_budget,
                per_attempt_estimate=declaration.per_attempt_estimate,
                budget_unit=declaration.budget_unit,
                max_attempts=declaration.max_attempts,
                stopping_rule=declaration.stopping_rule,
                event_stream_contract_version=2,
                event_stream_last_position=0,
                created_at=func.now(),
                updated_at=func.now(),
            )
        )
        append_event(
            connection,
            campaign_id=campaign_id,
            aggregate_id=campaign_id,
            aggregate_type="campaign",
            event_type="campaign.created",
            payload={
                "name": name,
                "environment_id": environment.environment_id,
                "adapter_version": environment.adapter_version,
                "data_origin": environment.data_origin,
                "execution_mode": environment.execution_mode,
                "declaration": declaration_payload,
                "declaration_hash": declaration_hash,
                "state": "active",
            },
            expected_version=0,
            correlation_id=correlation_id,
            causation_id=None,
        )
    return campaign_id


async def _drain_campaign(
    engine: Engine,
    adapter: HerReplayAdapter,
    store: S3ObjectStore,
    work_item_id: uuid.UUID,
    *,
    worker_name: str,
) -> int:
    worker = Worker(engine, adapter, store, name=worker_name, lease_seconds=5)
    report = worker.start()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with engine.begin() as connection:
            terminal = connection.execute(
                select(func.count())
                .select_from(attempt_outcomes)
                .where(
                    attempt_outcomes.c.work_item_id == work_item_id,
                    attempt_outcomes.c.status.in_(("succeeded", "failed_terminal", "cancelled")),
                )
            ).scalar_one()
        if terminal:
            return len(report.reclaimed)
        await worker.run_once()
        await asyncio.sleep(0.1)
    raise TimeoutError(f"campaign work item {work_item_id} did not reach a terminal outcome")


async def _redeliver(
    engine: Engine,
    adapter: HerReplayAdapter,
    store: S3ObjectStore,
    campaign_id: uuid.UUID,
    work_item_id: uuid.UUID,
) -> None:
    with engine.begin() as connection:
        context = connection.execute(
            select(events.c.event_id, events.c.correlation_id)
            .where(events.c.campaign_id == campaign_id, events.c.aggregate_id == work_item_id)
            .order_by(events.c.sequence.desc())
            .limit(1)
        ).one()
        enqueue(
            connection,
            campaign_id=campaign_id,
            work_item_id=work_item_id,
            instruction_key=f"fault-campaign-redelivery:{uuid.uuid4().hex}",
            command_version="1",
            correlation_id=context.correlation_id,
            causation_id=context.event_id,
        )
    outcome = await Worker(
        engine, adapter, store, name=f"fault-redelivery-{campaign_id.hex[:8]}"
    ).run_once()
    if outcome is None or outcome.status != "duplicate_suppressed":
        raise RuntimeError("fault-campaign redelivery did not reach duplicate_suppressed")


def _complete_campaign(engine: Engine, campaign_id: uuid.UUID) -> None:
    with engine.begin() as connection:
        context = connection.execute(
            select(events.c.event_id, events.c.correlation_id)
            .where(events.c.campaign_id == campaign_id)
            .order_by(events.c.campaign_position.desc())
            .limit(1)
        ).one()
        connection.execute(
            campaigns.update()
            .where(campaigns.c.campaign_id == campaign_id)
            .values(state="completed", updated_at=func.now())
        )
        append_event(
            connection,
            campaign_id=campaign_id,
            aggregate_id=campaign_id,
            aggregate_type="campaign",
            event_type="campaign.completed",
            payload={"state": "completed", "reason": "fault_campaign_work_terminal"},
            expected_version=current_sequence(
                connection,
                campaign_id=campaign_id,
                aggregate_type="campaign",
                aggregate_id=campaign_id,
            ),
            correlation_id=context.correlation_id,
            causation_id=context.event_id,
        )


def _measurement(connection, campaign_id: uuid.UUID) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    outcomes = connection.execute(
        select(attempt_outcomes.c.status, attempt_outcomes.c.failure).where(
            attempt_outcomes.c.campaign_id == campaign_id
        )
    ).all()
    observation_rows = connection.execute(
        select(observations.c.status).where(observations.c.campaign_id == campaign_id)
    ).scalars()
    observation_statuses = list(observation_rows)
    staged = connection.execute(
        select(func.count())
        .select_from(
            storage_objects.join(
                work_items, storage_objects.c.work_item_id == work_items.c.work_item_id
            )
        )
        .where(work_items.c.campaign_id == campaign_id)
    ).scalar_one()
    usage = budget_usage(connection, campaign_id)
    failures = tuple(
        sorted(
            str(outcome.failure.get("failure_code"))
            for outcome in outcomes
            if isinstance(outcome.failure, dict) and outcome.failure.get("failure_code")
        )
    )
    return {
        "attempts_created": connection.execute(
            select(func.count())
            .select_from(
                attempts.join(work_items, attempts.c.work_item_id == work_items.c.work_item_id)
            )
            .where(work_items.c.campaign_id == campaign_id)
        ).scalar_one(),
        "observations_staged": staged,
        "observations_received": observation_statuses.count("received"),
        "observations_accepted": observation_statuses.count("accepted"),
        "corrupted_receipts": observation_statuses.count("corrupted"),
        "accepted_outcomes": sum(row.status == "succeeded" for row in outcomes),
        "duplicate_suppressions": sum(row.status == "duplicate_suppressed" for row in outcomes),
        "hard_budget": usage.hard_limit,
        "budget_committed": usage.consumed + usage.outstanding,
        "budget_reserved": usage.reserved,
        "budget_consumed": usage.consumed,
        "budget_released": usage.released,
        "failure_codes": failures,
    }


async def execute_one(
    *,
    engine: Engine,
    adapter: HerReplayAdapter,
    store: S3ObjectStore,
    repo_root: Path,
    fixture_root: Path,
    output_root: Path,
    run_id: str,
    ordinal: int,
    seed: int,
    fault_point: FaultPoint,
    subprocess_env: dict[str, str],
    s3_client: Any,
) -> CampaignExecution:
    campaign_id = _declare_campaign(
        engine, adapter, name=f"Phase 7 fault campaign {ordinal:03d} seed {seed}"
    )
    keys = adapter.known_locations()
    key = keys[seed % len(keys)]
    _submit(engine, campaign_id, _candidate(key.library_id, key.measurement_area_id))
    with engine.begin() as connection:
        work_item_id = connection.execute(
            select(work_items.c.work_item_id).where(work_items.c.campaign_id == campaign_id)
        ).scalar_one()

    state_path = output_root / "process-state" / f"{ordinal:03d}.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    if fault_point == "during_evidence_export":
        lease_recoveries = await _drain_campaign(
            engine,
            adapter,
            store,
            work_item_id,
            worker_name=f"fault-normal-{ordinal:03d}",
        )
        await _redeliver(engine, adapter, store, campaign_id, work_item_id)
        _complete_campaign(engine, campaign_id)
        incomplete_destination = output_root / "interrupted" / f"{campaign_id}.zip"
        killed = _kill_at_checkpoint(
            [
                sys.executable,
                str(repo_root / "scripts" / "campaign_package_subprocess.py"),
                str(campaign_id),
                str(incomplete_destination),
                str(state_path),
                store.bucket,
            ],
            state_path,
            repo_root=repo_root,
            extra_env=subprocess_env,
        )
        partial = Path(str(killed.state["partial_path"]))
        if incomplete_destination.exists():
            raise RuntimeError("interrupted evidence export exposed a final archive")
        await asyncio.to_thread(partial.unlink, missing_ok=True)
        restart_at = _now()
    else:
        killed = _kill_at_checkpoint(
            [
                sys.executable,
                str(repo_root / "tests" / "integration" / "worker_subprocess.py"),
                str(fixture_root),
                str(state_path),
                fault_point,
            ],
            state_path,
            repo_root=repo_root,
            extra_env={
                **subprocess_env,
                "LABBRIDGE_WORKER_NAME": f"fault-worker-{ordinal:03d}",
                "LABBRIDGE_LEASE_SECONDS": "1",
                "LABBRIDGE_HEARTBEAT_SECONDS": "5",
                "LABBRIDGE_FAULT_BUCKET": store.bucket,
            },
        )
        if fault_point == "during_object_upload":
            s3_client.abort_multipart_upload(
                Bucket=store.bucket,
                Key=killed.state["object_keys"][0],
                UploadId=killed.state["multipart_upload_id"],
            )
        await asyncio.sleep(1.25)
        lease_recoveries = await _drain_campaign(
            engine,
            adapter,
            store,
            work_item_id,
            worker_name=f"fault-survivor-{ordinal:03d}",
        )
        await _redeliver(engine, adapter, store, campaign_id, work_item_id)
        _complete_campaign(engine, campaign_id)
        restart_at = _now()

    with engine.begin() as connection:
        comparison = compare_campaign_projection(connection, campaign_id)
        inputs = campaign_package_inputs_from_postgres(
            connection,
            campaign_id,
            producing_versions={"labbridge": "0.1.0", "campaign_package": "1"},
            limitations=[
                "Synthetic fixture replay does not establish physical-system performance.",
                "The campaign measures the listed process boundaries in the stated "
                "environment only.",
            ],
        )
        measured = _measurement(connection, campaign_id)
    package = build_campaign_experiment_package(inputs)
    package_path = output_root / "packages" / f"{ordinal:03d}-{campaign_id}.zip"
    verification = publish_verified_campaign_package(package, package_path, object_store=store)
    completed_at = _now()
    result = FaultCampaignResult(
        run_id=run_id,
        ordinal=ordinal,
        seed=seed,
        campaign_id=campaign_id,
        fault_point=fault_point,
        data_origin=adapter.environment.data_origin,
        execution_mode=adapter.environment.execution_mode,
        process_pid=killed.pid,
        process_exit_code=killed.exit_code,
        process_started_at=killed.started_at,
        checkpoint_reached_at=killed.checkpoint_at,
        process_killed_at=killed.killed_at,
        restart_completed_at=max(restart_at, completed_at),
        lease_recoveries=lease_recoveries,
        replay_equal=comparison.matches,
        package_verified=verification.verified,
        objects_referenced=verification.objects_referenced,
        objects_verified=verification.objects_verified,
        package_id=verification.package_id,
        package_sha256=verification.archive_sha256,
        exclusions=(),
        **measured,
    )
    return CampaignExecution(
        result=result,
        package_path=package_path,
        audit={
            "ordinal": ordinal,
            "campaign_id": str(campaign_id),
            "fault_point": fault_point,
            "process_state": killed.state,
            "stdout": killed.stdout,
            "stderr": killed.stderr,
            "multipart_aborted": fault_point == "during_object_upload",
            "partial_export_removed": fault_point == "during_evidence_export",
            "replay_mismatches": [
                mismatch.model_dump(mode="json") for mismatch in comparison.mismatches
            ],
        },
    )


__all__ = ["CampaignExecution", "execute_one"]

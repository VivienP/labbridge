"""Putting the runtime back into an explainable state after something stopped existing.

Three kinds of debris outlive a killed worker, and each needs a different answer:

* **a lease nobody holds** — the job is stuck until the lease is taken back. Reclaiming it bumps the
  fencing generation, which is what stops the dead worker's ghost from finalising if it ever wakes;
* **an attempt still marked `running`** — its process is gone, so it will never finish. Left alone
  it is indistinguishable from work in flight, and `docs/FAILURE_MATRIX.md` requires that no known
  failure leave an attempt there. It becomes `lease_lost`, which is what actually happened;
* **bytes in the store with nothing pointing at them** — classified rather than deleted. Which of
  the five verdicts applies is decided by `labbridge.domain.objects`, from facts gathered here.

**Deletion is never a recovery action.** Every verdict retains the bytes. An object that cannot be
explained is quarantined, not removed, because the unexplained object *is* the evidence.

This is a function, not a daemon. It runs once when a worker starts and behind `labbridge
reconcile`, so there is one implementation and no second process to supervise.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, Row, and_, func, or_, select, update

from labbridge.domain.identity import EnvironmentRef
from labbridge.domain.objects import ObjectFacts, ObjectVerdict, classify_object
from labbridge.domain.provenance import Provenance
from labbridge.infrastructure.objectstore import ObjectStore, ObjectStoreError, digest
from labbridge.infrastructure.persistence.tables import (
    attempt_outcomes,
    attempts,
    campaigns,
    events,
    experiment_packages,
    experiment_passports,
    jobs,
    normalised_cv_observations,
    observations,
    source_artifacts,
    storage_objects,
    work_items,
)
from labbridge.runtime.events import append_event, current_sequence
from labbridge.runtime.jobs import ReclaimedLease, recover_expired_leases


@dataclass(frozen=True)
class ClassifiedObject:
    object_uri: str
    classification: str
    reason: str


@dataclass
class ReconciliationReport:
    """What one pass did. Returned rather than logged so a caller can assert on it."""

    reclaimed: list[ReclaimedLease] = field(default_factory=list)
    closed_attempts: list[uuid.UUID] = field(default_factory=list)
    classified: list[ClassifiedObject] = field(default_factory=list)
    #: Objects the store could not be asked about. Left unclassified on purpose: a verdict reached
    #: while storage was unreachable would record an outage as a fact about the bytes.
    unreachable: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for entry in self.classified:
            tally[entry.classification] = tally.get(entry.classification, 0) + 1
        return tally


def close_abandoned_attempts(connection: Connection) -> list[uuid.UUID]:
    """Finish attempts whose job is no longer leased by anyone.

    An attempt is abandoned when it is still `running` while its job holds no live lease — the
    worker that owned it is gone. `lease_lost` is the accurate terminal state: the attempt did not
    fail on its merits, it lost the right to finish. Deliberately **not** `cancelled`, which means a
    campaign or an operator asked for the work to stop.

    The outcome is written first and the attempt second, so the pair is never observed the other way
    round. Both are in the caller's transaction.
    """
    rows = connection.execute(
        select(
            attempts.c.attempt_id,
            attempts.c.work_item_id,
            attempts.c.job_id,
            attempts.c.started_at,
            work_items.c.campaign_id,
            campaigns.c.environment_id,
            campaigns.c.adapter_version,
            campaigns.c.data_origin,
            campaigns.c.execution_mode,
            jobs.c.event_correlation_id,
            jobs.c.last_event_id,
        )
        .select_from(
            attempts.join(jobs, jobs.c.job_id == attempts.c.job_id)
            .join(work_items, work_items.c.work_item_id == attempts.c.work_item_id)
            .join(campaigns, campaigns.c.campaign_id == work_items.c.campaign_id)
        )
        .where(
            attempts.c.state == "running",
            # No live lease: either nobody holds the job, or the hold has lapsed. Both mean the
            # process that owned this attempt is not coming back to finish it.
            or_(jobs.c.lease_expires_at.is_(None), jobs.c.lease_expires_at <= func.now()),
        )
        .with_for_update(of=attempts)
    ).all()

    closed: list[uuid.UUID] = []
    for row in rows:
        existing = connection.execute(
            select(attempt_outcomes.c.status).where(attempt_outcomes.c.attempt_id == row.attempt_id)
        ).one_or_none()
        if existing is not None:
            # It reached an outcome and only the attempt row lagged; align the attempt rather than
            # inventing a second outcome for it.
            connection.execute(
                attempts.update()
                .where(attempts.c.attempt_id == row.attempt_id)
                .values(state=existing.status)
            )
            closed.append(row.attempt_id)
            continue
        provenance = Provenance(
            environment=EnvironmentRef(
                environment_id=row.environment_id,
                adapter_version=row.adapter_version,
                data_origin=row.data_origin,
                execution_mode=row.execution_mode,
            ),
            code_version="1",
            config_hash=row.adapter_version,
        ).model_dump(mode="json")
        failure = {
            "failure_code": "lease_lost",
            "category": "worker",
            "retryable": False,
            "summary": "the worker stopped holding this job before the attempt reached an outcome",
            "exception_type": None,
        }
        outcome = connection.execute(
            attempt_outcomes.insert()
            .values(
                attempt_id=row.attempt_id,
                work_item_id=row.work_item_id,
                campaign_id=row.campaign_id,
                status="lease_lost",
                observation_id=None,
                failure=failure,
                cost={},
                data_origin=row.data_origin,
                execution_mode=row.execution_mode,
                provenance=provenance,
                started_at=row.started_at,
                finished_at=func.now(),
            )
            .returning(attempt_outcomes.c.finished_at)
        ).one()
        connection.execute(
            attempts.update()
            .where(attempts.c.attempt_id == row.attempt_id)
            .values(state="lease_lost")
        )
        if row.event_correlation_id is None or row.last_event_id is None:
            raise RuntimeError(f"job {row.job_id} has no complete event context")
        latest_attempt_event = connection.execute(
            select(events.c.event_id)
            .where(
                events.c.aggregate_type == "attempt",
                events.c.aggregate_id == row.attempt_id,
            )
            .order_by(events.c.sequence.desc())
            .limit(1)
        ).scalar_one_or_none()
        append_event(
            connection,
            campaign_id=row.campaign_id,
            aggregate_id=row.attempt_id,
            aggregate_type="attempt",
            event_type="attempt.completed",
            payload={
                "work_item_id": row.work_item_id,
                "campaign_id": row.campaign_id,
                "state": "lease_lost",
                "status": "lease_lost",
                "observation_id": None,
                "failure": failure,
                "cost": {},
                "data_origin": row.data_origin,
                "execution_mode": row.execution_mode,
                "provenance": provenance,
                "started_at": row.started_at,
                "finished_at": outcome.finished_at,
            },
            expected_version=current_sequence(
                connection,
                campaign_id=row.campaign_id,
                aggregate_type="attempt",
                aggregate_id=row.attempt_id,
            ),
            correlation_id=row.event_correlation_id,
            causation_id=latest_attempt_event or row.last_event_id,
        )
        closed.append(row.attempt_id)
    return closed


def _object_facts(connection: Connection, row: Row[Any], store: ObjectStore) -> ObjectFacts | None:
    """Gather everything the verdict depends on, or None when the store cannot be reached."""
    try:
        exists = store.exists(row.object_key)
        actual = digest(store.get(row.object_key)) if exists else None
    except ObjectStoreError:
        return None

    referenced_by_observation = (
        connection.execute(
            select(func.count())
            .select_from(observations)
            .where(
                and_(
                    observations.c.object_uri == row.object_uri,
                    observations.c.status == "accepted",
                )
            )
        ).scalar_one()
        > 0
    )
    referenced_by_source = (
        connection.execute(
            select(func.count())
            .select_from(source_artifacts)
            .where(
                source_artifacts.c.object_uri == row.object_uri,
                source_artifacts.c.state == "committed",
            )
        ).scalar_one()
        > 0
    )
    referenced_by_normalised_cv = (
        connection.execute(
            select(func.count())
            .select_from(normalised_cv_observations)
            .where(normalised_cv_observations.c.object_uri == row.object_uri)
        ).scalar_one()
        > 0
    )
    referenced_by_passport = (
        connection.execute(
            select(func.count())
            .select_from(experiment_passports)
            .where(
                or_(
                    experiment_passports.c.json_object_uri == row.object_uri,
                    experiment_passports.c.html_object_uri == row.object_uri,
                )
            )
        ).scalar_one()
        > 0
    )
    referenced_by_package = (
        connection.execute(
            select(func.count())
            .select_from(experiment_packages)
            .where(experiment_packages.c.object_uri == row.object_uri)
        ).scalar_one()
        > 0
    )
    outcome_status: str | None = None
    if row.attempt_id is not None:
        outcome_status = connection.execute(
            select(attempt_outcomes.c.status).where(attempt_outcomes.c.attempt_id == row.attempt_id)
        ).scalar_one_or_none()

    return ObjectFacts(
        state=row.state,
        exists=exists,
        recorded_sha256=row.sha256,
        actual_sha256=actual,
        referenced_by_accepted=(
            referenced_by_observation
            or referenced_by_source
            or referenced_by_normalised_cv
            or referenced_by_passport
            or referenced_by_package
        ),
        outcome_status=outcome_status,
    )


def classify_objects(
    connection: Connection, store: ObjectStore
) -> tuple[list[ClassifiedObject], list[str]]:
    """Reach a verdict on every stored object, and record it with the evidence that produced it."""
    rows = connection.execute(
        select(
            storage_objects.c.object_uri,
            storage_objects.c.object_key,
            storage_objects.c.state,
            storage_objects.c.sha256,
            storage_objects.c.attempt_id,
        ).where(storage_objects.c.bucket == store.bucket)
    ).all()

    classified: list[ClassifiedObject] = []
    unreachable: list[str] = []
    for row in rows:
        facts = _object_facts(connection, row, store)
        if facts is None:
            unreachable.append(row.object_uri)
            continue
        verdict: ObjectVerdict = classify_object(facts)
        connection.execute(
            update(storage_objects)
            .where(storage_objects.c.object_uri == row.object_uri)
            .values(
                classification=verdict.classification,
                classification_reason=verdict.reason,
                reconciled_at=func.now(),
                # The digest the store actually holds is recorded for an object that had none. It is
                # never *overwritten*: a disagreement is the quarantine verdict, and refreshing the
                # checksum there would launder corruption into evidence (F-028).
                sha256=row.sha256 if row.sha256 is not None else facts.actual_sha256,
            )
        )
        classified.append(
            ClassifiedObject(
                object_uri=row.object_uri,
                classification=verdict.classification,
                reason=verdict.reason,
            )
        )
    return classified, unreachable


def reconcile(connection: Connection, store: ObjectStore) -> ReconciliationReport:
    """One pass: reclaim expired leases, close abandoned attempts, classify stored objects.

    Ordered, because each step depends on the previous one having happened. Reclaiming first bumps
    the fencing generation so a stale worker cannot race the rest of the pass. Closing attempts next
    means the outcome statuses the object verdicts read are already settled. Classification last
    therefore sees a consistent picture rather than one mid-repair.

    The whole pass shares the caller's transaction: a partially applied reconciliation — leases
    reclaimed but attempts left `running` — is a worse state than the one it started from.
    """
    reclaimed = recover_expired_leases(connection)
    closed = close_abandoned_attempts(connection)
    classified, unreachable = classify_objects(connection, store)
    return ReconciliationReport(
        reclaimed=reclaimed,
        closed_attempts=closed,
        classified=classified,
        unreachable=unreachable,
    )


__all__ = [
    "ClassifiedObject",
    "ReconciliationReport",
    "classify_objects",
    "close_abandoned_attempts",
    "reconcile",
]

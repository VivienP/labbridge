from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Connection, delete, func, select, update
from sqlalchemy.exc import IntegrityError

from labbridge.infrastructure.persistence.tables import (
    attempt_outcomes,
    attempts,
    budget_ledger,
    campaigns,
    events,
    jobs,
    observations,
    work_items,
)
from labbridge.runtime.budgets import consume
from labbridge.runtime.events import append_event, read_stream
from labbridge.runtime.jobs import (
    cancel_available_for_campaign,
    claim,
    enqueue,
    event_context,
    mark_running,
)
from labbridge.runtime.replay import (
    NonRebuildableProjectionError,
    compare_campaign_projection,
    rebuild_mutable_projections,
)

pytestmark = pytest.mark.integration


def test_replay_determinism_detects_postgres_projection_corruption_and_rebuilds(
    connection: Connection,
) -> None:
    campaign_id = uuid.uuid4()
    correlation_id = uuid.uuid4()
    created_at = datetime(2026, 8, 13, 9, tzinfo=UTC)
    completed_at = created_at + timedelta(minutes=1)
    declaration = {"budget": {"hard_budget": "10", "budget_unit": "credit"}}
    connection.execute(
        campaigns.insert().values(
            campaign_id=campaign_id,
            name="PostgreSQL replay",
            environment_id="her",
            adapter_version="1",
            data_origin="synthetic",
            execution_mode="replay",
            state="active",
            declaration=declaration,
            declaration_hash="a" * 64,
            hard_budget=Decimal("10"),
            per_attempt_estimate=Decimal("1"),
            budget_unit="credit",
            max_attempts=3,
            stopping_rule="hard_budget_exhausted",
            event_stream_contract_version=2,
            event_stream_last_position=0,
            created_at=created_at,
            updated_at=created_at,
        )
    )
    root = append_event(
        connection,
        campaign_id=campaign_id,
        aggregate_id=campaign_id,
        aggregate_type="campaign",
        event_type="campaign.created",
        payload={
            "name": "PostgreSQL replay",
            "environment_id": "her",
            "adapter_version": "1",
            "data_origin": "synthetic",
            "execution_mode": "replay",
            "declaration": declaration,
            "declaration_hash": "a" * 64,
            "state": "active",
        },
        expected_version=0,
        correlation_id=correlation_id,
        causation_id=None,
        occurred_at=created_at,
    )
    work_item_id = uuid.uuid4()
    candidate = {
        "kind": "her_location",
        "library_id": "library",
        "measurement_area_id": "area",
        "grid_x": {"value": "1", "unit": "mm"},
        "grid_y": {"value": "2", "unit": "mm"},
    }
    connection.execute(
        work_items.insert().values(
            work_item_id=work_item_id,
            campaign_id=campaign_id,
            candidate_id="cand:rebuild",
            candidate=candidate,
            state="queued",
            created_at=created_at,
            updated_at=created_at,
        )
    )
    queued = append_event(
        connection,
        campaign_id=campaign_id,
        aggregate_id=work_item_id,
        aggregate_type="work_item",
        event_type="work_item.queued",
        payload={"candidate_id": "cand:rebuild", "candidate": candidate, "state": "queued"},
        expected_version=0,
        correlation_id=correlation_id,
        causation_id=root.event_id,
        occurred_at=created_at,
    )
    enqueued = enqueue(
        connection,
        campaign_id=campaign_id,
        work_item_id=work_item_id,
        instruction_key=f"rebuild:{work_item_id}",
        command_version="1",
        correlation_id=correlation_id,
        causation_id=queued.event_id,
    )
    cancel_available_for_campaign(
        connection,
        campaign_id,
        causation_id=queued.event_id,
        reason="campaign completed",
    )
    connection.execute(
        update(campaigns)
        .where(campaigns.c.campaign_id == campaign_id)
        .values(state="completed", updated_at=completed_at)
    )
    append_event(
        connection,
        campaign_id=campaign_id,
        aggregate_id=campaign_id,
        aggregate_type="campaign",
        event_type="campaign.completed",
        payload={"state": "completed", "reason": None},
        expected_version=1,
        correlation_id=correlation_id,
        causation_id=root.event_id,
        occurred_at=completed_at,
    )

    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(delete(campaigns).where(campaigns.c.campaign_id == campaign_id))

    assert compare_campaign_projection(connection, campaign_id).matches
    immutable_counts = tuple(
        connection.execute(
            select(func.count()).select_from(table).where(table.c.campaign_id == campaign_id)
        ).scalar_one()
        for table in (events, attempt_outcomes, observations, budget_ledger)
    )

    connection.execute(
        update(campaigns).where(campaigns.c.campaign_id == campaign_id).values(state="paused")
    )
    connection.execute(delete(jobs).where(jobs.c.job_id == enqueued.job_id))
    connection.execute(delete(work_items).where(work_items.c.work_item_id == work_item_id))
    mismatch = compare_campaign_projection(connection, campaign_id)
    assert not mismatch.matches
    assert {item.path for item in mismatch.mismatches} == {"campaign", "work_items", "jobs"}

    rebuild_mutable_projections(connection, campaign_id)

    assert compare_campaign_projection(connection, campaign_id).matches
    assert (
        connection.execute(
            select(campaigns.c.state).where(campaigns.c.campaign_id == campaign_id)
        ).scalar_one()
        == "completed"
    )
    assert (
        tuple(
            connection.execute(
                select(func.count()).select_from(table).where(table.c.campaign_id == campaign_id)
            ).scalar_one()
            for table in (events, attempt_outcomes, observations, budget_ledger)
        )
        == immutable_counts
    )
    assert (
        connection.execute(
            select(jobs.c.state).where(jobs.c.job_id == enqueued.job_id)
        ).scalar_one()
        == "cancelled"
    )

    immutable_mutations = (
        (campaigns, campaigns.c.campaign_id, campaign_id, "name", "changed name"),
        (campaigns, campaigns.c.campaign_id, campaign_id, "declaration", {"changed": True}),
        (work_items, work_items.c.work_item_id, work_item_id, "candidate_id", "changed:candidate"),
        (work_items, work_items.c.work_item_id, work_item_id, "candidate", {**candidate, "x": 1}),
        (
            jobs,
            jobs.c.job_id,
            enqueued.job_id,
            "idempotency_key",
            f"changed:{enqueued.job_id}",
        ),
        (
            jobs,
            jobs.c.job_id,
            enqueued.job_id,
            "created_at",
            created_at + timedelta(seconds=1),
        ),
    )
    for table, identity_column, identity, field, changed_value in immutable_mutations:
        original = connection.execute(
            select(getattr(table.c, field)).where(identity_column == identity)
        ).scalar_one()
        connection.execute(
            update(table).where(identity_column == identity).values({field: changed_value})
        )
        with pytest.raises(NonRebuildableProjectionError):
            rebuild_mutable_projections(connection, campaign_id)
        assert (
            connection.execute(
                select(getattr(table.c, field)).where(identity_column == identity)
            ).scalar_one()
            == changed_value
        )
        assert (
            tuple(
                connection.execute(
                    select(func.count())
                    .select_from(evidence)
                    .where(evidence.c.campaign_id == campaign_id)
                ).scalar_one()
                for evidence in (events, attempt_outcomes, observations, budget_ledger)
            )
            == immutable_counts
        )
        connection.execute(
            update(table).where(identity_column == identity).values({field: original})
        )

    extra_work_item_id = uuid.uuid4()
    connection.execute(
        work_items.insert().values(
            work_item_id=extra_work_item_id,
            campaign_id=campaign_id,
            candidate_id="cand:extra",
            candidate=candidate,
            state="cancelled",
            created_at=created_at,
            updated_at=created_at,
        )
    )
    with pytest.raises(NonRebuildableProjectionError):
        rebuild_mutable_projections(connection, campaign_id)
    assert (
        connection.execute(
            select(work_items.c.work_item_id).where(work_items.c.work_item_id == extra_work_item_id)
        ).scalar_one()
        == extra_work_item_id
    )
    connection.execute(delete(work_items).where(work_items.c.work_item_id == extra_work_item_id))
    assert (
        connection.execute(
            select(work_items.c.state).where(work_items.c.work_item_id == work_item_id)
        ).scalar_one()
        == "cancelled"
    )


def test_budget_adjustment_events_reconstruct_postgres_totals(connection: Connection) -> None:
    campaign_id = uuid.uuid4()
    work_item_id = uuid.uuid4()
    correlation_id = uuid.uuid4()
    created_at = datetime(2026, 8, 13, 10, tzinfo=UTC)
    declaration = {"budget": {"hard_budget": "10", "budget_unit": "credit"}}
    candidate = {
        "kind": "her_location",
        "library_id": "library",
        "measurement_area_id": "area",
        "grid_x": {"value": "1", "unit": "mm"},
        "grid_y": {"value": "2", "unit": "mm"},
    }
    connection.execute(
        campaigns.insert().values(
            campaign_id=campaign_id,
            name="budget replay",
            environment_id="her",
            adapter_version="1",
            data_origin="synthetic",
            execution_mode="replay",
            state="active",
            declaration=declaration,
            declaration_hash="b" * 64,
            hard_budget=Decimal("10"),
            per_attempt_estimate=Decimal("3"),
            budget_unit="credit",
            max_attempts=3,
            stopping_rule="hard_budget_exhausted",
            event_stream_contract_version=2,
            event_stream_last_position=0,
            created_at=created_at,
            updated_at=created_at,
        )
    )
    root = append_event(
        connection,
        campaign_id=campaign_id,
        aggregate_id=campaign_id,
        aggregate_type="campaign",
        event_type="campaign.created",
        payload={
            "name": "budget replay",
            "environment_id": "her",
            "adapter_version": "1",
            "data_origin": "synthetic",
            "execution_mode": "replay",
            "declaration": declaration,
            "declaration_hash": "b" * 64,
            "state": "active",
        },
        expected_version=0,
        correlation_id=correlation_id,
        causation_id=None,
        occurred_at=created_at,
    )
    connection.execute(
        work_items.insert().values(
            work_item_id=work_item_id,
            campaign_id=campaign_id,
            candidate_id="cand:budget",
            candidate=candidate,
            state="queued",
            created_at=created_at,
            updated_at=created_at,
        )
    )
    queued = append_event(
        connection,
        campaign_id=campaign_id,
        aggregate_id=work_item_id,
        aggregate_type="work_item",
        event_type="work_item.queued",
        payload={"candidate_id": "cand:budget", "candidate": candidate, "state": "queued"},
        expected_version=0,
        correlation_id=correlation_id,
        causation_id=root.event_id,
        occurred_at=created_at,
    )
    enqueue(
        connection,
        campaign_id=campaign_id,
        work_item_id=work_item_id,
        instruction_key=f"budget:{work_item_id}",
        command_version="1",
        correlation_id=correlation_id,
        causation_id=queued.event_id,
    )
    lease = claim(connection, owner="budget-replay")
    assert lease is not None
    mark_running(connection, lease)
    attempt_id = uuid.uuid4()
    attempt_row = connection.execute(
        attempts.insert()
        .values(
            attempt_id=attempt_id,
            work_item_id=work_item_id,
            job_id=lease.job_id,
            ordinal=1,
            state="running",
            started_at=func.now(),
            created_at=func.now(),
        )
        .returning(attempts.c.started_at, attempts.c.created_at)
    ).one()
    _, last_job_event = event_context(connection, lease.job_id)
    append_event(
        connection,
        campaign_id=campaign_id,
        aggregate_id=attempt_id,
        aggregate_type="attempt",
        event_type="attempt.started",
        payload={
            "work_item_id": work_item_id,
            "job_id": lease.job_id,
            "ordinal": 1,
            "state": "running",
            "started_at": attempt_row.started_at,
            "created_at": attempt_row.created_at,
        },
        expected_version=0,
        correlation_id=correlation_id,
        causation_id=last_job_event,
    )
    immutable_counts = tuple(
        connection.execute(
            select(func.count()).select_from(table).where(table.c.campaign_id == campaign_id)
        ).scalar_one()
        for table in (events, attempt_outcomes, observations, budget_ledger)
    )
    connection.execute(delete(attempts).where(attempts.c.attempt_id == attempt_id))

    mismatch = compare_campaign_projection(connection, campaign_id)
    assert {item.path for item in mismatch.mismatches} == {"attempts"}

    rebuild_mutable_projections(connection, campaign_id)
    assert compare_campaign_projection(connection, campaign_id).matches
    assert (
        tuple(
            connection.execute(
                select(func.count()).select_from(table).where(table.c.campaign_id == campaign_id)
            ).scalar_one()
            for table in (events, attempt_outcomes, observations, budget_ledger)
        )
        == immutable_counts
    )
    immutable_attempt_mutations = (
        ("ordinal", 2),
        ("job_id", None),
        ("started_at", attempt_row.started_at + timedelta(seconds=1)),
    )
    for field, changed_value in immutable_attempt_mutations:
        original = connection.execute(
            select(getattr(attempts.c, field)).where(attempts.c.attempt_id == attempt_id)
        ).scalar_one()
        connection.execute(
            update(attempts)
            .where(attempts.c.attempt_id == attempt_id)
            .values({field: changed_value})
        )
        with pytest.raises(NonRebuildableProjectionError):
            rebuild_mutable_projections(connection, campaign_id)
        assert (
            connection.execute(
                select(getattr(attempts.c, field)).where(attempts.c.attempt_id == attempt_id)
            ).scalar_one()
            == changed_value
        )
        connection.execute(
            update(attempts).where(attempts.c.attempt_id == attempt_id).values({field: original})
        )
    consume(connection, lease.budget_reservation_id, attempt_id=attempt_id)
    consume(
        connection,
        lease.budget_reservation_id,
        attempt_id=attempt_id,
        actual_amount=Decimal("2"),
    )

    replayed = compare_campaign_projection(connection, campaign_id)
    event_types = [event["event_type"] for event in read_stream(connection, campaign_id)]

    assert replayed.matches
    assert event_types.count("budget.reserved") == 1
    assert event_types.count("budget.consumed") == 1
    assert event_types.count("budget.adjusted") == 1

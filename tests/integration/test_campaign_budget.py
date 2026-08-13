from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from sqlalchemy import Connection, Engine, func, select
from sqlalchemy.exc import IntegrityError

from labbridge.application.campaigns import CampaignControlService
from labbridge.infrastructure.persistence.tables import (
    attempts,
    budget_ledger,
    campaigns,
    events,
    jobs,
    work_items,
)
from labbridge.runtime.budgets import (
    PostgresCampaignControlRepository,
    ReservationSettledError,
    consume,
    release,
)
from labbridge.runtime.events import append_event
from labbridge.runtime.jobs import claim, complete, enqueue, mark_running

pytestmark = pytest.mark.integration
_SEEDED_CAMPAIGNS: list[uuid.UUID] = []


@pytest.fixture(autouse=True)
def clean_seeded_campaigns(
    migrated: Engine, purge_campaign: Callable[[Connection, uuid.UUID], None]
) -> Iterator[None]:
    """Keep the global claim queue isolated between budget tests."""
    yield
    with migrated.begin() as connection:
        for campaign_id in reversed(_SEEDED_CAMPAIGNS):
            purge_campaign(connection, campaign_id)
    _SEEDED_CAMPAIGNS.clear()


def _candidate_payload(index: int) -> dict[str, object]:
    return {
        "kind": "her_location",
        "library_id": "library",
        "measurement_area_id": str(index),
        "grid_x": {"value": "0", "unit": "mm"},
        "grid_y": {"value": "0", "unit": "mm"},
    }


def _seed_campaign(
    engine: Engine,
    *,
    hard_budget: Decimal = Decimal("1"),
    per_attempt: Decimal = Decimal("1"),
    job_count: int = 2,
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    campaign_id = uuid.uuid4()
    _SEEDED_CAMPAIGNS.append(campaign_id)
    work_item_ids: list[uuid.UUID] = []
    with engine.begin() as connection:
        connection.execute(
            campaigns.insert().values(
                campaign_id=campaign_id,
                name="budget concurrency",
                environment_id="her",
                adapter_version="1",
                data_origin="synthetic",
                execution_mode="replay",
                state="active",
                declaration={},
                declaration_hash="b" * 64,
                hard_budget=hard_budget,
                per_attempt_estimate=per_attempt,
                budget_unit="attempt-credit",
                max_attempts=3,
                stopping_rule="hard_budget_exhausted",
                event_stream_contract_version=1,
                event_stream_last_position=0,
                created_at=func.now(),
                updated_at=func.now(),
            )
        )
        correlation_id = uuid.uuid4()
        root = append_event(
            connection,
            campaign_id=campaign_id,
            aggregate_id=campaign_id,
            aggregate_type="campaign",
            event_type="campaign.created",
            payload={
                "name": "budget concurrency",
                "environment_id": "her",
                "adapter_version": "1",
                "data_origin": "synthetic",
                "execution_mode": "replay",
                "declaration": {},
                "declaration_hash": "b" * 64,
                "state": "active",
            },
            expected_version=0,
            correlation_id=correlation_id,
            causation_id=None,
        )
        for index in range(job_count):
            work_item_id = uuid.uuid4()
            work_item_ids.append(work_item_id)
            candidate = _candidate_payload(index)
            connection.execute(
                work_items.insert().values(
                    work_item_id=work_item_id,
                    campaign_id=campaign_id,
                    candidate_id=f"candidate:{index}",
                    candidate=candidate,
                    state="queued",
                    created_at=func.now(),
                    updated_at=func.now(),
                )
            )
            queued = append_event(
                connection,
                campaign_id=campaign_id,
                aggregate_id=work_item_id,
                aggregate_type="work_item",
                event_type="work_item.queued",
                payload={
                    "candidate_id": f"candidate:{index}",
                    "candidate": candidate,
                    "state": "queued",
                },
                expected_version=0,
                correlation_id=correlation_id,
                causation_id=root.event_id,
            )
            enqueue(
                connection,
                campaign_id=campaign_id,
                work_item_id=work_item_id,
                instruction_key=f"budget:{campaign_id}:{index}",
                command_version="1",
                correlation_id=correlation_id,
                causation_id=queued.event_id,
                max_attempts=3,
            )
    return campaign_id, work_item_ids


def test_concurrent_claims_cannot_reserve_past_the_hard_budget(migrated: Engine) -> None:
    campaign_id, _ = _seed_campaign(migrated)

    def take(owner: str):
        with migrated.begin() as connection:
            return claim(connection, owner=owner)

    with ThreadPoolExecutor(max_workers=2) as pool:
        leases = list(pool.map(take, ("worker-a", "worker-b")))

    with migrated.begin() as connection:
        reserved = connection.execute(
            select(func.coalesce(func.sum(budget_ledger.c.amount), 0)).where(
                budget_ledger.c.campaign_id == campaign_id,
                budget_ledger.c.kind == "reserved",
            )
        ).scalar_one()
        state = connection.execute(
            select(campaigns.c.state).where(campaigns.c.campaign_id == campaign_id)
        ).scalar_one()
        available = connection.execute(
            select(func.count())
            .select_from(jobs.join(work_items))
            .where(
                work_items.c.campaign_id == campaign_id,
                jobs.c.state == "available",
            )
        ).scalar_one()

    assert sum(lease is not None for lease in leases) == 1
    assert Decimal(reserved) == Decimal("1")
    assert state == "budget_exhausted"
    assert available == 0
    with migrated.begin() as connection:
        connection.execute(
            jobs.update()
            .where(
                jobs.c.work_item_id.in_(
                    select(work_items.c.work_item_id).where(work_items.c.campaign_id == campaign_id)
                )
            )
            .values(
                state="cancelled",
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
            )
        )


def test_cancellation_is_idempotent_and_never_leases_available_work(migrated: Engine) -> None:
    campaign_id, _ = _seed_campaign(migrated, hard_budget=Decimal("10"))
    service = CampaignControlService(PostgresCampaignControlRepository(migrated))

    first = service.cancel(campaign_id, expected_version=1, idempotency_key="cancel-command")
    replay = service.cancel(campaign_id, expected_version=1, idempotency_key="cancel-command")

    with migrated.begin() as connection:
        states = set(
            connection.execute(
                select(jobs.c.state)
                .select_from(jobs.join(work_items))
                .where(work_items.c.campaign_id == campaign_id)
            ).scalars()
        )
        control_events = connection.execute(
            select(func.count())
            .select_from(events)
            .where(
                events.c.campaign_id == campaign_id,
                events.c.event_type == "campaign.cancelled",
            )
        ).scalar_one()
        assert claim(connection, owner="worker-after-cancel") is None

    assert first.state == "cancelled"
    assert not first.replayed
    assert replay.replayed
    assert states == {"cancelled"}
    assert control_events == 1


def test_cancellation_allows_an_existing_lease_to_finish(migrated: Engine) -> None:
    campaign_id, _ = _seed_campaign(migrated, hard_budget=Decimal("10"), job_count=1)
    with migrated.begin() as connection:
        lease = claim(connection, owner="worker-before-cancel")
    assert lease is not None

    service = CampaignControlService(PostgresCampaignControlRepository(migrated))
    service.cancel(campaign_id, expected_version=1, idempotency_key="cancel-leased")

    with migrated.begin() as connection:
        state = connection.execute(
            select(jobs.c.state).where(jobs.c.job_id == lease.job_id)
        ).scalar_one()
        assert state == "leased"
        mark_running(connection, lease)
        complete(connection, lease)


def test_reservation_has_one_exact_append_only_settlement(migrated: Engine) -> None:
    _, work_item_ids = _seed_campaign(
        migrated, hard_budget=Decimal("10"), per_attempt=Decimal("2.5"), job_count=1
    )
    with migrated.begin() as connection:
        lease = claim(connection, owner="worker-cost")
        assert lease is not None
        attempt_id = uuid.uuid4()
        connection.execute(
            attempts.insert().values(
                attempt_id=attempt_id,
                work_item_id=work_item_ids[0],
                job_id=lease.job_id,
                ordinal=1,
                state="running",
                started_at=func.now(),
                created_at=func.now(),
            )
        )
        settled = consume(connection, lease.budget_reservation_id, attempt_id=attempt_id)
        assert settled.amount == Decimal("2.5")
        with pytest.raises(ReservationSettledError):
            release(connection, lease.budget_reservation_id, reason="duplicate settlement")

        entries = connection.execute(
            select(budget_ledger)
            .where(budget_ledger.c.entry_id.in_((lease.budget_reservation_id, settled.entry_id)))
            .order_by(budget_ledger.c.recorded_at)
        ).all()

    assert [entry.kind for entry in entries] == ["reserved", "consumed"]
    assert entries[0].amount == entries[1].amount
    assert entries[0].unit == entries[1].unit


def test_actual_cost_overrun_exhausts_campaign_and_blocks_further_claims(
    migrated: Engine,
) -> None:
    campaign_id, _ = _seed_campaign(
        migrated, hard_budget=Decimal("4"), per_attempt=Decimal("2"), job_count=2
    )
    with migrated.begin() as connection:
        lease = claim(connection, owner="overrun-worker")
        assert lease is not None
        attempt_id = uuid.uuid4()
        connection.execute(
            attempts.insert().values(
                attempt_id=attempt_id,
                work_item_id=lease.work_item_id,
                job_id=lease.job_id,
                ordinal=1,
                state="succeeded",
                started_at=func.now(),
                adapter_started_at=func.now(),
                created_at=func.now(),
            )
        )
        settlement = consume(
            connection,
            lease.budget_reservation_id,
            attempt_id=attempt_id,
            actual_amount=Decimal("5"),
        )
        state = connection.execute(
            select(campaigns.c.state).where(campaigns.c.campaign_id == campaign_id)
        ).scalar_one()
        remaining = (
            connection.execute(
                select(jobs.c.state).join(work_items).where(work_items.c.campaign_id == campaign_id)
            )
            .scalars()
            .all()
        )

    assert settlement.reserved_amount == Decimal("2")
    assert settlement.actual_amount == Decimal("5")
    assert state == "budget_exhausted"
    assert sorted(remaining) == ["cancelled", "leased"]
    with migrated.begin() as connection:
        connection.execute(
            jobs.update()
            .where(jobs.c.work_item_id == lease.work_item_id)
            .values(
                state="cancelled",
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
            )
        )


@pytest.mark.parametrize(
    ("actual", "hard_budget"),
    [(Decimal("1"), Decimal("3")), (Decimal("2"), Decimal("4"))],
)
def test_settled_actual_cost_replaces_the_reservation_for_future_claims(
    migrated: Engine, actual: Decimal, hard_budget: Decimal
) -> None:
    campaign_id, _ = _seed_campaign(
        migrated, hard_budget=hard_budget, per_attempt=Decimal("2"), job_count=2
    )
    with migrated.begin() as connection:
        first = claim(connection, owner="settled-cost-worker")
        assert first is not None
        attempt_id = uuid.uuid4()
        connection.execute(
            attempts.insert().values(
                attempt_id=attempt_id,
                work_item_id=first.work_item_id,
                job_id=first.job_id,
                ordinal=1,
                state="succeeded",
                started_at=func.now(),
                adapter_started_at=func.now(),
                created_at=func.now(),
            )
        )
        consume(
            connection,
            first.budget_reservation_id,
            attempt_id=attempt_id,
            actual_amount=actual,
        )
    with migrated.begin() as connection:
        second = claim(connection, owner="next-cost-worker")
        campaign_state = connection.execute(
            select(campaigns.c.state).where(campaigns.c.campaign_id == campaign_id)
        ).scalar_one()

    assert second is not None
    assert campaign_state == "active"
    with migrated.begin() as connection:
        connection.execute(
            jobs.update()
            .where(
                jobs.c.work_item_id.in_(
                    select(work_items.c.work_item_id).where(work_items.c.campaign_id == campaign_id)
                )
            )
            .values(
                state="cancelled",
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
            )
        )


def test_ledger_rejects_a_mixed_campaign_work_job_identity(migrated: Engine) -> None:
    first_campaign, first_items = _seed_campaign(migrated, job_count=1)
    second_campaign, _ = _seed_campaign(migrated, job_count=1)
    with migrated.connect() as connection:
        transaction = connection.begin()
        lease = claim(connection, owner="identity-worker")
        assert lease is not None
        with pytest.raises(IntegrityError):
            connection.execute(
                budget_ledger.insert().values(
                    entry_id=uuid.uuid4(),
                    campaign_id=second_campaign,
                    work_item_id=first_items[0],
                    job_id=lease.job_id,
                    lease_generation=lease.lease_generation + 10,
                    kind="reserved",
                    amount=Decimal("1"),
                    unit="attempt-credit",
                    reason="invalid mixed identity",
                    recorded_at=func.now(),
                )
            )
        transaction.rollback()
    with migrated.begin() as connection:
        connection.execute(
            jobs.update()
            .where(
                jobs.c.work_item_id.in_(
                    select(work_items.c.work_item_id).where(
                        work_items.c.campaign_id.in_((first_campaign, second_campaign))
                    )
                )
            )
            .values(state="cancelled")
        )
    assert first_campaign != second_campaign

"""The submission path, against real PostgreSQL.

A repeated API request, not only a repeated job delivery, exercises idempotency against the
database that enforces it rather than a stub.

`TestClient` runs the application in-process, which is enough: what is tested is the endpoint's
transaction and its idempotency record, not HTTP transport.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, func, select

from labbridge.api import create_app
from labbridge.domain.idempotency import MAX_IDEMPOTENCY_KEY_LENGTH
from labbridge.infrastructure.persistence.tables import (
    campaigns,
    events,
    idempotency_keys,
    jobs,
    work_items,
)

pytestmark = pytest.mark.integration

CREATED = 201
REPLAYED = 200
BAD_REQUEST = 400
NOT_FOUND = 404
CONFLICT = 409
UNPROCESSABLE = 422
TWO_CANDIDATES = 2
ONE_CAMPAIGN = 1
ONE_EVENT = 1
#: Enough concurrency that a check-then-insert would lose more than once, so a passing run is not
#: an accident of two threads happening to serialise.
CONCURRENT_SUBMISSIONS = 6


def _candidate(area: str) -> dict[str, Any]:
    return {
        "kind": "her_location",
        "library_id": "Au-rich",
        "measurement_area_id": area,
        "grid_x": {"value": "0", "unit": "mm"},
        "grid_y": {"value": "0", "unit": "mm"},
    }


def _body(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "api submission",
        "environment_id": "her_auirrh",
        "adapter_version": "1",
        "data_origin": "synthetic",
        "execution_mode": "replay",
        "candidates": [_candidate("1"), _candidate("2")],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def client(migrated: Engine) -> Iterator[TestClient]:
    created: list[uuid.UUID] = []
    keys: list[str] = []
    app = create_app(migrated)
    app.state.created = created  # type: ignore[attr-defined]
    with TestClient(app) as test_client:
        test_client.created = created  # type: ignore[attr-defined]
        test_client.keys = keys  # type: ignore[attr-defined]
        yield test_client
    with migrated.begin() as connection:
        # Idempotency records first: they reference the campaign, and the reference is `RESTRICT`,
        # which PostgreSQL checks at the delete rather than at commit however deferrable the
        # constraint is. Children before parents, as everywhere else in this suite.
        for key in keys:
            connection.execute(
                delete(idempotency_keys).where(idempotency_keys.c.idempotency_key == key)
            )
        for campaign_id in created:
            owned = select(work_items.c.work_item_id).where(work_items.c.campaign_id == campaign_id)
            connection.execute(delete(jobs).where(jobs.c.work_item_id.in_(owned)))
            connection.execute(delete(events).where(events.c.campaign_id == campaign_id))
            connection.execute(delete(work_items).where(work_items.c.campaign_id == campaign_id))
            connection.execute(delete(campaigns).where(campaigns.c.campaign_id == campaign_id))


def _submit(client: TestClient, key: str, **overrides: Any) -> Any:
    client.keys.append(key)  # type: ignore[attr-defined]
    response = client.post("/campaigns", json=_body(**overrides), headers={"Idempotency-Key": key})
    if response.status_code in (CREATED, REPLAYED):
        client.created.append(uuid.UUID(response.json()["campaign_id"]))  # type: ignore[attr-defined]
    return response


def test_a_submission_creates_a_campaign_and_one_job_per_candidate(client: TestClient) -> None:
    response = _submit(client, f"key:{uuid.uuid4().hex}")

    assert response.status_code == CREATED
    body = response.json()
    assert body["work_items"] == TWO_CANDIDATES
    assert body["replayed"] is False


def test_a_repeated_request_returns_the_same_campaign_and_creates_nothing(
    client: TestClient, migrated: Engine
) -> None:
    """A retry after an ambiguous timeout must not produce a second campaign."""
    key = f"key:{uuid.uuid4().hex}"

    first = _submit(client, key)
    second = _submit(client, key)

    assert first.status_code == CREATED
    assert second.status_code == REPLAYED
    assert second.json()["campaign_id"] == first.json()["campaign_id"]
    assert second.json()["replayed"] is True
    # One campaign, and one job per candidate rather than two. The second request created nothing.
    campaign_id = uuid.UUID(first.json()["campaign_id"])
    with migrated.begin() as connection:
        item_count = connection.execute(
            select(func.count())
            .select_from(work_items)
            .where(work_items.c.campaign_id == campaign_id)
        ).scalar_one()
        job_count = connection.execute(
            select(func.count())
            .select_from(jobs)
            .where(
                jobs.c.work_item_id.in_(
                    select(work_items.c.work_item_id).where(work_items.c.campaign_id == campaign_id)
                )
            )
        ).scalar_one()
    assert item_count == TWO_CANDIDATES
    assert job_count == TWO_CANDIDATES


def _campaign_footprint(engine: Engine, campaign_id: uuid.UUID) -> dict[str, int]:
    """Every count a second creation would move."""
    owned = select(work_items.c.work_item_id).where(work_items.c.campaign_id == campaign_id)
    with engine.begin() as connection:
        return {
            "campaigns": connection.execute(
                select(func.count())
                .select_from(campaigns)
                .where(campaigns.c.campaign_id == campaign_id)
            ).scalar_one(),
            "work_items": connection.execute(
                select(func.count())
                .select_from(work_items)
                .where(work_items.c.campaign_id == campaign_id)
            ).scalar_one(),
            "jobs": connection.execute(
                select(func.count()).select_from(jobs).where(jobs.c.work_item_id.in_(owned))
            ).scalar_one(),
            "campaign.created": connection.execute(
                select(func.count())
                .select_from(events)
                .where(
                    events.c.campaign_id == campaign_id,
                    events.c.event_type == "campaign.created",
                )
            ).scalar_one(),
        }


def test_concurrent_identical_submissions_create_exactly_one_campaign(
    client: TestClient, migrated: Engine
) -> None:
    """F-001, under real concurrency rather than in sequence.

    Six requests carrying the same key and the same body are released together. A check-then-insert
    lets all six past the check: six campaigns are created, and five of them then collide on the
    idempotency key — an integrity error the caller sees as a 5xx. Here the reservation is the first
    statement, so the unique key decides and the other five wait for its answer.
    """
    key = f"key:{uuid.uuid4().hex}"
    barrier = Barrier(CONCURRENT_SUBMISSIONS)

    def submit() -> Any:
        barrier.wait(timeout=30)
        return _submit(client, key)

    with ThreadPoolExecutor(max_workers=CONCURRENT_SUBMISSIONS) as pool:
        futures = [pool.submit(submit) for _ in range(CONCURRENT_SUBMISSIONS)]
        responses = [future.result(timeout=60) for future in futures]

    statuses = sorted(response.status_code for response in responses)
    assert statuses == [REPLAYED] * (CONCURRENT_SUBMISSIONS - 1) + [CREATED]
    identifiers = {response.json()["campaign_id"] for response in responses}
    assert len(identifiers) == ONE_CAMPAIGN
    assert sum(1 for response in responses if response.json()["replayed"] is False) == ONE_CAMPAIGN
    assert _campaign_footprint(migrated, uuid.UUID(identifiers.pop())) == {
        "campaigns": ONE_CAMPAIGN,
        "work_items": TWO_CANDIDATES,
        "jobs": TWO_CANDIDATES,
        "campaign.created": ONE_EVENT,
    }


def test_the_same_key_with_a_different_body_is_a_conflict(client: TestClient) -> None:
    """Returning the first result would silently discard this request; creating a second campaign
    would break the key's promise. Neither is acceptable, so it is reported."""
    key = f"key:{uuid.uuid4().hex}"
    _submit(client, key)

    clash = _submit(client, key, name="a different campaign")

    assert clash.status_code == CONFLICT
    assert clash.json()["detail"]["code"] == "idempotency_key_reused"


def test_a_key_raced_with_a_different_body_conflicts_rather_than_creating_a_second_campaign(
    client: TestClient, migrated: Engine
) -> None:
    """Same key, two bodies, released together. One of them wins the key; the other is told the key
    is taken. Which one wins is a race — that there is exactly one campaign is not."""
    key = f"key:{uuid.uuid4().hex}"
    barrier = Barrier(2)

    def submit(name: str) -> Any:
        barrier.wait(timeout=30)
        return _submit(client, key, name=name)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit, name) for name in ("first body", "second body")]
        responses = [future.result(timeout=60) for future in futures]

    assert sorted(response.status_code for response in responses) == [CREATED, CONFLICT]
    created = next(r for r in responses if r.status_code == CREATED)
    refused = next(r for r in responses if r.status_code == CONFLICT)
    assert refused.json()["detail"]["code"] == "idempotency_key_reused"
    assert _campaign_footprint(migrated, uuid.UUID(created.json()["campaign_id"])) == {
        "campaigns": ONE_CAMPAIGN,
        "work_items": TWO_CANDIDATES,
        "jobs": TWO_CANDIDATES,
        "campaign.created": ONE_EVENT,
    }


def test_a_submission_without_an_idempotency_key_is_refused(client: TestClient) -> None:
    response = client.post("/campaigns", json=_body())

    assert response.status_code == BAD_REQUEST
    assert response.json()["detail"]["code"] == "idempotency_key_required"


def test_a_blank_idempotency_key_is_refused(client: TestClient) -> None:
    """A header of spaces is an absent key, not an empty one, and must not become a stored key."""
    response = client.post("/campaigns", json=_body(), headers={"Idempotency-Key": "   "})

    assert response.status_code == BAD_REQUEST
    assert response.json()["detail"]["code"] == "idempotency_key_required"


def test_an_idempotency_key_too_long_to_store_is_refused_before_the_insert(
    client: TestClient,
) -> None:
    """Otherwise the driver rejects it mid-transaction and the caller sees a 5xx for what is a
    client error with a stable code."""
    oversized = "k" * (MAX_IDEMPOTENCY_KEY_LENGTH + 1)

    response = client.post("/campaigns", json=_body(), headers={"Idempotency-Key": oversized})

    assert response.status_code == BAD_REQUEST
    assert response.json()["detail"]["code"] == "idempotency_key_too_long"


def test_an_inadmissible_origin_mode_pair_is_refused_before_it_reaches_the_database(
    client: TestClient,
) -> None:
    """ADR-010. The database would refuse it too; refusing here gives the caller a typed code
    instead of a constraint-violation stack trace."""
    response = _submit(
        client, f"key:{uuid.uuid4().hex}", data_origin="observed", execution_mode="simulation"
    )

    assert response.status_code == UNPROCESSABLE
    assert response.json()["detail"]["code"] == "inadmissible_origin_mode"


def test_reading_back_a_campaign_reports_its_origin_and_counts(client: TestClient) -> None:
    created = _submit(client, f"key:{uuid.uuid4().hex}")
    campaign_id = created.json()["campaign_id"]

    view = client.get(f"/campaigns/{campaign_id}")

    assert view.status_code == REPLAYED
    body = view.json()
    assert body["data_origin"] == "synthetic"
    assert body["execution_mode"] == "replay"
    assert body["work_items"] == TWO_CANDIDATES
    assert body["succeeded"] == 0


def test_an_unknown_campaign_is_a_typed_not_found(client: TestClient) -> None:
    response = client.get(f"/campaigns/{uuid.uuid4()}")

    assert response.status_code == NOT_FOUND
    assert response.json()["detail"]["code"] == "campaign_not_found"


def test_health_does_not_depend_on_the_database(client: TestClient) -> None:
    """A liveness probe that fails when a dependency is down makes an orchestrator restart a
    process that is working fine."""
    response = client.get("/health")

    assert response.status_code == REPLAYED
    assert response.json()["status"] == "ok"


def test_ready_does_depend_on_the_database(client: TestClient) -> None:
    """That dependence is the entire difference from `/health` (F-024)."""
    response = client.get("/ready")

    assert response.status_code == REPLAYED
    assert response.json()["status"] == "ready"

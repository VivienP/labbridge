"""The FastAPI submission path.

`docs/SPEC.md` §11.1 lists the V1 endpoint set. The runtime currently needs one submission path and
one read path, so those are what exist. A stub for an endpoint with no code behind it would be a
claim without evidence (`AI_CONTRACT.md` invariant 10), so the rest are absent rather than
returning 501.

**Every mutating endpoint requires an idempotency key**, and the requirement is not decoration: a
client that retries a submission after an ambiguous timeout must get the same campaign back, not a
second one. The key is stored with a hash of the request body, so a key reused with a *different*
body is a client bug and is reported as a conflict rather than silently returning the first result
(F-001, F-002).

Errors use typed shapes with machine-readable codes, so a caller can branch on `code` rather than
parsing prose.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Annotated, Any, Final

from fastapi import FastAPI, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, create_engine, func, select

from labbridge.domain.candidates import HerCandidate, candidate_id
from labbridge.domain.canonical import canonical_bytes
from labbridge.domain.identity import ADMISSIBLE_PAIRS, DataOrigin, ExecutionMode
from labbridge.infrastructure.persistence.config import DatabaseSettings
from labbridge.infrastructure.persistence.tables import (
    attempt_outcomes,
    campaigns,
    idempotency_keys,
    work_items,
)
from labbridge.runtime.events import append_event
from labbridge.runtime.jobs import enqueue

API_VERSION: Final = "1"
COMMAND_VERSION: Final = "1"
#: Scope for the idempotency table, so a key reused across different operations does not collide.
SUBMIT_SCOPE: Final = "campaigns.create"


class ErrorBody(BaseModel):
    """A typed error. `code` is stable and machine-readable; `message` is for a human."""

    code: str
    message: str


class CampaignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    environment_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    data_origin: DataOrigin
    execution_mode: ExecutionMode
    candidates: list[HerCandidate] = Field(min_length=1)


class CampaignCreated(BaseModel):
    campaign_id: uuid.UUID
    work_items: int
    #: True when this request matched an earlier one by idempotency key and nothing new was created.
    replayed: bool


class CampaignView(BaseModel):
    campaign_id: uuid.UUID
    name: str
    state: str
    data_origin: str
    execution_mode: str
    work_items: int
    succeeded: int
    failed: int


def _error(code: str, message: str, http_status: int) -> HTTPException:
    return HTTPException(
        status_code=http_status, detail=ErrorBody(code=code, message=message).model_dump()
    )


def create_app(engine: Engine | None = None) -> FastAPI:
    """Build the application. The engine is injectable so tests bind their own.

    The handlers close over `_engine()` rather than taking it through `Depends`. With
    `from __future__ import annotations` every annotation is a string, and FastAPI resolves those
    against the *module* namespace — a dependency defined as a local closure is unresolvable there,
    so the `Depends` marker is silently lost and the parameter becomes a query field. Closing over
    it directly cannot fail that way.
    """
    bound = engine

    def _engine() -> Engine:
        nonlocal bound
        if bound is None:
            bound = create_engine(DatabaseSettings().dsn, future=True)
        return bound

    app = FastAPI(title="LabBridge", version=API_VERSION)

    @app.get("/health")
    def health() -> dict[str, str]:
        """Liveness only. Deliberately does not touch the database: a health check that fails when
        a dependency is down makes an orchestrator restart a process that is working fine."""
        return {"status": "ok", "api_version": API_VERSION}

    @app.get("/ready")
    def ready() -> dict[str, str]:
        """Readiness *does* touch the database, because that is the difference from `/health`:
        this one answers "can I serve traffic", and without PostgreSQL the answer is no (F-024)."""
        try:
            with _engine().connect() as connection:
                connection.execute(select(1))
        except Exception as error:  # any failure means the same thing to a load balancer
            raise _error(
                "dependency_unavailable",
                f"database unreachable: {error}",
                status.HTTP_503_SERVICE_UNAVAILABLE,
            ) from error
        return {"status": "ready"}

    @app.post("/campaigns", status_code=status.HTTP_201_CREATED)
    def create_campaign(
        request: CampaignRequest,
        response: Response,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> CampaignCreated:
        """Declare a campaign and enqueue one durable job per candidate.

        The whole submission is one transaction. A partial submission — some work items enqueued,
        the campaign never recorded — would leave jobs referencing a campaign that does not exist,
        and no retry could repair it.
        """
        if not idempotency_key:
            raise _error(
                "idempotency_key_required",
                "every mutating request requires an Idempotency-Key header",
                status.HTTP_400_BAD_REQUEST,
            )
        if (request.data_origin, request.execution_mode) not in ADMISSIBLE_PAIRS:
            raise _error(
                "inadmissible_origin_mode",
                f"{request.data_origin}+{request.execution_mode} is not an admissible pair "
                "(ADR-010)",
                status.HTTP_422_UNPROCESSABLE_CONTENT,
            )

        request_hash = hashlib.sha256(canonical_bytes(request.model_dump(mode="json"))).hexdigest()

        with _engine().begin() as connection:
            existing = connection.execute(
                select(idempotency_keys.c.request_hash, idempotency_keys.c.response).where(
                    idempotency_keys.c.idempotency_key == idempotency_key
                )
            ).one_or_none()
            if existing is not None:
                if existing.request_hash != request_hash:
                    # Same key, different body. Returning the first result would silently discard
                    # this request; creating a second campaign would break the key's promise.
                    raise _error(
                        "idempotency_key_reused",
                        "this Idempotency-Key was used with a different request body",
                        status.HTTP_409_CONFLICT,
                    )
                response.status_code = status.HTTP_200_OK
                stored: dict[str, Any] = existing.response or {}
                return CampaignCreated(
                    campaign_id=uuid.UUID(stored["campaign_id"]),
                    work_items=int(stored["work_items"]),
                    replayed=True,
                )

            campaign_uuid = uuid.uuid4()
            correlation_id = uuid.uuid4()
            declaration = request.model_dump(mode="json")
            connection.execute(
                campaigns.insert().values(
                    campaign_id=campaign_uuid,
                    name=request.name,
                    environment_id=request.environment_id,
                    adapter_version=request.adapter_version,
                    data_origin=request.data_origin,
                    execution_mode=request.execution_mode,
                    state="active",
                    declaration=declaration,
                    declaration_hash=request_hash,
                    event_stream_contract_version=1,
                    event_stream_last_position=0,
                    created_at=func.now(),
                    updated_at=func.now(),
                )
            )
            campaign_event = append_event(
                connection,
                campaign_id=campaign_uuid,
                aggregate_id=campaign_uuid,
                aggregate_type="campaign",
                event_type="campaign.created",
                payload={
                    "name": request.name,
                    "environment_id": request.environment_id,
                    "adapter_version": request.adapter_version,
                    "data_origin": request.data_origin,
                    "execution_mode": request.execution_mode,
                    "declaration": declaration,
                    "declaration_hash": request_hash,
                    "state": "active",
                },
                expected_version=0,
                correlation_id=correlation_id,
                causation_id=None,
            )
            for candidate in request.candidates:
                work_item_id = uuid.uuid4()
                connection.execute(
                    work_items.insert().values(
                        work_item_id=work_item_id,
                        campaign_id=campaign_uuid,
                        candidate_id=candidate_id(candidate),
                        candidate=candidate.model_dump(mode="json"),
                        state="queued",
                        created_at=func.now(),
                        updated_at=func.now(),
                    )
                )
                work_item_event = append_event(
                    connection,
                    campaign_id=campaign_uuid,
                    aggregate_id=work_item_id,
                    aggregate_type="work_item",
                    event_type="work_item.queued",
                    payload={
                        "candidate_id": candidate_id(candidate),
                        "candidate": candidate.model_dump(mode="json"),
                        "state": "queued",
                    },
                    expected_version=0,
                    correlation_id=correlation_id,
                    causation_id=campaign_event.event_id,
                )
                enqueue(
                    connection,
                    campaign_id=campaign_uuid,
                    work_item_id=work_item_id,
                    idempotency_key=f"{idempotency_key}:{candidate_id(candidate)}",
                    command_version=COMMAND_VERSION,
                    correlation_id=correlation_id,
                    causation_id=work_item_event.event_id,
                )
            connection.execute(
                idempotency_keys.insert().values(
                    idempotency_key=idempotency_key,
                    scope=SUBMIT_SCOPE,
                    request_hash=request_hash,
                    response={
                        "campaign_id": str(campaign_uuid),
                        "work_items": len(request.candidates),
                    },
                    created_at=func.now(),
                )
            )

        return CampaignCreated(
            campaign_id=campaign_uuid, work_items=len(request.candidates), replayed=False
        )

    @app.get("/campaigns/{campaign_id}")
    def read_campaign(campaign_id: uuid.UUID) -> CampaignView:
        with _engine().begin() as connection:
            row = connection.execute(
                select(campaigns).where(campaigns.c.campaign_id == campaign_id)
            ).one_or_none()
            if row is None:
                raise _error(
                    "campaign_not_found", f"no campaign {campaign_id}", status.HTTP_404_NOT_FOUND
                )
            item_count = connection.execute(
                select(func.count())
                .select_from(work_items)
                .where(work_items.c.campaign_id == campaign_id)
            ).scalar_one()
            succeeded = connection.execute(
                select(func.count())
                .select_from(attempt_outcomes)
                .where(
                    attempt_outcomes.c.campaign_id == campaign_id,
                    attempt_outcomes.c.status == "succeeded",
                )
            ).scalar_one()
            failed = connection.execute(
                select(func.count())
                .select_from(attempt_outcomes)
                .where(
                    attempt_outcomes.c.campaign_id == campaign_id,
                    attempt_outcomes.c.status == "failed_terminal",
                )
            ).scalar_one()

        return CampaignView(
            campaign_id=campaign_id,
            name=row.name,
            state=row.state,
            data_origin=row.data_origin,
            execution_mode=row.execution_mode,
            work_items=int(item_count),
            succeeded=int(succeeded),
            failed=int(failed),
        )

    return app

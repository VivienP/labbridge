"""The FastAPI submission path.

`docs/SPEC.md` §11.1 lists the V1 endpoint set. The runtime currently needs one submission path and
one read path, so those are what exist. A stub for an endpoint with no code behind it would be a
claim without evidence (`AI_CONTRACT.md` invariant 10), so the rest are absent rather than
returning 501.

**Every mutating endpoint requires an idempotency key**, and the requirement is not decoration: a
client that retries a submission after an ambiguous timeout must get the same campaign back, not a
second one. The key is stored with a canonical fingerprint of the request, so a key reused with a
*different* body is a client bug and is reported as a conflict rather than silently returning the
first result (F-001, F-002).

**The key reservation is the first statement of the submission transaction**, written with
`INSERT ... ON CONFLICT DO NOTHING`. That is what makes the uniqueness constraint decide which of
two concurrent identical requests creates the campaign. A prior `SELECT` would let both through:
each would find no record, each would create a campaign, and the loser would surface as a
constraint violation the caller sees as a 500 (ADR-015).

Errors use typed shapes with machine-readable codes, so a caller can branch on `code` rather than
parsing prose.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Final

from fastapi import FastAPI, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, Engine, create_engine, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from labbridge.application.campaigns import CampaignControlResult, CampaignControlService
from labbridge.application.cv_ingestion import CVIngestionService
from labbridge.application.experiments import ExperimentService
from labbridge.application.source_intake import SourceArtifactService
from labbridge.domain.campaigns import CampaignDeclaration
from labbridge.domain.candidates import HerCandidate, candidate_id
from labbridge.domain.idempotency import (
    IdempotencyConflictError,
    IdempotencyKeyError,
    check_request_fingerprint,
    normalise_idempotency_key,
    request_fingerprint,
    work_item_instruction_key,
)
from labbridge.domain.identity import ADMISSIBLE_PAIRS, DataOrigin, ExecutionMode
from labbridge.infrastructure.cv_wiring import build_cv_service
from labbridge.infrastructure.experiment_wiring import build_experiment_service
from labbridge.infrastructure.persistence.config import DatabaseSettings
from labbridge.infrastructure.persistence.tables import (
    attempt_outcomes,
    campaigns,
    idempotency_keys,
    work_items,
)
from labbridge.infrastructure.source_wiring import build_source_service
from labbridge.runtime.budgets import (
    BudgetError,
    PostgresCampaignControlRepository,
    budget_usage,
)
from labbridge.runtime.events import append_event
from labbridge.runtime.jobs import enqueue

from .cv import register_cv_routes
from .experiments import register_experiment_routes
from .frontend import register_frontend
from .source_artifacts import register_source_routes

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
    budget: CampaignDeclaration | None = None


class CampaignControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)


class CampaignCreated(BaseModel):
    campaign_id: uuid.UUID
    work_items: int
    #: True when this request matched an earlier one by idempotency key and nothing new was created.
    replayed: bool


class CampaignBudgetView(BaseModel):
    hard_limit: Decimal
    unit: str
    reserved: Decimal
    consumed: Decimal
    released: Decimal
    outstanding: Decimal
    remaining: Decimal


class CampaignAttemptsView(BaseModel):
    total: int
    by_status: dict[str, int]
    failure_codes: dict[str, int]


class CampaignView(BaseModel):
    campaign_id: uuid.UUID
    name: str
    state: str
    data_origin: str
    execution_mode: str
    work_items: int
    succeeded: int
    failed: int
    budget: CampaignBudgetView
    attempts: CampaignAttemptsView


def _error(code: str, message: str, http_status: int) -> HTTPException:
    return HTTPException(
        status_code=http_status, detail=ErrorBody(code=code, message=message).model_dump()
    )


def _source_service_provider(
    engine_provider: Callable[[], Engine], configured: SourceArtifactService | None
) -> Callable[[], SourceArtifactService]:
    bound = configured

    def provide() -> SourceArtifactService:
        nonlocal bound
        if bound is None:
            bound = build_source_service(engine_provider())
        return bound

    return provide


def _cv_service_provider(
    engine_provider: Callable[[], Engine],
    source_provider: Callable[[], SourceArtifactService],
    configured: CVIngestionService | None,
) -> Callable[[], CVIngestionService]:
    bound = configured

    def provide() -> CVIngestionService:
        nonlocal bound
        if bound is None:
            bound = build_cv_service(source_provider(), engine_provider())
        return bound

    return provide


def _experiment_service_provider(
    engine_provider: Callable[[], Engine],
    source_provider: Callable[[], SourceArtifactService],
    cv_provider: Callable[[], CVIngestionService],
    configured: ExperimentService | None,
) -> Callable[[], ExperimentService]:
    bound = configured

    def provide() -> ExperimentService:
        nonlocal bound
        if bound is None:
            bound = build_experiment_service(source_provider(), cv_provider(), engine_provider())
        return bound

    return provide


def _replay(
    connection: Connection,
    response: Response,
    *,
    key: str,
    request_hash: str,
    legacy_request_hash: str | None = None,
) -> CampaignCreated:
    """Answer a request whose key was already reserved, or refuse it as a conflict.

    Reached only when the reservation above inserted nothing, which means a committed record holds
    the key. `.one()` rather than `.one_or_none()` for that reason: had the holding transaction
    rolled back, `ON CONFLICT DO NOTHING` would have inserted instead of returning nothing, so an
    absent row here is a broken invariant and must fail loudly rather than be papered over. This
    rests on READ COMMITTED, where each statement takes a fresh snapshot; under a stricter isolation
    level PostgreSQL raises a serialisation failure rather than hiding the committed row.

    A record that names no campaign is refused rather than dereferenced. The column is nullable for
    records written before it existed, and the migration backfills it only where the campaign
    survives — so the one reachable case is a replay of a key whose campaign is gone, which is a
    typed conflict rather than a 500 from a null.
    """
    stored = connection.execute(
        select(
            idempotency_keys.c.request_hash,
            idempotency_keys.c.campaign_id,
            idempotency_keys.c.response,
        ).where(
            idempotency_keys.c.scope == SUBMIT_SCOPE,
            idempotency_keys.c.idempotency_key == key,
        )
    ).one()
    if stored.request_hash not in {request_hash, legacy_request_hash}:
        try:
            check_request_fingerprint(key=key, stored=stored.request_hash, offered=request_hash)
        except IdempotencyConflictError as error:
            raise _error(
                error.code,
                "this Idempotency-Key was used with a different request body",
                status.HTTP_409_CONFLICT,
            ) from error
    body: dict[str, Any] = stored.response or {}
    if stored.campaign_id is None or "work_items" not in body:
        raise _error(
            "idempotency_record_unresolvable",
            f"the record for Idempotency-Key {key!r} no longer names a campaign",
            status.HTTP_409_CONFLICT,
        )
    response.status_code = status.HTTP_200_OK
    return CampaignCreated(
        campaign_id=stored.campaign_id,
        work_items=int(body["work_items"]),
        replayed=True,
    )


def create_app(  # noqa: PLR0915 - one explicit registration point for all HTTP adapters
    engine: Engine | None = None,
    source_service: SourceArtifactService | None = None,
    cv_service: CVIngestionService | None = None,
    experiment_service: ExperimentService | None = None,
    frontend_dir: Path | None = None,
) -> FastAPI:
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
    source_provider = _source_service_provider(_engine, source_service)
    register_source_routes(app, source_provider)

    cv_provider = _cv_service_provider(_engine, source_provider, cv_service)
    register_cv_routes(app, cv_provider)
    register_experiment_routes(
        app,
        _experiment_service_provider(
            _engine,
            source_provider,
            cv_provider,
            experiment_service,
        ),
    )

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
        try:
            key = normalise_idempotency_key(idempotency_key)
        except IdempotencyKeyError as error:
            raise _error(error.code, str(error), status.HTTP_400_BAD_REQUEST) from error
        if (request.data_origin, request.execution_mode) not in ADMISSIBLE_PAIRS:
            raise _error(
                "inadmissible_origin_mode",
                f"{request.data_origin}+{request.execution_mode} is not an admissible pair "
                "(ADR-010)",
                status.HTTP_422_UNPROCESSABLE_CONTENT,
            )

        budget = request.budget or CampaignDeclaration(
            hard_budget=len(request.candidates) * 3,
            per_attempt_estimate=1,
            budget_unit="attempt",
            max_attempts=3,
            stopping_rule="hard_budget_exhausted",
        )
        declaration = request.model_dump(mode="json", exclude={"budget"})
        legacy_request_hash = request_fingerprint(declaration) if request.budget is None else None
        declaration["budget"] = budget.model_dump(mode="json")
        request_hash = request_fingerprint(declaration)
        campaign_uuid = uuid.uuid4()
        stored_response = {
            "campaign_id": str(campaign_uuid),
            "work_items": len(request.candidates),
        }

        with _engine().begin() as connection:
            # First statement, deliberately. The unique key is the arbiter: a concurrent identical
            # request blocks here until this transaction settles and then reads what it committed,
            # rather than both requests finding nothing and both creating a campaign.
            reserved = connection.execute(
                pg_insert(idempotency_keys)
                .values(
                    scope=SUBMIT_SCOPE,
                    idempotency_key=key,
                    request_hash=request_hash,
                    campaign_id=campaign_uuid,
                    response=stored_response,
                    created_at=func.now(),
                )
                .on_conflict_do_nothing(index_elements=["scope", "idempotency_key"])
                .returning(idempotency_keys.c.idempotency_key)
            ).one_or_none()
            if reserved is None:
                return _replay(
                    connection,
                    response,
                    key=key,
                    request_hash=request_hash,
                    legacy_request_hash=legacy_request_hash,
                )

            correlation_id = uuid.uuid4()
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
                    hard_budget=budget.hard_budget,
                    per_attempt_estimate=budget.per_attempt_estimate,
                    budget_unit=budget.budget_unit,
                    max_attempts=budget.max_attempts,
                    stopping_rule=budget.stopping_rule,
                    event_stream_contract_version=2,
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
                    # The instruction identity is the work item, not this request's token. A
                    # redelivery of the same instruction is then recognisable as the same work
                    # whatever delivery carries it.
                    instruction_key=work_item_instruction_key(
                        work_item_id=work_item_id, command_version=COMMAND_VERSION
                    ),
                    command_version=COMMAND_VERSION,
                    correlation_id=correlation_id,
                    causation_id=work_item_event.event_id,
                    max_attempts=budget.max_attempts,
                )

        return CampaignCreated(
            campaign_id=campaign_uuid, work_items=len(request.candidates), replayed=False
        )

    def _control_campaign(
        campaign_id: uuid.UUID,
        request: CampaignControlRequest,
        idempotency_key: str | None,
        action: str,
    ) -> CampaignControlResult:
        service = CampaignControlService(PostgresCampaignControlRepository(_engine()))
        try:
            operation = getattr(service, action)
            result: CampaignControlResult = operation(
                campaign_id,
                expected_version=request.expected_version,
                idempotency_key=idempotency_key,
            )
            return result
        except IdempotencyKeyError as error:
            raise _error(error.code, str(error), status.HTTP_400_BAD_REQUEST) from error
        except IdempotencyConflictError as error:
            raise _error(error.code, str(error), status.HTTP_409_CONFLICT) from error
        except BudgetError as error:
            http_status = (
                status.HTTP_404_NOT_FOUND
                if error.code == "campaign_not_found"
                else status.HTTP_409_CONFLICT
            )
            raise _error(error.code, str(error), http_status) from error
        except ValueError as error:
            code = getattr(error, "code", "campaign_control_conflict")
            raise _error(code, str(error), status.HTTP_409_CONFLICT) from error

    @app.post("/campaigns/{campaign_id}/pause")
    def pause_campaign(
        campaign_id: uuid.UUID,
        request: CampaignControlRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> CampaignControlResult:
        return _control_campaign(campaign_id, request, idempotency_key, "pause")

    @app.post("/campaigns/{campaign_id}/resume")
    def resume_campaign(
        campaign_id: uuid.UUID,
        request: CampaignControlRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> CampaignControlResult:
        return _control_campaign(campaign_id, request, idempotency_key, "resume")

    @app.post("/campaigns/{campaign_id}/cancel")
    def cancel_campaign(
        campaign_id: uuid.UUID,
        request: CampaignControlRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> CampaignControlResult:
        return _control_campaign(campaign_id, request, idempotency_key, "cancel")

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
            outcome_rows = connection.execute(
                select(attempt_outcomes.c.status, attempt_outcomes.c.failure).where(
                    attempt_outcomes.c.campaign_id == campaign_id
                )
            ).all()
            by_status: dict[str, int] = {}
            failure_codes: dict[str, int] = {}
            for outcome in outcome_rows:
                by_status[outcome.status] = by_status.get(outcome.status, 0) + 1
                if outcome.failure and outcome.failure.get("failure_code"):
                    code = str(outcome.failure["failure_code"])
                    failure_codes[code] = failure_codes.get(code, 0) + 1
            usage = budget_usage(connection, campaign_id)

        return CampaignView(
            campaign_id=campaign_id,
            name=row.name,
            state=row.state,
            data_origin=row.data_origin,
            execution_mode=row.execution_mode,
            work_items=int(item_count),
            succeeded=int(succeeded),
            failed=int(failed),
            budget=CampaignBudgetView(
                hard_limit=usage.hard_limit,
                unit=usage.unit,
                reserved=usage.reserved,
                consumed=usage.consumed,
                released=usage.released,
                outstanding=usage.outstanding,
                remaining=usage.remaining,
            ),
            attempts=CampaignAttemptsView(
                total=len(outcome_rows),
                by_status=by_status,
                failure_codes=failure_codes,
            ),
        )

    register_frontend(app, frontend_dir)
    return app

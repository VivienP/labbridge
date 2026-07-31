"""The Slice 1 demonstration: one campaign from declaration to a verified evidence bundle.

`docs/ROADMAP.md` Slice 1 asks for one complete path, runnable from a clean Compose environment.
This module is that path and nothing more — no policy, no selection, no scheduling. It exists so the
exit criterion can be *run* rather than argued about.

What it demonstrates is the runtime. It is **not** a scientific result: it replays a generated
fixture, so every record it produces is `synthetic`, and the manifest says so. A fixture-backed run
is not evidence about the physical system.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Engine, create_engine, func

from labbridge.domain.candidates import HerCandidate, candidate_id
from labbridge.domain.quantities import Quantity
from labbridge.environments.her_replay import HerReplayAdapter
from labbridge.evidence.bundle import build_bundle, verify_bundle
from labbridge.infrastructure.objectstore import ObjectStore
from labbridge.infrastructure.persistence.config import DatabaseSettings
from labbridge.infrastructure.persistence.tables import campaigns, work_items
from labbridge.runtime.jobs import enqueue
from labbridge.runtime.worker import Worker


@dataclass(frozen=True)
class DemoReport:
    campaign_id: uuid.UUID
    submitted: int
    succeeded: int
    failed_terminal: int
    suppressed: int
    bundle_path: Path
    manifest: dict[str, object]


def engine_from_settings() -> Engine:
    return create_engine(DatabaseSettings().dsn, future=True)


def _declare(engine: Engine, adapter: HerReplayAdapter, name: str) -> uuid.UUID:
    """Create the campaign with the adapter's own origin and mode.

    Taken from the adapter rather than passed in: the adapter derived them from the evidence on
    disk, and a campaign declaring something else would be refused by the composite foreign key
    anyway (ADR-010). Reading them here makes that agreement the default rather than a trap.
    """
    campaign_id = uuid.uuid4()
    environment = adapter.environment
    with engine.begin() as connection:
        connection.execute(
            campaigns.insert().values(
                campaign_id=campaign_id,
                name=name,
                environment_id=environment.environment_id,
                adapter_version=environment.adapter_version,
                data_origin=environment.data_origin,
                execution_mode=environment.execution_mode,
                state="active",
                declaration={"demo": True, "locations": "adapter-known"},
                declaration_hash="0" * 64,
                created_at=func.now(),
                updated_at=func.now(),
            )
        )
    return campaign_id


def _submit(engine: Engine, campaign_id: uuid.UUID, candidate: HerCandidate) -> None:
    work_item_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            work_items.insert().values(
                work_item_id=work_item_id,
                campaign_id=campaign_id,
                candidate_id=candidate_id(candidate),
                candidate=candidate.model_dump(mode="json"),
                state="queued",
                created_at=func.now(),
                updated_at=func.now(),
            )
        )
        enqueue(
            connection,
            work_item_id=work_item_id,
            idempotency_key=f"demo:{campaign_id}:{candidate_id(candidate)}",
            command_version="1",
        )


async def run_demo(
    engine: Engine,
    adapter: HerReplayAdapter,
    store: ObjectStore,
    bundle_root: Path,
    *,
    locations: int = 3,
    include_unmeasured: bool = True,
) -> DemoReport:
    """Declare, submit, drain the queue, export, verify.

    `include_unmeasured` submits one location the source never measured, so the demonstration shows
    a terminal outcome alongside the successes. A demo that only ever succeeded would say nothing
    about the runtime's actual job, which is handling the failures.
    """
    campaign_id = _declare(engine, adapter, "slice 1 demonstration")
    known = adapter.known_locations()[:locations]
    for key in known:
        _submit(engine, campaign_id, _candidate(key.library_id, key.measurement_area_id))

    submitted = len(known)
    if include_unmeasured and known:
        measured = {k.measurement_area_id for k in adapter.known_locations()}
        unmeasured = next(
            (str(n) for n in range(1, 500) if str(n) not in measured),
            None,
        )
        if unmeasured is not None:
            _submit(engine, campaign_id, _candidate(known[0].library_id, unmeasured))
            submitted += 1

    worker = Worker(engine, adapter, store, name="demo-worker")
    tally = {"succeeded": 0, "failed_terminal": 0, "duplicate_suppressed": 0}
    while True:
        outcome = await worker.run_once()
        if outcome is None:
            break
        tally[outcome.status] = tally.get(outcome.status, 0) + 1

    destination = bundle_root / str(campaign_id)
    with engine.begin() as connection:
        manifest = build_bundle(connection, campaign_id, destination)
    verify_bundle(destination)

    return DemoReport(
        campaign_id=campaign_id,
        submitted=submitted,
        succeeded=tally["succeeded"],
        failed_terminal=tally["failed_terminal"],
        suppressed=tally["duplicate_suppressed"],
        bundle_path=destination,
        manifest=manifest,
    )


def _candidate(library: str, area: str) -> HerCandidate:
    return HerCandidate(
        library_id=library,
        measurement_area_id=area,
        grid_x=Quantity(value=Decimal("0"), unit="mm"),
        grid_y=Quantity(value=Decimal("0"), unit="mm"),
    )

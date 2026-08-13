"""A worker that really dies, at a chosen boundary, for the crash-recovery tests.

Run as a subprocess and killed with `SIGKILL`/`TerminateProcess` mid-flight. That is the only way to
cross a real process boundary: an exception raised inside the worker is caught by the worker's own
handlers and recorded, which is correct behaviour but proves nothing about a process that stops
existing.

Run by path, not by module: `tests/` is not a package, so `-m` does not resolve it.

    python tests/integration/worker_subprocess.py <fixture-root> <state-path> <kill-stage>

The stage says where to stop. At that point the process writes everything the parent needs to prove
what recovery did — the identifiers, the lease it held, the fencing token, any staged object key —
and then blocks forever waiting to be killed. One harness, every worker boundary; a second fault
framework would be a second thing to keep honest.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from sqlalchemy import Engine, create_engine, select

from labbridge.environments.her_replay import HerReplayAdapter
from labbridge.infrastructure.objectstore import S3ObjectStore, StoredObject
from labbridge.infrastructure.persistence.config import DatabaseSettings, ObjectStoreSettings
from labbridge.infrastructure.persistence.tables import attempts, jobs
from labbridge.runtime import jobs as jobs_module
from labbridge.runtime.worker import Worker

#: Every boundary the harness can stop at, in the order a successful execution passes them.
KILL_STAGES = (
    "after_lease_acquisition",
    "after_adapter_return_before_upload",
    "during_object_upload",
    "after_upload_before_outcome_transaction",
    "after_commit_before_acknowledgement",
)
DEFAULT_KILL_STAGE = "after_upload_before_outcome_transaction"
_STAGE_ARGUMENT = 3


class _Reporter:
    """Accumulates what the parent needs, and publishes it atomically.

    Written through a temporary file and renamed: the parent polls this path, and a half-written
    JSON document read at the wrong moment is a flaky test rather than a failed one.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._state: dict[str, Any] = {"pid": os.getpid(), "kill_stage": None, "committed": False}
        self._publish_lease: Callable[[], None] | None = None

    def on_halt(self, publish: Callable[[], None]) -> None:
        """Register work to do before halting, so the published state is complete.

        The lease is read here rather than by a background poller: a poller races the halt, and a
        boundary reached early enough leaves the parent with a state file that never learned which
        job the child held.
        """
        self._publish_lease = publish

    def record(self, **values: Any) -> None:
        self._state.update(values)
        self.flush()

    def has(self, key: str) -> bool:
        return bool(self._state.get(key))

    def get(self, key: str) -> Any:
        return self._state.get(key)

    def flush(self) -> None:
        scratch = self._path.with_suffix(".partial")
        scratch.write_text(json.dumps(self._state, default=str), encoding="utf-8")
        # Retried because the parent is polling this path: on Windows a replace fails outright
        # while the reader has the file open, and losing the publish would strand the parent
        # waiting for a boundary the child has already reached.
        for attempt in range(50):
            try:
                scratch.replace(self._path)
            except PermissionError:
                if attempt == 49:  # noqa: PLR2004 - the last try re-raises rather than hiding it
                    raise
                time.sleep(0.02)
            else:
                return

    def halt(self, stage: str) -> None:
        """Report that this boundary was reached, then wait to be killed or resumed."""
        if self._publish_lease is not None:
            self._publish_lease()
        self.record(kill_stage=stage, reached=True)
        resume_path_value = os.environ.get("LABBRIDGE_RESUME_PATH")
        resume_path = Path(resume_path_value) if resume_path_value else None
        while True:  # pragma: no cover - the parent kills the process here
            if resume_path is not None and resume_path.exists():
                self.record(resumed=True)
                return
            time.sleep(0.05)


class StagedStore:
    """The real store, stopped either side of the upload according to the chosen stage."""

    def __init__(self, inner: S3ObjectStore, client: Any, reporter: _Reporter, stage: str) -> None:
        self._inner = inner
        self._client = client
        self._reporter = reporter
        self._stage = stage
        self.bucket = inner.bucket

    def put_and_verify(self, key: str, data: bytes, *, media_type: str) -> StoredObject:
        if self._stage == "after_adapter_return_before_upload":
            # The adapter has returned and the `pending` row is written, but no bytes exist yet.
            self._reporter.halt(self._stage)
        if self._stage == "during_object_upload":
            multipart = self._client.create_multipart_upload(
                Bucket=self.bucket, Key=key, ContentType=media_type
            )
            upload_id = str(multipart["UploadId"])
            self._client.upload_part(
                Bucket=self.bucket,
                Key=key,
                UploadId=upload_id,
                PartNumber=1,
                Body=data[: max(1, len(data) // 2)],
            )
            self._reporter.record(
                object_keys=[key], multipart_upload_id=upload_id, multipart_part_count=1
            )
            self._reporter.halt(self._stage)
        stored = self._inner.put_and_verify(key, data, media_type=media_type)
        self._reporter.record(object_keys=[key], sha256=stored.sha256, byte_size=stored.byte_size)
        if self._stage == "after_upload_before_outcome_transaction":
            # Bytes in the store, a `pending` row referencing them, nothing committed.
            self._reporter.halt(self._stage)
        return stored

    def get(self, key: str) -> bytes:
        return self._inner.get(key)

    def exists(self, key: str) -> bool:
        return self._inner.exists(key)


def _report_lease(engine: Engine, reporter: _Reporter, worker_name: str) -> None:
    """Publish the lease this process held, whether or not it still holds it.

    Looked up by job identity when one is already known, and by owner otherwise: a job that reached
    its outcome has released its lease, so the owner lookup alone finds nothing at exactly the
    boundary where the work is already durable.

    `lease_owner` is reported as this process's own name rather than read back from the row, for the
    same reason — the row may legitimately have released it by now, and what the parent needs to
    know is which worker held it.
    """
    known_job = reporter.get("job_id")
    where = jobs.c.job_id == known_job if known_job else jobs.c.lease_owner == worker_name
    with engine.begin() as connection:
        row = connection.execute(
            select(
                jobs.c.job_id,
                jobs.c.work_item_id,
                jobs.c.lease_generation,
                jobs.c.lease_expires_at,
                jobs.c.lease_token,
            ).where(where)
        ).one_or_none()
        if row is None:  # pragma: no cover - only if the queue was empty
            return
        attempt = connection.execute(
            select(attempts.c.attempt_id)
            .where(attempts.c.job_id == row.job_id)
            .order_by(attempts.c.ordinal.desc())
            .limit(1)
        ).scalar_one_or_none()
    reporter.record(
        job_id=row.job_id,
        work_item_id=row.work_item_id,
        lease_owner=worker_name,
        lease_token=row.lease_token,
        fencing_token=row.lease_generation,
        lease_expires_at=row.lease_expires_at,
        attempt_id=attempt,
    )


class _StagedWorker(Worker):
    """Reports the lease as soon as it is held, and can stop right there."""

    def __init__(self, *args: Any, reporter: _Reporter, stage: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._reporter = reporter
        self._stage = stage

    async def run_once(self):  # type: ignore[no-untyped-def]
        outcome = await super().run_once()
        # `after_commit_before_acknowledgement`: everything is durable and the caller never learns
        # it. The process dies holding a result that the database already has.
        self._reporter.record(
            committed=True,
            # Recorded before the halt: by now the job has released its lease, so the halt hook has
            # to look it up by identity rather than by owner.
            job_id=outcome.job_id if outcome else None,
            outcome_status=outcome.status if outcome else None,
            observation_id=outcome.observation_id if outcome else None,
        )
        if self._stage == "after_commit_before_acknowledgement":
            self._reporter.halt(self._stage)
        return outcome


async def _run(
    engine: Engine, worker: _StagedWorker, reporter: _Reporter, stage: str, lease_seconds: int
) -> None:
    if stage == "after_lease_acquisition":
        # Claim, publish the lease, and stop before the adapter is ever called. Done by hand rather
        # than through `run_once` because the boundary is *inside* the claim transaction's effects.
        # The lease duration has to be passed explicitly here: `claim` would otherwise take the
        # production default, and the parent's wait for expiry would never be long enough.
        worker.start()
        with engine.begin() as connection:
            lease = jobs_module.claim(connection, owner=worker.name, lease_seconds=lease_seconds)
            if lease is not None:
                jobs_module.mark_running(connection, lease)
        reporter.halt(stage)
        return
    await worker.run_once()


def main() -> int:
    fixture_root = Path(sys.argv[1])
    state_path = Path(sys.argv[2])
    stage = sys.argv[_STAGE_ARGUMENT] if len(sys.argv) > _STAGE_ARGUMENT else DEFAULT_KILL_STAGE
    if stage not in KILL_STAGES:
        raise SystemExit(f"unknown kill stage {stage!r}; expected one of {', '.join(KILL_STAGES)}")

    reporter = _Reporter(state_path)
    reporter.record(kill_stage=stage, reached=False)

    settings = ObjectStoreSettings()
    client = boto3.client(
        "s3",
        endpoint_url=settings.endpoint_url,
        aws_access_key_id=settings.access_key,
        aws_secret_access_key=settings.secret_key,
        config=BotoConfig(signature_version="s3v4"),
        region_name=settings.region,
    )
    store = S3ObjectStore(
        client, bucket=os.environ.get("LABBRIDGE_FAULT_BUCKET", f"{settings.bucket}-tests")
    )
    engine = create_engine(DatabaseSettings().dsn, future=True)
    worker_name = os.environ.get("LABBRIDGE_WORKER_NAME", "worker-subprocess")
    lease_seconds = int(os.environ.get("LABBRIDGE_LEASE_SECONDS", "60"))

    worker = _StagedWorker(
        engine,
        HerReplayAdapter(fixture_root),
        StagedStore(store, client, reporter, stage),  # type: ignore[arg-type]
        name=worker_name,
        reporter=reporter,
        stage=stage,
        lease_seconds=lease_seconds,
        heartbeat_seconds=float(os.environ.get("LABBRIDGE_HEARTBEAT_SECONDS", "5")),
    )
    # Read at the halt, whichever boundary that is, so the parent always learns which job and
    # which fencing token the child held.
    reporter.on_halt(lambda: _report_lease(engine, reporter, worker_name))
    asyncio.run(_run(engine, worker, reporter, stage, lease_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

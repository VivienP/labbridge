"""A worker that really dies, for the crash-recovery tests.

Run as a subprocess and killed with `SIGKILL`/`TerminateProcess` mid-flight. That is the only way to
cross a real process boundary: an exception raised inside the worker is caught by the worker's own
handlers and recorded, which is correct behaviour but proves nothing about a process that stops
existing (`AI_CONTRACT.md` §9).

Run by path, not by module: `tests/` is not a package, so `-m` does not resolve it.

    python tests/integration/worker_subprocess.py <fixture-root> <marker-path>

It claims one job, uploads the observation object, writes `<marker-path>` to say the upload landed,
then blocks forever. The parent kills it there — with the bytes in storage, a `pending` row
referencing them, and the job still leased by a process that will never return.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig
from sqlalchemy import create_engine

from labbridge.environments.her_replay import HerReplayAdapter
from labbridge.infrastructure.objectstore import S3ObjectStore, StoredObject
from labbridge.infrastructure.persistence.config import DatabaseSettings, ObjectStoreSettings
from labbridge.runtime.worker import Worker


class BlockAfterUpload:
    """Uploads for real, then stops responding — the state a killed process leaves behind."""

    def __init__(self, inner: S3ObjectStore, marker: Path) -> None:
        self._inner = inner
        self._marker = marker
        self.bucket = inner.bucket

    def put_and_verify(self, key: str, data: bytes, *, media_type: str) -> StoredObject:
        self._inner.put_and_verify(key, data, media_type=media_type)
        self._marker.write_text(key, encoding="utf-8")
        while True:  # pragma: no cover - the parent kills the process here
            time.sleep(0.05)

    def get(self, key: str) -> bytes:
        return self._inner.get(key)

    def exists(self, key: str) -> bool:
        return self._inner.exists(key)


def main() -> int:
    fixture_root = Path(sys.argv[1])
    marker = Path(sys.argv[2])

    settings = ObjectStoreSettings()
    client = boto3.client(
        "s3",
        endpoint_url=settings.endpoint_url,
        aws_access_key_id=settings.access_key,
        aws_secret_access_key=settings.secret_key,
        config=BotoConfig(signature_version="s3v4"),
        region_name=settings.region,
    )
    store = S3ObjectStore(client, bucket=f"{settings.bucket}-tests")
    engine = create_engine(DatabaseSettings().dsn, future=True)
    worker = Worker(
        engine,
        HerReplayAdapter(fixture_root),
        BlockAfterUpload(store, marker),  # type: ignore[arg-type]
        name="worker-subprocess",
    )
    asyncio.run(worker.run_once())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

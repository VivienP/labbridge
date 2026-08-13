"""Build a campaign Package, expose a partial export, and wait to be terminated."""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig
from sqlalchemy import create_engine

from labbridge.evidence.campaign_package import (
    build_campaign_experiment_package,
    campaign_package_inputs_from_postgres,
)
from labbridge.infrastructure.objectstore import S3ObjectStore
from labbridge.infrastructure.persistence.config import DatabaseSettings, ObjectStoreSettings


def _publish_state(path: Path, payload: dict[str, object]) -> None:
    scratch = path.with_suffix(".partial")
    scratch.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(scratch, path)


def main() -> int:
    campaign_id = uuid.UUID(sys.argv[1])
    output = Path(sys.argv[2])
    state_path = Path(sys.argv[3])
    bucket = sys.argv[4]
    settings = ObjectStoreSettings()
    client = boto3.client(
        "s3",
        endpoint_url=settings.endpoint_url,
        aws_access_key_id=settings.access_key,
        aws_secret_access_key=settings.secret_key,
        config=BotoConfig(signature_version="s3v4"),
        region_name=settings.region,
    )
    store = S3ObjectStore(client, bucket=bucket)
    engine = create_engine(DatabaseSettings().dsn, future=True)
    with engine.begin() as connection:
        inputs = campaign_package_inputs_from_postgres(
            connection,
            campaign_id,
            producing_versions={"labbridge": "0.1.0", "campaign_package": "1"},
            limitations=[
                "Synthetic fixture replay does not establish physical-system performance."
            ],
        )
    package = build_campaign_experiment_package(inputs)
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(output.suffix + ".partial")
    midpoint = max(1, len(package.archive_bytes) // 2)
    partial.write_bytes(package.archive_bytes[:midpoint])
    _publish_state(
        state_path,
        {
            "pid": os.getpid(),
            "reached": True,
            "kill_stage": "during_evidence_export",
            "partial_path": str(partial),
            "partial_byte_size": midpoint,
            "complete_byte_size": len(package.archive_bytes),
            "package_id": package.package_id,
            "object_store_bucket": store.bucket,
        },
    )
    while True:  # pragma: no cover - the parent terminates this process
        time.sleep(0.05)


if __name__ == "__main__":
    raise SystemExit(main())

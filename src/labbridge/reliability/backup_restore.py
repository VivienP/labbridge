"""Quiescent PostgreSQL and object-store backup/restore verification for Phase 7."""

from __future__ import annotations

import hashlib
import secrets
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from sqlalchemy import Engine, create_engine, func, select

from labbridge.evidence.campaign_package import (
    build_campaign_experiment_package,
    campaign_package_inputs_from_postgres,
)
from labbridge.evidence.experiment_package import verify_experiment_package
from labbridge.infrastructure.objectstore import S3ObjectStore, digest
from labbridge.infrastructure.persistence.config import DatabaseSettings
from labbridge.infrastructure.persistence.tables import campaigns, metadata
from labbridge.runtime.replay import compare_campaign_projection

TERMINAL_CAMPAIGN_STATES = frozenset({"completed", "budget_exhausted", "cancelled", "failed"})
_MINIO_ROOT_PASSWORD_VARIABLE = "MINIO_ROOT_" + "PASSWORD"


def _run(
    argv: list[str],
    *,
    cwd: Path,
    input_bytes: bytes | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        input=input_bytes,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if completed.returncode:
        raise RuntimeError(
            f"operational command {argv[0]} failed: " + completed.stderr.decode(errors="replace")
        )
    return completed


def _create_empty_database(name: str) -> None:
    admin = create_engine(DatabaseSettings(name="labbridge").dsn, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        present = connection.exec_driver_sql(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
        ).scalar_one_or_none()
        if present is not None:
            raise RuntimeError(f"restore database already exists: {name}")
        connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
    admin.dispose()


def _table_counts(engine: Engine) -> dict[str, int]:
    with engine.begin() as connection:
        return {
            table.name: int(
                connection.execute(select(func.count()).select_from(table)).scalar_one()
            )
            for table in metadata.sorted_tables
        }


def _wait_minio(endpoint: str, access_key: str, secret_key: str) -> Any:
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 1}),
        region_name="us-east-1",
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            client.list_buckets()
        except Exception:
            time.sleep(0.5)
        else:
            return client
    raise TimeoutError("restored MinIO did not become ready")


def verify_backup_restore(  # noqa: PLR0915 - each step is an audited restore assertion
    *,
    source_engine: Engine,
    source_store: S3ObjectStore,
    repo_root: Path,
    source_database: str,
    restore_database: str,
    working_root: Path,
    restore_minio_port: int = 59100,
) -> dict[str, object]:
    """Exercise PO-09 in distinct database and MinIO instances.

    The temporary restored object-store process is removed after verification.
    """
    started = datetime.now(UTC)
    with source_engine.begin() as connection:
        nonterminal = connection.execute(
            select(campaigns.c.campaign_id, campaigns.c.state).where(
                campaigns.c.state.not_in(TERMINAL_CAMPAIGN_STATES)
            )
        ).all()
        campaign_ids = list(
            connection.execute(select(campaigns.c.campaign_id).order_by(campaigns.c.campaign_id))
            .scalars()
            .all()
        )
    if nonterminal:
        raise RuntimeError("backup requires quiescent terminal campaigns")

    working_root.mkdir(parents=True, exist_ok=True)
    dump = _run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "pg_dump",
            "--username=labbridge",
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            source_database,
        ],
        cwd=repo_root,
        timeout=600,
    ).stdout
    dump_path = working_root / "postgres.dump"
    dump_path.write_bytes(dump)
    dump_sha256 = hashlib.sha256(dump).hexdigest()

    source_client = source_store._client
    listed = source_client.list_objects_v2(Bucket=source_store.bucket).get("Contents", [])
    object_bytes: dict[str, bytes] = {}
    object_inventory: list[dict[str, object]] = []
    for item in sorted(listed, key=lambda value: str(value["Key"])):
        key = str(item["Key"])
        payload = source_store.get(key)
        object_bytes[key] = payload
        object_inventory.append({"key": key, "byte_size": len(payload), "sha256": digest(payload)})

    _create_empty_database(restore_database)
    _run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "pg_restore",
            "--username=labbridge",
            "--dbname",
            restore_database,
            "--no-owner",
            "--no-privileges",
        ],
        cwd=repo_root,
        input_bytes=dump,
        timeout=600,
    )
    source_counts = _table_counts(source_engine)
    restore_engine = create_engine(DatabaseSettings(name=restore_database).dsn, future=True)
    restored_counts = _table_counts(restore_engine)
    if source_counts != restored_counts:
        raise RuntimeError("restored PostgreSQL table counts differ from the backup source")

    container_name = f"labbridge-phase7-restore-{uuid.uuid4().hex[:8]}"
    endpoint = f"http://localhost:{restore_minio_port}"
    access_key = "labbridge-restore"
    secret_key = secrets.token_urlsafe(32)
    _run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            container_name,
            "--publish",
            f"{restore_minio_port}:9000",
            "--env",
            f"MINIO_ROOT_USER={access_key}",
            "--env",
            f"{_MINIO_ROOT_PASSWORD_VARIABLE}={secret_key}",
            "minio/minio:latest",
            "server",
            "/data",
        ],
        cwd=repo_root,
    )
    try:
        restore_client = _wait_minio(endpoint, access_key, secret_key)
        restore_store = S3ObjectStore(restore_client, bucket=source_store.bucket)
        restore_store.ensure_bucket()
        for key, payload in object_bytes.items():
            restore_store.put_and_verify(key, payload, media_type="application/octet-stream")
        for entry in object_inventory:
            if digest(restore_store.get(str(entry["key"]))) != entry["sha256"]:
                raise RuntimeError("restored object checksum differs from backup inventory")

        replay_matches = 0
        package_verifications = 0
        with restore_engine.begin() as connection:
            for campaign_id in campaign_ids:
                comparison = compare_campaign_projection(connection, campaign_id)
                if not comparison.matches:
                    raise RuntimeError(f"restored campaign {campaign_id} differs from replay")
                replay_matches += 1
                inputs = campaign_package_inputs_from_postgres(
                    connection,
                    campaign_id,
                    producing_versions={"labbridge": "0.1.0", "campaign_package": "1"},
                    limitations=["Restored synthetic replay reliability evidence."],
                )
                package = build_campaign_experiment_package(inputs)
                verification = verify_experiment_package(
                    package.archive_bytes, object_store=restore_store
                )
                if not getattr(verification, "verified", False):
                    raise RuntimeError("restored Package verification failed")
                package_verifications += 1
    finally:
        restore_engine.dispose()
        _run(["docker", "stop", container_name], cwd=repo_root)
        _run(["docker", "rm", container_name], cwd=repo_root)

    return {
        "passed": True,
        "status": "PASSED",
        "started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "source_database": source_database,
        "restore_database": restore_database,
        "postgres_dump_sha256": dump_sha256,
        "postgres_dump_byte_size": len(dump),
        "table_counts": source_counts,
        "object_count": len(object_inventory),
        "object_inventory": object_inventory,
        "restored_object_checksums_verified": len(object_inventory),
        "campaigns_replayed_equal": replay_matches,
        "restored_packages_fully_verified": package_verifications,
        "restore_minio_distinct_instance": True,
        "writers_quiescent": True,
    }


__all__ = ["verify_backup_restore"]

"""Run and release the measured Phase 7 process-boundary reliability campaign."""
# ruff: noqa: E402 - a clean checkout must add src before importing LabBridge

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import os
import platform
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import boto3
from alembic import command
from alembic.config import Config
from botocore.config import Config as BotoConfig
from sqlalchemy import create_engine, func, select

from labbridge.environments.her_replay import HerReplayAdapter
from labbridge.infrastructure.her_ingestion.fixture import (
    FIXTURE_MANIFEST_FILENAME,
    FixtureSpec,
    build_fixture,
)
from labbridge.infrastructure.her_ingestion.provenance import write_document
from labbridge.infrastructure.objectstore import S3ObjectStore
from labbridge.infrastructure.persistence.config import DatabaseSettings, ObjectStoreSettings
from labbridge.infrastructure.persistence.tables import campaigns
from labbridge.reliability.backup_restore import verify_backup_restore
from labbridge.reliability.fault_campaign import plan_fault_points
from labbridge.reliability.migration_rehearsal import rehearse_migration
from labbridge.reliability.producer_identity import require_clean_committed_producer
from labbridge.reliability.release import (
    verify_fault_campaign_release,
    write_fault_campaign_release,
)
from labbridge.reliability.runner import CampaignExecution, execute_one

_SAFE_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{2,62}$")


def _ensure_database(name: str) -> None:
    if not _SAFE_NAME.fullmatch(name):
        raise ValueError("database name must contain only letters, numbers, and underscores")
    settings = DatabaseSettings(name="labbridge")
    admin = create_engine(settings.dsn, future=True, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        present = connection.exec_driver_sql(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
        ).scalar_one_or_none()
        if present is None:
            connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
    admin.dispose()


def _migrate() -> None:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(config, "head")


def _source_tree_digest() -> str:
    paths = [
        *sorted((REPO_ROOT / "src").rglob("*.py")),
        *sorted((REPO_ROOT / "scripts").glob("*.py")),
        *sorted((REPO_ROOT / "migrations" / "versions").glob("*.py")),
        REPO_ROOT / "tests" / "integration" / "worker_subprocess.py",
    ]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(REPO_ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _command_output(argv: list[str]) -> str:
    completed = subprocess.run(
        argv, cwd=REPO_ROOT, capture_output=True, text=True, check=False, timeout=30
    )
    return (completed.stdout or completed.stderr).strip()


def _environment_manifest(
    *,
    database_name: str,
    bucket: str,
    fixture_manifest_sha256: str,
    producer: dict[str, object],
) -> dict[str, object]:
    versions = {}
    for distribution in ("labbridge", "sqlalchemy", "psycopg", "boto3", "alembic"):
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[distribution] = "not-installed-as-distribution"
    return {
        "recorded_at": datetime.now(UTC).isoformat(),
        "operating_system": platform.platform(),
        "python": sys.version,
        "python_executable": sys.executable,
        "dependency_versions": versions,
        "git_head": producer["git_head"],
        "origin_main": producer["origin_main"],
        "merge_base_with_origin_main": producer["merge_base_with_origin_main"],
        "origin_main_contained": producer["origin_main_contained"],
        "working_tree": producer["working_tree"],
        "source_tree_sha256": _source_tree_digest(),
        "docker_version": _command_output(["docker", "version", "--format", "{{.Server.Version}}"]),
        "compose_images": _command_output(["docker", "compose", "images", "--format", "json"]),
        "database_name": database_name,
        "object_store_bucket": bucket,
        "fixture_manifest_sha256": fixture_manifest_sha256,
        "data_origin": "synthetic",
        "execution_mode": "replay",
        "future_live_execution": "not_run_out_of_scope",
        "secrets_recorded": False,
    }


async def _run(args: argparse.Namespace) -> list[CampaignExecution]:
    producer = require_clean_committed_producer(REPO_ROOT, allow_dirty=args.allow_dirty)
    os.environ["LABBRIDGE_DB_NAME"] = args.database_name
    os.environ["LABBRIDGE_S3_BUCKET"] = args.bucket
    _ensure_database(args.database_name)
    _migrate()
    engine = create_engine(DatabaseSettings().dsn, future=True)
    with engine.begin() as connection:
        existing = connection.execute(select(func.count()).select_from(campaigns)).scalar_one()
    if existing:
        raise RuntimeError("fault campaign requires a dedicated database with no campaigns")

    settings = ObjectStoreSettings()
    client = boto3.client(
        "s3",
        endpoint_url=settings.endpoint_url,
        aws_access_key_id=settings.access_key,
        aws_secret_access_key=settings.secret_key,
        config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 1}),
        region_name=settings.region,
    )
    store = S3ObjectStore(client, bucket=args.bucket)
    store.ensure_bucket()
    if client.list_objects_v2(Bucket=args.bucket).get("KeyCount", 0):
        raise RuntimeError("fault campaign requires a dedicated empty object-store bucket")

    fixture_root = args.output / "working" / "fixture"
    fixture_root.mkdir(parents=True, exist_ok=True)
    fixture_manifest = build_fixture(
        fixture_root,
        spec=FixtureSpec(areas_per_library=6, seccm_areas_per_library=2),
        generator_version="0.1.0",
    )
    write_document(fixture_root / FIXTURE_MANIFEST_FILENAME, fixture_manifest)
    adapter = HerReplayAdapter(fixture_root)
    plan = plan_fault_points(campaigns=args.campaigns, master_seed=args.master_seed)
    run_id = f"phase7-{args.master_seed}-{args.campaigns}"
    subprocess_env = {
        "LABBRIDGE_DB_NAME": args.database_name,
        "LABBRIDGE_S3_BUCKET": args.bucket,
    }
    executions: list[CampaignExecution] = []
    progress = args.output / "working" / "raw-progress.jsonl"
    progress.parent.mkdir(parents=True, exist_ok=True)
    for index, fault_point in enumerate(plan, start=1):
        execution = await execute_one(
            engine=engine,
            adapter=adapter,
            store=store,
            repo_root=REPO_ROOT,
            fixture_root=fixture_root,
            output_root=args.output / "working",
            run_id=run_id,
            ordinal=index,
            seed=args.master_seed + index,
            fault_point=fault_point,
            subprocess_env=subprocess_env,
            s3_client=client,
        )
        executions.append(execution)
        with progress.open("a", encoding="utf-8") as stream:
            stream.write(execution.result.model_dump_json() + "\n")
        print(
            f"[{index:03d}/{args.campaigns:03d}] {fault_point} "
            f"campaign={execution.result.campaign_id} replay={execution.result.replay_equal} "
            f"package={execution.result.package_verified}",
            flush=True,
        )
    fixture_sha = hashlib.sha256(
        (fixture_root / FIXTURE_MANIFEST_FILENAME).read_bytes()
    ).hexdigest()
    environment = _environment_manifest(
        database_name=args.database_name,
        bucket=args.bucket,
        fixture_manifest_sha256=fixture_sha,
        producer=dict(producer),
    )
    operational = verify_backup_restore(
        source_engine=engine,
        source_store=store,
        repo_root=REPO_ROOT,
        source_database=args.database_name,
        restore_database=f"{args.database_name}_restore",
        working_root=args.output / "working" / "backup",
    )
    migration = rehearse_migration(
        repo_root=REPO_ROOT, database_name=f"{args.database_name}_migration"
    )
    reproduce = (
        f"python scripts/reproduce_campaign_reliability.py --campaigns {args.campaigns} "
        f"--master-seed {args.master_seed} --database-name {args.database_name} "
        f"--bucket {args.bucket} --output build/phase7-fault-campaign"
    )
    write_fault_campaign_release(
        args.output / "release",
        executions,
        environment=environment,
        backup_restore=operational,
        migration_checks=migration,
        reproduce_command=reproduce,
    )
    verify_fault_campaign_release(args.output / "release")
    engine.dispose()
    return executions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaigns", type=int, default=100)
    parser.add_argument("--master-seed", type=int, default=20260813)
    parser.add_argument("--database-name", default="labbridge_phase7_fault_campaign")
    parser.add_argument("--bucket", default="labbridge-phase7-fault-campaign")
    parser.add_argument("--output", type=Path, default=Path("build/phase7-fault-campaign"))
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow an uncommitted worktree; still records working_tree=dirty in the manifest.",
    )
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        raise SystemExit(f"output directory is not empty: {args.output}")
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

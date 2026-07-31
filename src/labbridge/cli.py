"""The `labbridge` command line.

Registers only what is implemented. docs/SPEC.md section 11.2 lists the V1 minimum command set; a
stub for a command with no code behind it would be a claim without evidence (AI_CONTRACT.md
invariant 10).

This module translates options, calls the application operation, and renders the result. It holds no
acquisition logic (AI_CONTRACT.md section 5).
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final

import boto3
import typer
from botocore.config import Config as BotoConfig
from rich.console import Console
from rich.table import Table

from labbridge import __version__
from labbridge.demo import engine_from_settings, run_demo
from labbridge.environments.her_replay import HerReplayAdapter
from labbridge.evidence.bundle import BundleVerificationError, verify_bundle
from labbridge.infrastructure.her_ingestion.errors import HerIngestionError
from labbridge.infrastructure.her_ingestion.fetch import (
    DEFAULT_LANDING_ROOT,
    DEFAULT_MAX_BYTES,
    FetchReport,
    FetchRequest,
    run_fetch,
)
from labbridge.infrastructure.her_ingestion.fixture import (
    FIXTURE_MANIFEST_FILENAME,
    FixtureSpec,
    build_fixture,
)
from labbridge.infrastructure.her_ingestion.httpx_transport import HttpxTransport
from labbridge.infrastructure.her_ingestion.inspect import build_inventory
from labbridge.infrastructure.her_ingestion.provenance import (
    DATASET_INVENTORY_FILENAME,
    PROVENANCE_FILENAME,
    write_document,
)
from labbridge.infrastructure.her_ingestion.records import PINNED_DOI
from labbridge.infrastructure.her_ingestion.zenodo import ZenodoTransport
from labbridge.infrastructure.objectstore import S3ObjectStore
from labbridge.infrastructure.persistence.config import ObjectStoreSettings

EXPECTED_DOI: Final = PINNED_DOI
#: Beside the landing root and git-ignored with it. The fixture is regenerable from its seed, so
#: committing the archives would add bytes that the manifest already accounts for.
DEFAULT_FIXTURE_ROOT: Final = Path("data/her/fixture")
#: Bundles are regenerable from the database, so they live beside the data and stay git-ignored.
DEFAULT_BUNDLE_ROOT: Final = Path("data/bundles")

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__.splitlines()[0])
console = Console()


@app.callback()
def main() -> None:
    """Keep subcommand dispatch even while only one command is registered.

    Without a callback, Typer collapses a single-command app into a root command, and
    `labbridge fetch-her` would fail as an unexpected argument. docs/SPEC.md section 11.2 fixes the
    subcommand form.
    """


def _build_transport() -> ZenodoTransport:
    """The single place the real network transport is constructed; monkeypatched in tests."""
    return HttpxTransport()


def _utc_now() -> datetime:
    return datetime.now(UTC)


@app.command("fetch-her")
def fetch_her(
    record_id: Annotated[str, typer.Option("--record-id", help="Zenodo record identifier.")],
    file: Annotated[
        list[str] | None,
        typer.Option(
            "--file", help="A filename to acquire. Repeatable. Required unless --dry-run."
        ),
    ] = None,
    landing_root: Annotated[
        Path, typer.Option("--landing-root", help="Immutable raw landing directory.")
    ] = DEFAULT_LANDING_ROOT,
    max_bytes: Annotated[
        int, typer.Option("--max-bytes", help="Refuse any file above this size.")
    ] = DEFAULT_MAX_BYTES,
    allow_large: Annotated[
        list[str] | None,
        typer.Option("--allow-large", help="Permit one named file above --max-bytes. Repeatable."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Read the record and write the inventory; download nothing."
        ),
    ] = False,
) -> None:
    """Acquire explicitly named files from the pinned HER Zenodo record."""
    request = FetchRequest(
        record_id=record_id,
        filenames=tuple(file or ()),
        landing_root=landing_root,
        max_bytes=max_bytes,
        allow_large=tuple(allow_large or ()),
        dry_run=dry_run,
        expected_doi=EXPECTED_DOI,
    )
    try:
        report = run_fetch(
            request,
            transport=_build_transport(),
            clock=_utc_now,
            tool_version=__version__,
        )
    except HerIngestionError as error:
        console.print(f"[red]{error.code}[/red]: {error}")
        raise typer.Exit(code=1) from error

    _render(report, dry_run=dry_run)
    if report.inventory.licence.redistribution != "unresolved":
        console.print(f"[dim]attribution required: {request.data_use.attribution}[/dim]")


def _render(report: FetchReport, *, dry_run: bool) -> None:
    inventory = report.inventory
    console.print(
        f"record [bold]{inventory.record_id}[/bold] version {inventory.record_version} — "
        f"{inventory.title}"
    )
    open_gate = inventory.licence.redistribution == "unresolved"
    console.print(
        f"declared licence: {inventory.licence.raw_value or 'none'}  "
        f"redistribution: [{'yellow' if open_gate else 'green'}]"
        f"{inventory.licence.redistribution}[/]"
    )

    table = Table(title="record files")
    table.add_column("filename")
    table.add_column("bytes", justify="right")
    table.add_column("checksum")
    table.add_column("selected", justify="center")
    selected = {remote.filename for remote in report.selected}
    for remote in inventory.files:
        table.add_row(
            remote.filename,
            str(remote.byte_size),
            f"{remote.checksum_algorithm}:{remote.checksum_value[:12]}...",
            "yes" if remote.filename in selected else "",
        )
    console.print(table)
    console.print(f"inventory written to {report.inventory_path}")

    if dry_run:
        console.print("[dim]dry run: nothing was downloaded[/dim]")
        return
    for fetched in report.fetched:
        console.print(f"landed {fetched.landing_path}  sha256:{fetched.computed_sha256[:16]}...")
    console.print(f"provenance written to {report.provenance_path}")


@app.command("build-her-fixture")
def build_her_fixture(
    root: Annotated[
        Path, typer.Option("--root", help="Directory to write the fixture archives into.")
    ] = DEFAULT_FIXTURE_ROOT,
    seed: Annotated[
        int, typer.Option("--seed", help="Generator seed, recorded in the manifest.")
    ] = (FixtureSpec().seed),
) -> None:
    """Generate the independently produced, schema-compatible HER fixture."""
    manifest = build_fixture(root, spec=FixtureSpec(seed=seed), generator_version=__version__)
    write_document(root / FIXTURE_MANIFEST_FILENAME, manifest)

    table = Table(title="fixture archives")
    table.add_column("archive")
    table.add_column("members", justify="right")
    table.add_column("sha256")
    for archive in manifest.archives:
        table.add_row(archive.filename, str(archive.member_count), f"{archive.sha256[:16]}...")
    console.print(table)
    console.print(f"[yellow]data_origin: {manifest.data_origin}[/yellow] — {manifest.note}")
    console.print(f"manifest written to {root / FIXTURE_MANIFEST_FILENAME}")


@app.command("validate-artifacts")
def validate_artifacts(
    bundle: Annotated[
        Path | None,
        typer.Option("--bundle", help="One bundle to verify. Omit to verify every bundle found."),
    ] = None,
    bundle_root: Annotated[
        Path, typer.Option("--bundle-root", help="Where to look when --bundle is omitted.")
    ] = DEFAULT_BUNDLE_ROOT,
) -> None:
    """Recompute every checksum in an evidence bundle and report whether it still matches.

    Recomputed from the bytes on disk, never compared against a recorded size: a file edited in
    place keeps its size far more often than it keeps its hash.

    With no `--bundle` it verifies every bundle under the root, so the bare command is the gate
    `AI_CONTRACT.md` §10 names. An empty root is reported as such rather than passing silently —
    "nothing to check" and "everything checks out" are different answers.
    """
    if bundle is not None:
        _verify_one(bundle)
        return

    candidates = sorted(p for p in bundle_root.glob("*") if (p / "manifest.json").exists())
    if not candidates:
        console.print(f"[yellow]no bundle found under {bundle_root}[/yellow] — nothing verified")
        return
    for candidate in candidates:
        _verify_one(candidate)
    console.print(f"[green]{len(candidates)} bundle(s) verified[/green]")


def _verify_one(bundle: Path) -> None:
    try:
        manifest = verify_bundle(bundle)
    except BundleVerificationError as error:
        console.print(f"[red]bundle verification failed[/red] ({len(error.problems)} problem(s)):")
        for problem in error.problems:
            console.print(f"  - {problem}")
        raise typer.Exit(code=1) from error

    files = manifest["files"]
    assert isinstance(files, list)
    table = Table(title=f"bundle {manifest['campaign_id']}")
    table.add_column("file")
    table.add_column("bytes", justify="right")
    table.add_column("sha256")
    for entry in files:
        table.add_row(entry["name"], str(entry["byte_size"]), f"{entry['sha256'][:16]}...")
    console.print(table)
    origin = manifest["data_origin"]
    colour = "yellow" if origin == "synthetic" else "green"
    console.print(f"data_origin: [{colour}]{origin}[/{colour}]  mode: {manifest['execution_mode']}")
    console.print("[green]every checksum matches[/green]")


@app.command("demo-her")
def demo_her(
    landing_root: Annotated[
        Path, typer.Option("--root", help="Fixture or landing root the replay adapter reads.")
    ] = DEFAULT_FIXTURE_ROOT,
    bundle_root: Annotated[
        Path, typer.Option("--bundle-root", help="Where to write the evidence bundle.")
    ] = DEFAULT_BUNDLE_ROOT,
    locations: Annotated[
        int, typer.Option("--locations", help="How many measured locations to submit.")
    ] = 3,
) -> None:
    """Run one campaign end to end and verify the evidence bundle it produces.

    Requires PostgreSQL and MinIO from `docker-compose.yml`, and a fixture or landing root. This
    demonstrates the runtime; it is not a scientific result, and a fixture-backed run records
    itself as synthetic throughout.
    """
    adapter = HerReplayAdapter(landing_root)
    settings = ObjectStoreSettings()
    client = boto3.client(
        "s3",
        endpoint_url=settings.endpoint_url,
        aws_access_key_id=settings.access_key,
        aws_secret_access_key=settings.secret_key,
        config=BotoConfig(signature_version="s3v4"),
        region_name=settings.region,
    )
    store = S3ObjectStore(client, bucket=settings.bucket)
    store.ensure_bucket()

    report = asyncio.run(
        run_demo(engine_from_settings(), adapter, store, bundle_root, locations=locations)
    )

    table = Table(title=f"campaign {report.campaign_id}")
    table.add_column("outcome")
    table.add_column("count", justify="right")
    table.add_row("submitted", str(report.submitted))
    table.add_row("succeeded", str(report.succeeded))
    table.add_row("failed_terminal", str(report.failed_terminal))
    table.add_row("duplicate_suppressed", str(report.suppressed))
    console.print(table)
    console.print(
        f"[yellow]data_origin: {report.manifest['data_origin']}[/yellow] — a fixture-backed run "
        "is not evidence about the physical system"
    )
    console.print(f"bundle written and verified at {report.bundle_path}")


@app.command("inspect-her")
def inspect_her(
    landing_root: Annotated[
        Path,
        typer.Option("--landing-root", help="Landing directory holding the acquired archives."),
    ] = DEFAULT_LANDING_ROOT,
) -> None:
    """Inspect the acquired archives and write the versioned dataset inventory."""
    provenance = landing_root / PROVENANCE_FILENAME
    inventory = build_inventory(
        landing_root,
        clock=_utc_now,
        tool_version=__version__,
        provenance_sha256=(
            hashlib.sha256(provenance.read_bytes()).hexdigest() if provenance.exists() else None
        ),
    )
    destination = landing_root / DATASET_INVENTORY_FILENAME
    write_document(destination, inventory)

    table = Table(title="archives inspected")
    table.add_column("archive")
    table.add_column("members", justify="right")
    table.add_column("tables", justify="right")
    table.add_column("filename shapes", justify="right")
    for archive in inventory.archives:
        table.add_row(
            archive.archive_filename,
            str(archive.member_count),
            str(len(archive.tables)),
            str(len(archive.groups)),
        )
    console.print(table)
    if inventory.provenance_sha256 is None:
        console.print(
            "[yellow]no provenance.json found: the inventory is not tied to an acquisition[/yellow]"
        )
    console.print(f"dataset inventory written to {destination}")

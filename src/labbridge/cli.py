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
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final, cast

import boto3
import typer
from botocore.config import Config as BotoConfig
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from sqlalchemy import create_engine

from labbridge import __version__
from labbridge.application.cv_ingestion import CVIngestionError, CVIngestionService
from labbridge.application.electrolysis_ingestion import (
    ElectrolysisIngestionError,
    ElectrolysisIngestionService,
)
from labbridge.application.experiments import (
    ExperimentApplicationError,
    ExperimentService,
    UserAssertionCommand,
)
from labbridge.application.source_intake import (
    IntakeSource,
    SourceArtifactService,
    SourceIntakeError,
)
from labbridge.demo import engine_from_settings, run_demo
from labbridge.domain.cv import CSVFormat, CVImportProfile
from labbridge.domain.electrolysis import ElectrolysisImportProfile
from labbridge.domain.identity import DataOrigin, ExecutionMode
from labbridge.domain.parser_diagnostics import SourceFormat
from labbridge.environments.her_replay import HerReplayAdapter
from labbridge.evidence.bundle import (
    BundleVerificationError,
    VerificationMode,
    VerificationStatus,
    verify_bundle,
)
from labbridge.evidence.experiment_package import (
    ExperimentPackageVerificationError,
    verify_experiment_package,
)
from labbridge.evidence.manifest import ArtifactVerificationError, verify_manifest
from labbridge.infrastructure.cv_csv import CsvParseError
from labbridge.infrastructure.cv_wiring import build_cv_service
from labbridge.infrastructure.electrolysis_wiring import build_electrolysis_service
from labbridge.infrastructure.experiment_wiring import build_experiment_service
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
from labbridge.infrastructure.objectstore import ObjectStore, S3ObjectStore
from labbridge.infrastructure.persistence.config import DatabaseSettings, ObjectStoreSettings
from labbridge.infrastructure.source_wiring import build_source_service
from labbridge.runtime.reconciliation import reconcile

EXPECTED_DOI: Final = PINNED_DOI
#: Beside the landing root and git-ignored with it. The fixture is regenerable from its seed, so
#: committing the archives would add bytes that the manifest already accounts for.
DEFAULT_FIXTURE_ROOT: Final = Path("data/her/fixture")
#: Bundles are regenerable from the database, so they live beside the data and stay git-ignored.
DEFAULT_BUNDLE_ROOT: Final = Path("data/bundles")
#: Released evidence is committed here and is what the bare gate must protect. It is searched
#: alongside the bundle root so the gate cannot pass by finding nothing in a git-ignored directory.
DEFAULT_ARTIFACT_ROOT: Final = Path("artifacts")
DEFAULT_VERIFICATION_ROOTS: Final = (DEFAULT_ARTIFACT_ROOT, DEFAULT_BUNDLE_ROOT)

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__.splitlines()[0])
#: `docs/SPEC.md` §11.2 fixes `labbridge demo her` and `labbridge evidence verify <bundle>` as
#: subcommand groups, so they are groups rather than hyphenated names.
demo_app = typer.Typer(no_args_is_help=True, help="Run a demonstration campaign end to end.")
evidence_app = typer.Typer(no_args_is_help=True, help="Build and verify evidence bundles.")
source_app = typer.Typer(no_args_is_help=True, help="Retain and verify opaque source files.")
cv_app = typer.Typer(
    no_args_is_help=True, help="Inspect and normalise explicitly mapped CV source files."
)
electrolysis_app = typer.Typer(
    no_args_is_help=True,
    help="Retain and normalise explicitly mapped galvanostatic electrolysis files.",
)
experiment_app = typer.Typer(
    no_args_is_help=True, help="Version experiments and release Experiment Passports."
)
package_app = typer.Typer(
    no_args_is_help=True, help="Create, download, and independently verify Experiment Packages."
)
app.add_typer(demo_app, name="demo")
app.add_typer(evidence_app, name="evidence")
app.add_typer(source_app, name="source")
app.add_typer(cv_app, name="cv")
app.add_typer(electrolysis_app, name="electrolysis")
app.add_typer(experiment_app, name="experiment")
app.add_typer(package_app, name="package")
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


def _build_object_store() -> ObjectStore:
    settings = ObjectStoreSettings()
    client = boto3.client(
        "s3",
        endpoint_url=settings.endpoint_url,
        aws_access_key_id=settings.access_key,
        aws_secret_access_key=settings.secret_key,
        config=BotoConfig(signature_version="s3v4"),
        region_name=settings.region,
    )
    return S3ObjectStore(client, bucket=settings.bucket)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _build_source_service() -> SourceArtifactService:
    return build_source_service()


def _build_cv_service() -> CVIngestionService:
    return build_cv_service(_build_source_service())


def _build_electrolysis_service() -> ElectrolysisIngestionService:
    return build_electrolysis_service(_build_source_service())


def _build_experiment_service() -> ExperimentService:
    engine = create_engine(DatabaseSettings().dsn, future=True)
    source_service = _build_source_service()
    cv_service = build_cv_service(source_service, engine)
    # Both normalisation readers, so `experiment create` resolves an electrolysis observation
    # instead of falling through to the CV reader and reporting it as not found.
    return build_experiment_service(
        source_service,
        cv_service,
        engine,
        electrolysis_service=build_electrolysis_service(source_service, engine),
    )


def _experiment_failure(error: Exception) -> None:
    code = getattr(error, "code", "experiment_request_invalid")
    console.print(f"[red]{escape(str(code))}[/red]: {escape(str(error))}")
    raise typer.Exit(code=1) from error


def _cv_failure(error: Exception) -> None:
    code = getattr(error, "code", "cv_ingestion_error")
    parser_record_id = getattr(error, "parser_record_id", None)
    parser_identity = "" if parser_record_id is None else f" parser_record_id={parser_record_id}"
    console.print(f"[red]{code}[/red]: {error}{parser_identity}")
    raise typer.Exit(code=1) from error


def _electrolysis_failure(error: Exception) -> None:
    code = getattr(error, "code", "electrolysis_ingestion_error")
    console.print(f"[red]{escape(str(code))}[/red]: {escape(str(error))}")
    raise typer.Exit(code=1) from error


@cv_app.command("inspect")
def cv_inspect(
    source_artifact_id: Annotated[str, typer.Argument(help="Retained Phase 1 source identity.")],
    encoding: Annotated[str, typer.Option("--encoding", help="Explicit source encoding.")],
    delimiter: Annotated[
        str, typer.Option("--delimiter", help="Explicit one-character delimiter.")
    ],
    header_row: Annotated[int, typer.Option("--header-row", help="One-based header row.")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON.")
    ] = False,
) -> None:
    """Inspect declared CSV structure without assigning column roles."""
    try:
        csv_format = CSVFormat.model_validate(
            {"encoding": encoding, "delimiter": delimiter, "header_row": header_row}
        )
        inspected = _build_cv_service().inspect(source_artifact_id, csv_format)
    except (CVIngestionError, SourceIntakeError, CsvParseError, ValueError) as error:
        _cv_failure(error)
    payload = {
        "source_artifact_id": inspected.source_artifact_id,
        "source_sha256": inspected.source_sha256,
        "headers": inspected.headers,
        "row_count": inspected.row_count,
    }
    if json_output:
        typer.echo(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    else:
        console.print("\n".join(inspected.headers))


@cv_app.command("profile-create")
def cv_profile_create(
    profile_file: Annotated[Path, typer.Argument(help="Versioned JSON import profile.")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON.")
    ] = False,
) -> None:
    """Validate and retain one immutable explicit import profile."""
    try:
        profile = CVImportProfile.model_validate_json(profile_file.read_text(encoding="utf-8"))
        stored = _build_cv_service().create_profile(profile)
    except (OSError, ValueError, CVIngestionError) as error:
        _cv_failure(error)
    payload = {
        "profile_id": stored.profile_id,
        "profile": stored.profile.model_dump(mode="json"),
        "replayed": stored.replayed,
    }
    if json_output:
        typer.echo(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    else:
        console.print(stored.profile_id)


@cv_app.command("normalise")
def cv_normalise(
    source_artifact_id: Annotated[str, typer.Argument(help="Retained Phase 1 source identity.")],
    profile_id: Annotated[
        str, typer.Option("--profile-id", help="Explicit import profile identity.")
    ],
    source_format: Annotated[
        str,
        typer.Option("--source-format", help="Explicit source format: generic_csv or gamry_dta."),
    ] = "generic_csv",
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON.")
    ] = False,
) -> None:
    """Normalise one retained source through the shared application service."""
    try:
        if source_format not in {"generic_csv", "gamry_dta"}:
            raise ValueError("source format must be `generic_csv` or `gamry_dta`")
        stored = _build_cv_service().normalise(
            source_artifact_id,
            profile_id,
            source_format=cast(SourceFormat, source_format),
        )
    except (CVIngestionError, SourceIntakeError, CsvParseError, ValueError) as error:
        _cv_failure(error)
    payload = {"result": stored.result.model_dump(mode="json"), "replayed": stored.replayed}
    if json_output:
        typer.echo(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    else:
        console.print(stored.result.observation.observation_id)


@cv_app.command("parser-record")
def cv_parser_record(
    parser_record_id: Annotated[str, typer.Argument(help="Retained parser record identity.")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON.")
    ] = False,
) -> None:
    """Read one accepted or rejected parser diagnostic record."""
    try:
        stored = _build_cv_service().get_parser_record(parser_record_id)
    except CVIngestionError as error:
        _cv_failure(error)
    payload = {"record": stored.record.model_dump(mode="json"), "replayed": True}
    if json_output:
        typer.echo(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    else:
        console.print(stored.record.parser_record_id)


@cv_app.command("plot")
def cv_plot(
    observation_id: Annotated[str, typer.Argument(help="Normalised CV observation identity.")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON.")
    ] = False,
) -> None:
    """Return backend-approved plot values without display transformations."""
    try:
        plot = _build_cv_service().plot_series(observation_id)
    except CVIngestionError as error:
        _cv_failure(error)
    payload = {
        "observation_id": plot.observation_id,
        "data_origin": plot.data_origin,
        "execution_mode": plot.execution_mode,
        "environment_id": plot.environment_id,
        "series": [item.model_dump(mode="json") for item in plot.series],
        "provenance": plot.provenance.model_dump(mode="json"),
    }
    if json_output:
        typer.echo(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    else:
        console.print(plot.observation_id)


@electrolysis_app.command("profile-create")
def electrolysis_profile_create(
    profile_file: Annotated[Path, typer.Argument(help="Versioned JSON import profile.")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON.")
    ] = False,
) -> None:
    """Validate and retain one immutable explicit electrolysis import profile."""
    try:
        profile = ElectrolysisImportProfile.model_validate_json(
            profile_file.read_text(encoding="utf-8")
        )
        stored = _build_electrolysis_service().create_profile(profile)
    except (OSError, ValueError, ElectrolysisIngestionError) as error:
        _electrolysis_failure(error)
    payload = {
        "profile_id": stored.profile_id,
        "profile": stored.profile.model_dump(mode="json"),
        "replayed": stored.replayed,
    }
    if json_output:
        typer.echo(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    else:
        console.print(stored.profile_id)


@electrolysis_app.command("normalise")
def electrolysis_normalise(
    source_artifact_id: Annotated[str, typer.Argument(help="Retained source identity.")],
    profile_id: Annotated[
        str, typer.Option("--profile-id", help="Explicit electrolysis import profile identity.")
    ],
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON.")
    ] = False,
) -> None:
    """Normalise one retained electrolysis source through the shared application service."""
    try:
        stored = _build_electrolysis_service().normalise(source_artifact_id, profile_id)
    except (
        ElectrolysisIngestionError,
        SourceIntakeError,
        CsvParseError,
        ValueError,
    ) as error:
        _electrolysis_failure(error)
    payload = {"result": stored.result.model_dump(mode="json"), "replayed": stored.replayed}
    if json_output:
        typer.echo(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    else:
        console.print(stored.result.observation.observation_id)


@experiment_app.command("create")
def experiment_create(
    observation_id: Annotated[str, typer.Argument(help="Retained Phase 2 observation identity.")],
    expected_version: Annotated[
        int,
        typer.Option("--expected-version", help="Expected experiment version; use 0 to create."),
    ],
    idempotency_key: Annotated[
        str, typer.Option("--idempotency-key", help="Stable request identity.")
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Create the initial immutable experiment version from one normalised observation."""
    try:
        stored = _build_experiment_service().create_experiment(
            observation_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
    except (ExperimentApplicationError, ValueError) as error:
        _experiment_failure(error)
    payload = {"experiment": stored.experiment.model_dump(mode="json"), "replayed": stored.replayed}
    if json_output:
        typer.echo(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    else:
        console.print(stored.experiment.experiment_id)


@experiment_app.command("show")
def experiment_show(
    experiment_id: Annotated[str, typer.Argument(help="Experiment identity.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Read the current immutable experiment version."""
    try:
        stored = _build_experiment_service().get_experiment(experiment_id)
    except ExperimentApplicationError as error:
        _experiment_failure(error)
    payload = {"experiment": stored.experiment.model_dump(mode="json"), "replayed": True}
    if json_output:
        typer.echo(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    else:
        console.print(f"{stored.experiment.experiment_id} v{stored.experiment.version}")


@experiment_app.command("assert")
def experiment_assert(
    experiment_id: Annotated[str, typer.Argument(help="Experiment identity.")],
    assertion_file: Annotated[
        Path, typer.Argument(help="User assertion JSON; origin is always user_supplied.")
    ],
    expected_version: Annotated[int, typer.Option("--expected-version")],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Append one user supplement or correction without rewriting earlier assertions."""
    try:
        command = UserAssertionCommand.model_validate_json(
            assertion_file.read_text(encoding="utf-8")
        )
        stored = _build_experiment_service().add_user_assertion(
            experiment_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            command=command,
        )
    except (OSError, ExperimentApplicationError, ValueError) as error:
        _experiment_failure(error)
    payload = {"experiment": stored.experiment.model_dump(mode="json"), "replayed": stored.replayed}
    if json_output:
        typer.echo(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    else:
        console.print(f"{stored.experiment.experiment_id} v{stored.experiment.version}")


@experiment_app.command("validate")
def experiment_validate(
    experiment_id: Annotated[str, typer.Argument(help="Experiment identity.")],
    expected_version: Annotated[int, typer.Option("--expected-version")],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Persist the deterministic validation findings and release decision."""
    try:
        stored = _build_experiment_service().run_validation(
            experiment_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
    except (ExperimentApplicationError, ValueError) as error:
        _experiment_failure(error)
    payload = {"validation": stored.validation.model_dump(mode="json"), "replayed": stored.replayed}
    if json_output:
        typer.echo(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    else:
        console.print(
            f"{stored.validation.validation_id}: {stored.validation.release_decision.status}"
        )


@experiment_app.command("passport-preview")
def experiment_passport_preview(
    experiment_id: Annotated[str, typer.Argument(help="Experiment identity.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Render an unpersisted Passport preview, including blockers."""
    try:
        passport = _build_experiment_service().preview_passport(experiment_id)
    except (ExperimentApplicationError, ValueError) as error:
        _experiment_failure(error)
    payload = {"passport": passport.model_dump(mode="json"), "replayed": False}
    if json_output:
        typer.echo(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    else:
        console.print(f"{passport.passport_id}: {passport.release_decision.status}")


@experiment_app.command("passport-release")
def experiment_passport_release(
    experiment_id: Annotated[str, typer.Argument(help="Experiment identity.")],
    expected_version: Annotated[int, typer.Option("--expected-version")],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Release an immutable Passport when no blocking finding exists."""
    try:
        stored = _build_experiment_service().release_passport(
            experiment_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
    except (ExperimentApplicationError, ValueError) as error:
        _experiment_failure(error)
    payload = {"passport": stored.passport.model_dump(mode="json"), "replayed": stored.replayed}
    if json_output:
        typer.echo(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    else:
        console.print(stored.passport.passport_id)


@package_app.command("create")
def experiment_package_create(
    experiment_id: Annotated[str, typer.Argument(help="Experiment identity.")],
    passport_id: Annotated[str, typer.Option("--passport-id")],
    expected_version: Annotated[int, typer.Option("--expected-version")],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Create one manifest-verified immutable Experiment Package."""
    try:
        stored = _build_experiment_service().create_package(
            experiment_id,
            passport_id=passport_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
    except (ExperimentApplicationError, ValueError) as error:
        _experiment_failure(error)
    payload = {"package": stored.package.model_dump(mode="json"), "replayed": stored.replayed}
    if json_output:
        typer.echo(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    else:
        console.print(stored.package.package_id)


@package_app.command("download")
def experiment_package_download(
    package_id: Annotated[str, typer.Argument(help="Experiment Package identity.")],
    output: Annotated[Path, typer.Option("--output", help="New ZIP destination.")],
) -> None:
    """Download exact package bytes after checksum and internal verification."""
    if output.exists():
        _experiment_failure(FileExistsError(f"refusing to replace existing path: {output}"))
    try:
        archive_bytes = _build_experiment_service().download_package(package_id)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(archive_bytes)
    except (OSError, ExperimentApplicationError, ValueError) as error:
        _experiment_failure(error)
    console.print(output)


@package_app.command("verify")
def experiment_package_verify(
    package_file: Annotated[Path, typer.Argument(help="Downloaded Experiment Package ZIP.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Verify a downloaded Package independently of PostgreSQL and object storage."""
    try:
        verification = verify_experiment_package(package_file.read_bytes())
    except (OSError, ExperimentPackageVerificationError) as error:
        _experiment_failure(error)
    payload = verification.model_dump(mode="json")
    if json_output:
        typer.echo(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    else:
        console.print(f"[green]verified[/green] {verification.package_id}")


@source_app.command("intake")
def source_intake(
    source: Annotated[Path, typer.Argument(help="Opaque source file to retain.")],
    intake_id: Annotated[str, typer.Option("--intake-id", help="Stable request identity.")],
    media_type: Annotated[str, typer.Option("--media-type", help="Declared media type.")],
    data_origin: Annotated[
        str, typer.Option("--data-origin", help="Explicit observed or synthetic origin.")
    ],
    execution_mode: Annotated[
        str, typer.Option("--execution-mode", help="Explicit replay, simulation, or live mode.")
    ],
) -> None:
    """Retain exact source bytes without interpreting their contents."""
    command = IntakeSource(
        intake_id=intake_id,
        data=source.read_bytes(),
        filename=source.name,
        media_type=media_type,
        data_origin=cast(DataOrigin, data_origin),
        execution_mode=cast(ExecutionMode, execution_mode),
    )
    try:
        result = _build_source_service().intake(command)
    except SourceIntakeError as error:
        console.print(f"[red]{error.code}[/red]: {error}")
        raise typer.Exit(code=1) from error
    artifact = result.artifact
    console.print(artifact.source_artifact_id)
    console.print(
        f"{artifact.filename}  {artifact.byte_size} bytes  sha256:{artifact.sha256}  "
        f"{artifact.data_origin} + {artifact.execution_mode}"
    )
    if result.replayed:
        console.print("[dim]idempotent replay: existing source artifact returned[/dim]")


@source_app.command("verify")
def source_verify(source_artifact_id: str) -> None:
    """Read source bytes back and compare their size and SHA-256."""
    try:
        artifact = _build_source_service().verify(source_artifact_id)
    except SourceIntakeError as error:
        console.print(f"[red]{error.code}[/red]: {error}")
        raise typer.Exit(code=1) from error
    console.print(
        f"[green]verified[/green] {artifact.source_artifact_id}  "
        f"{artifact.byte_size} bytes  sha256:{artifact.sha256}"
    )


@app.command("fetch-her")
def fetch_her(
    *,
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


@evidence_app.command("verify")
@app.command("validate-artifacts")
def validate_artifacts(
    mode: Annotated[
        VerificationMode,
        typer.Option("--mode", help="Verification scope: bundle-only or full."),
    ] = VerificationMode.BUNDLE_ONLY,
    bundle: Annotated[
        Path | None,
        typer.Option("--bundle", help="One bundle to verify. Omit to verify every bundle found."),
    ] = None,
    bundle_root: Annotated[
        Path | None,
        typer.Option(
            "--bundle-root",
            help="Search one root instead of the committed artifacts and the local bundle root.",
        ),
    ] = None,
) -> None:
    """Recompute every checksum in an evidence bundle and report whether it still matches.

    Recomputed from the bytes on disk, never compared against a recorded size: a file edited in
    place keeps its size far more often than it keeps its hash.

    With no `--bundle` it verifies everything under the committed artifact root and the local
    bundle root, so the bare command is the gate `AI_CONTRACT.md` §10 names. Searching only
    `data/bundles` would let the gate pass on a clean checkout by finding nothing in a git-ignored
    directory, leaving the released evidence it exists to protect unchecked. Finding nothing at all
    exits non-zero: "nothing to check" and "everything checks out" are different answers.

    Registered under two names because the documents disagree: `AI_CONTRACT.md` §10 calls the gate
    `labbridge validate-artifacts`, while `docs/SPEC.md` §11.2 lists `labbridge evidence verify`.
    Both resolve here rather than one being silently preferred.
    """
    object_store = _build_object_store() if mode is VerificationMode.FULL else None
    if bundle is not None:
        _verify_one(bundle, mode, object_store)
        return

    roots = (bundle_root,) if bundle_root is not None else DEFAULT_VERIFICATION_ROOTS
    candidates = sorted(
        directory
        for root in roots
        for directory in root.glob("*")
        if (directory / "manifest.json").exists()
    )
    if not candidates:
        searched = ", ".join(str(root) for root in roots)
        console.print(f"[red]no_evidence_found[/red]: nothing to verify under {escape(searched)}")
        raise typer.Exit(code=1)
    for candidate in candidates:
        _verify_one(candidate, mode, object_store)
    console.print(f"[green]{len(candidates)} bundle(s) verified[/green]")


def _verify_one(bundle: Path, mode: VerificationMode, object_store: ObjectStore | None) -> None:
    try:
        raw_manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raw_manifest = None
    if isinstance(raw_manifest, dict) and "campaign_id" not in raw_manifest:
        _verify_flat_artifact(bundle)
        return
    try:
        result = verify_bundle(bundle, mode=mode, object_store=object_store)
    except BundleVerificationError as error:
        failure = error.to_dict()
        console.print(
            f"[red]{escape(str(failure['code']))}[/red]: {escape(str(failure['message']))}"
        )
        details = failure["details"]
        if isinstance(details, dict):
            for key in sorted(details):
                console.print(f"  {escape(str(key))}: {escape(str(details[key]))}")
        raise typer.Exit(code=1) from error

    manifest = result.manifest
    files = manifest["files"]
    assert isinstance(files, list)
    artifact_kind = str(manifest.get("artifact_kind", "campaign_bundle"))
    artifact_id = manifest.get("campaign_id", manifest.get("source_artifact_id", bundle.name))
    table = Table(title=f"{escape(artifact_kind)} {escape(str(artifact_id))}")
    table.add_column("file")
    table.add_column("bytes", justify="right")
    table.add_column("sha256")
    for entry in files:
        table.add_row(
            escape(str(entry["name"])),
            str(entry["byte_size"]),
            f"{escape(str(entry['sha256'][:16]))}...",
        )
    console.print(table)
    origin = manifest["data_origin"]
    colour = "yellow" if origin == "synthetic" else "green"
    console.print(
        f"data_origin: [{colour}]{escape(str(origin))}[/{colour}]  execution_mode: "
        f"{escape(str(manifest['execution_mode']))}"
    )
    console.print(f"mode: {result.mode.value}  status: {result.status.value}")
    console.print(
        f"bundle files verified: {result.bundle_files_verified}  "
        f"objects referenced: {result.objects_referenced}  "
        f"objects verified: {result.objects_verified}"
    )
    if result.limitations:
        console.print("limitations:")
        for limitation in result.limitations:
            console.print(f"  - {limitation}")
    elif result.status is VerificationStatus.COMPLETE:
        console.print("[green]bundle members and recorded object bytes match[/green]")


def _verify_flat_artifact(destination: Path) -> None:
    try:
        manifest = verify_manifest(destination)
    except ArtifactVerificationError as error:
        console.print(f"[red]artifact_verification_failed[/red]: {escape(str(error))}")
        raise typer.Exit(code=1) from error
    files = manifest["files"]
    assert isinstance(files, list)
    artifact_kind = str(manifest.get("artifact_kind", destination.name))
    table = Table(title=escape(artifact_kind))
    table.add_column("file")
    table.add_column("bytes", justify="right")
    table.add_column("sha256")
    for entry in files:
        assert isinstance(entry, dict)
        table.add_row(
            escape(str(entry["name"])),
            str(entry["byte_size"]),
            f"{escape(str(entry['sha256'][:16]))}...",
        )
    console.print(table)
    console.print("mode: closed-manifest  status: complete")


@demo_app.command("her")
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


@app.command("reconcile")
def reconcile_command() -> None:
    """Reclaim expired leases, close abandoned attempts, and classify stored objects.

    The same function a worker runs at startup (`labbridge.runtime.reconciliation.reconcile`), so an
    operator investigating a stuck queue and a worker recovering from a crash reach identical
    conclusions. There is deliberately no reconciliation daemon: one implementation, two entry
    points, nothing extra to supervise.

    Nothing here deletes bytes. An object that cannot be explained is quarantined and reported, and
    a released evidence object is never touched.
    """
    engine = create_engine(DatabaseSettings().dsn, future=True)
    store = _build_object_store()
    with engine.begin() as connection:
        report = reconcile(connection, store)

    table = Table("what", "count", title="reconciliation")
    table.add_row("leases reclaimed", str(len(report.reclaimed)))
    table.add_row("attempts closed", str(len(report.closed_attempts)))
    for classification, count in sorted(report.counts.items()):
        table.add_row(f"objects {classification}", str(count))
    if report.unreachable:
        table.add_row("objects unreachable", str(len(report.unreachable)))
    console.print(table)

    for reclaimed in report.reclaimed:
        console.print(
            f"  job {reclaimed.job_id} reclaimed from {reclaimed.previous_owner or 'nobody'}: "
            f"generation {reclaimed.fenced_generation} fenced out, now "
            f"{reclaimed.lease_generation}"
        )
    for entry in report.classified:
        if entry.classification != "accepted_evidence":
            console.print(f"  [yellow]{entry.classification}[/yellow] {entry.object_uri}")
            console.print(f"    {entry.reason}")
    if report.unreachable:
        # Not a verdict: a classification reached while storage was down would record an outage as
        # a fact about the bytes.
        console.print(
            f"[yellow]{len(report.unreachable)} object(s) could not be read and were left "
            "unclassified[/yellow]"
        )


if __name__ == "__main__":
    app()

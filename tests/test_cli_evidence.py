from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from labbridge import cli
from labbridge.evidence.bundle import (
    BundleErrorCode,
    BundleFailure,
    BundleVerificationError,
)

runner = CliRunner()


def _write_version_1_bundle(destination: Path) -> None:
    destination.mkdir()
    payload = b"[]"
    (destination / "observations.json").write_bytes(payload)
    files = [
        {
            "name": "observations.json",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "byte_size": len(payload),
        }
    ]
    canonical_files = json.dumps(
        files, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    manifest = {
        "schema_version": "1",
        "campaign_id": "campaign-1",
        "data_origin": "synthetic",
        "execution_mode": "replay",
        "files": files,
        "files_digest": hashlib.sha256(canonical_files).hexdigest(),
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2), encoding="utf-8"
    )


@pytest.mark.parametrize(
    "command",
    [
        ["validate-artifacts"],
        ["evidence", "verify"],
    ],
)
def test_both_verification_aliases_render_partial_bundle_only_results(
    command: list[str], tmp_path: Path
) -> None:
    bundle = tmp_path / "bundle"
    _write_version_1_bundle(bundle)

    result = runner.invoke(
        cli.app,
        [*command, "--bundle", str(bundle), "--mode", "bundle-only"],
    )

    assert result.exit_code == 0, result.output
    assert "mode: bundle-only" in result.output
    assert "status: partial" in result.output
    assert "limitations:" in result.output
    assert "Object-store existence" in result.output


def test_cli_renders_the_stable_structured_failure_code(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    _write_version_1_bundle(bundle)

    result = runner.invoke(
        cli.app,
        [
            "validate-artifacts",
            "--bundle",
            str(bundle),
            "--mode",
            "full",
        ],
    )

    assert result.exit_code == 1
    assert "full_verification_requires_manifest_v2" in result.output
    assert "Traceback" not in result.output


def test_bare_validate_artifacts_defaults_to_bundle_only_and_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    root = Path("data/bundles")
    root.mkdir(parents=True)
    _write_version_1_bundle(root / "bundle")

    result = runner.invoke(cli.app, ["validate-artifacts"])

    assert result.exit_code == 0, result.output
    assert "mode: bundle-only" in result.output
    assert "status: partial" in result.output


def test_structured_failure_details_escape_rich_markup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    def fail_verification(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise BundleVerificationError(
            BundleFailure(
                code=BundleErrorCode.OBJECT_MISSING,
                message="recorded object [missing]",
                details={"key": "observations/[bold]one[/bold].bin"},
            )
        )

    monkeypatch.setattr(cli, "verify_bundle", fail_verification)

    result = runner.invoke(cli.app, ["validate-artifacts", "--bundle", str(bundle)])

    assert result.exit_code == 1
    assert "[missing]" in result.output
    assert "observations/[bold]one[/bold].bin" in result.output

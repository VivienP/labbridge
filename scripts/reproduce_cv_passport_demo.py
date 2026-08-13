"""Assemble and verify the inspectable Phase 3.5 demonstration artifact."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "src"))
sys.path.insert(0, str(REPOSITORY))

from labbridge.evidence.experiment_package import verify_experiment_package  # noqa: E402
from labbridge.evidence.manifest import build_manifest, verify_manifest  # noqa: E402
from scripts.check_frontend_build import check_frontend  # noqa: E402

FRONTEND = REPOSITORY / "frontend"
FIXTURE = FRONTEND / "public" / "demo-fixtures" / "synthetic-cv-passport-demo.csv"
EXACT_COMMAND = "docker compose --profile demo up -d --build --wait"
LIMITATIONS = """# Limitations

This artifact covers one local, single-user, synthetic + replay CV Passport workflow. The CSV,
plot, Passport, and Package are demonstration evidence, not measured electrochemistry.

The operator-supplied `RHE` value is retained as a `user_supplied` declaration. LabBridge does not
infer it from the CSV, validate it as physically correct, or convert the plotted potential to that
reference scale.

Capability status is `implemented`, not `demonstrated`. A recorded human electrochemistry domain
review must decide whether the missing reference scale is a blocker or warning and approve
consistent API, UI, Passport, Package, and artifact semantics. A separate unfamiliar-viewer
acceptance run must
record both 60-90 second completion and comprehension of the raw-to-Package chain plus completeness,
integrity, scientific validity, and reproducibility. Neither human evidence is present here.

The artifact does not establish scientific validity, data quality, experimental reproducibility,
journal readiness, production readiness, authentication, tenancy, collaboration, instrument
connectivity, or mobile support.
"""


def _npm_command() -> str:
    return "npm.cmd" if os.name == "nt" else "npm"


def _require_empty(output: Path) -> None:
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"refusing to replace non-empty artifact directory: {output}")
    output.mkdir(parents=True, exist_ok=True)


def _single(paths: list[Path], description: str) -> Path:
    if len(paths) != 1:
        raise FileNotFoundError(f"expected one {description}, found {len(paths)}")
    return paths[0]


def _run_cli_verifier(package: Path) -> dict[str, object]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(REPOSITORY / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "labbridge.cli",
            "package",
            "verify",
            str(package),
            "--json",
        ],
        cwd=REPOSITORY,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def assemble_artifact(results: Path, output: Path) -> dict[str, object]:
    _require_empty(output)
    evidence = results / "evidence"
    source_package = evidence / "synthetic-experiment-package.zip"
    source_screenshot = evidence / "final-package.png"
    trace = _single(list(results.glob("**/trace.zip")), "Playwright trace")
    for required in (source_package, source_screenshot, FIXTURE):
        if not required.is_file():
            raise FileNotFoundError(required)

    package = output / "experiment-package.zip"
    shutil.copyfile(source_package, package)
    shutil.copyfile(source_screenshot, output / "final-package.png")
    shutil.copyfile(trace, output / "browser-trace.zip")
    shutil.copyfile(FIXTURE, output / FIXTURE.name)
    (output / "EXACT_COMMAND.txt").write_text(EXACT_COMMAND + "\n", encoding="utf-8", newline="\n")
    (output / "LIMITATIONS.md").write_text(LIMITATIONS, encoding="utf-8", newline="\n")

    verification = _run_cli_verifier(package)
    direct = verify_experiment_package(package.read_bytes()).model_dump(mode="json")
    if verification != direct:
        raise ValueError("CLI and direct Package verification results differ")
    (output / "cli-verification.json").write_text(
        json.dumps(verification, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    check_frontend(FRONTEND, output / "built-assets.json")

    with zipfile.ZipFile(package) as archive:
        lineage = json.loads(archive.read("lineage.json"))
        passport = json.loads(archive.read("passport/passport.json"))
        package_manifest = json.loads(archive.read("manifest.json"))
        retained_fixture = archive.read(f"source/{FIXTURE.name}")
    if retained_fixture != FIXTURE.read_bytes():
        raise ValueError("Package source bytes differ from the dedicated synthetic fixture")
    if (
        package_manifest["data_origin"] != "synthetic"
        or package_manifest["execution_mode"] != "replay"
    ):
        raise ValueError("Package is not the synthetic + replay demonstration release")
    if not any(
        assertion.get("field_name") == "reference_scale"
        and assertion.get("origin") == "user_supplied"
        and assertion.get("value", {}).get("value") == "RHE"
        for assertion in passport["assertions"]
    ):
        raise ValueError("Passport lacks the retained user-supplied RHE declaration")

    manifest = build_manifest(
        output,
        metadata={
            "schema_version": "1",
            "artifact_kind": "single_user_cv_passport_demo",
            "capability_status": "implemented",
            "data_origin": "synthetic",
            "execution_mode": "replay",
            "description": (
                "Browser-driven synthetic CV chain of custody with a CLI-verified Package."
            ),
            "source_artifact_id": lineage["source_artifact_id"],
            "observation_id": lineage["observation_id"],
            "passport_id": verification["passport_id"],
            "package_id": verification["package_id"],
            "producing_versions": {
                "labbridge": "0.1.0",
                "frontend": "0.1.0",
                "openapi_contract": "1",
                "experiment_package": "1",
            },
            "outstanding_acceptance": [
                "human_domain_review_of_reference_scale_severity",
                "unfamiliar_viewer_60_to_90_second_comprehension_run",
            ],
        },
    )
    verify_manifest(output)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, default=FRONTEND / "test-results")
    parser.add_argument("--output", type=Path, default=REPOSITORY / "artifacts/cv-passport-demo")
    parser.add_argument("--skip-browser", action="store_true")
    arguments = parser.parse_args()
    if not arguments.skip_browser:
        subprocess.run([_npm_command(), "run", "e2e"], cwd=FRONTEND, check=True)
    manifest = assemble_artifact(arguments.results, arguments.output)
    print(f"verified {manifest['artifact_kind']} {manifest['package_id']} at {arguments.output}")


if __name__ == "__main__":
    main()

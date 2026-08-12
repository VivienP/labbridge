"""Reproduce the minimum source-capture evidence through the application service."""

from __future__ import annotations

import argparse
from pathlib import Path

from labbridge import __version__
from labbridge.application.source_intake import IntakeSource
from labbridge.evidence.manifest import verify_manifest
from labbridge.evidence.source_capture import build_source_capture
from labbridge.infrastructure.source_wiring import build_source_service

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPOSITORY_ROOT / "fixtures/source/synthetic-replay-cv-opaque.csv"


def reproduce(output: Path) -> dict[str, object]:
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"refusing to replace non-empty artifact directory: {output}")
    service = build_source_service()
    data = FIXTURE.read_bytes()
    result = service.intake(
        IntakeSource(
            intake_id="phase1-synthetic-replay-source-capture-v1",
            data=data,
            filename=FIXTURE.name,
            media_type="text/csv",
            data_origin="synthetic",
            execution_mode="replay",
        )
    )
    retrieved = service.retrieve(result.artifact.source_artifact_id)
    build_source_capture(retrieved, output, producing_version=__version__)
    return verify_manifest(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("build/source-capture"))
    args = parser.parse_args()
    manifest = reproduce(args.output)
    print(f"verified {manifest['artifact_kind']} {manifest['source_artifact_id']} at {args.output}")


if __name__ == "__main__":
    main()

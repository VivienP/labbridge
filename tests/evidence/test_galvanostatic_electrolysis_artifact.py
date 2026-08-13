from __future__ import annotations

import json
from pathlib import Path

from labbridge.evidence.experiment_package import verify_experiment_package
from labbridge.evidence.galvanostatic_electrolysis import (
    reproduce_galvanostatic_electrolysis_artifact,
)
from labbridge.evidence.manifest import verify_manifest

ROOT = Path(__file__).resolve().parents[2]
COMMITTED = ROOT / "artifacts/galvanostatic-electrolysis"
SOURCE = ROOT / "fixtures/source/synthetic-galvanostatic-electrolysis.csv"


def test_electrolysis_artifact_separates_electrical_and_chemical_scope(
    tmp_path: Path,
) -> None:
    manifest = reproduce_galvanostatic_electrolysis_artifact(tmp_path, producing_version="0.1.0")

    assert verify_manifest(tmp_path) == manifest
    assert (tmp_path / SOURCE.name).read_bytes() == SOURCE.read_bytes()
    observation = json.loads((tmp_path / "normalised-observation.json").read_text(encoding="utf-8"))
    verification = json.loads((tmp_path / "verification.json").read_text(encoding="utf-8"))
    report = (tmp_path / "passport.html").read_text(encoding="utf-8")
    package_verification = verify_experiment_package(
        (tmp_path / "experiment-package.zip").read_bytes()
    )

    assert manifest["artifact_kind"] == "galvanostatic_electrolysis_package"
    assert manifest["capability_status"] == "implemented"
    assert {series["role"] for series in observation["series"]} == {
        "time",
        "current",
        "potential",
    }
    assert verification["electrical_series_complete"] is True
    assert verification["chemical_analysis"] == "unavailable"
    assert "Chemical/product quantification: unavailable" in report
    assert package_verification.verified is True
    assert package_verification.lineage_closed is True


def test_electrolysis_artifact_rebuild_is_byte_identical(
    tmp_path: Path,
) -> None:
    committed_manifest = verify_manifest(COMMITTED)
    reproduced_manifest = reproduce_galvanostatic_electrolysis_artifact(
        tmp_path, producing_version="0.1.0"
    )

    assert reproduced_manifest == committed_manifest
    assert {item.name: item.read_bytes() for item in tmp_path.iterdir()} == {
        item.name: item.read_bytes() for item in COMMITTED.iterdir()
    }

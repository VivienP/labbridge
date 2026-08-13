from __future__ import annotations

import json
from pathlib import Path

import pytest

from labbridge.evidence.echemdb_cv_artifact import reproduce_echemdb_cv_exchange_artifact
from labbridge.evidence.manifest import ArtifactVerificationError, verify_manifest

ROOT = Path(__file__).resolve().parents[2]
COMMITTED = ROOT / "artifacts/echemdb-cv-exchange"


def test_phase_6_artifact_is_validated_and_closes_every_export_trace(tmp_path: Path) -> None:
    manifest = reproduce_echemdb_cv_exchange_artifact(tmp_path, producing_version="0.1.0")

    assert verify_manifest(tmp_path) == manifest
    validation = json.loads((tmp_path / "validation.json").read_text(encoding="utf-8"))
    mapping = json.loads((tmp_path / "mapping.json").read_text(encoding="utf-8"))
    provenance = json.loads((tmp_path / "labbridge-provenance.json").read_text(encoding="utf-8"))
    experiment = json.loads((tmp_path / "experiment.json").read_text(encoding="utf-8"))
    observation = json.loads((tmp_path / "normalised-observation.json").read_text(encoding="utf-8"))

    assert manifest["artifact_kind"] == "echemdb_cv_exchange"
    assert manifest["capability_status"] == "implemented"
    assert validation["valid"] is True
    assert validation["echemdb_schema_valid"] is True
    assert validation["data_package_profile_valid"] is True
    assert validation["frictionless_valid"] is True
    assert validation["versions"]["echemdb_metadata_schema"] == "0.8.3"
    assert validation["versions"]["frictionless"] == "5.19.0"
    assert mapping["untraced_exported_paths"] == []
    assert mapping["mapping_collisions"] == []
    assert any(entry["status"] == "lossy" for entry in mapping["entries"])
    assert any(entry["status"] == "omitted" for entry in mapping["entries"])
    assertion_ids = {item["assertion_id"] for item in experiment["assertions"]}
    series_ids = {item["series_id"] for item in observation["series"]}
    assert all(
        trace["source_id"] in assertion_ids | series_ids | {observation["observation_id"]}
        for trace in provenance["traces"]
    )


def test_phase_6_artifact_manifest_detects_package_tampering(tmp_path: Path) -> None:
    reproduce_echemdb_cv_exchange_artifact(tmp_path, producing_version="0.1.0")
    (tmp_path / "cv.csv").write_bytes((tmp_path / "cv.csv").read_bytes() + b"\n")

    with pytest.raises(ArtifactVerificationError) as raised:
        verify_manifest(tmp_path)

    assert any("cv.csv: sha256" in problem for problem in raised.value.problems)


def test_phase_6_artifact_rebuild_is_byte_identical_to_candidate(tmp_path: Path) -> None:
    committed_manifest = verify_manifest(COMMITTED)
    reproduced_manifest = reproduce_echemdb_cv_exchange_artifact(
        tmp_path, producing_version="0.1.0"
    )

    assert reproduced_manifest == committed_manifest
    assert {item.name: item.read_bytes() for item in tmp_path.iterdir()} == {
        item.name: item.read_bytes() for item in COMMITTED.iterdir()
    }

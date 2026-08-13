from __future__ import annotations

import json
from pathlib import Path

from labbridge.evidence.experiment_package import verify_experiment_package
from labbridge.evidence.gamry_dta_cv import reproduce_gamry_dta_cv_artifact
from labbridge.evidence.manifest import verify_manifest

ROOT = Path(__file__).resolve().parents[2]
COMMITTED = ROOT / "artifacts/gamry-dta-cv"
SOURCE = ROOT / "fixtures/source/synthetic-gamry-cv.dta"
EXPECTED_HEADER_LINE = 7


def test_phase_4_artifact_preserves_parser_locations_and_verifies_package(
    tmp_path: Path,
) -> None:
    manifest = reproduce_gamry_dta_cv_artifact(tmp_path, producing_version="0.1.0")

    assert verify_manifest(tmp_path) == manifest
    assert (tmp_path / SOURCE.name).read_bytes() == SOURCE.read_bytes()
    parser_record = json.loads((tmp_path / "parser-record.json").read_text(encoding="utf-8"))
    observation = json.loads((tmp_path / "normalised-observation.json").read_text(encoding="utf-8"))
    passport = json.loads((tmp_path / "passport.json").read_text(encoding="utf-8"))
    package_verification = verify_experiment_package(
        (tmp_path / "experiment-package.zip").read_bytes()
    )

    assert manifest["artifact_kind"] == "gamry_dta_cv_ingestion"
    assert manifest["capability_status"] == "implemented"
    assert parser_record["status"] == "accepted"
    assert parser_record["parser_record_id"] == observation["parser_record_id"]
    assert {field["source_column"] for field in parser_record["fields"]} == {
        "T",
        "Vf",
        "Im",
        "Cycle",
    }
    assert all(field["header_line"] == EXPECTED_HEADER_LINE for field in parser_record["fields"])
    assert any(
        parser_record["parser_record_id"] in assertion["evidence_ids"]
        for assertion in passport["assertions"]
    )
    assert package_verification.verified is True
    assert package_verification.lineage_closed is True


def test_phase_4_artifact_rebuild_is_byte_identical_to_the_committed_candidate(
    tmp_path: Path,
) -> None:
    committed_manifest = verify_manifest(COMMITTED)
    reproduced_manifest = reproduce_gamry_dta_cv_artifact(tmp_path, producing_version="0.1.0")

    assert reproduced_manifest == committed_manifest
    assert {item.name: item.read_bytes() for item in tmp_path.iterdir()} == {
        item.name: item.read_bytes() for item in COMMITTED.iterdir()
    }

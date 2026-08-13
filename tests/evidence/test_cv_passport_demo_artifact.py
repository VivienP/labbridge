import json
from pathlib import Path

import pytest

from labbridge.evidence.experiment_package import verify_experiment_package
from labbridge.evidence.manifest import verify_manifest

ROOT = Path(__file__).parents[2]
ARTIFACT = ROOT / "artifacts" / "cv-passport-demo"
REQUIRED = {
    "synthetic-cv-passport-demo.csv",
    "browser-trace.zip",
    "final-package.png",
    "built-assets.json",
    "experiment-package.zip",
    "cli-verification.json",
    "EXACT_COMMAND.txt",
    "LIMITATIONS.md",
}


@pytest.mark.skipif(not ARTIFACT.exists(), reason="candidate demo artifact not reproduced yet")
def test_candidate_artifact_is_closed_and_explicitly_implemented() -> None:
    manifest = verify_manifest(ARTIFACT)
    names = {str(item["name"]) for item in manifest["files"]}

    assert names >= REQUIRED
    assert manifest["capability_status"] == "implemented"
    assert manifest["data_origin"] == "synthetic"
    assert manifest["execution_mode"] == "replay"
    assert manifest["outstanding_acceptance"] == [
        "human_domain_review_of_reference_scale_severity",
        "unfamiliar_viewer_60_to_90_second_comprehension_run",
    ]


@pytest.mark.skipif(not ARTIFACT.exists(), reason="candidate demo artifact not reproduced yet")
def test_candidate_package_matches_recorded_cli_verification() -> None:
    package = verify_experiment_package((ARTIFACT / "experiment-package.zip").read_bytes())
    recorded = json.loads((ARTIFACT / "cli-verification.json").read_text(encoding="utf-8"))

    assert recorded == package.model_dump(mode="json")
    assert package.data_origin == "synthetic"
    assert package.execution_mode == "replay"


@pytest.mark.skipif(not ARTIFACT.exists(), reason="candidate demo artifact not reproduced yet")
def test_candidate_limitations_state_domain_and_operator_declaration_boundaries() -> None:
    limitations = " ".join((ARTIFACT / "LIMITATIONS.md").read_text(encoding="utf-8").split())

    assert "human electrochemistry domain review" in limitations
    assert "blocker or warning" in limitations
    assert "does not infer it from the CSV" in limitations
    assert "validate it as physically correct" in limitations
    assert "60-90 second" in limitations


@pytest.mark.skipif(not ARTIFACT.exists(), reason="candidate demo artifact not reproduced yet")
def test_candidate_text_members_are_platform_independent_lf() -> None:
    for name in (
        "built-assets.json",
        "cli-verification.json",
        "EXACT_COMMAND.txt",
        "LIMITATIONS.md",
        "manifest.json",
    ):
        assert b"\r\n" not in (ARTIFACT / name).read_bytes(), name

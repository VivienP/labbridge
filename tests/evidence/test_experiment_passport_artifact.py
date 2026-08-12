from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from runpy import run_path
from typing import cast

from labbridge.evidence.experiment_package import verify_experiment_package
from labbridge.evidence.manifest import verify_manifest

ROOT = Path(__file__).resolve().parents[2]
COMMITTED = ROOT / "artifacts/experiment-passport"
reproduce = cast(
    Callable[[Path], dict[str, object]],
    run_path(str(ROOT / "scripts/reproduce_experiment_passport.py"))["reproduce"],
)
INITIAL_VERSION = 1
SUPERSEDING_VERSION = 2


def test_committed_phase3_artifact_verifies_and_reproduces(tmp_path: Path) -> None:
    committed_manifest = verify_manifest(COMMITTED)
    reproduced_manifest = reproduce(tmp_path / "experiment-passport")

    assert reproduced_manifest == committed_manifest
    for name in ("initial-package.zip", "superseding-package.zip"):
        verification = verify_experiment_package((COMMITTED / name).read_bytes())
        assert verification.verified is True
        assert verification.lineage_closed is True


def test_demonstration_proves_append_only_user_supplement() -> None:
    proof = json.loads((COMMITTED / "assertion-immutability.json").read_text(encoding="utf-8"))
    initial = json.loads((COMMITTED / "initial-passport.json").read_text(encoding="utf-8"))
    superseding = json.loads((COMMITTED / "superseding-passport.json").read_text(encoding="utf-8"))

    assert proof["source_assertion_unchanged"] is True
    assert proof["initial_package_unchanged_after_supplement"] is True
    assert proof["source_assertion_before"] == proof["source_assertion_after"]
    assert initial["experiment_version"] == INITIAL_VERSION
    assert superseding["experiment_version"] == SUPERSEDING_VERSION
    assert any(item["origin"] == "source_file" for item in superseding["assertions"])
    assert any(item["origin"] == "user_supplied" for item in superseding["assertions"])


def test_cli_verification_output_matches_released_packages() -> None:
    output = json.loads((COMMITTED / "cli-verification.json").read_text(encoding="utf-8"))

    assert output["initial"]["verified"] is True
    assert output["superseding"]["verified"] is True
    assert output["initial"]["experiment_version"] == INITIAL_VERSION
    assert output["superseding"]["experiment_version"] == SUPERSEDING_VERSION

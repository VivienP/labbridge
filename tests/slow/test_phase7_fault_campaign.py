from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.integration]

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_COUNT = 6


def test_fault_campaign_crosses_every_process_boundary_and_restores(tmp_path: Path) -> None:
    identity = uuid.uuid4().hex[:10]
    database = f"labbridge_fault_test_{identity}"
    bucket = f"labbridge-fault-test-{identity}"
    output = tmp_path / "fault-campaign"
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "reproduce_campaign_reliability.py"),
            "--campaigns",
            str(SCENARIO_COUNT),
            "--master-seed",
            "20260813",
            "--database-name",
            database,
            "--bucket",
            bucket,
            "--output",
            str(output),
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    summary = json.loads((output / "release" / "summary.json").read_text(encoding="utf-8"))
    backup = json.loads((output / "release" / "backup-restore.json").read_text(encoding="utf-8"))
    migration = json.loads(
        (output / "release" / "migration-checks.json").read_text(encoding="utf-8")
    )
    assert summary["acceptance_met"] is True
    assert summary["campaigns_executed"] == SCENARIO_COUNT
    assert len(summary["fault_point_counts"]) == SCENARIO_COUNT
    assert backup["passed"] is True
    assert backup["campaigns_replayed_equal"] == SCENARIO_COUNT
    assert backup["restored_packages_fully_verified"] == SCENARIO_COUNT
    assert migration["passed"] is True
    assert migration["campaign_rows_before"] == migration["campaign_rows_after"] == 1

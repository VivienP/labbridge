from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from labbridge.evidence.manifest import (
    ArtifactVerificationError,
    build_manifest,
    verify_manifest,
)
from labbridge.reliability.fault_campaign import (
    FAULT_POINTS,
    FaultCampaignResult,
    plan_fault_points,
    summarize_results,
)
from labbridge.reliability.release import (
    verify_fault_campaign_release,
    write_fault_campaign_release,
)
from labbridge.reliability.runner import CampaignExecution

TWO_CAMPAIGNS = 2


def test_seeded_fault_plan_is_reproducible_balanced_and_complete() -> None:
    first = plan_fault_points(campaigns=100, master_seed=20260813)
    second = plan_fault_points(campaigns=100, master_seed=20260813)

    assert first == second
    assert first != plan_fault_points(campaigns=100, master_seed=20260814)
    assert set(first) == set(FAULT_POINTS)
    assert max(Counter(first).values()) - min(Counter(first).values()) <= 1


def test_summary_is_derived_from_raw_rows_and_keeps_unfavourable_results() -> None:
    rows = [
        _row(ordinal=1),
        _row(
            ordinal=2,
            observations_accepted=2,
            accepted_outcomes=1,
            budget_committed=Decimal("3"),
            hard_budget=Decimal("2"),
            replay_equal=False,
            package_verified=False,
            corrupted_receipts=1,
            failure_codes=("corrupted",),
        ),
    ]

    summary = summarize_results(rows)

    assert summary.campaigns_executed == TWO_CAMPAIGNS
    assert summary.lost_accepted_observations == 0
    assert summary.unintended_duplicate_acceptances == 1
    assert summary.hard_budget_overspends == 1
    assert summary.projection_mismatches == 1
    assert summary.failed_package_verifications == 1
    assert summary.corrupted_receipts_retained == 1
    assert summary.acceptance_met is False
    payload = json.loads(summary.model_dump_json())
    assert payload["failure_code_counts"] == {"corrupted": 1, "lease_lost": 1}


def test_invalid_measurement_rows_fail_closed() -> None:
    with pytest.raises(ValueError, match="objects_verified"):
        _row(ordinal=1, objects_referenced=1, objects_verified=2)


def test_release_is_closed_and_tamper_evident(tmp_path: Path) -> None:
    package = tmp_path / "package.zip"
    package.write_bytes(b"package")
    execution = CampaignExecution(result=_row(ordinal=1), package_path=package, audit={"ok": True})
    destination = tmp_path / "release"

    manifest = write_fault_campaign_release(
        destination,
        [execution],
        environment={
            "python": "3.12",
            "secrets_recorded": False,
            "data_origin": "synthetic",
            "execution_mode": "replay",
        },
        backup_restore={"passed": True},
        migration_checks={"passed": True},
        reproduce_command="python scripts/reproduce_campaign_reliability.py --campaigns 100",
        generated_at=datetime(2026, 8, 13, 12, 0, tzinfo=UTC),
    )

    assert manifest["capability_status"] == "demonstrated"
    assert verify_fault_campaign_release(destination)["campaigns_executed"] == 1
    assert verify_manifest(destination)["artifact_kind"] == "phase7_fault_campaign"
    summary = json.loads((destination / "fault-campaign-report.json").read_text())
    assert summary["summary"]["campaigns_executed"] == 1
    assert summary["data_classification"] == "synthetic + replay"
    assert (
        b"exactly-once execution claim" in (destination / "fault-campaign-report.html").read_bytes()
    )

    (destination / "raw-results.csv").write_bytes(b"changed")
    with pytest.raises(ArtifactVerificationError, match="byte size"):
        verify_fault_campaign_release(destination)


def test_release_rejects_origin_mode_disagreement_and_verifier_rechecks_it(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package.zip"
    package.write_bytes(b"package")
    execution = CampaignExecution(result=_row(ordinal=1), package_path=package, audit={"ok": True})
    destination = tmp_path / "release"
    with pytest.raises(ValueError, match="environment manifest origin/mode"):
        write_fault_campaign_release(
            destination,
            [execution],
            environment={"data_origin": "observed", "execution_mode": "live"},
            backup_restore={"passed": True},
            migration_checks={"passed": True},
            reproduce_command="python reproduce.py",
        )

    manifest = write_fault_campaign_release(
        destination,
        [execution],
        environment={"data_origin": "synthetic", "execution_mode": "replay"},
        backup_restore={"passed": True},
        migration_checks={"passed": True},
        reproduce_command="python reproduce.py",
    )
    raw_path = destination / "raw-results.csv"
    raw_path.write_text(
        raw_path.read_text().replace("synthetic,replay", "observed,live"), encoding="utf-8"
    )
    (destination / "manifest.json").unlink()
    metadata = {
        key: value for key, value in manifest.items() if key not in {"files", "files_digest"}
    }
    build_manifest(destination, metadata=metadata)

    with pytest.raises(ValueError, match="raw result origin/mode"):
        verify_fault_campaign_release(destination)


def _row(ordinal: int, **overrides: object) -> FaultCampaignResult:
    values: dict[str, object] = {
        "run_id": "fault-campaign-20260813",
        "ordinal": ordinal,
        "seed": 20260813 + ordinal,
        "campaign_id": f"00000000-0000-0000-0000-{ordinal:012d}",
        "fault_point": FAULT_POINTS[(ordinal - 1) % len(FAULT_POINTS)],
        "data_origin": "synthetic",
        "execution_mode": "replay",
        "process_pid": 1000 + ordinal,
        "process_exit_code": 1,
        "process_started_at": datetime(2026, 8, 13, 12, 0, tzinfo=UTC),
        "checkpoint_reached_at": datetime(2026, 8, 13, 12, 0, 1, tzinfo=UTC),
        "process_killed_at": datetime(2026, 8, 13, 12, 0, 2, tzinfo=UTC),
        "restart_completed_at": datetime(2026, 8, 13, 12, 0, 3, tzinfo=UTC),
        "attempts_created": 2,
        "lease_recoveries": 1,
        "observations_staged": 1,
        "observations_received": 0,
        "observations_accepted": 1,
        "corrupted_receipts": 0,
        "accepted_outcomes": 1,
        "duplicate_suppressions": 0,
        "hard_budget": Decimal("2"),
        "budget_committed": Decimal("2"),
        "budget_reserved": Decimal("2"),
        "budget_consumed": Decimal("2"),
        "budget_released": Decimal("0"),
        "replay_equal": True,
        "package_verified": True,
        "objects_referenced": 1,
        "objects_verified": 1,
        "package_id": f"experiment-package:{ordinal:032x}",
        "package_sha256": f"{ordinal:064x}",
        "failure_codes": ("lease_lost",),
        "exclusions": (),
    }
    values.update(overrides)
    return FaultCampaignResult.model_validate(values)

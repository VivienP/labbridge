"""Typed rows and derived findings for the Phase 7 process-boundary campaign."""

from __future__ import annotations

import random
import uuid
from collections import Counter
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

FaultPoint = Literal[
    "after_lease_acquisition",
    "after_adapter_return_before_upload",
    "during_object_upload",
    "after_upload_before_outcome_transaction",
    "after_commit_before_acknowledgement",
    "during_evidence_export",
]

FAULT_POINTS: tuple[FaultPoint, ...] = (
    "after_lease_acquisition",
    "after_adapter_return_before_upload",
    "during_object_upload",
    "after_upload_before_outcome_transaction",
    "after_commit_before_acknowledgement",
    "during_evidence_export",
)


class FaultCampaignResult(BaseModel):
    """One inspectable measurement row, never a target or inferred success."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1)
    ordinal: int = Field(ge=1)
    seed: int
    campaign_id: uuid.UUID
    fault_point: FaultPoint
    data_origin: Literal["observed", "synthetic"]
    execution_mode: Literal["replay", "simulation", "live"]
    process_pid: int = Field(gt=0)
    process_exit_code: int
    process_started_at: datetime
    checkpoint_reached_at: datetime
    process_killed_at: datetime
    restart_completed_at: datetime
    attempts_created: int = Field(ge=0)
    lease_recoveries: int = Field(ge=0)
    observations_staged: int = Field(ge=0)
    observations_received: int = Field(ge=0)
    observations_accepted: int = Field(ge=0)
    corrupted_receipts: int = Field(ge=0)
    accepted_outcomes: int = Field(ge=0)
    duplicate_suppressions: int = Field(ge=0)
    hard_budget: Decimal = Field(gt=0)
    budget_committed: Decimal = Field(ge=0)
    budget_reserved: Decimal = Field(ge=0)
    budget_consumed: Decimal = Field(ge=0)
    budget_released: Decimal = Field(ge=0)
    replay_equal: bool
    package_verified: bool
    objects_referenced: int = Field(ge=0)
    objects_verified: int = Field(ge=0)
    package_id: str = Field(min_length=1)
    package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    failure_codes: tuple[str, ...]
    exclusions: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_measurement(self) -> FaultCampaignResult:
        for value in (
            self.process_started_at,
            self.checkpoint_reached_at,
            self.process_killed_at,
            self.restart_completed_at,
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("fault-campaign timestamps must be timezone-aware")
        if not (
            self.process_started_at
            <= self.checkpoint_reached_at
            <= self.process_killed_at
            <= self.restart_completed_at
        ):
            raise ValueError("fault-campaign timestamps are not monotonic")
        if self.objects_verified > self.objects_referenced:
            raise ValueError("objects_verified cannot exceed objects_referenced")
        return self


class FaultCampaignSummary(BaseModel):
    """Aggregate findings computed only from retained raw rows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    campaigns_executed: int = Field(ge=0)
    fault_point_counts: dict[str, int]
    lost_accepted_observations: int = Field(ge=0)
    unintended_duplicate_acceptances: int = Field(ge=0)
    hard_budget_overspends: int = Field(ge=0)
    projection_mismatches: int = Field(ge=0)
    failed_package_verifications: int = Field(ge=0)
    corrupted_receipts_retained: int = Field(ge=0)
    duplicate_suppressions: int = Field(ge=0)
    failure_code_counts: dict[str, int]
    acceptance_met: bool


def plan_fault_points(*, campaigns: int, master_seed: int) -> tuple[FaultPoint, ...]:
    """Return balanced coverage in a reproducibly shuffled order."""
    if campaigns < len(FAULT_POINTS):
        raise ValueError(f"at least {len(FAULT_POINTS)} campaigns are required for full coverage")
    plan = [FAULT_POINTS[index % len(FAULT_POINTS)] for index in range(campaigns)]
    random.Random(master_seed).shuffle(plan)
    return tuple(plan)


def summarize_results(rows: list[FaultCampaignResult]) -> FaultCampaignSummary:
    """Calculate PO-10 and Phase 7 counters without discarding adverse rows."""
    lost = sum(max(row.accepted_outcomes - row.observations_accepted, 0) for row in rows)
    duplicates = sum(max(row.observations_accepted - row.accepted_outcomes, 0) for row in rows)
    overspends = sum(row.budget_committed > row.hard_budget for row in rows)
    mismatches = sum(not row.replay_equal for row in rows)
    verification_failures = sum(not row.package_verified for row in rows)
    failure_counts = Counter(code for row in rows for code in row.failure_codes)
    acceptance_met = not any((lost, duplicates, overspends, mismatches, verification_failures))
    return FaultCampaignSummary(
        campaigns_executed=len(rows),
        fault_point_counts=dict(sorted(Counter(row.fault_point for row in rows).items())),
        lost_accepted_observations=lost,
        unintended_duplicate_acceptances=duplicates,
        hard_budget_overspends=overspends,
        projection_mismatches=mismatches,
        failed_package_verifications=verification_failures,
        corrupted_receipts_retained=sum(row.corrupted_receipts for row in rows),
        duplicate_suppressions=sum(row.duplicate_suppressions for row in rows),
        failure_code_counts=dict(sorted(failure_counts.items())),
        acceptance_met=acceptance_met,
    )


__all__ = [
    "FAULT_POINTS",
    "FaultCampaignResult",
    "FaultCampaignSummary",
    "FaultPoint",
    "plan_fault_points",
    "summarize_results",
]

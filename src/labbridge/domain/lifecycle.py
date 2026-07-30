"""Campaign, work-item, and attempt state machines.

`docs/SPEC.md` §7. The transition tables are data, not `if` chains, so a reviewer can read the whole
machine in one place and a test can enumerate it exhaustively.

Terminal states are derived from the tables rather than listed separately: a state with no outgoing
transition is terminal, and that cannot drift out of step with the table above it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Literal

CampaignState = Literal[
    "draft", "ready", "active", "paused", "completed", "budget_exhausted", "cancelled", "failed"
]
WorkItemState = Literal[
    "proposed", "validated", "queued", "accepted", "quarantined", "rejected", "cancelled"
]
AttemptState = Literal[
    "pending",
    "leased",
    "running",
    "succeeded",
    "timed_out",
    "failed_retryable",
    "failed_terminal",
    "corrupted",
    "lease_lost",
    "cancelled",
]
JobState = Literal[
    "available",
    "leased",
    "running",
    "succeeded",
    "retry_scheduled",
    "failed_terminal",
    "timed_out",
    "cancelled",
]

CAMPAIGN_TRANSITIONS: Final[dict[CampaignState, frozenset[CampaignState]]] = {
    "draft": frozenset({"ready", "cancelled", "failed"}),
    "ready": frozenset({"active", "cancelled", "failed"}),
    # `budget_exhausted` is a valid terminal state, not an exception (docs/SPEC.md section 7.1).
    "active": frozenset({"paused", "completed", "budget_exhausted", "cancelled", "failed"}),
    "paused": frozenset({"active", "completed", "budget_exhausted", "cancelled", "failed"}),
    "completed": frozenset(),
    "budget_exhausted": frozenset(),
    "cancelled": frozenset(),
    "failed": frozenset(),
}

WORK_ITEM_TRANSITIONS: Final[dict[WorkItemState, frozenset[WorkItemState]]] = {
    "proposed": frozenset({"validated", "rejected"}),
    "validated": frozenset({"queued", "rejected"}),
    "queued": frozenset({"accepted", "quarantined", "rejected", "cancelled"}),
    # Quarantine preserves the candidate and its failure history. Release is a deliberate act, so
    # the only way out is back to `queued` (docs/SPEC.md section 7.2).
    "quarantined": frozenset({"queued", "rejected", "cancelled"}),
    "accepted": frozenset(),
    "rejected": frozenset(),
    "cancelled": frozenset(),
}

ATTEMPT_TRANSITIONS: Final[dict[AttemptState, frozenset[AttemptState]]] = {
    "pending": frozenset({"leased", "cancelled"}),
    "leased": frozenset({"running", "lease_lost", "cancelled"}),
    "running": frozenset(
        {
            "succeeded",
            "timed_out",
            "failed_retryable",
            "failed_terminal",
            "corrupted",
            "lease_lost",
            "cancelled",
        }
    ),
    "succeeded": frozenset(),
    "timed_out": frozenset(),
    "failed_retryable": frozenset(),
    "failed_terminal": frozenset(),
    "corrupted": frozenset(),
    "lease_lost": frozenset(),
    "cancelled": frozenset(),
}

JOB_TRANSITIONS: Final[dict[JobState, frozenset[JobState]]] = {
    "available": frozenset({"leased", "cancelled"}),
    # A lease expiring returns the job to `available`; the worker that lost it must not also
    # complete it, which is enforced by the lease token rather than by this table.
    "leased": frozenset({"running", "available", "failed_terminal", "cancelled"}),
    "running": frozenset(
        {"succeeded", "retry_scheduled", "failed_terminal", "timed_out", "available", "cancelled"}
    ),
    "retry_scheduled": frozenset({"available", "cancelled"}),
    "succeeded": frozenset(),
    "failed_terminal": frozenset(),
    "timed_out": frozenset(),
    "cancelled": frozenset(),
}


class IllegalTransitionError(ValueError):
    """A transition the state machine does not allow. Never silently ignored."""

    def __init__(self, machine: str, source: str, target: str) -> None:
        self.machine = machine
        self.source = source
        self.target = target
        super().__init__(f"{machine}: {source} -> {target} is not a legal transition")


def _check[StateT: str](
    machine: str, table: Mapping[StateT, frozenset[StateT]], source: StateT, target: StateT
) -> None:
    """One checker for all four machines: no table is widened to `str`, so the call sites keep
    their literal types and an invalid state name is a type error rather than a runtime miss."""
    if target not in table.get(source, frozenset()):
        raise IllegalTransitionError(machine, source, target)


def check_campaign_transition(source: CampaignState, target: CampaignState) -> None:
    _check("campaign", CAMPAIGN_TRANSITIONS, source, target)


def check_work_item_transition(source: WorkItemState, target: WorkItemState) -> None:
    _check("work_item", WORK_ITEM_TRANSITIONS, source, target)


def check_attempt_transition(source: AttemptState, target: AttemptState) -> None:
    _check("attempt", ATTEMPT_TRANSITIONS, source, target)


def check_job_transition(source: JobState, target: JobState) -> None:
    _check("job", JOB_TRANSITIONS, source, target)


def terminal_states[StateT: str](table: Mapping[StateT, frozenset[StateT]]) -> frozenset[StateT]:
    """States with no outgoing transition. Derived, so it cannot drift from the table."""
    return frozenset(state for state, targets in table.items() if not targets)

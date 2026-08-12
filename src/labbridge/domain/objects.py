"""What a stored object turns out to be, once nobody is left to say what it was meant to be.

An object is written before the transaction that would reference it commits (`docs/SPEC.md` §4.2),
so a process that dies in that window leaves bytes in the store and a row that never became
evidence. Reconciliation has to decide what those bytes are, and the decision must be reproducible:
the same facts must always yield the same verdict, and the verdict must say which fact produced it.

That is why the rule is a pure function over gathered facts rather than a walk through the database.
It can be enumerated offline, it cannot depend on the order rows are visited in, and a reviewer can
read every branch in one place.

**Deletion is never a verdict.** No classification here removes bytes. An object that cannot be
explained is quarantined, which keeps it for inspection; discarding it would destroy the only
evidence that the failure happened (ADR-005).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, get_args

ObjectClassification = Literal[
    "accepted_evidence",
    "diagnostic_duplicate",
    "diagnostic_orphan",
    "quarantined",
    "missing",
]

#: Every classification, derived from the type so the two cannot drift apart.
OBJECT_CLASSIFICATIONS: Final[tuple[ObjectClassification, ...]] = get_args(ObjectClassification)

#: Classifications no recovery action may modify or delete. `accepted_evidence` backs a released
#: scientific claim; `quarantined` is the record of something unexplained, and deleting it would
#: erase the only trace of the failure that produced it.
IMMUTABLE_CLASSIFICATIONS: Final[frozenset[str]] = frozenset({"accepted_evidence", "quarantined"})


@dataclass(frozen=True)
class ObjectFacts:
    """Everything the verdict is allowed to depend on.

    Gathered by the caller from PostgreSQL and the object store. Passing facts rather than a
    connection is what makes the rule enumerable offline: there is no query hidden inside a branch.
    """

    #: The write lifecycle the worker drove: `pending`, `committed`, or `orphaned`.
    state: str
    #: Whether the object is retrievable from the store right now.
    exists: bool
    #: The digest the database recorded, if it ever recorded one.
    recorded_sha256: str | None
    #: The digest of the bytes actually in the store, if they are there.
    actual_sha256: str | None
    #: Whether an `accepted` observation references this object.
    referenced_by_accepted: bool
    #: The status of the outcome of the attempt that staged it, if that attempt reached one.
    outcome_status: str | None


@dataclass(frozen=True)
class ObjectVerdict:
    classification: ObjectClassification
    reason: str


def classify_object(facts: ObjectFacts) -> ObjectVerdict:  # noqa: PLR0911
    """Decide what an object is. Ordered most-certain first; every branch states its evidence.

    One `return` per verdict-and-reason pair rather than a single exit with an accumulated result:
    the reasons differ per branch, and threading them through a variable would hide which fact
    produced which verdict — the one thing this function exists to make legible.
    """
    if not facts.exists:
        # The row promises bytes the store does not have. Whether that is a lost artifact or an
        # upload that never landed is exactly the distinction `state` already records, so the
        # verdict is the same and the reason carries the difference.
        if facts.state == "committed":
            return ObjectVerdict(
                "missing",
                "the database records this object as committed evidence, but the store has no "
                "such object",
            )
        return ObjectVerdict(
            "missing",
            f"staged as `{facts.state}` but never landed in the store; no bytes were retained",
        )

    if (
        facts.recorded_sha256 is not None
        and facts.actual_sha256 is not None
        and facts.recorded_sha256 != facts.actual_sha256
    ):
        # Never resolved by trusting one side. Refreshing the checksum would launder corruption
        # into evidence, and deleting the object would destroy the proof it happened (F-028).
        return ObjectVerdict(
            "quarantined",
            f"the store holds sha256:{facts.actual_sha256} where the database recorded "
            f"sha256:{facts.recorded_sha256}; neither is assumed correct",
        )

    if facts.referenced_by_accepted:
        return ObjectVerdict(
            "accepted_evidence",
            "referenced by an accepted observation, and the stored bytes match the recorded digest",
        )

    if facts.outcome_status == "duplicate_suppressed":
        # Reached only when the bytes differ from the accepted object's: identical bytes are the
        # same content-addressed key and therefore the same row, which the accepted branch above
        # has already claimed. Different bytes under one work item is a real divergence between two
        # reads of one location, and it is kept rather than assumed away.
        return ObjectVerdict(
            "diagnostic_duplicate",
            "staged by an execution whose result was refused as a duplicate, and its bytes differ "
            "from the accepted object; retained as diagnostic data and not promoted",
        )

    if facts.outcome_status is None and facts.state == "pending":
        return ObjectVerdict(
            "diagnostic_orphan",
            "bytes were uploaded but the transaction that would reference them never committed; "
            "retained as diagnostic data",
        )

    return ObjectVerdict(
        "diagnostic_orphan",
        f"bytes exist but no accepted observation references them; the staging attempt ended as "
        f"`{facts.outcome_status or 'unfinished'}`",
    )


__all__ = [
    "IMMUTABLE_CLASSIFICATIONS",
    "OBJECT_CLASSIFICATIONS",
    "ObjectClassification",
    "ObjectFacts",
    "ObjectVerdict",
    "classify_object",
]

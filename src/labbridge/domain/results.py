"""Observations, attempt outcomes, and derived metrics.

`docs/SPEC.md` §3.4, §3.5, §3.6. Three rules shape these models more than anything else:

* **an attempt always produces an outcome** — the outcome is the record, the observation is optional
  (ADR-005);
* **if bytes were received, an observation exists** — even corrupted, even scientifically rejected.
  Persist first, classify second. A validation check that returns before persisting is the failure
  mode invariant 2 exists to prevent;
* **no derived value without a version** — `analysis_name`, `analysis_version`, and `parameter_hash`
  are mandatory, so a metric can always be reinterpreted later (invariant 3, §3.6).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical import content_id
from .provenance import Provenance
from .quantities import CostRecord, Quantity, Uncertainty

ObservationStatus = Literal["received", "accepted", "corrupted", "invalidated", "superseded"]
SignalKind = Literal["lsv", "cv", "amperometry", "scalar", "table"]
AttemptStatus = Literal[
    "succeeded",
    "timed_out",
    "failed_retryable",
    "failed_terminal",
    "corrupted",
    "cancelled",
    "lease_lost",
    "duplicate_suppressed",
]
FailureCategory = Literal[
    "transport",
    "instrument",
    "worker",
    "schema",
    "scientific_validation",
    "policy",
    "storage",
    "unknown",
]
QualityStatus = Literal["accepted", "warning", "rejected"]

#: Outcomes that may carry an observation. Every other status means no bytes were received, so an
#: observation on one of them would be a fabrication.
_STATUSES_WITH_BYTES: frozenset[AttemptStatus] = frozenset({"succeeded", "corrupted"})


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class QuantityDescriptor(_Model):
    """One column or axis of an observation: what it is, in what unit, along which axis.

    Recorded per observation rather than inferred per parser, so an observation stays interpretable
    without the code that produced it.
    """

    name: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    #: Position along the observation's ordered axes. Part of the content identity.
    axis: int = Field(ge=0)
    length: int | None = Field(default=None, ge=0)


class Observation(_Model):
    """Bytes that arrived, with what is known about them.

    `object_uri` points at immutable storage. The bytes are never edited: a correction creates a new
    observation and a relation (ADR-006).
    """

    observation_id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    work_item_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    object_uri: str = Field(min_length=1)
    byte_size: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)
    schema_version: str = Field(min_length=1)
    signal_kind: SignalKind
    quantities: tuple[QuantityDescriptor, ...]
    status: ObservationStatus
    received_at: datetime
    provenance: Provenance
    #: Why the observation is not `accepted`. Required for `corrupted` and `invalidated`, so a
    #: rejected observation always carries the reason it was rejected (PO-05).
    status_reason: str | None = None

    @model_validator(mode="after")
    def _a_rejected_observation_states_why(self) -> Self:
        if self.status in ("corrupted", "invalidated") and not self.status_reason:
            raise ValueError(f"observation status `{self.status}` requires a status_reason")
        return self


def observation_id(
    *,
    sha256: str,
    schema_version: str,
    signal_kind: SignalKind,
    quantities: tuple[QuantityDescriptor, ...],
    provenance: Provenance,
) -> str:
    """Content identity for an observation.

    Covers the bytes, the schema version, the signal kind, the ordered quantity descriptors with
    their units, and the provenance root (`docs/DATA_STRATEGY.md` §5). Changing a unit, an axis
    order, or the schema version changes the identity; reordering a mapping does not.
    """
    return content_id(
        "obs",
        {
            "sha256": sha256,
            "schema_version": schema_version,
            "signal_kind": signal_kind,
            "quantities": [q.model_dump(mode="python") for q in quantities],
            "origin": provenance.environment.data_origin,
            "mode": provenance.environment.execution_mode,
            "source": (
                provenance.source_record.model_dump(mode="python")
                if provenance.source_record
                else provenance.synthetic_root.model_dump(mode="python")
                if provenance.synthetic_root
                else None
            ),
        },
    )


class FailureRecord(_Model):
    """Structured failure detail. `retryable` is recorded, not inferred at read time."""

    failure_code: str = Field(min_length=1)
    category: FailureCategory
    retryable: bool
    summary: str = Field(min_length=1)
    details: tuple[tuple[str, str], ...] = ()
    exception_type: str | None = None


class AttemptOutcome(_Model):
    """Exactly one per attempt. A retry creates a new attempt, never rewrites this one."""

    attempt_id: str = Field(min_length=1)
    work_item_id: str = Field(min_length=1)
    status: AttemptStatus
    observation_id: str | None = None
    failure: FailureRecord | None = None
    cost: CostRecord = CostRecord()
    started_at: datetime | None = None
    finished_at: datetime
    provenance: Provenance

    @model_validator(mode="after")
    def _observation_and_failure_match_the_status(self) -> Self:
        if self.observation_id is not None and self.status not in _STATUSES_WITH_BYTES:
            allowed = ", ".join(sorted(_STATUSES_WITH_BYTES))
            raise ValueError(
                f"status `{self.status}` cannot carry an observation; only {allowed} receive bytes"
            )
        if self.status == "corrupted" and self.observation_id is None:
            # `corrupted` is a classification of bytes that arrived. Without the observation the
            # classification has no subject, and the bytes ADR-005 requires retaining are gone.
            raise ValueError(
                "a corrupted outcome requires the observation whose bytes were classified "
                "(ADR-005, PO-05)"
            )
        if self.status == "succeeded" and self.failure is not None:
            raise ValueError("a succeeded outcome carries no failure record")
        if self.status == "duplicate_suppressed" and self.failure is not None:
            # A suppressed duplicate carrying a retryable failure would drive re-delivery of the
            # thing that was suppressed.
            raise ValueError("a duplicate_suppressed outcome carries no failure record")
        if self.status in ("failed_retryable", "failed_terminal") and self.failure is None:
            raise ValueError(f"status `{self.status}` requires a failure record")
        if (
            self.status == "failed_retryable"
            and self.failure is not None
            and not self.failure.retryable
        ):
            raise ValueError("failed_retryable requires failure.retryable=True")
        if self.status == "failed_terminal" and self.failure is not None and self.failure.retryable:
            raise ValueError("failed_terminal requires failure.retryable=False")
        return self


class DerivedMetric(_Model):
    """A value LabBridge computed, tied to one observation and to the exact code that produced it.

    `analysis_name` separates a source-provided fit from a LabBridge recomputation (§3.6). They are
    never merged into one column, one metric, or one chart series.
    """

    metric_id: str = Field(min_length=1)
    observation_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    value: Quantity
    uncertainty: Uncertainty | None = None
    analysis_name: str = Field(min_length=1)
    analysis_version: str = Field(min_length=1)
    parameter_hash: str = Field(min_length=1)
    quality_status: QualityStatus
    provenance: Provenance
    #: Required when the metric is not `accepted`, so a warning or rejection is always explicable.
    quality_reason: str | None = None

    @model_validator(mode="after")
    def _a_non_accepted_metric_states_why(self) -> Self:
        if self.quality_status != "accepted" and not self.quality_reason:
            raise ValueError(f"quality_status `{self.quality_status}` requires a quality_reason")
        return self


def metric_id(
    *,
    observation_id: str,
    attempt_id: str,
    name: str,
    analysis_name: str,
    analysis_version: str,
    parameter_hash: str,
) -> str:
    """Identity for a derived metric: one receipt, one analysis, one parameter set.

    Re-running the same analysis at the same version with the same parameters over the same receipt
    yields the same id, which is what makes recomputation idempotent.

    `attempt_id` is part of the identity for the same reason it is part of the observation's key.
    `observation_id` is content-derived, so two campaigns replaying one location share it; without
    the attempt, a metric from the second campaign's receipt would collide with the first's, and
    the two are not the same result however identical the bytes.
    """
    return content_id(
        "metric",
        {
            "observation_id": observation_id,
            "attempt_id": attempt_id,
            "name": name,
            "analysis_name": analysis_name,
            "analysis_version": analysis_version,
            "parameter_hash": parameter_hash,
        },
    )

"""Campaign declarations, retry classification, and deterministic backoff."""

from __future__ import annotations

from decimal import Decimal
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

StoppingRule = Literal["hard_budget_exhausted"]


class CampaignDeclaration(BaseModel):
    """The execution limits that must be known before a campaign becomes active."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hard_budget: Decimal = Field(gt=0, allow_inf_nan=False)
    per_attempt_estimate: Decimal = Field(gt=0, allow_inf_nan=False)
    budget_unit: str = Field(min_length=1, max_length=32)
    max_attempts: int = Field(ge=1)
    stopping_rule: StoppingRule

    @field_validator("budget_unit")
    @classmethod
    def _unit_is_not_whitespace(cls, value: str) -> str:
        unit = value.strip()
        if not unit:
            raise ValueError("budget_unit must contain a unit")
        return unit

    @model_validator(mode="after")
    def _one_attempt_fits_the_budget(self) -> Self:
        if self.per_attempt_estimate > self.hard_budget:
            raise ValueError("per_attempt_estimate must not exceed hard_budget")
        return self


RETRYABLE_FAILURE_CODES: Final[frozenset[str]] = frozenset(
    {
        "adapter_transient",
        "database_conflict",
        "lease_lost",
        "object_store_unavailable",
        "outcome_write_failed",
        "storage_unavailable",
        "timeout",
        "transport_failure",
    }
)


def retryable_failure(failure_code: str) -> bool:
    """Return the declared V1 retry decision for one stable failure code."""
    return failure_code in RETRYABLE_FAILURE_CODES


class RetryPolicy(BaseModel):
    """Bounded exponential backoff expressed as data, without sleeping."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_seconds: int = Field(default=2, ge=1)
    maximum_seconds: int = Field(default=300, ge=1)

    @model_validator(mode="after")
    def _cap_is_not_below_the_base(self) -> Self:
        if self.maximum_seconds < self.base_seconds:
            raise ValueError("maximum_seconds must be at least base_seconds")
        return self

    def backoff_seconds(self, attempt_number: int) -> int:
        if attempt_number < 1:
            raise ValueError("attempt_number must be at least 1")
        delay: int = self.base_seconds * (2 ** (attempt_number - 1))
        return min(delay, self.maximum_seconds)


DEFAULT_RETRY_POLICY: Final = RetryPolicy()


__all__ = [
    "DEFAULT_RETRY_POLICY",
    "RETRYABLE_FAILURE_CODES",
    "CampaignDeclaration",
    "RetryPolicy",
    "StoppingRule",
    "retryable_failure",
]

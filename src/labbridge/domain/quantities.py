"""Typed scientific quantities.

`AI_CONTRACT.md` invariant 8 requires scientific values, uncertainty, and cost to use typed
structures with explicit units rather than `dict[str, Any]`. `docs/SPEC.md` §3.3 fixes the shapes.

A unit is never inferred and never defaulted. A quantity whose unit is genuinely unknown records
`UNKNOWN_UNIT` explicitly, so downstream code meets an unknown rather than a plausible guess
(F-015). Nothing converts an unknown unit.

No conversion is implemented here. `docs/SPEC.md` §3.3 asks for a validated registry and requires
unsupported conversions to fail explicitly; an unvalidated conversion table would be worse than
none, so this module carries units and refuses to reinterpret them.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: The recorded absence of a unit. Distinct from a dimensionless quantity, which has unit "1".
UNKNOWN_UNIT: Final = "unknown"
#: A pure number: a transfer coefficient, a ratio, a retained fraction.
DIMENSIONLESS: Final = "1"

UncertaintyKind = Literal["standard_deviation", "standard_error", "confidence_interval", "unknown"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Quantity(_Model):
    """A scalar or array value with its unit.

    `value` is `Decimal`, never `float`: a binary float has no canonical decimal form, so a float
    here would make every identity derived from it platform-dependent (invariant 7).
    """

    #: Non-finite values are rejected by Pydantic itself: `allow_inf_nan` is False for `Decimal`, so
    #: NaN and the infinities fail with `finite_number` before any validator here would run. Stated
    #: rather than re-implemented — a redundant check would read as the enforcement and hide where
    #: the real one lives.
    value: Decimal | tuple[Decimal, ...]
    unit: str = Field(min_length=1)

    @property
    def is_unknown_unit(self) -> bool:
        return self.unit == UNKNOWN_UNIT


class Uncertainty(_Model):
    """How well a value is known. `unknown` is a real answer and must stay expressible."""

    kind: UncertaintyKind
    value: Quantity | None = None
    confidence_level: Decimal | None = None
    #: Replicate count. A dispersion estimated from very few replicates is a poor estimate, and a
    #: consumer cannot judge that without n.
    n: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _confidence_interval_needs_a_level(self) -> Self:
        if self.kind == "confidence_interval" and self.confidence_level is None:
            raise ValueError("a confidence_interval requires an explicit confidence_level")
        if self.kind == "unknown" and self.value is not None:
            raise ValueError("an unknown uncertainty carries no value")
        return self


class Money(_Model):
    amount: Decimal
    #: ISO 4217. Recorded, never assumed, because a bare number is not a cost.
    currency: str = Field(min_length=3, max_length=3)


class CostRecord(_Model):
    """What an attempt was expected to cost and what it actually cost.

    Every field is optional: a replay costs no consumables, and an attempt that never started has no
    actual duration. Absent is recorded as absent rather than as zero.
    """

    estimated_duration: Quantity | None = None
    actual_duration: Quantity | None = None
    consumable_cost: Money | None = None
    compute_cost: Money | None = None

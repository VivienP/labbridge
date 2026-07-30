"""Data origin, execution mode, and the pairs an adapter is allowed to emit.

`AI_CONTRACT.md` invariant 1 requires that an adapter *cannot* emit an incompatible pair, and that
the constraint be enforced by the type system or by validation and proven by a test. A convention is
not enforcement, so the admissible set lives here and `EnvironmentRef` refuses anything outside it.

The two fields are independent. Origin says where the values came from; mode says how they reached
the runtime. Collapsing them into one field is the withdrawn `Fidelity` vocabulary (ADR-003) and
must not reappear.
"""

from __future__ import annotations

from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

DataOrigin = Literal["observed", "synthetic"]
ExecutionMode = Literal["replay", "simulation", "live"]

#: ADR-010. `observed + simulation` is absent on purpose: simulation cannot produce observed data.
#: `observed + live` is reserved for an instrument integration outside V1 and is admissible here so
#: the constraint does not have to be relaxed later under deadline pressure.
ADMISSIBLE_PAIRS: Final[frozenset[tuple[DataOrigin, ExecutionMode]]] = frozenset(
    {
        ("observed", "replay"),
        ("synthetic", "replay"),
        ("synthetic", "simulation"),
        ("observed", "live"),
    }
)


class EnvironmentRef(BaseModel):
    """Which environment produced a record, and under which origin and mode.

    Carried on every observation, outcome, metric, export row, and manifest entry. It is propagated,
    never re-derived downstream: nothing may infer an origin from a filename or default a mode.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    environment_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    data_origin: DataOrigin
    execution_mode: ExecutionMode

    @model_validator(mode="after")
    def _pair_must_be_admissible(self) -> Self:
        pair = (self.data_origin, self.execution_mode)
        if pair not in ADMISSIBLE_PAIRS:
            admissible = ", ".join(f"{o}+{m}" for o, m in sorted(ADMISSIBLE_PAIRS))
            raise ValueError(
                f"inadmissible origin/mode pair {self.data_origin}+{self.execution_mode} "
                f"(ADR-010; admissible: {admissible})"
            )
        return self

    @property
    def is_synthetic(self) -> bool:
        """Whether every human-readable surface for this record must be labelled synthetic."""
        return self.data_origin == "synthetic"

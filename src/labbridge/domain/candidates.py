"""Typed candidates and their content-derived identity.

`docs/SPEC.md` §3.2 requires a discriminated union rather than one universal mapping, and a
`candidate_id` computed from canonical serialisation including schema version and units.

Only the HER candidate exists here. The biosensor candidate arrives with the simulator, and adding
it now would be a claim without code behind it (`AI_CONTRACT.md` invariant 10). The union is written
so a second member costs one line.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from .canonical import content_id
from .quantities import Quantity

#: Bumped whenever a field is added, removed, or reinterpreted. It is part of the identity, so a
#: schema change produces new candidate ids rather than silently reusing the old ones.
CANDIDATE_SCHEMA_VERSION: Final = "1"


class _Candidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class HerCandidate(_Candidate):
    """One measurement location on one materials library.

    The grid coordinates are `Quantity`, not bare numbers, because the archive records them in
    millimetres and a coordinate without its unit cannot be compared across sources.
    """

    kind: Literal["her_location"] = "her_location"
    library_id: str = Field(min_length=1)
    measurement_area_id: str = Field(min_length=1)
    grid_x: Quantity
    grid_y: Quantity


Candidate = Annotated[HerCandidate, Field(discriminator="kind")]


def candidate_id(candidate: Candidate) -> str:
    """A stable identity for a candidate, covering its schema version, kind, values, and units.

    Two candidates differing only in unit are different candidates. Two differing only in the order
    their fields were written are the same one.
    """
    return content_id(
        "cand",
        {
            "schema_version": CANDIDATE_SCHEMA_VERSION,
            "candidate": candidate.model_dump(mode="python"),
        },
    )

"""Provenance and correction relations.

`docs/DATA_STRATEGY.md` §6 requires every accepted derived metric to resolve to exactly one root:
an observed root identified by Zenodo record, version, filename, checksum, source path, raw
observation hash, parsing version, and analysis version; or a synthetic root identified by model
version, canonical configuration hash, seed, component-model versions, generated observation hash,
and analysis version.

`SourceRecord` and `SyntheticRoot` are the two roots. **At most one** is present, validated here:
resolving to both is unconstructable. Resolving to *neither* is allowed only on this type, because
an attempt that read nothing — a timeout, an unavailable location, a lost lease — has no member to
cite and inventing one would put a false path in the record. The root is required where PO-06
actually requires it: `Observation` and `DerivedMetric` both reject provenance without one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .identity import EnvironmentRef

RelationPredicate = Literal["derived_from", "supersedes", "invalidates", "replaces"]

#: What kind of source value a record came from. Enumerated, not free text, because this field is
#: the **only** thing separating a measurement from a model output: the archive stores measured EDX
#: and GP-predicted XPS in structurally identical files — same headers, same units, same row count —
#: so no column check can tell them apart (F-046). A free-form string left the one defence against
#: that conflation to whatever a caller happened to type.
#:
#: The mapping from an archive path to one of these values belongs to the adapter that knows the
#: archive, not here: `AI_CONTRACT.md` §5 keeps filesystem knowledge out of the domain. The domain
#: owns the scientific categories; infrastructure owns which file is which.
SourceType = Literal[
    "measured_lsv",
    "measured_edx",
    "measured_xps",
    "predicted_xps",
    "source_fitted_parameters",
]

#: The subset that is a measurement. Anything else is a model output or a source-provided
#: derivation, and must never be presented as observed (`docs/DATA_STRATEGY.md` §2.2).
MEASURED_SOURCE_TYPES: frozenset[SourceType] = frozenset(
    {"measured_lsv", "measured_edx", "measured_xps"}
)


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SourceRecord(_Model):
    """The observed root: which published bytes a record ultimately came from."""

    doi: str = Field(min_length=1)
    record_version: str = Field(min_length=1)
    source_filename: str = Field(min_length=1)
    #: SHA-256 of the acquired file, as recorded in provenance.json at fetch time.
    source_sha256: str = Field(min_length=64, max_length=64)
    #: Where inside the archive the values came from, e.g. a member path.
    source_path: str = Field(min_length=1)
    #: Set from the path at ingestion, never from column names (F-046).
    source_type: SourceType
    parsing_version: str = Field(min_length=1)

    @property
    def is_measured(self) -> bool:
        """Whether these values are a measurement rather than a model output or a source fit."""
        return self.source_type in MEASURED_SOURCE_TYPES


class SyntheticRoot(_Model):
    """The synthetic root: what would have to be re-run to regenerate the same bytes."""

    generator: str = Field(min_length=1)
    generator_version: str = Field(min_length=1)
    seed: int
    #: Hash of the canonical configuration the generator was run with.
    config_hash: str = Field(min_length=1)
    #: Version per component model, so changing one component changes the identity.
    component_versions: tuple[tuple[str, str], ...] = ()


class Provenance(_Model):
    """Carried on every observation, outcome, and metric. Propagated, never re-derived."""

    environment: EnvironmentRef
    source_record: SourceRecord | None = None
    synthetic_root: SyntheticRoot | None = None
    code_version: str = Field(min_length=1)
    config_hash: str = Field(min_length=1)
    #: The records this one was produced from. Empty for a root record.
    parent_ids: tuple[str, ...] = ()

    @property
    def has_root(self) -> bool:
        """Whether this provenance resolves to a lineage root.

        False only for an attempt that produced no observation — a timeout, an unavailable
        location, a lost lease. There is no source member to cite because nothing was read, and
        inventing one would put a false path in the record. `docs/DATA_STRATEGY.md` §6 requires a
        root of every accepted *derived metric* and every accepted observation, which is where the
        requirement is enforced; a failure record is not one of those.
        """
        return self.source_record is not None or self.synthetic_root is not None

    @model_validator(mode="after")
    def _at_most_one_root(self) -> Self:
        roots = [self.source_record is not None, self.synthetic_root is not None]
        if sum(roots) > 1:
            raise ValueError(
                "provenance resolves to two roots; a record derives from observed data or from "
                "generated data, never both (docs/DATA_STRATEGY.md section 6, PO-06)"
            )
        if self.source_record is not None and self.environment.data_origin != "observed":
            raise ValueError("a source_record root requires data_origin=observed")
        if self.synthetic_root is not None and self.environment.data_origin != "synthetic":
            raise ValueError("a synthetic_root requires data_origin=synthetic")
        return self


class RecordRelation(_Model):
    """An append-only correction link. Nothing is ever mutated in place (invariant 3, ADR-006)."""

    subject_id: str = Field(min_length=1)
    predicate: RelationPredicate
    object_id: str = Field(min_length=1)
    #: Why. An invalidation without a stated reason is not auditable.
    reason: str = Field(min_length=1)
    recorded_at: datetime

    @model_validator(mode="after")
    def _a_record_may_not_relate_to_itself(self) -> Self:
        if self.subject_id == self.object_id:
            raise ValueError(f"{self.predicate} relation from {self.subject_id} to itself")
        return self

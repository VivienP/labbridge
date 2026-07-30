"""Provenance and correction relations.

`docs/DATA_STRATEGY.md` §6 requires every accepted derived metric to resolve to exactly one root:
an observed root identified by Zenodo record, version, filename, checksum, source path, raw
observation hash, parsing version, and analysis version; or a synthetic root identified by model
version, canonical configuration hash, seed, component-model versions, generated observation hash,
and analysis version.

`SourceRecord` and the seed are the two roots. Exactly one is present, and that is validated here
rather than left to the caller: a record resolving to neither root, or to both, is a blocking defect
(PO-06), and it is cheaper to make it unconstructable than to detect it later.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .identity import EnvironmentRef

RelationPredicate = Literal["derived_from", "supersedes", "invalidates", "replaces"]


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
    #: Distinguishes measured from predicted and raw from source-provided fit. Set from the path at
    #: ingestion, never from column names: the archive stores measured EDX and GP-predicted XPS in
    #: structurally identical files, so column validation cannot separate them (F-046).
    source_type: str = Field(min_length=1)
    parsing_version: str = Field(min_length=1)


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

    @model_validator(mode="after")
    def _exactly_one_root(self) -> Self:
        roots = [self.source_record is not None, self.synthetic_root is not None]
        if sum(roots) != 1:
            raise ValueError(
                "provenance must resolve to exactly one root: a source_record for observed data "
                "or a synthetic_root for generated data (docs/DATA_STRATEGY.md section 6, PO-06)"
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

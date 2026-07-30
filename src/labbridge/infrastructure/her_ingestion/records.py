"""Typed records for HER source acquisition.

These live in infrastructure, not in `domain/`: `FetchedFile` carries a filesystem path, and
AI_CONTRACT.md section 5 bars the domain layer from depending on filesystem paths. They carry every
field docs/DATA_STRATEGY.md section 6 requires of the observed lineage root — Zenodo record and
version, source filename and checksum, source identifier — so a later `SourceRecord` can be built
from a recorded fetch without re-downloading anything.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

#: The pinned HER dataset record (docs/DATA_STRATEGY.md section 2.1).
PINNED_DOI: Final = "10.5281/zenodo.20439519"
#: The associated preprint, recorded for provenance; not fetched by this layer.
PINNED_ARXIV: Final = "2606.00779"

PROVENANCE_SCHEMA_VERSION: Final = "1"
INVENTORY_SCHEMA_VERSION: Final = "1"

ChecksumAlgorithm = Literal["md5", "sha256"]

#: Redistribution is never set by parsing a record. Only a recorded data-use decision resolves it
#: (docs/DATA_STRATEGY.md section 2.3, ADR-009); see `data_use.py`.
RedistributionStatus = Literal["unresolved", "permitted_with_attribution"]


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LicenceStatus(_Record):
    """What the record declares, kept separate from what LabBridge is permitted to redistribute.

    `raw_value` and `access_right` are whatever the record published, verbatim or None.
    `redistribution` defaults to `unresolved` and no parser may widen it: a licence string on a
    record is evidence, not a decision. Only `data_use.resolve_redistribution` applying a recorded,
    dated decision moves it (docs/DATA_STRATEGY.md section 2.3, ADR-009).
    """

    raw_value: str | None
    access_right: str | None = None
    redistribution: RedistributionStatus = "unresolved"


class DataUseDecision(_Record):
    """A recorded, dated decision about what may be redistributed from one source record.

    Pinned to `doi` and `licence_id`. The decision is evidence about a specific declared licence, so
    it applies only while the record still declares that licence: an upstream relicensing reopens
    the gate rather than letting a decision outlive the evidence it rests on.
    """

    #: The architecture decision carrying the reasoning, e.g. `ADR-009`.
    adr: str
    doi: str
    #: The licence identifier read from the record at `verified_on`.
    licence_id: str
    #: When the licence was read from `verified_from` — not when this file was last edited.
    verified_on: date
    #: The exact endpoint the licence was read from, so the check is repeatable.
    verified_from: str
    redistribution: RedistributionStatus
    #: What every redistributed artifact must carry. Empty when the decision requires none.
    attribution: str


class RemoteFile(_Record):
    """One file offered by the record, as advertised before any download."""

    filename: str
    byte_size: int = Field(ge=0)
    checksum_algorithm: ChecksumAlgorithm
    checksum_value: str
    download_url: str


class ArchiveInventory(_Record):
    """What the record contains. Written in dry-run and alongside every fetch."""

    schema_version: str = INVENTORY_SCHEMA_VERSION
    doi: str
    record_id: str
    #: The record publishes no semantic version string. Zenodo exposes a zero-based version index
    #: under `metadata.relations.version`; that index is recorded verbatim rather than renumbered.
    record_version: str
    #: Identifies the version series this record belongs to, distinct from the version DOI above.
    concept_doi: str | None = None
    #: Zenodo's metadata edit counter. A changed revision with an unchanged version means the
    #: metadata moved while the files did not.
    revision: int | None = None
    title: str
    licence: LicenceStatus
    #: Kept as the raw string the record published; parsing it to a datetime would invent a time.
    published_date: str | None
    files: tuple[RemoteFile, ...]
    retrieved_at: datetime


class FetchedFile(_Record):
    """One file that landed, with the eight fields docs/DATA_STRATEGY.md section 2.4 requires."""

    filename: str
    source_url: str
    byte_size: int = Field(ge=0)
    provided_checksum_algorithm: ChecksumAlgorithm
    provided_checksum_value: str
    computed_sha256: str
    #: POSIX and relative to the landing root, so the document carries no operator home directory
    #: and stays comparable across machines.
    landing_path: str
    retrieved_at: datetime


class ProvenanceDocument(_Record):
    """`provenance.json`: the exact source and checksums for one fetch.

    Written inside the git-ignored landing root. It carries the data-use decision that was in force
    at fetch time, so a consumer reading only this document knows the licence, the date it was
    verified, and the attribution it must reproduce.
    """

    schema_version: str = PROVENANCE_SCHEMA_VERSION
    doi: str
    record_id: str
    record_version: str
    source_licence: LicenceStatus
    #: The decision applied to this fetch, or None when none matched and the gate stayed open.
    data_use: DataUseDecision | None = None
    tool_version: str
    files: tuple[FetchedFile, ...]
    written_at: datetime

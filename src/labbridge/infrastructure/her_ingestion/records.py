"""Typed records for HER source acquisition.

These live in infrastructure, not in `domain/`: `FetchedFile` carries a filesystem path, and
AI_CONTRACT.md section 5 bars the domain layer from depending on filesystem paths. They carry every
field docs/DATA_STRATEGY.md section 6 requires of the observed lineage root — Zenodo record and
version, source filename and checksum, source identifier — so a later `SourceRecord` can be built
from a recorded fetch without re-downloading anything.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

#: The pinned HER dataset record (docs/DATA_STRATEGY.md section 2.1).
PINNED_DOI: Final = "10.5281/zenodo.20439519"
#: The associated preprint, recorded for provenance; not fetched by this layer.
PINNED_ARXIV: Final = "2606.00779"

PROVENANCE_SCHEMA_VERSION: Final = "1"
INVENTORY_SCHEMA_VERSION: Final = "1"

ChecksumAlgorithm = Literal["md5", "sha256"]

#: Redistribution is an unresolved blocker until a recorded data-use decision closes the gate
#: (docs/DATA_STRATEGY.md section 2.3). No parser may widen this type.
RedistributionStatus = Literal["unresolved"]


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LicenceStatus(_Record):
    """What the record declares, kept separate from what LabBridge is permitted to redistribute.

    `raw_value` and `access_right` are whatever the record published, verbatim or None.
    `redistribution` is fixed at `unresolved`: closing the gate requires a recorded data-use
    decision and an ADR (docs/DATA_STRATEGY.md section 2.3), never a string match here.
    """

    raw_value: str | None
    access_right: str | None = None
    redistribution: RedistributionStatus = "unresolved"


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

    Written inside the git-ignored landing root. docs/DATA_STRATEGY.md section 8 permits committing
    source inventory metadata only once redistribution is permitted, and that gate is open.
    """

    schema_version: str = PROVENANCE_SCHEMA_VERSION
    doi: str
    record_id: str
    record_version: str
    source_licence: LicenceStatus
    tool_version: str
    files: tuple[FetchedFile, ...]
    written_at: datetime

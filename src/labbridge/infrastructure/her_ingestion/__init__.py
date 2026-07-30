"""HER source acquisition: read the pinned Zenodo record, land files with verified integrity.

Gate 0 scope. No module here references any archive-internal path, table, column, unit, or filename;
that awaits the inspection inventory (AI_CONTRACT.md section 7).
"""

from __future__ import annotations

from .errors import (
    FileSelectionError,
    FileTooLargeError,
    HerIngestionError,
    LandingConflictError,
    NoFilesRequestedError,
    SourceIntegrityError,
    SourceUnavailableError,
    UnsupportedRecordError,
)
from .fetch import (
    DEFAULT_LANDING_ROOT,
    DEFAULT_MAX_BYTES,
    FetchReport,
    FetchRequest,
    run_fetch,
)
from .records import PINNED_ARXIV, PINNED_DOI, ArchiveInventory, ProvenanceDocument
from .zenodo import ZenodoTransport

__all__ = [
    "DEFAULT_LANDING_ROOT",
    "DEFAULT_MAX_BYTES",
    "PINNED_ARXIV",
    "PINNED_DOI",
    "ArchiveInventory",
    "FetchReport",
    "FetchRequest",
    "FileSelectionError",
    "FileTooLargeError",
    "HerIngestionError",
    "LandingConflictError",
    "NoFilesRequestedError",
    "ProvenanceDocument",
    "SourceIntegrityError",
    "SourceUnavailableError",
    "UnsupportedRecordError",
    "ZenodoTransport",
    "run_fetch",
]

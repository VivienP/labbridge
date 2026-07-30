"""Orchestration of one acquisition run.

The sequence lives here rather than in the CLI: AI_CONTRACT.md section 5 requires CLI code to
"translate inputs into application commands and render application results", not to own the
workflow.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final

from .errors import NoFilesRequestedError, UnsupportedRecordError
from .landing import LandingZone, fetch_file
from .provenance import INVENTORY_FILENAME, PROVENANCE_FILENAME, write_document
from .records import (
    PINNED_DOI,
    ArchiveInventory,
    FetchedFile,
    ProvenanceDocument,
    RemoteFile,
)
from .zenodo import ZenodoTransport, parse_record, record_api_url, select_files

#: Under `data/her/`, which .gitignore excludes, so provenance.json stays uncommittable by default
#: while the redistribution gate is open.
DEFAULT_LANDING_ROOT: Final = Path("data/her/raw")
#: A ceiling, not a statement about the archive. A file above it must be named in `allow_large`,
#: which is how "avoid downloading the large measurement video unless explicitly requested"
#: (docs/DATA_STRATEGY.md section 2.4) is honoured without naming a file no inventory confirmed.
DEFAULT_MAX_BYTES: Final = 64 * 1024 * 1024


@dataclass(frozen=True)
class FetchRequest:
    record_id: str
    filenames: tuple[str, ...] = ()
    landing_root: Path = DEFAULT_LANDING_ROOT
    max_bytes: int = DEFAULT_MAX_BYTES
    allow_large: tuple[str, ...] = ()
    dry_run: bool = False
    #: The DOI the record must declare. Defaults to the pinned record; injectable so the offline
    #: suite can exercise the mismatch path without putting the real DOI in a fixture.
    expected_doi: str = PINNED_DOI


@dataclass(frozen=True)
class FetchReport:
    inventory: ArchiveInventory
    selected: tuple[RemoteFile, ...]
    inventory_path: Path
    fetched: tuple[FetchedFile, ...] = field(default_factory=tuple)
    provenance_path: Path | None = None


def run_fetch(
    request: FetchRequest,
    *,
    transport: ZenodoTransport,
    clock: Callable[[], datetime],
    tool_version: str,
) -> FetchReport:
    """Read the record, then either report the inventory (dry-run) or acquire the named files.

    Nothing is written outside `request.landing_root`. A dry-run's only filesystem effect is the
    inventory document; it never opens a download.
    """
    retrieved_at = clock()
    payload = transport.get_json(record_api_url(request.record_id))
    inventory = parse_record(payload, retrieved_at=retrieved_at)

    if inventory.doi != request.expected_doi:
        raise UnsupportedRecordError(
            f"record {request.record_id} declares DOI `{inventory.doi}`, "
            f"expected `{request.expected_doi}`",
            field="doi",
        )

    selected = select_files(
        inventory,
        filenames=request.filenames,
        max_bytes=request.max_bytes,
        allow_large=request.allow_large,
    )

    inventory_path = request.landing_root / INVENTORY_FILENAME
    write_document(inventory_path, inventory)

    if request.dry_run:
        return FetchReport(inventory=inventory, selected=selected, inventory_path=inventory_path)

    if not selected:
        raise NoFilesRequestedError(available=tuple(f.filename for f in inventory.files))

    zone = LandingZone(root=request.landing_root)
    fetched = tuple(
        fetch_file(remote, transport=transport, zone=zone, retrieved_at=retrieved_at)
        for remote in selected
    )

    provenance_path = request.landing_root / PROVENANCE_FILENAME
    write_document(
        provenance_path,
        ProvenanceDocument(
            doi=inventory.doi,
            record_id=inventory.record_id,
            record_version=inventory.record_version,
            source_licence=inventory.licence,
            tool_version=tool_version,
            files=fetched,
            written_at=retrieved_at,
        ),
    )

    return FetchReport(
        inventory=inventory,
        selected=selected,
        inventory_path=inventory_path,
        fetched=fetched,
        provenance_path=provenance_path,
    )

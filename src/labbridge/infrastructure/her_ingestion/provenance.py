"""Canonical serialisation of the acquisition documents.

Byte-stability serves reproducibility and checksumming, not Git diffs: these files are written
inside the git-ignored landing root, because docs/DATA_STRATEGY.md section 8 permits committing
source inventory metadata only "when redistribution is permitted" and that gate is open. Do not
"fix" this later by committing them.

`sort_keys=True` makes identical content serialise identically regardless of field insertion order.
`allow_nan=False` is deliberate: docs/DATA_STRATEGY.md section 5 requires NaN and infinity handling
to be defined, and refusing them is the definition here. Writing is binary, so Windows cannot
substitute CRLF and break `sha256sum -c` on a file nobody edited.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from pydantic import BaseModel

from .dataset import DatasetInventory
from .records import ArchiveInventory, ProvenanceDocument

PROVENANCE_FILENAME: Final = "provenance.json"
INVENTORY_FILENAME: Final = "archive_inventory.json"
DATASET_INVENTORY_FILENAME: Final = "dataset_inventory.json"


def canonical_json_bytes(model: BaseModel) -> bytes:
    """The one serialisation used for both writing and checksumming."""
    payload = model.model_dump(mode="json")
    encoded = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
    return encoded.encode("utf-8") + b"\n"


def write_document(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(model))


def read_provenance(path: Path) -> ProvenanceDocument:
    return ProvenanceDocument.model_validate_json(path.read_bytes())


def read_inventory(path: Path) -> ArchiveInventory:
    return ArchiveInventory.model_validate_json(path.read_bytes())


def read_dataset_inventory(path: Path) -> DatasetInventory:
    return DatasetInventory.model_validate_json(path.read_bytes())

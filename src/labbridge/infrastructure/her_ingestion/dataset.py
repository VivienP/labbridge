"""Typed records for the dataset inventory produced by inspecting acquired archives.

The inventory reports what the archives actually contain. It interprets nothing: numeric extremes
are kept as source text, never coerced to float, because an inventory that silently reformats a
value stops being evidence of what the file held.

docs/DATA_STRATEGY.md section 2.4 fixes the required contents: archive paths, file formats, table
names and columns, inferred and declared units, row and array dimensions, missing-value summaries,
identifier ranges, duplicate checks, and the relationships among the source files.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

DATASET_INVENTORY_SCHEMA_VERSION: Final = "1"

LineEnding = Literal["LF", "CRLF", "CR", "mixed", "none"]
InferredType = Literal["integer", "decimal", "text", "empty"]


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ColumnProfile(_Record):
    """One column as it appears in the file, plus what can be measured about it without guessing."""

    position: int = Field(ge=0)
    #: The header cell verbatim, including any unit annotation.
    header: str
    #: The unit as the header declares it, e.g. `at.%` from `Au [at.%]`. None when the header
    #: carries no bracketed annotation. Never inferred from the values.
    declared_unit: str | None
    #: The header with its unit annotation removed, for grouping equivalent columns across files.
    bare_name: str
    inferred_type: InferredType
    non_empty_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    distinct_count: int = Field(ge=0)
    #: Extremes as source text. Ordered numerically when the column is numeric, lexically otherwise.
    minimum: str | None
    maximum: str | None


class TableProfile(_Record):
    """One delimited text member of an archive."""

    member_path: str
    byte_size: int = Field(ge=0)
    sha256: str
    has_utf8_bom: bool
    line_ending: LineEnding
    delimiter: str
    #: Data rows, excluding the header.
    row_count: int = Field(ge=0)
    column_count: int = Field(ge=0)
    duplicate_row_count: int = Field(ge=0)
    ragged_row_count: int = Field(ge=0)
    columns: tuple[ColumnProfile, ...]


class MemberGroup(_Record):
    """Members sharing one filename grammar.

    The shape replaces every digit run and signed decimal with a placeholder, so a set of
    per-location files collapses to one template. This is how the inventory reports the relationship
    among source files with no pattern written into the code in advance.
    """

    shape: str
    member_count: int = Field(ge=0)
    example: str


class ArchiveProfile(_Record):
    archive_filename: str
    archive_sha256: str
    member_count: int = Field(ge=0)
    #: Members deliberately not profiled, with the reason, so the omission is visible.
    skipped_members: tuple[str, ...]
    groups: tuple[MemberGroup, ...]
    tables: tuple[TableProfile, ...]


class DatasetInventory(_Record):
    """The versioned inventory Gate 0 requires before any dataset-specific code is written."""

    schema_version: str = DATASET_INVENTORY_SCHEMA_VERSION
    generated_at: datetime
    tool_version: str
    #: SHA-256 of the provenance.json that recorded the acquisition, tying this inventory to the
    #: exact bytes it describes.
    provenance_sha256: str | None
    archives: tuple[ArchiveProfile, ...]

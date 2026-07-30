"""Inspect acquired archives and report what they actually contain.

Nothing here knows a column name, a table name, or a filename pattern in advance. The module reads
the bytes and describes them, which is the only reading of AI_CONTRACT.md section 7 that holds:
"Column names, file paths, types, and units MUST NOT be copied from memory or inferred solely from
article prose."

Two consequences follow from that rule and are worth stating, because they look like omissions:

* numeric extremes are reported as the source text, never coerced to float, so the inventory records
  what the file held rather than what Python would render;
* a declared unit is read from a bracketed header annotation only. A column with no annotation has
  `declared_unit=None`; no unit is ever inferred from the values.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final

from .dataset import (
    ArchiveProfile,
    ColumnProfile,
    DatasetInventory,
    InferredType,
    LineEnding,
    MemberGroup,
    TableProfile,
)

TEXT_SUFFIXES: Final = frozenset({".csv", ".txt", ".dat", ".tsv"})
#: Resource forks a macOS zip adds. Not source data; skipped and reported.
_RESOURCE_FORK: Final = "__MACOSX/"
_UNIT_IN_HEADER: Final = re.compile(r"^(?P<name>.*?)\s*\[(?P<unit>[^\]]+)\]\s*$")
_NUMBER_RUN: Final = re.compile(r"[-+]?\d+(?:\.\d+)?")
_SAMPLE_BYTES: Final = 4096


def _line_ending(raw: bytes) -> LineEnding:
    crlf = raw.count(b"\r\n")
    cr = raw.count(b"\r") - crlf
    lf = raw.count(b"\n") - crlf
    present = [name for name, count in (("CRLF", crlf), ("CR", cr), ("LF", lf)) if count]
    if not present:
        return "none"
    return present[0] if len(present) == 1 else "mixed"  # type: ignore[return-value]


def _sniff_delimiter(text: str) -> str:
    try:
        return csv.Sniffer().sniff(text[:_SAMPLE_BYTES], delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def _split_unit(header: str) -> tuple[str, str | None]:
    match = _UNIT_IN_HEADER.match(header)
    if match:
        return match.group("name").strip(), match.group("unit").strip()
    return header.strip(), None


def _as_decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def _infer_type(values: Sequence[str]) -> InferredType:
    present = [v for v in values if v != ""]
    if not present:
        return "empty"
    numbers = [_as_decimal(v) for v in present]
    if any(n is None for n in numbers):
        return "text"
    return "integer" if all(v.lstrip("+-").isdigit() for v in present) else "decimal"


def _extremes(values: Sequence[str], inferred: InferredType) -> tuple[str | None, str | None]:
    """Extremes as source text: ordered numerically for numbers, lexically otherwise."""
    present = [v for v in values if v != ""]
    if not present:
        return None, None
    if inferred in ("integer", "decimal"):
        keyed = [(d, v) for v in present if (d := _as_decimal(v)) is not None]
        if keyed:
            return min(keyed)[1], max(keyed)[1]
    return min(present), max(present)


def _profile_columns(
    header: Sequence[str], rows: Sequence[Sequence[str]]
) -> tuple[ColumnProfile, ...]:
    profiles: list[ColumnProfile] = []
    for index, cell in enumerate(header):
        column = [row[index] if index < len(row) else "" for row in rows]
        inferred = _infer_type(column)
        low, high = _extremes(column, inferred)
        bare, unit = _split_unit(cell)
        profiles.append(
            ColumnProfile(
                position=index,
                header=cell,
                declared_unit=unit,
                bare_name=bare,
                inferred_type=inferred,
                non_empty_count=sum(1 for v in column if v != ""),
                missing_count=sum(1 for v in column if v == ""),
                distinct_count=len({v for v in column if v != ""}),
                minimum=low,
                maximum=high,
            )
        )
    return tuple(profiles)


def profile_table(member_path: str, raw: bytes) -> TableProfile:
    """Describe one delimited text member. Never raises on malformed content; it reports it."""
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig", errors="replace")
    delimiter = _sniff_delimiter(text)
    # `newline=None` applies universal newlines, so a CR-only file parses like any other.
    parsed = [
        row for row in csv.reader(io.StringIO(text, newline=None), delimiter=delimiter) if row
    ]
    header: list[str] = parsed[0] if parsed else []
    rows = parsed[1:]
    return TableProfile(
        member_path=member_path,
        byte_size=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        has_utf8_bom=has_bom,
        line_ending=_line_ending(raw),
        delimiter=delimiter,
        row_count=len(rows),
        column_count=len(header),
        duplicate_row_count=len(rows) - len({tuple(r) for r in rows}),
        ragged_row_count=sum(1 for r in rows if len(r) != len(header)),
        columns=_profile_columns(header, rows),
    )


def filename_shape(member_path: str) -> str:
    """Collapse every number in a path to `#`, revealing the naming grammar behind a file set."""
    return _NUMBER_RUN.sub("#", member_path)


def profile_archive(archive: Path) -> ArchiveProfile:
    """Describe one zip archive: its members, their filename grammar, and every text table."""
    raw_archive = archive.read_bytes()
    skipped: list[str] = []
    tables: list[TableProfile] = []
    shapes: Counter[str] = Counter()
    examples: dict[str, str] = {}

    with zipfile.ZipFile(io.BytesIO(raw_archive)) as zf:
        members = [info for info in zf.infolist() if not info.is_dir()]
        for info in members:
            name = info.filename
            if name.startswith(_RESOURCE_FORK) or f"/{_RESOURCE_FORK}" in name:
                skipped.append(f"{name} (macOS resource fork)")
                continue
            shape = filename_shape(name)
            shapes[shape] += 1
            examples.setdefault(shape, name)
            if Path(name).suffix.lower() not in TEXT_SUFFIXES:
                skipped.append(f"{name} (not a delimited text file)")
                continue
            tables.append(profile_table(name, zf.read(info)))

    return ArchiveProfile(
        archive_filename=archive.name,
        archive_sha256=hashlib.sha256(raw_archive).hexdigest(),
        member_count=len(members),
        skipped_members=tuple(skipped),
        groups=tuple(
            MemberGroup(shape=shape, member_count=count, example=examples[shape])
            for shape, count in sorted(shapes.items())
        ),
        tables=tuple(tables),
    )


def build_inventory(
    landing_root: Path,
    *,
    clock: Callable[[], datetime],
    tool_version: str,
    provenance_sha256: str | None,
) -> DatasetInventory:
    """Inspect every archive in the landing root, in a stable order."""
    archives = sorted(landing_root.glob("*.zip"))
    return DatasetInventory(
        generated_at=clock(),
        tool_version=tool_version,
        provenance_sha256=provenance_sha256,
        archives=tuple(profile_archive(path) for path in archives),
    )

"""The inspector reports what an archive holds, and knows no column or filename in advance.

Every fixture here is a synthetic zip built in `tmp_path`. The column names deliberately do not
resemble the real archive's, so a test cannot pass by encoding a remembered schema.
"""

from __future__ import annotations

import zipfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from labbridge.infrastructure.her_ingestion.inspect import (
    build_inventory,
    filename_shape,
    profile_archive,
    profile_table,
)

LF_TABLE = b"Ident,Alpha [pz],Beta [pz]\n1,10.5,2\n2,11.5,3\n3,,4\n"
CR_TABLE = b"Ident,Alpha [pz]\r1,10.5\r2,11.5\r"
CRLF_TABLE = b"Ident,Alpha [pz]\r\n1,10.5\r\n2,11.5\r\n"
EXPECTED_ROWS = 3
EXPECTED_COLUMNS = 3
NON_EMPTY_IN_ALPHA = 2
ROWS_IN_TWO_LINE_TABLE = 2
MEMBERS_IN_GROUPED_ARCHIVE = 4
SHA256_HEX_CHARS = 64


def _archive(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)
    return path


def test_profile_table_reads_headers_units_and_shape() -> None:
    profile = profile_table("pack/table.csv", LF_TABLE)

    assert profile.row_count == EXPECTED_ROWS
    assert profile.column_count == EXPECTED_COLUMNS
    assert profile.delimiter == ","
    assert profile.line_ending == "LF"
    assert [c.header for c in profile.columns] == ["Ident", "Alpha [pz]", "Beta [pz]"]
    assert [c.bare_name for c in profile.columns] == ["Ident", "Alpha", "Beta"]
    assert [c.declared_unit for c in profile.columns] == [None, "pz", "pz"]


def test_a_header_without_a_bracketed_annotation_has_no_declared_unit() -> None:
    """No unit is ever inferred from the values."""
    profile = profile_table("pack/table.csv", LF_TABLE)

    assert profile.columns[0].declared_unit is None


def test_missing_values_are_counted_not_filled() -> None:
    profile = profile_table("pack/table.csv", LF_TABLE)
    alpha = profile.columns[1]

    assert alpha.missing_count == 1
    assert alpha.non_empty_count == NON_EMPTY_IN_ALPHA


def test_extremes_are_source_text_ordered_numerically() -> None:
    """`10.5` must not become `10.50`, and must sort below `11.5` rather than lexically above it."""
    profile = profile_table("pack/table.csv", LF_TABLE)
    alpha = profile.columns[1]

    assert alpha.inferred_type == "decimal"
    assert alpha.minimum == "10.5"
    assert alpha.maximum == "11.5"


def test_an_identifier_column_reports_its_range_and_distinct_count() -> None:
    profile = profile_table("pack/table.csv", LF_TABLE)
    ident = profile.columns[0]

    assert ident.inferred_type == "integer"
    assert (ident.minimum, ident.maximum) == ("1", "3")
    assert ident.distinct_count == EXPECTED_ROWS


def test_cr_only_line_endings_are_detected_and_parsed() -> None:
    """The real archive mixes CR, LF and CRLF; a CR-only file must not collapse into one row."""
    profile = profile_table("pack/legacy.csv", CR_TABLE)

    assert profile.line_ending == "CR"
    assert profile.row_count == ROWS_IN_TWO_LINE_TABLE


def test_crlf_is_reported_as_crlf_not_as_cr_plus_lf() -> None:
    profile = profile_table("pack/dos.csv", CRLF_TABLE)

    assert profile.line_ending == "CRLF"
    assert profile.row_count == ROWS_IN_TWO_LINE_TABLE


def test_duplicate_and_ragged_rows_are_counted() -> None:
    raw = b"Ident,Alpha\n1,2\n1,2\n3\n"
    profile = profile_table("pack/messy.csv", raw)

    assert profile.duplicate_row_count == 1
    assert profile.ragged_row_count == 1


def test_filename_shape_collapses_numbers_so_a_file_set_becomes_one_template() -> None:
    a = "pack/run_area_100_x=-22.50_y=-13.50_curve.csv"
    b = "pack/run_area_7_x=0.00_y=4.50_curve.csv"

    assert filename_shape(a) == filename_shape(b)
    assert "#" in filename_shape(a)


def test_profile_archive_groups_members_by_filename_grammar(tmp_path: Path) -> None:
    """This is how the inventory reports a per-location file set without a pattern in the code."""
    archive = _archive(
        tmp_path / "pack.zip",
        {
            "pack/run_area_1_curve.csv": LF_TABLE,
            "pack/run_area_2_curve.csv": LF_TABLE,
            "pack/run_area_3_curve.csv": LF_TABLE,
            "pack/summary.csv": LF_TABLE,
        },
    )

    profile = profile_archive(archive)

    grouped = {g.shape: g.member_count for g in profile.groups}
    assert sorted(grouped.values()) == [1, 3]
    assert len(profile.tables) == MEMBERS_IN_GROUPED_ARCHIVE


def test_macos_resource_forks_are_skipped_and_reported(tmp_path: Path) -> None:
    archive = _archive(
        tmp_path / "pack.zip",
        {"pack/table.csv": LF_TABLE, "__MACOSX/pack/._table.csv": b"\x00\x01"},
    )

    profile = profile_archive(archive)

    assert [t.member_path for t in profile.tables] == ["pack/table.csv"]
    assert any("resource fork" in s for s in profile.skipped_members)


def test_a_non_text_member_is_skipped_with_its_reason(tmp_path: Path) -> None:
    archive = _archive(
        tmp_path / "pack.zip", {"pack/image.png": b"\x89PNG", "pack/t.csv": LF_TABLE}
    )

    profile = profile_archive(archive)

    assert [t.member_path for t in profile.tables] == ["pack/t.csv"]
    assert any("not a delimited text file" in s for s in profile.skipped_members)


def test_archive_and_member_checksums_are_recorded(tmp_path: Path) -> None:
    """The inventory must tie to exact bytes, not to a filename."""
    archive = _archive(tmp_path / "pack.zip", {"pack/table.csv": LF_TABLE})

    profile = profile_archive(archive)

    assert len(profile.archive_sha256) == SHA256_HEX_CHARS
    assert len(profile.tables[0].sha256) == SHA256_HEX_CHARS


def test_build_inventory_covers_every_archive_in_a_stable_order(
    tmp_path: Path, fixed_clock: Callable[[], datetime]
) -> None:
    _archive(tmp_path / "beta.zip", {"b/t.csv": LF_TABLE})
    _archive(tmp_path / "alpha.zip", {"a/t.csv": LF_TABLE})

    inventory = build_inventory(
        tmp_path, clock=fixed_clock, tool_version="0.1.0", provenance_sha256="a" * 64
    )

    assert [a.archive_filename for a in inventory.archives] == ["alpha.zip", "beta.zip"]
    assert inventory.generated_at == fixed_clock()
    assert inventory.provenance_sha256 == "a" * 64


def test_inventory_without_a_provenance_reference_says_so(
    tmp_path: Path, fixed_clock: Callable[[], datetime]
) -> None:
    """An inventory not tied to a recorded acquisition must be visibly untied, not silently fine."""
    _archive(tmp_path / "alpha.zip", {"a/t.csv": LF_TABLE})

    inventory = build_inventory(
        tmp_path, clock=fixed_clock, tool_version="0.1.0", provenance_sha256=None
    )

    assert inventory.provenance_sha256 is None

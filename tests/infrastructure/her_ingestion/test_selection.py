"""File selection is explicit by name, with a size ceiling that must be opted out of loudly."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import pytest

from helpers import build_file_entry, build_payload
from labbridge.infrastructure.her_ingestion.errors import FileSelectionError, FileTooLargeError
from labbridge.infrastructure.her_ingestion.records import ArchiveInventory
from labbridge.infrastructure.her_ingestion.zenodo import parse_record, select_files

LARGE = 4096


def _inventory(fixed_clock: Callable[[], datetime]) -> ArchiveInventory:
    payload = build_payload(
        files=[
            build_file_entry("alpha_table.csv", b"a,b\n"),
            build_file_entry("beta_signal.txt", b"0.1 0.2\n"),
            build_file_entry("large_blob.bin", b"z", size=LARGE * 4),
        ]
    )
    return parse_record(payload, retrieved_at=fixed_clock())


def test_selection_returns_only_the_named_files(fixed_clock: Callable[[], datetime]) -> None:
    selected = select_files(
        _inventory(fixed_clock), filenames=["beta_signal.txt"], max_bytes=LARGE, allow_large=[]
    )

    assert [remote.filename for remote in selected] == ["beta_signal.txt"]


def test_selection_preserves_the_requested_order(fixed_clock: Callable[[], datetime]) -> None:
    selected = select_files(
        _inventory(fixed_clock),
        filenames=["beta_signal.txt", "alpha_table.csv"],
        max_bytes=LARGE,
        allow_large=[],
    )

    assert [remote.filename for remote in selected] == ["beta_signal.txt", "alpha_table.csv"]


def test_no_names_selects_nothing_rather_than_everything(
    fixed_clock: Callable[[], datetime],
) -> None:
    """docs/DATA_STRATEGY.md section 2.4: "download only explicitly selected files"."""
    assert (
        select_files(_inventory(fixed_clock), filenames=[], max_bytes=LARGE, allow_large=()) == ()
    )


def test_unknown_requested_filename_raises_and_lists_what_is_available(
    fixed_clock: Callable[[], datetime],
) -> None:
    """A typo must be an error. Silently skipping would let the operator believe it downloaded."""
    with pytest.raises(FileSelectionError) as caught:
        select_files(
            _inventory(fixed_clock), filenames=["alpha_tabel.csv"], max_bytes=LARGE, allow_large=[]
        )

    assert caught.value.requested == "alpha_tabel.csv"
    assert "alpha_table.csv" in caught.value.available


def test_file_over_the_size_ceiling_is_refused_unless_explicitly_allowed(
    fixed_clock: Callable[[], datetime],
) -> None:
    """The ceiling is how "unless explicitly requested" is honoured without naming any file."""
    inventory = _inventory(fixed_clock)

    with pytest.raises(FileTooLargeError) as caught:
        select_files(inventory, filenames=["large_blob.bin"], max_bytes=LARGE, allow_large=[])

    assert caught.value.filename == "large_blob.bin"
    assert caught.value.limit_bytes == LARGE

    allowed = select_files(
        inventory, filenames=["large_blob.bin"], max_bytes=LARGE, allow_large=["large_blob.bin"]
    )
    assert [remote.filename for remote in allowed] == ["large_blob.bin"]

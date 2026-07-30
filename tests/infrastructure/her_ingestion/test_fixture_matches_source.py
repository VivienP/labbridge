"""The fixture's schemas must equal the archive's, checked against the real inspected inventory.

Marked `data`: it needs the fetched HER dataset on disk, so it is excluded from the default offline
run. Everything else about the fixture is provable offline; only *this* claim — that the fixture is
schema-compatible with the actual source — needs the source.

The comparison runs the same inspector over both trees and compares the inventories it produces.
Nothing is asserted against a schema written here by hand.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from labbridge.infrastructure.her_ingestion.fixture import FixtureSpec, build_fixture
from labbridge.infrastructure.her_ingestion.inspect import build_inventory
from labbridge.infrastructure.her_ingestion.provenance import DATASET_INVENTORY_FILENAME

pytestmark = pytest.mark.data

SOURCE_INVENTORY = Path("data/her/raw") / DATASET_INVENTORY_FILENAME

Schema = tuple[tuple[str, ...], str, str]


def _schemas_from_document(payload: dict[str, object]) -> set[Schema]:
    found: set[Schema] = set()
    for archive in payload["archives"]:  # type: ignore[union-attr]
        for table in archive["tables"]:
            found.add(
                (
                    tuple(column["header"] for column in table["columns"]),
                    table["line_ending"],
                    table["delimiter"],
                )
            )
    return found


@pytest.fixture
def source_schemas() -> set[Schema]:
    if not SOURCE_INVENTORY.exists():
        pytest.skip(
            f"{SOURCE_INVENTORY} absent: run `labbridge fetch-her` then `labbridge inspect-her`"
        )
    return _schemas_from_document(json.loads(SOURCE_INVENTORY.read_text(encoding="utf-8")))


def test_the_fixture_reproduces_every_source_schema_and_invents_none(
    tmp_path: Path, source_schemas: set[Schema]
) -> None:
    build_fixture(tmp_path, spec=FixtureSpec(), generator_version="0.1.0")
    inventory = build_inventory(
        tmp_path,
        clock=lambda: datetime.now(UTC),
        tool_version="0.1.0",
        provenance_sha256=None,
    )
    fixture_schemas = _schemas_from_document(inventory.model_dump(mode="json"))

    assert fixture_schemas == source_schemas


def test_the_fixture_shares_no_row_with_the_source(source_schemas: set[Schema]) -> None:
    """Independence is the point. A shared data row would mean archive values were copied in."""
    fixture_inventory = Path("data/her/fixture") / DATASET_INVENTORY_FILENAME
    if not fixture_inventory.exists():
        pytest.skip("run `labbridge build-her-fixture` then `labbridge inspect-her` on it")

    source = json.loads(SOURCE_INVENTORY.read_text(encoding="utf-8"))
    fixture = json.loads(fixture_inventory.read_text(encoding="utf-8"))

    def digests(payload: dict[str, object]) -> set[str]:
        return {
            table["sha256"]
            for archive in payload["archives"]  # type: ignore[union-attr]
            for table in archive["tables"]
        }

    assert not digests(source) & digests(fixture)

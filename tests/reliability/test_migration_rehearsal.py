"""The rehearsal must reach the unique current Alembic head, not a stale parent."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from labbridge.reliability.migration_rehearsal import (
    EVENT_STREAM_CONTRACT_REVISION,
    PREVIOUS_REVISION,
    expected_upgrade_revision,
)

ROOT = Path(__file__).resolve().parents[2]


def _script() -> ScriptDirectory:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return ScriptDirectory.from_config(config)


def test_rehearsal_targets_the_unique_current_alembic_head() -> None:
    script = _script()
    heads = script.get_heads()
    ancestry = {revision.revision for revision in script.walk_revisions("base", heads[0])}

    assert heads == [expected_upgrade_revision(ROOT)]
    assert EVENT_STREAM_CONTRACT_REVISION in ancestry
    assert PREVIOUS_REVISION in ancestry
    assert expected_upgrade_revision(ROOT) != EVENT_STREAM_CONTRACT_REVISION


def test_split_alembic_heads_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ScriptDirectory, "get_heads", lambda self: ["aaaa1111", "bbbb2222"])

    with pytest.raises(RuntimeError, match="unique Alembic head"):
        expected_upgrade_revision(ROOT)


def test_missing_event_stream_contract_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "labbridge.reliability.migration_rehearsal.EVENT_STREAM_CONTRACT_REVISION",
        "missing00000000",
    )

    with pytest.raises(RuntimeError, match="event-stream contract"):
        expected_upgrade_revision(ROOT)

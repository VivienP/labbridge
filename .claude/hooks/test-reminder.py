#!/usr/bin/env python3
"""PostToolUse reminder: nudge for the test layer a LabBridge edit actually needs.

Non-blocking. Reads the hook JSON on stdin and emits at most one systemMessage:

  - a durability-bearing module (infrastructure, worker, application, alembic) was edited, and
    the reminder names the layer its guarantee must be proven at. An offline unit test does not
    prove a transaction, a constraint, or a process boundary (AI_CONTRACT.md section 9);
  - a science-bearing module (environments, evidence, domain provenance/quantities) was edited,
    and the reminder names the integrity properties that need a test;
  - any src/labbridge module was edited and no test file for it exists yet.

Fails silent on anything unexpected: a reminder hook must never break a turn.
"""

from __future__ import annotations

import json
import os
import re
import sys

SKIP_MODULES = {"__init__", "py"}

DURABILITY = re.compile(
    r"(^|/)src/labbridge/(infrastructure|worker|application)/"
    r"|(^|/)src/labbridge/domain/(events|budgets)\.py$"
    r"|(^|/)(alembic|migrations)/"
)
SCIENCE = re.compile(
    r"(^|/)src/labbridge/(environments|evidence)/"
    r"|(^|/)src/labbridge/domain/(provenance|quantities)\.py$"
    r"|(^|/)scripts/(fetch|inspect)_her\.py$"
)
# Repository-relative anchor, so the message shows a clickable path regardless of how the host
# spells the project root (drive letter, MSYS prefix, symlink).
REPO_ANCHOR = re.compile(r"(?:^|/)((?:src/labbridge|tests|scripts|alembic|migrations)/.*)$")

DURABILITY_MSG = (
    "Reminder (proof layer): {path} is durability-bearing. An offline unit test does not prove "
    "a transaction, a unique constraint, a lease, or a process boundary. AI_CONTRACT.md "
    "section 9: a test that mocks away the database transaction, object store, or process "
    "boundary does not prove the corresponding operational guarantee. Mark the proof "
    "@pytest.mark.integration, run it against real PostgreSQL/MinIO, and check the relevant "
    "docs/FAILURE_MATRIX.md rows."
)
SCIENCE_MSG = (
    "Reminder (scientific integrity): {path} carries scientific records. Check that "
    "data_origin and execution_mode propagate, that received bytes are retained even when "
    "rejected (ADR-005), that no raw record is mutated (ADR-006), that lineage closes to an "
    "observed source or a synthetic seed, and that any HER column or path comes from the "
    "inspection inventory rather than memory."
)
MISSING_TEST_MSG = (
    "Reminder (test-first): edited {path} but {expected} does not exist. LabBridge is "
    "test-first; add or adjust the test alongside the change (AI_CONTRACT.md sections 8 and 9)."
)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return

    raw = str((data.get("tool_input") or {}).get("file_path") or "").replace("\\", "/")
    if not raw.endswith(".py"):
        return

    root = (os.environ.get("CLAUDE_PROJECT_DIR") or str(data.get("cwd") or os.getcwd())).replace(
        "\\", "/"
    )
    anchored = REPO_ANCHOR.search(raw)
    path = anchored.group(1) if anchored else raw

    message = _message_for(path, root)
    if message:
        print(json.dumps({"systemMessage": message, "suppressOutput": True}))


def _message_for(path: str, root: str) -> str | None:
    if DURABILITY.search(path):
        return DURABILITY_MSG.format(path=path)
    if SCIENCE.search(path):
        return SCIENCE_MSG.format(path=path)
    if not path.startswith("src/labbridge/"):
        return None
    module = os.path.basename(path)[:-3]
    if module in SKIP_MODULES:
        return None
    expected = f"tests/test_{module}.py"
    if os.path.exists(os.path.join(root, expected)):
        return None
    return MISSING_TEST_MSG.format(path=path, expected=expected)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""SubagentStop hook: append one audit line per subagent invocation.

Purpose: make review-lens usage observable. The most likely failure of a configured agent is not a
crash but silent non-triggering — a description that never matches. A usage log surfaces that, and
`.claude/hooks/guard_bash.py` reads it to remind at commit time when a durability- or
science-bearing surface is staged with no lens run.

Writes JSON Lines to `.claude/logs/agent-usage.jsonl` (git-ignored), so the state lives in the
repo tree but never in history and survives session restarts. Records the fields that are
present and never fails a turn: any error exits 0 silently.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return

    root = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
    log_dir = os.path.join(root, ".claude", "logs")

    entry = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "event": data.get("hook_event_name") or "SubagentStop",
        # agent_type is the frontmatter `name` of the subagent that just finished.
        "agent_type": data.get("agent_type"),
        "session_id": data.get("session_id"),
    }

    try:
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "agent-usage.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        return


if __name__ == "__main__":
    main()

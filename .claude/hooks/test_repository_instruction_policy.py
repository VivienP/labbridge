from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[2]
SHARED_PATHS = (
    "AGENTS.md",
    "CLAUDE.md",
    "AI_CONTRACT.md",
    "docs/DEVELOPMENT_WORKFLOW.md",
    ".claude/settings.json",
    ".claude/agents/reviewer.md",
    ".claude/commands/verify.md",
    ".claude/hooks/guard_bash.py",
    ".claude/skills/verification-before-completion/SKILL.md",
    ".claude/tools/check_agent_system.py",
    ".agents/skills/verification-before-completion/SKILL.md",
)
LOCAL_STATE_SAMPLES = (
    ".claude/settings.local.json",
    ".claude/logs/agent-usage.jsonl",
    ".claude/state/session.json",
    ".agents/settings.local.json",
    ".agents/logs/session.jsonl",
    ".agents/state/session.json",
    ".codex/settings.local.json",
    ".codex/logs/session.jsonl",
)


def _check_ignore(path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "check-ignore", "--no-index", "-v", "--", path],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def test_shared_instruction_paths_are_visible_to_git() -> None:
    for path in SHARED_PATHS:
        assert (ROOT / path).is_file(), path
        result = _check_ignore(path)
        assert result.returncode == 1, f"{path} is ignored by {result.stdout.strip()}"


def test_machine_local_agent_state_is_ignored() -> None:
    for path in LOCAL_STATE_SAMPLES:
        result = _check_ignore(path)
        assert result.returncode == 0, path

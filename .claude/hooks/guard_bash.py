#!/usr/bin/env python3
"""PreToolUse guard for Bash commands in the LabBridge repository.

Denies, before the shell runs, the command classes that AI_CONTRACT.md section 11 forbids and that
cannot be undone by a later review:

  1. `git commit --no-verify` / `-n` — bypasses the repository gate (format, lint, types,
     offline tests, agent-system check, forbidden-file and secret scan).
  2. `git commit` while the Git gate is not wired (`core.hooksPath != scripts/hooks`), which
     would let a commit land with no check at all. Denied with the one-line fix.
  3. `git commit` with a forbidden file staged: `.env*`, a private key, or a runtime log.
     `.gitignore` is the first defence; this catches a `git add -f` slip or a tracked file.
  4. `git push --force` / `--force-with-lease` / `-f`, and `git reset --hard`, `git clean -f`,
     `git checkout -- .`, `git restore .` — destructive commands with a safer alternative.
  5. A write or truncation targeting a `.env*` file.
  6. `rm -rf` / `rm -fr` with a broad target.

It also emits a NON-blocking reminder (never a deny) when a commit stages a durability-bearing
or science-bearing module but no review lens ran in this session, per
`.claude/logs/agent-usage.jsonl`. That surfaces a skipped review at the one moment it matters;
the commit still proceeds.

Only Bash tool calls are inspected. On any internal error the guard stays silent (exit 0, no
decision) so a guard bug can never wedge the session.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

# --- staged files that must never enter history ------------------------------------------------
FORBIDDEN_STAGED = re.compile(
    r"(^|/)\.env($|\.)"
    r"|(^|/)[^/]*\.(pem|key|p12|pfx)$"
    r"|(^|/)(id_rsa|id_ed25519)$"
    r"|(^|/)\.(claude|agents|codex)/(settings\.local\.json|logs/|state/)",
    re.IGNORECASE,
)

# --- durability- and science-bearing surfaces that warrant a review lens ------------------------
REVIEWED_SURFACE = re.compile(
    r"(^|/)src/labbridge/(infrastructure|worker|application|environments|evidence)/"
    r"|(^|/)src/labbridge/domain/(events|budgets|provenance|quantities)\.py$"
    r"|(^|/)(alembic|migrations)/",
)
REVIEW_LENSES = {
    "reviewer",
    "data-integrity-reviewer",
    "reliability-reviewer",
    "verification-auditor",
}

# --- command patterns ---------------------------------------------------------------------------
# `commit` must be the git sub-command word, so `git diff` next to the text "pre-commit" does not
# match.
GIT_COMMIT = re.compile(r"\bgit\b(?:\s+-[^\s]+(?:\s+[^\s-][^\s]*)?)*\s+commit\b")
# --no-verify / -n only as a real flag (avoids matching `grep -n`, `head -n`).
NO_VERIFY = re.compile(r"(?:^|\s)(?:--no-verify|-n)(?=\s|$)")
FORCE_PUSH = re.compile(
    r"\bgit\b[^\n;&|]*\bpush\b[^\n;&|]*(?:--force(?:-with-lease)?|(?:^|\s)-f(?=\s|$))"
)
RESET_HARD = re.compile(r"\bgit\b[^\n;&|]*\breset\b[^\n;&|]*--hard")
GIT_CLEAN_FORCE = re.compile(r"\bgit\b[^\n;&|]*\bclean\b[^\n;&|]*(?:^|\s)-[a-zA-Z]*[fd]")
GIT_DISCARD_ALL = re.compile(
    r"\bgit\b[^\n;&|]*\b(?:checkout|restore)\b[^\n;&|]*(?:--\s+\.|\s\.\s*$)"
)
ENV_WRITE = re.compile(
    r"(?:>|>>|\btee\b|\bcp\b[^\n;&|]*|\bmv\b[^\n;&|]*)\s*[^\s;&|]*\.env(?:\.[^\s;&|]*)?(?:\s|$)"
)
RM_RECURSIVE = re.compile(
    r"\brm\b[^\n;&|]*\s-[a-zA-Z]*r[a-zA-Z]*f|\brm\b[^\n;&|]*\s-[a-zA-Z]*f[a-zA-Z]*r"
)

CONTRACT = "AI_CONTRACT.md section 11"
# How many staged surfaces to name in the reminder before truncating.
MAX_LISTED_SURFACES = 4


def deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


def _git(args: list[str], cwd: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5, check=False
        ).stdout.strip()
    except Exception:
        return ""


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return

    if data.get("tool_name") != "Bash":
        return
    command = str((data.get("tool_input") or {}).get("command") or "")
    if not command:
        return
    cwd = str(data.get("cwd") or ".")

    if ENV_WRITE.search(command):
        deny(
            "Blocked: this command writes to a .env file. Writing secrets or modifying "
            f"`.env*` is forbidden ({CONTRACT}). Ask the author to change it, or use an "
            "example file with no real values."
        )

    if FORCE_PUSH.search(command):
        deny(
            "Blocked: force-push. Pushing at all requires the author's explicit approval, and "
            "a force-push additionally requires an explicit request "
            f"({CONTRACT}, skill git-commit-rules)."
        )

    if (
        RESET_HARD.search(command)
        or GIT_CLEAN_FORCE.search(command)
        or GIT_DISCARD_ALL.search(command)
    ):
        deny(
            "Blocked: destructive Git command. `git reset --hard`, `git clean -f`, and "
            "discarding the worktree can delete unrelated author changes. Use a safer "
            "alternative — `git stash`, a targeted `git restore <file>`, or an explicit "
            f"revert commit ({CONTRACT})."
        )

    if RM_RECURSIVE.search(command):
        deny(
            "Blocked: recursive force delete. Use a targeted removal, or move the path aside, "
            f"so the operation is reversible ({CONTRACT}: never use a broad destructive "
            "command where a safer alternative exists)."
        )

    if not GIT_COMMIT.search(command):
        return

    if NO_VERIFY.search(command):
        deny(
            "Commit blocked: --no-verify / -n bypasses the LabBridge Git gate (ruff, mypy, "
            "offline tests, agent-system check, forbidden-file and secret scan). Fix the "
            "failing check instead of skipping it. Bypassing a hook requires explicit "
            f"authorisation ({CONTRACT})."
        )

    hooks_path = _git(["config", "--get", "core.hooksPath"], cwd)
    if hooks_path.replace("\\", "/").rstrip("/") != "scripts/hooks":
        deny(
            "Commit blocked: the Git pre-commit gate is not wired. Enable it first:\n"
            "  git config core.hooksPath scripts/hooks\n"
            "This keeps every commit gated and the history bisectable."
        )

    staged = [
        f.replace("\\", "/")
        for f in _git(["diff", "--cached", "--name-only"], cwd).splitlines()
        if f
    ]
    bad = [f for f in staged if FORBIDDEN_STAGED.search(f)]
    if bad:
        listing = "\n".join(f"  - {f}" for f in bad)
        deny(
            "Commit blocked: forbidden file(s) staged (skill git-commit-rules, 'Never commit'):\n"
            f"{listing}\n"
            "Unstage with: git restore --staged <file>"
        )

    _warn_if_review_skipped(staged, str(data.get("session_id") or ""), cwd)


def _warn_if_review_skipped(staged: list[str], session_id: str, cwd: str) -> None:
    """Non-blocking reminder when a durability- or science-bearing module is staged unreviewed."""
    surfaces = sorted(f for f in staged if REVIEWED_SURFACE.search(f))
    if not surfaces:
        return
    if _lenses_run_this_session(session_id, cwd):
        return

    listing = ", ".join(surfaces[:MAX_LISTED_SURFACES])
    if len(surfaces) > MAX_LISTED_SURFACES:
        listing += " ..."
    print(
        json.dumps(
            {
                "systemMessage": (
                    f"Reminder: staged {listing} — a durability- or science-bearing surface — "
                    "but no review lens ran this session. Consider @reviewer, plus "
                    "@data-integrity-reviewer or @reliability-reviewer where they apply, and "
                    "/verify before claiming completion. Non-blocking: the commit proceeds."
                ),
                "suppressOutput": True,
            }
        )
    )


def _lenses_run_this_session(session_id: str, cwd: str) -> set[str]:
    root = os.environ.get("CLAUDE_PROJECT_DIR") or cwd or os.getcwd()
    log_path = os.path.join(root, ".claude", "logs", "agent-usage.jsonl")
    ran: set[str] = set()
    try:
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if (not session_id or rec.get("session_id") == session_id) and rec.get(
                    "agent_type"
                ) in REVIEW_LENSES:
                    ran.add(str(rec.get("agent_type")))
    except FileNotFoundError:
        return set()
    except Exception:
        return set()
    return ran


if __name__ == "__main__":
    main()

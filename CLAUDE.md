# CLAUDE.md

Claude Code uses the repository-wide instructions in [`AGENTS.md`](AGENTS.md) and the normative
engineering rules in [`AI_CONTRACT.md`](AI_CONTRACT.md). This file contains only Claude-specific
guidance shared by all repository users.

## Project configuration

- `.claude/settings.json` defines shared permissions and hook wiring.
- `.claude/agents/` contains review and implementation lenses.
- `.claude/commands/` contains repository workflows.
- `.claude/skills/` contains reusable disciplines mirrored into `.agents/skills/`.
- `.claude/hooks/` and `.claude/tools/` contain shared guards and verification helpers.
- `.claude/settings.local.json`, `.claude/logs/`, caches, and session state are machine-local and must
  remain ignored.

Invoke a named review lens when that lens represents a material risk in the change. Reviewer lenses
are read-only. Use the default workflow for non-trivial implementation:

```text
/plan-slice → /implement → /review → /verify
```

When a coherent worktree task is complete and finalization is explicitly authorised, invoke
`$finish-worktree`. It follows [`docs/DEVELOPMENT_WORKFLOW.md`](docs/DEVELOPMENT_WORKFLOW.md), pushes a
task branch rather than `main`, and opens but never merges the pull request. `/prepare-commit` remains
the narrower workflow for preparing a proposed commit without completing branch integration.

Run `python .claude/tools/gates.py` to inspect the available verification gates and never report a
scaffolded or deferred gate as passing.

Claude hooks do not replace repository tests or the public Git hook under `scripts/hooks/`.

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

When a roadmap phase or other substantial coherent implementation is complete, invoke
`$finalize-phase`. It coordinates the repository reviewer, requested specialist handoffs, final
verification, and `$finish-worktree`. Git operations still require the explicit checkpoints in
`git-commit-rules`, and the workflow opens but never merges the pull request. Use `$finish-worktree`
directly only for an already reviewed and verified task. `/prepare-commit` remains the narrower
workflow for preparing a proposed commit without completing branch integration.

Run `python .claude/tools/gates.py` to inspect the available verification gates and never report a
scaffolded or deferred gate as passing.

Claude hooks do not replace repository tests or the public Git hook under `scripts/hooks/`.

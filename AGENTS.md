# AGENTS.md

Repository-level instructions for automated contributors working on LabBridge.

## Authorities

Read [`AI_CONTRACT.md`](AI_CONTRACT.md) before any non-trivial change. It defines the engineering
invariants, architectural boundaries, proof requirements, and document precedence. Then read the
relevant sections of:

- [`docs/SPEC.md`](docs/SPEC.md) for required behaviour;
- [`docs/ROADMAP.md`](docs/ROADMAP.md) for the active delivery slice;
- [`docs/DATA_STRATEGY.md`](docs/DATA_STRATEGY.md) for source, metadata, and lineage rules;
- [`docs/FAILURE_MATRIX.md`](docs/FAILURE_MATRIX.md) for required failure semantics;
- [`docs/ARCHITECTURE_DECISIONS.md`](docs/ARCHITECTURE_DECISIONS.md) for accepted decisions;
- [`docs/DEVELOPMENT_WORKFLOW.md`](docs/DEVELOPMENT_WORKFLOW.md) for branches, worktrees, and shared
  automation policy.

Report contradictions instead of choosing the most convenient interpretation.

## Working rules

- Inspect the existing implementation, migrations, tests, fixtures, and source data before editing.
- Keep each change to the smallest coherent unit that satisfies a current roadmap exit criterion.
- Add or update tests at the layer required by the claim; do not weaken an invariant to make a test
  pass.
- Never infer source columns, units, electrochemical conventions, or dataset semantics from memory.
- Keep metadata origin independent from extraction, normalisation, and derivation state.
- Preserve immutable source bytes, append-only scientific history, and explicit provenance.
- Never conflate observed and synthetic data or claim exactly-once execution.
- Run the relevant gates and inspect their output before reporting completion.
- Preserve unrelated worktree changes. Do not stage, commit, push, change branches, bypass hooks, or
  perform destructive cleanup without explicit authorisation for that action.
- Keep secrets, machine-local configuration, temporary plans, session state, and generated logs out
  of version control.

## Worktree integration

- Follow [`docs/DEVELOPMENT_WORKFLOW.md`](docs/DEVELOPMENT_WORKFLOW.md) for Git, branch, and worktree
  practices.
- When a roadmap phase or other substantial coherent implementation is complete, use the repository
  `finalize-phase` skill for review, selective specialist handoffs, verification, and integration
  preparation.
- `finalize-phase` delegates Git preparation to `finish-worktree`; both preserve every explicit
  authorisation checkpoint in `git-commit-rules`.
- Finalization uses validation proportional to the change rather than an automatic repository-wide
  audit.
- Never push worktree changes directly to `main`.
- The normal integration path is task branch → commit → push → pull request → review/CI → merge.
- Pull-request merging remains a separate integration decision.

## Shared tooling

- Codex skills live under `.agents/skills/` and mirror `.claude/skills/` byte-for-byte.
- Claude review definitions, commands, hooks, skills, and checkers under `.claude/` are shared project
  configuration. Codex may read their checklists as repository guidance but must not pretend that
  Claude-specific aliases exist in Codex.
- Apply the review lens that matches the risk: scope, architecture, implementation, code review, data
  integrity, reliability, or verification.
- Read `electrochemistry-expert` before work where the meaning of a potential, reference scale,
  current density, kinetic parameter, composition measurement, or biosensor metric determines
  correctness.
- Use `rg` for repository search and `apply_patch` for focused file edits.

All committed code, comments, identifiers, schemas, and technical documentation are written in
English and describe project facts rather than a particular development session.

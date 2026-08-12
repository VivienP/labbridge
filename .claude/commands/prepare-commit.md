---
description: Prepare a commit — verify, review, stage explicitly, and present it for approval. Does not authorise the commit.
argument-hint: [scope hint]
---

Prepare a commit for the current work. Load `git-commit-rules` once for the batch.

**Scope:** $ARGUMENTS

Use this command when the requested outcome is commit preparation only. For an explicitly authorised
completed-worktree finalization through branch push and pull-request creation, use
`$finish-worktree`.

This command prepares and explains a commit. It does **not** authorise one. Staging, committing,
pushing, initialising a repository, and changing branches each require explicit authorisation for that
action (`AI_CONTRACT.md` §11).

## Preflight

- `git status --porcelain`, the current branch, and `git config --get core.hooksPath`.
- The Git gate must be wired through `scripts/hooks`. If it is not, stop rather than committing
  without it.
- Stop on overlapping unrelated changes that cannot be preserved.

## Verify before staging

Run `/verify` for the claim this commit makes. A `NOT SUPPORTED` or `PARTIALLY SUPPORTED` verdict means
the commit is not ready — report the gap instead of committing.

Then confirm:

1. If a normative document changed, including `AGENTS.md`, `CLAUDE.md`, or `AI_CONTRACT.md`,
   `SHA256SUMS.txt` was regenerated in this change.
2. If a public claim changed, `evidence-status-discipline` was applied and the artifact it names exists.
3. If a failure code, event type, or state transition was added, the corresponding
   `docs/FAILURE_MATRIX.md` row or `docs/SPEC.md` section was updated.
4. If an accepted architecture decision changed, a **superseding** ADR was added rather than the old one
   edited.
5. The relevant review lenses ran, and no `REQUEST-CHANGES` verdict is outstanding.
6. Tracked prose contains no prompt, private deliberation, owner-specific reminder, workstation path,
   or execution constraint that is not intrinsic to LabBridge.

## Compose the units

List the smallest independently green commit units. A migration and the code that requires it belong
together; unrelated cleanup does not. Do not manufacture scaffold, test, and refactor commits when one
coherent unit is clearer.

## Per unit

1. Stage explicit files. Never `git add -A` or `git add .`.
2. Read `git diff --staged` in full.
3. Confirm nothing forbidden is staged: `.env*`, keys or tokens, the fetched HER archive, archive-derived
   content missing its ADR-009 attribution, large generated simulator output, a result artifact without
   its manifest, machine-local settings, logs, caches, or session state.
4. Draft the Conventional Commits message. No AI product, vendor, model, or co-author trailer. No
   emojis.

## Present for approval

Show, per unit: the staged file list, the message, the gate output that supports it, and anything the
commit does not prove.

Then stop and ask. Do not run `git commit` until explicitly authorised. Never `--no-verify`.

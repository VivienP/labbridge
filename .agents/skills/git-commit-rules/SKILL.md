---
name: git-commit-rules
description: Use before the first staging operation in a commit batch, before a push, before initialising a repository or changing branches, after context compaction, or after the repository rules change.
---

# Git commit rules

Load once before a commit batch. Reload after context compaction or a rule change. The Git pre-commit
gate enforces each commit; repeated skill invocations inside an unchanged batch add no safety.

## Authorisation

Do not stage, commit, push, initialise a repository, or change branches without explicit authorisation
for that action. An ordinary implementation request is not approval to commit. Approval for one commit
does not extend to the next one. Never force-push. Never pass `--no-verify` or bypass a hook.

`AI_CONTRACT.md` §11 lists unauthorised Git state changes among the forbidden patterns.

## Commit message

Conventional Commits:

```text
<type>(<scope>): <imperative summary, at most 72 characters>
```

Allowed types: `feat`, `fix`, `refactor`, `perf`, `docs`, `test`, `chore`, `ci`, `build`, `style`.
Useful scopes here: `domain`, `application`, `infra`, `worker`, `api`, `cli`, `evidence`, `environments`,
`migrations`, `agents`, `docs`.

Explain **why** in a short body when the reason is not obvious from the diff — particularly for a
transaction boundary, a constraint, a failure classification, or a claim-status change.

Never mention an AI product, vendor, model, generated-by statement, or co-author trailer. No emojis
unless requested.

## Pull request

Use a precise, professional title that describes the change rather than the work session. The
description states what changed, why it is needed, important design decisions, exact validation
evidence, and material limitations or follow-up work. Keep it concise: no implementation diary, prompt,
agent attribution, private deliberation, non-project operating constraint, or unsupported completion
claim.

## Scope and staging

- One commit is one coherent, independently green unit. A migration and the code that requires it belong
  together; unrelated cleanup does not.
- Run `git status` and read `git diff --staged` before every commit.
- Stage explicit files. Never `git add -A` or `git add .`.
- Shared repository instructions and reusable automation are version-controlled. Machine-local
  overrides and runtime state are not.

## Never commit

- machine-local agent configuration and state, including `settings.local.json`, logs, caches, and
  session records under `.claude/`, `.agents/`, or `.codex/`;
- `.env` or any `.env.*` file;
- private keys, certificates, tokens, or credentials in any form;
- the fetched HER archive or extracted archive data;
- archive-derived rows, transformed fixtures, or archive-derived plots that do not carry the CC BY
  attribution from `data_use.HER_DATA_USE` on the artifact itself (ADR-009);
- large generated simulator output;
- local PostgreSQL or MinIO volumes;
- a result artifact without its manifest.

If a forbidden file is already tracked, stop and ask for direction before removing it from the index.
Git ignore rules do not apply to an already-tracked path, and in a repository with no commits the
index may hold the only copy of a file — preserve it outside the repository before dropping it.

## Before staging

1. Fresh gate output has been read — see `verification-before-completion`.
2. If a published document under `docs/` changed, `SHA256SUMS.txt` was regenerated in the same commit:

   ```bash
   git ls-files -co --exclude-standard -- AGENTS.md AI_CONTRACT.md CLAUDE.md 'docs/*.md' \
     | xargs sha256sum > SHA256SUMS.txt
   ```
3. If a public claim changed, `evidence-status-discipline` was applied and the artifact it points to
   exists.
4. If a failure code, event type, or state transition was added, the corresponding
   `docs/FAILURE_MATRIX.md` row or `docs/SPEC.md` section was updated.
5. If an accepted architecture decision changed, a superseding ADR was added rather than the old one
   edited.

## Push check

Before a push: inspect the recent commits, the branch diff, and the sensitive-file list. Confirm no
forbidden file, secret, AI attribution, or unrelated change is included. Then ask for approval.

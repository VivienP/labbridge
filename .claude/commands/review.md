---
description: High-signal read-only code review of the current diff.
argument-hint: [base branch]
---

Invoke `@reviewer` for a read-only review of the current diff.

Optional argument: a base branch (default: the configured upstream, else `main`).

**Base:** $ARGUMENTS

## What it does

1. Scopes the change — committed, staged, and unstaged — and reports the base it chose.
2. Resolves the approved task specification, else the relevant `docs/SPEC.md` section.
3. Walks the durability, provenance, test, error-handling, claim, and dependency checklists.
4. Runs an adversarial pass on the strongest assumptions the change relies on.
5. Runs the live gates and reports what it could not run.
6. Emits BLOCKING / WARNING / SUGGESTION findings with `file:line`, quoted rules, and a verdict.

## Also run, when they apply

- `/review-data` — observations, units, lineage, origin and execution mode, HER ingestion, simulator.
- `/review-failure` — workers, leases, retries, failure classification, failure-matrix coverage.
- `/review-migration` — an Alembic migration, a new constraint, or an event `schema_version` change.
- `/review-architecture` — a module boundary, schema, state machine, or transaction design.

`@reviewer` reviews code and rules. It does not adjudicate scientific data, certify failure coverage, or
reproduce experiments — it hands those over explicitly.

## After the review

Act on the findings with the `receiving-code-review` discipline: verify each before implementing, state
`confirmed` / `partially valid` / `disagree` with evidence, and push back where a suggested fix would
weaken an invariant.

Never resolve a finding by loosening the check that produced it.

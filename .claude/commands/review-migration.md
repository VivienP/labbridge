---
description: Review a database migration or an event schema-version change for safety, compatibility, and testability.
argument-hint: [migration revision, file, or diff scope]
---

Review the following migration or schema change. Load the `migration-and-schema-evolution` skill and
apply it in full. Use `@architect` in review mode for the design implications and `@reviewer` for the
code.

**Target:** $ARGUMENTS

Two kinds of evolution live here and must not be conflated: **database schema** (Alembic against
PostgreSQL) and **event or record schema** (`schema_version` on envelopes, observations, candidates, and
analyses). A change often needs both.

## Database migration

- Transactional on PostgreSQL, or its non-transactional step documented with a recovery path (F-042).
- `upgrade()` tested from the **previous tagged schema**, not only from empty.
- `downgrade()` tested where safe and supported, or explicitly unsupported with the documented restore
  procedure as the recovery path.
- Backfill idempotent and restartable.
- Safe with the previous application version still running, or split into expand and contract phases
  with the sequencing stated.
- No bare `ALTER ... SET NOT NULL` on a populated table.
- Adding a unique constraint: duplicates detected and reported before enforcement.
- Locks acceptable for a running worker — a long `ALTER` on a job table stalls the runtime.
- Nothing deletes or rewrites a raw observation (invariant 3) or mutates a released artifact.
- For every constraint that enforces an invariant, a test proving it rejects the violating insert.

## Event and record schema

- Unknown event types and unsupported `schema_version` values fail explicitly — never skipped, never
  coerced (F-039).
- Previously persisted events still replay, or a deterministic, tested upcaster exists.
- The event type and payload version are registered; an unregistered type is rejected at append time.
- A content identity that should change does; one that should not, does not (F-043, F-044).
- An analysis-version change creates new derived metrics without changing observation identity, and does
  not recompute in place.

## Output

Use the review block at the end of the `migration-and-schema-evolution` skill. Close with a verdict —
`SAFE`, `SAFE WITH SEQUENCING`, or `UNSAFE` — and an explicit *Untested claims* line.

The migration file existing is not evidence the migration is safe. Until it has been exercised against
production-like data with a documented rollback (`docs/ROADMAP.md` Slice 6), migration safety is
`implemented`, never `demonstrated`.

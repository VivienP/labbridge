---
name: migration-and-schema-evolution
description: Use when adding or changing an Alembic migration, a PostgreSQL table or constraint, an event type, an event schema_version, an upcaster, a candidate or observation schema version, or an analysis version. Covers migration safety, backward compatibility, and deterministic event evolution.
paths: alembic/**, migrations/**, src/labbridge/infrastructure/postgres/**, src/labbridge/domain/events.py
---

# Migration and schema evolution

Authority: `AI_CONTRACT.md` §6 and §9; `docs/SPEC.md` §4.1, §5; `docs/FAILURE_MATRIX.md` F-019, F-039,
F-042; `docs/ROADMAP.md` Slice 3 and Slice 6.

Two distinct kinds of evolution live here. Do not conflate them.

- **Database schema evolution** — Alembic migrations against PostgreSQL.
- **Event and record schema evolution** — `schema_version` on event envelopes, observations, candidates,
  and analyses.

A change often needs both, and they have different failure modes.

## Part 1 — Database migrations

### Before writing the migration

1. What invariant does each new constraint enforce? A unique constraint that implements idempotency
   (invariant 5) is a correctness mechanism, not a hygiene detail — name it in the migration docstring.
2. Is the change additive, or does it require a backfill?
3. Will the previous application version still run against the new schema during a rolling deployment?
   If not, split into an expand phase and a contract phase.
4. Does the migration lock a table that a running worker holds open? A long `ALTER` on a job table stalls
   the runtime.

### Safety checklist

- Migration is transactional on PostgreSQL, or its non-transactional step is explicitly documented with
  a recovery path (F-042).
- `upgrade()` is tested from the previous tagged schema, not only from empty.
- `downgrade()` exists and is tested where it is safe and supported; where it is not, the migration says
  so explicitly and the recovery path is the documented restore procedure.
- A backfill is idempotent and restartable — it will be interrupted.
- Adding a `NOT NULL` column to a populated table uses a default or a three-step expand/backfill/enforce
  sequence, never a bare `ALTER ... SET NOT NULL`.
- Adding a unique constraint to existing data: what happens if duplicates already exist? Detect and
  report before enforcing.
- Index creation on a large table considers `CONCURRENTLY` and its non-transactional consequence.
- Nothing in the migration deletes or rewrites a raw observation record (invariant 3).
- Nothing in the migration mutates a released evidence artifact.

### Required tests

- upgrade from the previous tagged release schema;
- downgrade where supported;
- a data-preserving assertion: rows that existed before still exist and still mean the same thing;
- for a constraint that enforces an invariant, a test proving the constraint actually rejects the
  violating insert.

The migration file existing is not evidence the migration is safe. `AI_CONTRACT.md` §9 requires
migration upgrade tests and downgrade tests where safe and supported.

### Deployment

`docs/ROADMAP.md` Slice 6 requires a migration exercised against production-like data and a documented
application rollback procedure, plus the interrupted-migration recovery path. Until that has been run
and recorded, migration safety is `implemented`, never `demonstrated`.

## Part 2 — Event and record schema evolution

### The rule

Unknown event types and unsupported `schema_version` values fail **explicitly**. They are never skipped,
never coerced, never defaulted (`AI_CONTRACT.md` §6, F-039). Silent event dropping during replay
destroys the only guarantee replay provides.

### Before bumping a version

1. Is the change genuinely incompatible? Adding an optional field with a safe default may not need a
   bump — but a field the reader must understand does.
2. Can previously persisted events still be replayed by the new code? If not, an upcaster is required,
   and it must be deterministic and tested.
3. Does the upcaster round-trip? Replay of old events plus new events must produce one coherent state.
4. Does the change alter a content identity? Changing an observation's `schema_version` changes its
   hash — that is correct behaviour (`docs/DATA_STRATEGY.md` §5), but check nothing caches across the
   boundary and reuses a stale identity (F-044).

### Registration

Event types and payload versions are registered (`docs/SPEC.md` §5.1). An unregistered type must be
rejected at append time, not discovered at replay time.

### Analysis versions

Changing only the analysis version creates **new** derived metrics; it does not change the observation
identity (`docs/SIMULATOR_MODEL.md` §10). Old metrics remain, related to the new ones. Never
recompute in place.

### Source schema changes

An upstream archive whose schema the parser does not recognise produces an explicit unsupported-schema
failure and stops ingestion until an adapter version is added (F-019). Raw source is retained. Never
coerce an unknown column.

### Required tests

- replay of events persisted under the previous version;
- upcaster determinism: the same input yields the same upcast output;
- explicit failure on an unregistered event type;
- explicit failure on an unsupported `schema_version`;
- content-identity change when the schema version changes;
- content-identity stability when only mapping order changes.

## Reviewing a migration

Report per item: satisfied · gap · not applicable.

```text
## MIGRATION REVIEW

Migration: <revision id and file>
Direction: additive | expand/contract | destructive
Previous tagged schema: <revision>

- Transactional on PostgreSQL: yes / no + documented recovery
- Upgrade tested from previous tagged schema: yes / no
- Downgrade tested or explicitly unsupported with a recovery path: yes / no
- Backfill idempotent and restartable: yes / no / n-a
- Safe with the previous application version running: yes / no + required sequencing
- Locks acceptable for a running worker: yes / no
- Constraints that enforce an invariant, and the test that proves each: <list>
- Raw observations untouched: yes / no
- Released artifacts untouched: yes / no
- Event schema_version implications: <none | bump + upcaster + tests>
- Interrupted-migration recovery path (F-042): <documented where>

Verdict: SAFE | SAFE WITH SEQUENCING | UNSAFE
Untested claims: <what the tests do not prove>
```

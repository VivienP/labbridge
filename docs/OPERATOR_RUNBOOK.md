# LabBridge operator runbook

This runbook covers the fault-aware campaign runtime demonstrated in Phase 7. It does not describe
an instrument-control deployment. The released campaign uses generated synthetic fixture bytes in
replay mode; observed replay and future observed live execution remain distinct modes.

## 1. Preconditions and stop conditions

Use a PostgreSQL database and S3-compatible bucket dedicated to the operation. Never run a migration,
backup, projection rebuild, or reliability campaign while an unaccounted writer is active.

```powershell
docker compose up -d postgres minio
docker compose ps
python -m alembic current
python .claude/tools/gates.py
```

Proceed only when PostgreSQL and MinIO are healthy, the Alembic revision is known to this checkout,
and credentials are supplied through `LABBRIDGE_DB_*` and `LABBRIDGE_S3_*`. Stop if the database has
an unknown revision, object verification fails, a campaign stream is pre-contract, or replay differs
from persisted state. Do not repair evidence by changing a checksum.

## 2. Campaign control

The campaign API exposes idempotent `pause`, `resume`, and `cancel` commands with an expected version
and idempotency key. Pausing prevents new claims. Cancelling prevents new claims and cancels available
jobs. In accordance with `docs/SPEC.md` and `docs/FAILURE_MATRIX.md` F-034, an already-leased job may
finish, but no retry or new job is scheduled after cancellation. Any received bytes remain evidence.

Before control, read the campaign and note its current version. Repeat the same command with the same
key only when replaying the same request. A version conflict is a decision point: read current state;
do not blindly increment the expected version.

## 3. Stuck jobs and lease recovery

Symptoms are a job remaining `leased` or `running` beyond `lease_expires_at`, or an attempt remaining
`running` without a live lease. Start one normal worker. Worker startup runs reconciliation in this
order:

1. reclaim expired leases and increment the fencing generation;
2. close abandoned attempts as `lease_lost`;
3. release reservations for attempts that never crossed the adapter boundary;
4. consume reservations for attempts that crossed it;
5. classify staged objects without deleting their bytes.

Stop if reconciliation reports an unreachable object. Restore MinIO access and rerun; do not turn an
unknown object into committed evidence. At-least-once delivery is expected. Correctness is expressed
as idempotent effects and at most one accepted observation per work item, not exactly-once execution.

## 4. Projection comparison and reconstruction

Use `compare_campaign_projection(connection, campaign_id)` before rebuilding. A mismatch report names
each divergent projection. `rebuild_mutable_projections` may recreate missing work-item, job, and
outcome-free attempt projections and update declared mutable fields. It refuses immutable identity
changes, extra rows, append-only evidence divergence, or a missing campaign root.

The rebuild runs in a savepoint and compares again before committing. Stop on
`NonRebuildableProjectionError` or `ProjectionMismatchError`. Events, outcomes, observations, budget
ledger rows, stored bytes, and released Packages are never rewritten by this operation.

## 5. Campaign Package creation and verification

Build inputs with `campaign_package_inputs_from_postgres`, build with
`build_campaign_experiment_package`, and publish with `publish_verified_campaign_package`. Publication
verifies every Package member and every referenced S3 object before atomically exposing the final ZIP.
A remaining `*.partial` file records an interrupted export and is not a release.

```powershell
labbridge package verify <campaign-package.zip> --json
```

Stop on any schema, event-stream, lineage, projection, relation, budget, report, size, or SHA-256
failure. Preserve the archive and object for diagnosis. Never refresh the manifest around changed
bytes.

## 6. Quiescent backup and restore

The reliability command automates the procedure below and records its result in
`backup-restore.json`.

1. Stop API and worker writers, then require every campaign to be terminal.
2. Run `pg_dump --format=custom --no-owner --no-privileges` against PostgreSQL.
3. List every object in the campaign bucket and record key, byte size, and SHA-256.
4. Create a new empty PostgreSQL database and restore with `pg_restore`.
5. Start a distinct empty MinIO instance, create the same logical bucket, and restore every object.
6. Compare every PostgreSQL table count.
7. Read back and hash every restored object.
8. Compare every restored campaign with replay.
9. rebuild and fully verify every restored campaign Package against the restored MinIO instance.

Stop if a writer is active, the target is not empty, counts differ, an object differs, replay differs,
or a Package does not fully verify. PostgreSQL and object storage do not share a distributed
transaction; quiescence is therefore mandatory.

## 7. Migration and rollback

Before deployment, restore a production-like snapshot into a separate database at the previous
revision and run `alembic upgrade head`. Verify row counts, the resulting revision, the schema
constraints, replay, and object references. Phase 7 migrations are additive. Downgrade refuses data
whose meaning an older schema cannot preserve, including replay-contract v2 streams and adjusted
budget rows.

On an interrupted transactional migration, stop traffic, inspect `alembic current`, and rerun only
when the recorded revision and database schema agree. Otherwise restore the pre-migration backup.
Never stamp past an unknown state to make the revision appear current.

## 8. Reproduce and interpret the fault campaign

Use dedicated empty targets. The release command rejects non-empty campaign databases, non-empty
buckets, and non-empty output directories.

```powershell
python scripts/reproduce_campaign_reliability.py `
  --campaigns 100 `
  --master-seed 20260813 `
  --database-name labbridge_phase7_fault_campaign `
  --bucket labbridge-phase7-fault-campaign `
  --output build/phase7-fault-campaign
```

The planner balances all six boundaries and shuffles them deterministically: after lease, after
adapter response, during multipart upload, after upload before the outcome transaction, after commit
before acknowledgement, and during evidence export. Each row records the process exit, attempts,
lease recovery, receipts, accepted outcomes, duplicate suppression, budget, replay comparison, and
full Package verification.

Interpret `summary.json` only as a derivation of `raw-results.csv`. The Phase 7 targets are met only
when lost accepted observations, unintended duplicate acceptances, hard-budget overspends, projection
mismatches, and failed Package verifications are all zero. Any nonzero counter remains in the released
artifact and prevents promotion to `demonstrated`.

## 9. Verification failure response

Retain the database, object, Package, raw result row, checkpoint state, stdout, and stderr. Classify
the failure by stable code. Do not delete corrupted or unfavourable receipts and do not create a
metric from them. A correction is a new record and explicit invalidation or supersession relation;
an existing release remains immutable.

For the released Phase 7 evidence, run:

```powershell
labbridge validate-artifacts --bundle artifacts/fault-campaign
python -m pytest -m "not slow and not data and not integration"
python -m pytest -m integration
python -m pytest -m "slow and integration" -k fault_campaign
```

Record any unavailable prerequisite as `NOT RUN` with its reason. A suite that collected no tests is
not a pass.

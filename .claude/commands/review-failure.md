---
description: Certify failure-matrix coverage and proof-obligation status for the current change or slice.
argument-hint: [slice, F-row list, or diff scope]
---

Invoke `@reliability-reviewer`.

**Target:** $ARGUMENTS

Run this whenever the change touches a worker, a lease, a heartbeat, a retry, a failure classification,
a cancellation, an object-store reconciliation, a replay path, or any scenario in
`docs/FAILURE_MATRIX.md`.

## What it certifies

For each in-scope row, whether the repository **represents** the scenario, **reproduces** it
deterministically, **tests it at an adequate layer**, makes it **observable**, **recovers or terminates**
as the matrix specifies, and produces **no duplicate accepted effect**.

A row is `COVERED` only when all six hold. Code existing is not coverage.

## The layer rule

The commonest false pass here is a scenario tested one layer above where it can actually fail. Worker
death needs real process termination. A duplicate needs two real actors racing a real unique constraint.
A budget race needs concurrent transactions against real PostgreSQL. A rollback needs a real transaction
and a read-back of committed state. Artifact tampering needs a real mutated byte.

`AI_CONTRACT.md` §9: *"A test that mocks away the database transaction, object store, or process
boundary does not prove the corresponding operational guarantee."*

## Proof obligations

The report states, per obligation in scope: `not started`, `implemented`, or `demonstrated` — and names
the artifact for anything marked `demonstrated`. Never report `demonstrated` from code inspection.

PO-10 (the 100-campaign fault experiment) is an acceptance criterion, not a result. Until the raw result
artifact exists, it is never `demonstrated`. Publishing the acceptance targets as though they were
measurements is a blocking finding (`docs/FAILURE_MATRIX.md` §6).

## Always filled

The report always includes an *Untested claims* section listing every guarantee the code or
documentation asserts that no adequate-layer test proves — `none` if there are none. Separating what is
implemented from what is demonstrated is the point of this lens.

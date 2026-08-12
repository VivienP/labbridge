---
description: Review module boundaries, schemas, state machines, transactional design, and failure semantics against the contract and the accepted decisions.
argument-hint: [module, spec path, or diff scope]
---

Invoke `@architect` in review mode.

**Target:** $ARGUMENTS

Assess the design against `AI_CONTRACT.md` §4–§6, `docs/SPEC.md`, and
`docs/ARCHITECTURE_DECISIONS.md`. Report per item: satisfied · gap · not applicable, with an exact
quotation for each gap.

## Checklist

- **Layer boundaries** — the domain layer does not depend on FastAPI, SQLAlchemy ORM objects, filesystem
  paths, cloud SDKs, or process-global configuration. API and CLI code contains no scientific
  calculation, state-transition rule, budget logic, or direct object-store semantics.
- **Persistence** — tables, constraints, and indexes; which constraint enforces which invariant; whether
  PostgreSQL is authoritative for everything `docs/SPEC.md` §4.1 lists.
- **Object-storage boundary** — staging, `pending`, `committed`, `orphaned`; the checksum verification
  point; what the database may claim before the bytes exist.
- **Transaction design** — every path with more than one durable effect; what partial success would
  corrupt; whether the outcome, event, projection, and budget update are one transaction.
- **Concurrency** — expected-version append, atomic job claim, row-locked or conditionally-updated
  budget, lease and heartbeat semantics, and the interleavings each defends against.
- **State machines** — campaign, work item, attempt, and job as four separate machines; permitted
  transitions; terminal states; where illegal transitions are rejected.
- **Event model** — envelope completeness, `sequence` scoping, registration, version handling, upcasting
  determinism, and whether replay of previously persisted events still works.
- **Failure semantics** — the failure codes, their category and retryability, their failure-matrix rows,
  and whether received bytes are retained in each.
- **Migration implications** — whether the design forces a breaking schema change, and whether it is safe
  with the previous application version running.
- **Stack conformance** — nothing outside `AI_CONTRACT.md` §4 without an accepted ADR. Kubernetes,
  Kafka, Temporal, Airflow, Redis, and Celery each require an ADR proving the current stack cannot meet a
  concrete proof obligation.
- **ADR consistency** — nothing silently contradicts ADR-001 through ADR-008. A change to an accepted
  decision needs a superseding ADR, not an edit.
- **Observability** — the log fields, metrics, and trace spans that make a failing attempt diagnosable
  from campaign to dependency.

## Output

Per item: the verdict, the quoted contract or specification text for each gap, and the required
property. State the required behaviour; do not rewrite the design or produce a patch.

Close with: the invariants at risk, the proof obligations affected, and whether the design as it stands
can support the exit criterion it is meant to serve.

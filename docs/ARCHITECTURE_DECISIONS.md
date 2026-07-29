# LabBridge — architecture decisions

**Status:** accepted baseline decisions for V1  
**Purpose:** preserve the reasoning behind choices that materially affect correctness, scope, and portfolio value.

Each future change to an accepted decision MUST add a superseding decision rather than deleting history.

---

## ADR-001 — Separate operational state from scientific artifacts

**Status:** accepted  
**Decision:** use PostgreSQL for operational state and S3-compatible object storage for raw signals, Parquet exports, manifests, and evidence bundles.

### Context

Campaigns require concurrency control, idempotency, durable jobs, atomic budget updates, and recoverable state. Scientific artifacts require immutable, content-addressed storage and efficient columnar export. A single filesystem or Parquet-only design cannot provide both sets of guarantees credibly.

### Consequences

- campaigns, attempts, jobs, approvals, events, projections, and artifact metadata live in PostgreSQL;
- binary and columnar artifacts live in object storage;
- object publication uses pending and committed states;
- JSONL is generated as an evidence export, not used as the concurrent source of truth;
- local development requires PostgreSQL and MinIO through Docker Compose.

---

## ADR-002 — Use a database-backed durable worker without a separate message broker

**Status:** accepted  
**Decision:** implement one worker process using PostgreSQL-backed jobs, atomic claims, leases, heartbeats, retry scheduling, and lease recovery.

### Context

The project must demonstrate durable asynchronous execution without becoming a stack showcase. Kafka, Redis/Celery, Temporal, and Airflow would add operational surface before the core semantics are understood.

### Consequences

- job delivery is at least once;
- accepted outcomes are deduplicated through database constraints;
- worker termination and lease expiry are first-class test scenarios;
- a separate broker MAY be adopted later only if a measured limitation justifies it.

---

## ADR-003 — Treat HER and biosensor as separate environments, not fidelities

**Status:** accepted  
**Decision:** each campaign belongs to one `environment_id`. HER replay and biosensor simulation implement the same adapter protocol but do not share a candidate space or acquisition policy.

### Context

The systems represent different reactions, instruments, parameters, outputs, and candidate spaces. Labelling one as a cheap simulation and the other as an expensive experiment would create a scientifically invalid multi-fidelity problem.

### Consequences

- universal `Fidelity = simulation | experiment` is removed;
- `data_origin` and `execution_mode` are separate concepts;
- environment-specific fidelity is optional and introduced only when matched observations exist for the same candidate space;
- genuine sim-to-real optimisation is deferred.

---

## ADR-004 — Build the HER vertical slice before the full biosensor simulator

**Status:** accepted  
**Decision:** the first complete path uses a small, licence-safe HER-compatible fixture and one replay adapter. The biosensor simulator is implemented only after durable runtime correctness is proven.

### Context

Building two environments, a simulator, a data platform, and a runtime horizontally would delay the first end-to-end proof and increase the chance of a polished but shallow project.

### Consequences

- Gate 0 verifies source, schema, and licence;
- Slice 1 proves campaign-to-evidence flow;
- Slice 2 proves durable crash recovery;
- the simulator becomes a controlled fault-generation environment rather than a prerequisite for architecture validation.

---

## ADR-005 — Retain corrupted observations

**Status:** accepted  
**Decision:** an attempt outcome and a received observation are separate records. Any received bytes are retained and content-addressed even when classification is `CORRUPTED`.

### Context

Discarding corrupted signals destroys the evidence required for diagnosis and contradicts the project's fault-aware positioning.

### Consequences

- `AttemptOutcome` is always present;
- `Observation` is optional but may accompany successful or corrupted outcomes;
- `FailureRecord` captures structured failure details;
- derived metrics reference an observation and declare whether the observation was accepted for analysis.

---

## ADR-006 — Use append-only correction semantics

**Status:** accepted  
**Decision:** raw observations are never overwritten. Corrections and invalidations create new records and explicit lineage relations.

### Context

Scientific traceability requires preserving what was originally received while allowing later knowledge to change which record is considered valid.

### Consequences

- current views may point to the latest valid record;
- historical evidence remains reconstructable;
- released evidence bundles do not mutate;
- projections and indexes remain rebuildable.

---

## ADR-007 — Keep the V1 decision layer minimal

**Status:** accepted  
**Decision:** V1 includes a deterministic random baseline and MAY include one simple GP-based policy after reliability gates pass.

### Context

The project targets scientific platform, backend, and reliability roles. Multiple surrogates, conformal intervals, and multi-fidelity acquisition would create a second ML research project and dilute the operational proof.

### Consequences

- runtime correctness does not depend on model quality;
- repeat, stop, quarantine, and escalation remain runtime decisions;
- claims about calibrated uncertainty are prohibited until defined and evaluated;
- additional policies are deferred.

---

## ADR-008 — Use status-qualified documentation

**Status:** accepted  
**Decision:** every material capability is described as `planned`, `implemented`, `demonstrated`, or `deferred`.

### Context

The current repository begins as a specification. Present-tense claims about unimplemented infrastructure would weaken credibility.

### Consequences

- public and internal documentation must identify status;
- only reproducible evidence can promote a claim to `demonstrated`;
- role-fit language distinguishes target signal from current evidence.

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

---

## ADR-009 — Redistribution of the HER dataset is permitted with attribution

**Status:** accepted (data-use decision)  
**Decision:** the pinned HER dataset may be redistributed, including in adapted form, under CC BY 4.0, provided the source is attributed and changes are indicated. The repository nonetheless keeps the archive and archive-derived rows fetched on demand and git-ignored.

### Context

`docs/DATA_STRATEGY.md` §2.3 recorded that the Zenodo record showed no clear licence value, which made redistribution an unresolved blocker on Gate 0. That statement is superseded by direct verification.

Read from `https://zenodo.org/api/records/20439519` on 2026-07-30: `metadata.license.id` is `cc-by-4.0` and `access` is `open`. CC BY 4.0 permits reproduction and redistribution of the material in any medium or format, and of adapted material, subject to attribution and to indicating whether changes were made.

Two things this decision does **not** rest on. LabBridge's own source licence is irrelevant here: releasing LabBridge under any licence grants nothing over a third party's dataset. And no LabBridge code infers redistribution from a licence string at runtime — the evidence is recorded here, and the code applies the recorded decision.

### Consequences

- `LicenceStatus.redistribution` gains `permitted_with_attribution`; `unresolved` remains the default and remains the only value a parser can produce;
- a `DataUseDecision` is pinned to the DOI **and** to the licence identifier it was verified against. If the record stops declaring `cc-by-4.0`, the decision stops applying and the gate reopens without anyone editing this file;
- `provenance.json` carries the decision in force at fetch time, so a consumer reading only that document knows the licence, the verification date, and the attribution to reproduce;
- any committed artifact derived from the archive MUST carry the attribution recorded in `data_use.HER_DATA_USE` and MUST indicate the changes LabBridge made;
- the archive, extracted data, and derived rows remain git-ignored and fetched on demand. This is a repository-hygiene policy, narrower than the licence permits, adopted so the DOI stays the single source of truth. It MUST NOT be read as a licence restriction;
- Gate 0's redistribution criterion is satisfied. The offline fixture stays independently generated for the separate reason that a test suite must not depend on a multi-hundred-megabyte download.

### Scope — amended 2026-07-31

Review found the attribution obligation breached by the commit that introduced it: `fixture.py` and its tests reproduce the archive's column headers, declared units, filename grammar, line endings, and row counts, and carried no attribution. Rather than leave a rule the repository violates, the scope is stated:

**Attribution is required on** any artifact carrying archive *values* — rows, subsets, aggregates, fitted parameters, plots, exports, and evidence bundles derived from them.

**Attribution is not required on** structural metadata: column names, declared units, filename grammar, line endings, row and column counts, and the schemas recorded in `dataset_inventory.json`. These are facts about the shape of the data, they are what `AI_CONTRACT.md` §7 obliges the implementation to record by inspection, and a schema cannot be described without restating them.

The distinction is between describing a dataset and redistributing it. Where citing costs nothing — the fixture generator, the Gate 0 spec — the source is cited anyway, because a reader meeting an archive-shaped file should be able to find the archive.

### Limits

This decision records what the record declares, with the date and the endpoint it was read from. It is not legal advice, and it does not cover material the record does not itself license. The scope amendment above is an engineering rule about this repository's own obligations; it does not interpret CC BY 4.0 on anyone else's behalf.

---

## ADR-010 — Admissible data-origin and execution-mode pairs

**Status:** accepted  
**Decision:** `synthetic + replay` is a third admissible pair, alongside `observed + replay` and `synthetic + simulation`. `observed + simulation` is inadmissible and MUST be rejected by validation.

### Context

`docs/DATA_STRATEGY.md` §1 listed two admissible pairs. ADR-004 makes the first vertical slice run on the independently generated fixture, which is a replay adapter reading generated bytes — `synthetic + replay`, a pair the table did not list.

Three ways out were available. Labelling a fixture run `observed` is a lie about the origin of the data and is the exact conflation invariant 1 exists to prevent. Labelling it `simulation` is a lie about the execution mode: nothing is simulated, a recorded file is replayed. Waiting for the real archive before Slice 1 makes the first end-to-end proof depend on a multi-hundred-megabyte download and contradicts ADR-004.

The pair is admissible because the two fields are independent by construction: origin says where the values came from, mode says how they reached the runtime. A generated file replayed through an adapter is honestly both.

### Consequences

- the admissible set is `observed + replay`, `synthetic + replay`, `synthetic + simulation`, and `observed + live` reserved outside V1;
- `observed + simulation` is inadmissible: simulation cannot produce observed data, and a pair claiming otherwise is a defect, not a configuration;
- admissibility is enforced by validation on `EnvironmentRef` and proven by a test, not left to convention (invariant 1);
- a fixture-backed campaign is labelled `synthetic` everywhere a synthetic export must be labelled — chart titles, table columns, filenames, report sections (`docs/SIMULATOR_MODEL.md` §13, F-045). A fixture demo MUST NOT be described as a replay of measured data;
- when the HER replay adapter is pointed at the real archive it emits `observed + replay`, and the same adapter code emits `synthetic + replay` against the fixture. The pair comes from the source the adapter was configured with, never from the adapter's identity.

### Limits

This decision widens what may be *recorded*. It widens nothing about what may be *claimed*: a `synthetic + replay` result is not evidence about the physical system, and no proof obligation is discharged by a fixture-backed run.

---

ADR-011 and ADR-012 remain reserved and are not reassigned here.

## ADR-013 — Separate local bundle verification from stored-object verification

**Status:** accepted

**Decision:** evidence verification has two explicit modes. `bundle-only` validates the closed bundle
and returns `partial`; `full` also reads every version 2 inventory object through `ObjectStore`, checks
its byte size and SHA-256, and returns `complete` only when all requested checks pass.

### Context

A bundle can prove that its local members still match its manifest without proving that referenced raw
observations remain present and unchanged in object storage. Treating both checks as one success would
overstate the evidence available from an offline verification.

Manifest version 2 therefore records a deterministic object inventory produced from the observations
to `storage_objects` join. Each observation attempt remains represented even when several attempts
refer to the same physical object. Verification may read shared physical bytes once while retaining
every observation-attempt reference in the manifest. An `objects_digest` covers the canonical inventory,
while `manifest_digest` covers every manifest field except itself. Construction fails when observation
and storage-object metadata disagree or the object is not recorded as `committed`.

### Consequences

- version 1 bundles remain verifiable only in `bundle-only` mode and return `partial`;
- `full` rejects version 1 bundles with `full_verification_requires_manifest_v2`;
- `full` requires an `ObjectStore` and never falls back to local verification;
- lookup uses the recorded bucket and key; `object_uri` remains evidence and is not parsed into new
  storage coordinates;
- missing objects, size mismatches, SHA-256 mismatches, and object-store access failures remain
  distinct structured failures;
- version 2 verification validates manifest identity fields and requires the object inventory to match
  `observations.json`, including each row's origin, execution mode, and validated rooted provenance
  environment;
- verification is read-only and writes no result to PostgreSQL or object storage;
- released bundle destinations are immutable, and the builder refuses an existing path.

### Limits

`bundle-only` does not establish object-store availability or content integrity. `full` validates the
objects referenced by one manifest at verification time; it does not publish the bundle, create a
database snapshot guarantee, or make the capability `demonstrated` without a released reproducible
artifact.

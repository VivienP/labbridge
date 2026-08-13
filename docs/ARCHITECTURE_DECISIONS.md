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

## ADR-004 — Build one complete HER path before the full biosensor simulator

**Status:** accepted  
**Decision:** the first complete path uses a small, licence-safe HER-compatible fixture and one replay adapter. The biosensor simulator is implemented only after durable runtime correctness is proven.

### Context

Building two environments, a simulator, a data platform, and a runtime horizontally would delay the first end-to-end proof and increase the chance of a polished but shallow project.

### Consequences

- source acquisition verifies source, schema, and licence before any dataset-specific code;
- the campaign-to-evidence path is proven end to end before breadth is added;
- durable crash recovery is proven next, on that same path;
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

`docs/DATA_STRATEGY.md` §1 listed two admissible pairs. ADR-004 makes the first complete path run on the independently generated fixture, which is a replay adapter reading generated bytes — `synthetic + replay`, a pair the table did not list.

Three ways out were available. Labelling a fixture run `observed` is a lie about the origin of the data and is the exact conflation invariant 1 exists to prevent. Labelling it `simulation` is a lie about the execution mode: nothing is simulated, a recorded file is replayed. Waiting for the real archive makes the first end-to-end proof depend on a multi-hundred-megabyte download and contradicts ADR-004.

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

---

## ADR-014 — Version event-stream completeness and serialize append per campaign

**Status:** accepted

**Decision:** each campaign row contains an explicit event-stream contract version and the last
allocated campaign position. Event append locks that row, requires an expected
aggregate version, validates the registered payload schema, and allocates aggregate sequence and
campaign position inside the caller's transaction.

### Context

The original event table has a per-aggregate uniqueness constraint but accepts unregistered payloads,
makes expected version optional, and has no campaign-wide order. Existing campaigns also lack events
for several mutable projections. Their history cannot be recovered honestly from current projection
rows.

Keeping the metadata on the campaign makes the contract version structurally unavoidable: an older
application creates version `0`, never an apparently complete campaign without stream metadata. The
event store is the only component that interprets version `0`. A separate aggregate-counter table
would permit more parallel append but would add another durable state machine without evidence that
campaign-level serialization is a bottleneck. Advisory locks would make the same guarantee less
inspectable.

### Consequences

- contract version `0` means `legacy_incomplete`; contract version `1` means complete for the fields in
  `docs/SPEC.md` section 5.4;
- migration creates version `0` metadata for every existing campaign and never manufactures a missing
  event from a projection;
- new campaigns create version `1` metadata and their required initial events in the campaign-creation
  transaction;
- append and replay loading reject version `0` with `IncompleteEventStreamError`;
- evidence export may preserve version `0` events only with the explicit incomplete label;
- existing event rows receive only a deterministic technical position; this does not make their stream
  historically complete or replayable;
- `expected_version`, event type, schema version, payload, occurrence time, correlation identifier,
  and causation identifier are explicit append inputs;
- registry entries are keyed by event type and schema version and bind an exact Pydantic payload model
  to one aggregate type;
- jobs persist the originating correlation and their last event identifier so a worker continues the
  same causal chain after crossing the durable queue;
- append and stream loading reject absent or non-prior causes, cross-campaign causes, correlation
  changes along a causal edge, repeated identity fields that disagree with the envelope, and naive
  producer timestamps;
- `campaign.created` is the unique root at position one; an empty purportedly complete stream is
  invalid;
- one transaction commits or rolls back the event, stream position, and required projection together;
- unique aggregate sequence over the full campaign/type/identifier key and unique campaign position
  constraints remain the database backstops;
- the campaign metadata lock makes aggregate `MAX(sequence) + 1` safe because no append for that
  campaign can calculate concurrently outside the lock;
- unknown event types, unsupported schema versions, malformed payloads, aggregate-version conflicts,
  identity or causation violations, and position gaps fail explicitly.

The migration requires a write outage. API and worker writers are stopped and drained before its
PostgreSQL transaction adds and backfills the ordering metadata without creating events. The
contract-aware application is deployed before writes resume; an older writer cannot append after
`campaign_position` becomes mandatory. The database default remains version `0` so an unrecognised
direct writer cannot create an apparently complete stream. Downgrade is refused after any version `1`
campaign exists because removing its contract metadata would make the stream's meaning ambiguous.

### Limits

This decision implements a complete typed stream contract and its loading preconditions. It does not
implement a replay fold, rebuild projections, include budget state in contract version `1`, or establish
deterministic state reconstruction. Those claims remain `planned` until persisted events are folded and
compared with persisted projections.

---

## ADR-015 — Arbitrate idempotency with database constraints, not read-then-insert

**Status:** accepted

**Decision:** every idempotent write reserves its identity with a conflict-safe statement as the
first write of its transaction, and reads the constraint's answer. Campaign submission reserves
`(scope, idempotency_key)`; durable enqueueing reserves the instruction key; outcome finalisation
reserves the work item's single accepted outcome. No path decides idempotency by reading first and
inserting afterwards.

### Context

Three writes previously asked the database whether a record existed and then inserted one. Each
gap between the question and the answer is a window in which a second caller asks the same question
and gets the same answer. Under concurrency all callers pass the check, all attempt the insert, and
the constraint that was supposed to express the rule surfaces instead as an integrity error the
caller never asked for — a 5xx for a retry that should have been a replay, or an accepted outcome
misclassified as a retryable write failure.

`INSERT ... ON CONFLICT DO NOTHING ... RETURNING` closes the window. PostgreSQL waits for a
conflicting insertion to commit or abort before answering, so a caller that inserts nothing knows a
committed record holds the identity, and a caller that inserts knows it is the only one. The
identity a client chooses and the identity of the work are kept separate: a client token says "this
is my retry", while the instruction key says "this is that unit of work", and only the second
survives a delivery layer that redelivers under a new delivery identity.

### Consequences

- `idempotency_keys` is keyed by `(scope, idempotency_key)`, so a token chosen for one operation
  cannot answer a different operation with the first one's response;
- an idempotency record stores the canonical request fingerprint and the campaign it produced, the
  latter as a typed column under a foreign key that is `DEFERRABLE INITIALLY DEFERRED` so the
  reservation can precede the campaign row inside one transaction;
- same key and same canonical request returns the original result with `200` and `replayed: true`;
  same key and a different canonical request returns `409 idempotency_key_reused`;
- a missing, blank, or oversized client key is refused with a stable `400` code before any
  statement runs, rather than becoming a driver error mid-transaction;
- the durable instruction identity is derived from the work item and the command version, never
  from a client token; enqueueing the same instruction returns the existing job with `created`
  false, and one instruction key naming a different work item raises `InstructionConflictError`;
- outcome finalisation inserts the accepted outcome with no observation, arbitrated by the partial
  unique index, and closes the observation reference in the same transaction once it has won;
- a delivery that loses the acceptance race records `duplicate_suppressed` and writes no
  observation, no derived metric, no budget entry, and no `observation.accepted` or
  `work_item.accepted` event, while still recording its own attempt and outcome;
- the suppressed delivery consumed a real adapter call and a real object upload but appends no
  budget-ledger entry, unlike every failure path, so the ledger under-counts adapter calls by one
  per suppressed delivery. Recorded here rather than left implicit; budget accounting is outside
  this decision;
- the whole campaign submission, including the reservation, remains one transaction, so a failed
  submission frees the key rather than reserving it against work that does not exist.

### Limits

This decision covers idempotent identity and duplicate-effect suppression. It does not deliver lease
fencing beyond the existing lease token, heartbeat scheduling, lease reclamation, object
reconciliation, budget reservation, retry policy, cancellation, or deterministic replay. At-least-once
delivery with idempotent effect handling remains the protocol; nothing here may be described as
exactly-once.

One retention gap is carried forward rather than closed. A suppressed delivery received bytes and
records no `Observation` for them. That is sound only while its bytes are identical to the accepted
delivery's, in which case the accepted observation already content-addresses them at the same key.
The runtime does not verify the premise: the suppressed outcome records no digest of its own, so two
reads of one location that returned different bytes are indistinguishable from two that agreed, and
the diverging bytes would sit behind a `pending` object row with nothing referencing them. Read
strictly, that is short of invariant 1's companion retention rule in `AI_CONTRACT.md` invariant 2.
The behaviour predates this decision; what is new is that it is now written down. Closing it needs
the received payload digest recorded on the suppressed outcome, and MUST NOT be described as
satisfied until that exists.

---

## ADR-016 — Fence leases with a monotonic generation, and reconcile rather than delete

**Status:** accepted

**Decision:** every claim and every reclaim increments a per-job `lease_generation`. Ownership is
proven by job identity, lease token, generation, and a live expiry evaluated by the database, and it
is checked **inside** the transaction that writes an accepted effect. A heartbeat on an independent
connection extends the lease while work runs and surfaces its own refusal to the worker. One
reconciliation function reclaims expired leases, closes abandoned attempts, and classifies stored
objects; it runs at worker startup and behind `labbridge reconcile`. Reconciliation never deletes.

### Context

A lease token alone answers "are you the holder?" but not "is your answer current?". Two holders can
present valid-looking tokens across a reclaim, and nothing orders them. A monotonic generation makes
staleness decidable by comparison rather than by trusting a clock, and incrementing it *at the
reclaim* closes the window in which a fenced-out holder still looks current.

Checking ownership before opening the finalisation transaction proves nothing: the lease can lapse
and be reclaimed in the gap. `jobs.complete` re-checked at the end, but by then the observation, the
metrics and the events had been written, and the only remedy was a rollback that discarded the record
of what happened.

A killed worker leaves three kinds of debris — a lease nobody holds, an attempt stuck `running`, and
bytes with nothing pointing at them. Deleting the third is the tempting recovery action and the wrong
one: the unexplained object is the evidence that the failure happened.

### Consequences

- `jobs.lease_generation` is monotonic per job, never reset, and returned by `claim`; `_held` checks
  job, token, generation, and expiry together;
- `jobs.assert_held` runs as the first statement of the finalisation transaction, holding the job row
  with `FOR UPDATE` so the answer cannot change before the commit that depends on it;
- a heartbeat runs on its own connection, at a configurable interval, and updates only a row still
  matching owner, token and generation; a refusal is latched and re-raised on the worker's thread, so
  a stale execution cannot proceed to finalisation;
- reclaiming an expired lease increments the generation immediately, so the stale holder is fenced
  out whether or not anyone claims the job next;
- an attempt left `running` by a process that no longer holds its job becomes `lease_lost` — never
  `cancelled`, which means a campaign or an operator asked for the work to stop;
- **late-result policy**: a result returned after ownership is lost is refused from accepted
  scientific state. Its bytes, if already stored, are retained as a `received` observation under its
  own attempt; the outcome is `lease_lost` and its failure summary names the fencing-token mismatch.
  No accepted observation, no acceptance event, no metric. Any earlier accepted state stays
  authoritative;
- a duplicate-suppressed execution likewise retains its bytes as a `received` observation. Because
  `observation_id` is content-derived, an identical read lands the same identity under a different
  attempt — the match is a fact in the table rather than an assumption — and a divergent read lands a
  different identity and is visible;
- `uq_observations_one_accepted_per_work_item` makes at most one *accepted* receipt per work item a
  database guarantee; retained receipts sit outside the predicate;
- `storage_objects` records media type, staging attempt, work item, a classification and its reason;
  the five verdicts are `accepted_evidence`, `diagnostic_duplicate`, `diagnostic_orphan`,
  `quarantined`, and `missing`, decided by a pure function over gathered facts so the verdict is
  reproducible and states the evidence that produced it;
- a checksum disagreement is quarantined and neither side is trusted: the recorded digest is not
  refreshed and the object is not removed;
- an object the store cannot be asked about is left unclassified rather than judged, so an outage is
  never recorded as a fact about the bytes;
- reconciliation runs once at worker startup and behind `labbridge reconcile`, sharing one
  implementation. There is no reconciliation daemon.

### Limits

This decision covers ownership, liveness, recovery, and object classification. It does not deliver
budget reservation or accounting, retry backoff policy, retry caps, campaign pause, resume or
cancellation, deterministic replay, or event upcasting.

Execution-boundary information is preserved so that later budget accounting can distinguish work that
never began execution (an attempt with no staged object), work that began it, work that uploaded
bytes (a `storage_objects` row naming the attempt), and work that committed an accepted effect (a
`succeeded` outcome). The ledger itself is unchanged and still under-counts: a suppressed duplicate
consumed a real adapter call and appends no entry.

The attempt lifecycle has no `duplicate_suppressed` state, so a suppressed attempt is recorded as
`cancelled` while its outcome carries the real meaning. That is a temporary compromise, recorded as
one: no documentation or metric may read those attempt rows as user-requested cancellations.

---

## ADR-017 — Use a bounded in-repository parser for the first Gamry DTA CV variant

**Status:** accepted

**Decision:** Phase 4 uses a small, fail-closed parser owned by LabBridge for one pinned textual
Gamry DTA CV variant. It does not add `echemdb-converters` as a runtime dependency. Every attempt
creates a content-addressed parser record; accepted records enter the common CV transformation,
Passport, and Package contracts, while rejected records remain queryable without creating a partial
observation. A DTA-backed Experiment Package uses schema `2` and contains the parser record.

### Context

The documented `echemdb-converters` 0.4.1 Gamry loader was evaluated before implementation. Its
loader locates the first `CURVE` table and delegates table parsing to general tabular tooling. That
is useful for conversion workflows, but the inspected version does not enforce the Phase 4 boundary:
one technique, one Framework version, one table schema, an exact declared row count, rejection of
mixed table objects, durable failure diagnostics, and exact line locations for every accepted field.
Wrapping it would still require a second strict parser around its output while adding pandas,
clevercsv, unitpackage, and their transitive surface to this path.

The relevant inspected sources are the
[`gamryloader.py`](https://github.com/echemdb/echemdb-converters/blob/main/echemdbconverters/gamryloader.py)
implementation, its
[`pyproject.toml`](https://github.com/echemdb/echemdb-converters/blob/main/pyproject.toml), and Gamry's
documented [DTA object format](https://help.gamry.com/Framework/general-information_datafileformat.html).
The decision is about the inspected version and required contract, not a general quality judgment
about the converter project.

### Consequences

- the accepted variant is `TAG CV`, Framework `7.07`, exactly one `CURVE TABLE`, and the pinned
  column/unit layout in `artifacts/gamry-dta-cv/SUPPORT.md`;
- encoding and decimal convention come only from the immutable import profile;
- declared row counts, headers, units, leading DTA table fields, numeric cells, and trailing content
  are checked without dialect detection or row repair;
- `Vf`, `Im`, `T`, and `Cycle` enter the same explicit mapping and unit-conversion code used by
  generic CSV; `TITLE`, `NOTES`, and ignored table columns are preserved or named but not interpreted;
- `V vs. Ref.` converts the numeric potential dimension to volts without assigning a reference
  electrode or potential scale;
- accepted and rejected parser records are durable PostgreSQL evidence tied to exact Phase 1 bytes;
- DTA observations expose the same API, CLI, validation, Passport, and Package operations as generic
  CSV, with no vendor-only observation or report model;
- Package schema `1` remains verifiable for existing generic CSV evidence; schema `2` requires the
  parser member and producing parser version.

### Limits

This decision does not claim support for other Gamry Framework versions, other electrochemical
techniques, multiple curves, proprietary binary formats, automatic locale or unit detection,
reference-electrode interpretation, current-density normalisation, live instrument control, or
compatibility with vendor files outside the pinned fixture variants. Each wider variant requires a
new explicit contract, fixture, diagnostics, and differential proof before acceptance.

---

## ADR-018 — Keep EchemDB exchange behind a versioned evidence adapter

**Status:** accepted

**Decision:** Phase 6 exports one CV observation through evidence adapter `echemdb-cv/1`. The
LabBridge domain model remains unchanged and contains no EchemDB classes or field names. The adapter
targets EchemDB metadata-schema `0.8.3` at commit
`f48f583f83b1de9f5601d05dae5e5fcd1c25a3f0`, Data Package profile `2.0`, and Frictionless
`5.19.0`. Validation also pins `jsonschema` `4.26.0` and `referencing` `0.37.0`; the exact external
schema bytes are vendored for offline verification, and the EchemDB schema licence is retained beside
its vendored file.

Required target fields without source evidence are accepted only as known `user_supplied`
assertions. Export traces qualify those values as explicit external metadata that is not
source-declared. An `inferred` assertion cannot enter those fields. Unsupported or unknown metadata
is omitted, while the mapping report records every omission, companion-only field, and lossy
projection.

### Consequences

- every descriptor leaf and CSV cell has a trace to a LabBridge assertion, series, or observation;
- the companion manifest retains experiment, observation, source-artifact, transformation, origin,
  execution-mode, assertion, and series identities that the target schema cannot represent;
- `figureDescription.type` is supplied by an explicit assertion and recorded as a lossy projection
  because it cannot preserve `data_origin` and `execution_mode` as independent dimensions;
- potential values, current values, units, signs, reference scales, electrode roles, areas, scan
  rates, electrolyte compositions, and cycle meaning are never inferred or converted by the
  adapter;
- schema checks run from vendored bytes, and the Frictionless validator must report the exact pinned
  installed version before an artifact is valid;
- the field inventory, machine-readable mapping, human-readable mapping table, validation output,
  external versions, source bytes, and reproduction command are closed by one artifact manifest.

### Limits

This decision demonstrates only the project-owned synthetic CV fixture and the exact versions above.
It does not claim compatibility with other EchemDB metadata-schema versions, other Data Package
profiles, other Frictionless versions, other techniques, EchemDB ingestion or publication, or
scientific completeness of the explicit fixture declarations.

---

## ADR-019 — Keep electrolysis technique-specific and extend Package verification with schema 3

**Status:** accepted

**Decision:** Galvanostatic electrolysis uses dedicated profile, observation, transformation,
finding, and auxiliary-result persistence. A minimal normalised-observation identity registry is
shared because both CV and electrolysis Experiments require one referentially enforced root. Package
schema `3` carries electrolysis evidence and optional auxiliary source artifacts through the existing
independent verification entry point; CV schemas `1` and `2` retain their prior contracts.

### Consequences

- electrolysis requires explicit time, current or current-density, and potential mappings with
  role-compatible units and a distinct current quantity kind;
- electrical completeness requires aligned series, increasing time, and agreement with a declared
  sampling interval, and is reported separately from unavailable chemical analysis;
- CV-only scan-rate and cycle assertions are `not_applicable` for electrolysis;
- profile- and observation-owned electrolysis assertions cannot be supplemented or corrected within
  the same Experiment; corrected semantics require a new immutable profile and observation;
- auxiliary analytical declarations close to exact electrical and analytical source bytes, sample and
  collection-point identifiers, and declared method versions; their source locations are not parsed;
- no conversion, selectivity, yield, or Faradaic-efficiency derivation is approved by this decision;
- PostgreSQL references both technique-specific observation tables through the minimal shared
  identity registry, while normalised payloads remain immutable objects in S3-compatible storage.

### Limits

This decision does not add instrument control, chromatography ingestion, automatic product
assignment, mechanism attribution, or a generic workflow/technique abstraction. Any derived
chemical quantity requires a separate reviewed analysis contract with equations, dimensions,
method version, and complete provenance.

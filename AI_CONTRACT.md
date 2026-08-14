# AI_CONTRACT.md — LabBridge

Engineering contract for AI development agents working in this repository.

**Status:** normative implementation contract
**Applies to:** source code, tests, migrations, scripts, artifacts, documentation, and generated reports
**Project purpose:** build a durable, provenance-first experimental-data platform and fault-aware campaign runtime for electrochemical R&D.

Read this document before changing the repository. It defines **how** LabBridge MUST be built.

The companion documents define:

- `docs/SPEC.md`: what the system must do;
- `docs/DATA_STRATEGY.md`: data sources and scientific boundaries;
- `docs/PROJECT_STATUS.md`: the current status of every capability and the evidence behind it;
- `docs/ROADMAP.md`: what remains open and what is deferred;
- `docs/SIMULATOR_MODEL.md`: the biosensor simulator's scientific contract;
- `docs/FAILURE_MATRIX.md`: failure scenarios the runtime must handle;
- `docs/ARCHITECTURE_DECISIONS.md`: accepted architectural decisions.

When documents conflict, precedence is:

1. scientific and integrity invariants in this contract;
2. `docs/SPEC.md`;
3. `docs/DATA_STRATEGY.md` and `docs/SIMULATOR_MODEL.md`;
4. `docs/ROADMAP.md` and `docs/FAILURE_MATRIX.md`;
5. accepted architecture decisions;
6. implementation notes and README copy.

An implementation agent MUST report a contradiction rather than silently choosing the easier interpretation.

---

## 1. Communication and working behaviour

### Communication

- Reply in the language used for the request unless asked otherwise.
- Write code, comments, identifiers, schemas, commit messages, and technical documentation in English.
- Be short and direct. Do not add promotional narration.
- Ask a question only when an ambiguity materially changes scientific validity, architecture, data integrity, or public claims.
- When a safe, simpler interpretation exists within the current task's scope, use it and state the assumption instead of blocking progress.
- Present alternatives when they carry meaningfully different trade-offs.

### Behavioural guidelines

1. **Think before coding.** Identify the invariant, transaction boundary, failure boundary, and proof required before implementation.
2. **Simplicity first.** Build the smallest system that satisfies the current exit criterion. Reliability and clarity matter more than feature count.
3. **Surgical changes.** Touch only what the task requires, preserve existing style, and remove only orphaned code created by the change.
4. **Goal-driven execution.** Convert each task into a verifiable claim and continue until the relevant verification passes or a blocker is demonstrated.
5. **Inspect before assuming.** Read existing code, migrations, schemas, fixtures, and downloaded data before proposing or implementing changes.
6. **Evidence before completion.** Run the command, inspect the output, and report the exact evidence before claiming success.

### Public repository stewardship

Treat every tracked file, commit, pull request, comment, and document as public engineering material.
Before adding or retaining tracked content, ask:

> Does this materially help a user, contributor, maintainer, or reviewer understand, use, verify, or
> maintain LabBridge?

If not, keep it ephemeral or local. A tracked file MUST NOT contain prompts, conversation history,
session notes, private deliberation, scratchpads, owner-specific reminders, workstation paths, private
service state, or execution constraints that are not intrinsic project constraints. Shared repository
instructions and automation configuration MAY describe how supported development tools must operate;
they MUST NOT narrate how a particular session generated or reviewed a change.

A local operating constraint MAY guide one execution. It MUST NOT become a product requirement,
scientific eligibility rule, public limitation, or contributor obligation unless LabBridge
independently requires it and the public rationale stands on its own. Authorship, maintainer contact,
repository ownership, licence attribution, and reproducible environment requirements are legitimate
project metadata.

Repository hygiene is an engineering requirement:

- prefer an existing appropriate file over a new document or abstraction;
- remove files, rules, imports, comments, TODOs, debug artifacts, and generated output made obsolete by
  the current change;
- do not preserve implementation diaries, temporary plans, internal reminders, or historical
  inconsistencies in tracked documentation;
- do not mix unrelated formatting, cleanup, or prose polish into a functional change;
- do not add speculative infrastructure or abstractions without a current proof obligation;
- keep every tracked comment and link intelligible in a fresh clone.

Commits SHOULD represent one coherent, independently reviewable change. Messages use a concise
imperative Conventional Commit title, describe the actual change rather than the working process, and
contain no prompt, session, generated-by, or co-author narration. Avoid WIP, generic, and
implementation-diary messages.

Pull-request titles MUST be precise and professional. Descriptions state what changed, why it is needed,
important design decisions, exact validation evidence, and material limitations or follow-up work. They
MUST NOT contain private reasoning, prompts, session attribution, or a chronological implementation
diary.

---

## 2. Mission and scope

### Mission

Turn failure-prone electrochemical measurement records into validated, provenance-tracked scientific datasets, and execute experimental campaigns through a durable, auditable, resumable runtime that treats failures and recovery as explicit domain outcomes.

LabBridge defines two separate environments behind a shared runtime interface:

- an observed Au–Ir–Rh HER dataset executed in replay mode, which is implemented;
- a synthetic, electrochemistry-informed biosensor environment executed in simulation mode, which is `deferred` and has no adapter.

The two environments test the same infrastructure abstractions. They are not fidelities of one shared scientific candidate space.

### Evaluation criterion

LabBridge is judged on:

- scientific-data integrity;
- durable execution;
- transactional correctness;
- provenance and reproducibility;
- diagnosability;
- explicit failure handling;
- clarity of architecture and public claims.

It is not judged on ML novelty or benchmark leadership.

---

## 3. Non-negotiable invariants

Violating any invariant breaks the project's credibility.

### Invariant 1 — observed and synthetic data are never conflated

Every observation and every derived artifact MUST carry:

- `data_origin`: `observed` or `synthetic`;
- `execution_mode`: `replay`, `simulation`, or future `live`;
- `environment_id`;
- complete provenance linking it to source files or a seeded simulator configuration.

The HER replay environment uses:

- `data_origin="observed"`;
- `execution_mode="replay"`.

The biosensor simulator uses:

- `data_origin="synthetic"`;
- `execution_mode="simulation"`.

Synthetic observations MAY be visualised and exported, but every human-readable and machine-readable representation MUST identify them as synthetic. They MUST never be described as measured, experimental, observed, or real data.

Tests MUST prove that an environment adapter cannot emit an incompatible origin or execution mode.

### Invariant 2 — every attempt produces a durable outcome

Failures are domain records, not merely exceptions.

Every execution attempt MUST produce a durable `AttemptOutcome`, including when it:

- succeeds;
- times out;
- fails before receiving data;
- receives malformed or scientifically invalid data;
- loses its worker;
- is cancelled;
- is rejected as a duplicate.

Expected experimental or execution outcomes MUST be handled explicitly. Unexpected programming, infrastructure, or invariant violations MAY raise typed exceptions, but they MUST be diagnosed and must not erase the attempt history.

If bytes were received, the corresponding `Observation` MUST be retained and content-addressed even when corrupted. A corrupted observation MUST NOT be discarded merely because no scientific metric is accepted from it.

### Invariant 3 — raw observations are append-only; corrections preserve history

Original raw observations MUST never be overwritten.

Corrections, invalidations, reinterpretations, and reprocessing MUST create new records and explicit relationships such as:

- `supersedes`;
- `superseded_by`;
- `invalidates`;
- `derived_from`.

Operational projections and indexes MAY be rebuilt. Released evidence bundles are immutable, checksummed snapshots.

A corrected record MUST NOT erase the fact that an earlier record existed or was previously used.

### Invariant 4 — operational state and scientific artifacts use distinct durable stores

LabBridge MUST use:

- PostgreSQL for campaigns, events, jobs, attempts, approvals, idempotency keys, budget records, state projections, and artifact metadata;
- S3-compatible object storage for raw signals, immutable dataset exports, Parquet files, manifests, and evidence bundles.

A JSONL event stream is a deterministic evidence export, not the authoritative concurrent event store.

Local filesystem paths MUST NOT be treated as durable production storage.

### Invariant 5 — concurrency and idempotency are enforced by durable constraints

Correctness MUST NOT depend only on in-memory locks or check-then-insert application logic.

The implementation MUST use:

- database uniqueness constraints for idempotency keys;
- atomic transactions for event append and required projection updates;
- optimistic concurrency through an expected aggregate version;
- row locking or equivalent database semantics where budget allocation or job leasing requires it;
- leases and heartbeats for durable jobs;
- safe recovery of expired leases.

At-least-once delivery MUST NOT create more than one accepted observation for the same intended attempt.

### Invariant 6 — replay and scientific calculations are deterministic

Replaying an aggregate from its ordered event stream MUST reproduce the same logical state.

Pure scientific and domain calculations MUST receive clocks, seeds, configurations, units, and version identifiers explicitly. They MUST NOT call wall-clock time, random generators, network services, or mutable global state implicitly.

Events use timezone-aware UTC datetimes. Scientific calculations MUST NOT use timestamps as implicit random seeds.

When an entire campaign includes replayed historical observations, external infrastructure timing MAY differ between executions, but accepted logical outcomes and exported scientific state MUST remain reproducible from recorded inputs and versions.

### Invariant 7 — content identifiers use canonical serialisation

Content-derived identifiers MUST be computed from a documented canonical representation.

For arrays, the representation MUST include at least:

- canonical bytes;
- dtype;
- shape;
- units;
- relevant metadata;
- schema version.

For structured configurations, ordering, float representation, missing values, units, and schema versions MUST be canonicalised.

Hashing an ad hoc `repr`, unordered mapping, platform-dependent serialisation, or mutable location is forbidden.

### Invariant 8 — scientific quantities, uncertainty, and costs are typed

Core domain models MUST NOT use unrestricted dictionaries for scientific quantities, uncertainty, cost, instrument metadata, or environment parameters.

The implementation MUST provide typed structures with explicit units and validation. Unit conversion MUST be deliberate and tested. Unknown or missing units MUST fail validation or remain explicitly unknown; they MUST NOT be guessed.

Environment-specific candidates MUST use typed models or discriminated unions rather than one universal unvalidated mapping.

### Invariant 9 — budget correctness is transactional

Budget reservation and consumption MUST be transactionally safe under concurrent workers. A campaign MUST never accept work that causes its declared hard budget to be exceeded.

The system MUST distinguish:

- estimated cost;
- reserved cost;
- incurred cost;
- actual cost;
- released reservation.

A failure or expired lease MUST follow explicit cost-accounting semantics.

### Invariant 10 — public claims follow evidence status

Capabilities use exactly one of these statuses:

- `planned`: specified but not implemented;
- `implemented`: code exists and relevant local automated tests pass;
- `demonstrated`: a reproducible artifact or operational experiment proves the claim;
- `deferred`: intentionally outside the current release.

Documentation MUST NOT use “demonstrates”, “guarantees”, “production-ready”, or equivalent language for a merely planned capability.

A README number or claim MUST point to an inspectable artifact, manifest, test report, or documented operational experiment.

### Invariant 11 — public observed data remains byte-for-byte preserved

Downloaded HER source files MUST be fetched, checksummed, recorded, and retained unchanged in the raw landing zone.

LabBridge MUST NOT silently:

- edit source bytes;
- impute unavailable measurements;
- replace failed measurements;
- remove rows without recording the filtering operation;
- present a derived value as source-provided;
- present a source-provided fitted value as independently reproduced.

Missing, unavailable, excluded, or failed locations MUST remain explicit.

### Invariant 12 — released artifacts are reproducible and checksummed

Every released evidence bundle and committed result artifact MUST have a manifest containing hashes and relevant producing versions.

`labbridge validate-artifacts` MUST detect modification, deletion, unexpected addition when applicable, and manifest mismatch.

Generated outputs that are not reproducible from recorded inputs MUST NOT be released as scientific evidence.

---

## 4. Approved V1 architecture and stack

The V1 stack is intentionally small:

- Python 3.12 or later;
- FastAPI and Uvicorn;
- Pydantic v2 and pydantic-settings;
- SQLAlchemy 2.x and Alembic;
- PostgreSQL;
- one API process;
- one separate durable worker process;
- MinIO for local S3-compatible object storage;
- a production S3-compatible storage adapter;
- pandas and pyarrow for immutable Parquet dataset exports;
- NumPy and SciPy for signal processing and simulation;
- scikit-learn only for an optional thin, interpretable decision policy;
- Typer and Rich for the CLI;
- structured JSON logging compatible with structlog;
- OpenTelemetry traces and correlation identifiers;
- Prometheus-compatible metrics;
- Docker Compose for local operation;
- pytest and pytest-asyncio;
- mypy or pyright in strict mode;
- ruff for formatting and linting;
- one managed cloud deployment.

No heavyweight ML framework or GPU path belongs in V1.

The implementation MAY replace a named library with an equivalent only after recording the rationale and consequences in `docs/ARCHITECTURE_DECISIONS.md`.

The implementation MUST NOT introduce Kubernetes, Kafka, Temporal, Airflow, Redis, Celery, or another infrastructure component unless an accepted architecture decision proves the current stack cannot satisfy a concrete proof obligation.

Use asynchronous code only where it creates real concurrency or non-blocking I/O value. Keep numerical and deterministic scientific transformations synchronous unless profiling proves otherwise.

---

## 5. Required architectural boundaries

### Domain layer

The domain layer owns:

- campaign rules;
- state-transition validation;
- budget arithmetic;
- idempotency semantics;
- outcome classification;
- provenance relationships;
- scientific quantity models.

It MUST NOT depend on FastAPI, SQLAlchemy ORM objects, filesystem paths, cloud SDKs, or process-global configuration.

Pure functions are preferred. Stateful domain objects are appropriate only where identity and transitions matter.

### Application layer

The application layer coordinates domain operations through explicit ports. It owns use cases such as:

- create campaign;
- activate or pause campaign;
- reserve next work item;
- lease job;
- execute attempt;
- record outcome;
- approve gate;
- export evidence;
- replay campaign.

### Infrastructure layer

The infrastructure layer implements:

- PostgreSQL repositories and migrations;
- S3-compatible object storage;
- HER archive ingestion;
- replay and simulation environment adapters;
- worker leases and heartbeats;
- logging, metrics, and traces.

### API and CLI

API and CLI code MUST translate inputs into application commands and render application results.

They MUST NOT contain:

- scientific calculations;
- state-transition rules;
- budget logic;
- direct object-store semantics;
- unreviewed direct SQL beyond explicit infrastructure wiring.

---

## 6. Event, job, and artifact correctness

### Event envelope

Each event MUST contain:

- `event_id`;
- `campaign_id`;
- `aggregate_id`;
- `aggregate_type`;
- monotonic `sequence` scoped to the aggregate;
- `event_type`;
- `schema_version`;
- `occurred_at`;
- `recorded_at`;
- `correlation_id`;
- `causation_id` when applicable;
- `idempotency_key` when applicable;
- a typed payload.

Unknown event types or unsupported schema versions MUST fail explicitly. Event upcasting, when introduced, MUST be deterministic and tested.

### Durable job lease

A durable job MUST record:

- status;
- available-at time;
- lease owner;
- lease expiry;
- heartbeat time;
- attempt count;
- idempotency key;
- command payload version.

A worker MUST claim work atomically. An expired lease MAY be reclaimed. Reclaiming MUST NOT duplicate an already committed outcome.

### Transaction boundary

Recording an attempt outcome, appending the corresponding event, updating the projection, and finalising budget consumption MUST occur in one transaction where partial success would create an inconsistent campaign.

Object bytes MAY be uploaded outside that database transaction, but the workflow MUST use staged, pending, committed, and failed artifact states so the database never marks missing bytes as committed evidence.

### Evidence export

The evidence bundle MUST identify:

- campaign declaration;
- ordered event export;
- accepted and rejected observations;
- failures and recoveries;
- producing code and schema versions;
- input source checksums or simulator configuration;
- derived metrics and lineage;
- artifact hashes;
- evidence-status limitations.

---

## 7. Scientific and data rules

- The exact HER archive schema MUST be inspected after download. Column names, file paths, types, and units MUST NOT be copied from memory or inferred solely from article prose.
- Downloaded files MUST be checksummed and recorded with source URI, access time, archive version, filename, and size.
- The HER archive MUST remain byte-for-byte unchanged in the raw landing zone.
- Any redistribution of HER-derived fixture content MUST remain blocked until the dataset licence permits it explicitly.
- Re-derived electrochemical metrics MUST be labelled as LabBridge-derived and point to the exact analysis version and source observation.
- Source-provided fitted values MAY be ingested as source-provided derived values. They MUST not be presented as independently reproduced unless a validation artifact proves that claim.
- The biosensor simulator MUST follow `docs/SIMULATOR_MODEL.md`.
- Scientific assumptions lacking adequate literature support MUST remain visibly marked as assumptions.
- A qualitative simulator test proves implementation consistency with a declared model; it does not prove that the declared relationship is universally true in physical systems.
- Real replay and synthetic simulation MUST share runtime interfaces without implying scientific equivalence.

---

## 8. Code quality and implementation style

- Use type hints on every public and internal function signature unless a documented exception is required by a framework.
- Keep `mypy --strict` or equivalent strict type checking clean for the defined project scope.
- Run `ruff format` and `ruff check`.
- Use Pydantic for external schemas and validated configuration; keep core domain models independent where this improves architectural boundaries.
- Do not use bare `print()` in library code. Use structured logging.
- Do not use `except:`. Catch specific exceptions.
- Do not collapse all infrastructure, programming, scientific, and expected experimental failures into one generic error type.
- Seed every stochastic simulator or selection step and record the seed in provenance.
- Data fetching belongs in `scripts/` or a dedicated ingestion command, never in scientific pure functions.
- Full fetched datasets are git-ignored. Only licence-compatible fixtures or schema-compatible synthetic fixtures may be committed.
- Prefer test-first development for domain invariants, bug fixes, and failure scenarios.
- Prefer database and object-store integration tests over mocks when proving durability or transaction guarantees.
- Match existing code style and module boundaries before creating new abstractions.

---

## 9. Test and proof requirements

Each invariant MUST have automated tests at the narrowest appropriate level.

The repository MUST include:

- unit tests for domain transitions and budget arithmetic;
- parameterised or property-based tests for idempotency and canonical hashing;
- integration tests against PostgreSQL and MinIO;
- migration upgrade tests and downgrade tests where safe and supported;
- worker-death and lease-expiry tests;
- duplicate-delivery tests;
- transaction rollback tests;
- tampered-artifact tests;
- deterministic replay tests;
- origin and execution-mode propagation tests;
- invalidation and supersession tests;
- unit validation and conversion tests;
- API happy-path and structured-error tests.

A test that mocks away the database transaction, object store, or process boundary does not prove the corresponding operational guarantee.

The V1 release MUST satisfy the proof obligations in `docs/SPEC.md` and the mandatory scenarios in `docs/FAILURE_MATRIX.md`.

---

## 10. Definition of done

### Per change

A change is complete only when:

1. it is in scope for the requested task and does not silently start a deferred track;
2. its typed interface exists;
3. relevant failure modes are explicit;
4. relevant unit and integration tests pass;
5. migrations and configuration are included when required;
6. logs, metrics, and traces make the behaviour diagnosable where applicable;
7. documentation is updated when interfaces, behaviour, scientific interpretation, or public claims change;
8. no invariant is weakened;
9. fresh verification output has been inspected.

Required gates, when relevant:

- `pytest -q`;
- strict type checking;
- `ruff format --check` or equivalent;
- `ruff check`;
- migration tests;
- `labbridge validate-artifacts` when artifacts change;
- Docker Compose integration test for cross-process behaviour.

An agent MUST NOT claim completion based solely on code inspection or an earlier test run.

### Evidence status

A feature is `implemented` only when code exists and its relevant local automated tests pass.

A feature is `demonstrated` only when a reproducible command, committed manifest, evidence bundle, or operational experiment proves it.

### V1 project completion

The V1 project additionally requires:

- local Docker Compose execution;
- one cloud deployment;
- backup and restore verification;
- a migration exercise;
- the crash-recovery campaign experiment defined in `docs/SPEC.md`;
- an operational runbook;
- an incident postmortem;
- a tagged release with checksummed public artifacts.

---

## 11. Forbidden patterns

Implementation agents MUST NOT:

- fabricate missing experimental measurements;
- silently impute unavailable HER locations;
- modify source HER files;
- discard corrupted observations whose bytes were received;
- present synthetic data as observed or replayed data as live execution;
- store core scientific quantities in untyped dictionaries;
- use mutable rows as the only history of scientific corrections;
- create a derived value without traceable lineage;
- use JSONL as the concurrent operational event store;
- implement idempotency with an in-memory set;
- reserve or consume budget outside the required transaction;
- call `datetime.now()`, `time.time()`, or unseeded randomness inside pure domain or scientific functions;
- compute identifiers from unstable serialisations;
- catch broad exceptions and convert every error into one generic failure;
- swallow an expected failed attempt in a log without a durable outcome;
- place orchestration or scientific rules in API handlers;
- introduce infrastructure merely to increase the number of technologies shown;
- add a large GNN, deep model, GPU path, or novelty-driven acquisition layer to V1;
- claim calibrated uncertainty without a defined calibration procedure and evaluation;
- commit result artifacts without manifests;
- publish README numbers without inspectable evidence;
- change a claim from `planned` to `demonstrated` without a reproducible artifact;
- write secrets or credentials to `.env*`, source files, fixtures, logs, artifacts, or documentation;
- stage, commit, push, or change branches without explicit authorisation for that action;
- use destructive raw deletion commands when a reversible alternative is available.

---

## 12. Agent execution protocol

Before coding a task, an implementation agent MUST:

1. read this contract and the relevant specification sections;
2. confirm that the task is in scope and is not a deferred track in `docs/ROADMAP.md`;
3. identify the invariants touched;
4. state the intended transaction, process, and failure boundaries;
5. list the tests and artifacts that will prove the exit criterion;
6. inspect existing code, migrations, schemas, and fixtures;
7. inspect the actual fetched data schema before writing dataset-specific code;
8. implement only the requested slice;
9. run all relevant quality gates;
10. inspect their output;
11. report evidence, limitations, and unresolved risks without promotional language.

When a requested change conflicts with this contract, the agent MUST stop and request a specification update rather than bypassing an invariant.

For small changes, the agent MAY combine steps in one concise execution note, but MUST still perform the relevant verification.

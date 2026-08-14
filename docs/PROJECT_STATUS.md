# LabBridge — project status

**Status:** canonical statement of current capability and evidence
**Companion:** [`ROADMAP.md`](ROADMAP.md) lists what is still open and what is deferred

This document is the single place where a capability's status is decided. Where any other document
implies a stronger status than the table below, this document is correct.

## How to read a status

Every material capability carries exactly one status.

| Status | Means | What it takes to move up |
|---|---|---|
| `planned` | Specified, not implemented | Code |
| `implemented` | Code exists and its relevant local automated tests pass | A reproducible artifact or an operational experiment |
| `demonstrated` | A committed, reproducible artifact or operational experiment proves it | — |
| `deferred` | Intentionally outside the current release | A product decision |

Code existing is never sufficient for `demonstrated`. Tests passing is never sufficient for
`demonstrated`. Only an inspectable artifact that can be named is.

Four further distinctions matter and are never collapsed:

- **Completeness** — the declared evidence a Package should carry is present. It is not
  reproducibility.
- **Integrity** — retained bytes still hash to their recorded checksums. It is not scientific
  validity.
- **Scientific validity** — whether a measurement means what it appears to mean. LabBridge does not
  assess this.
- **Reproducibility** — whether an experiment repeated would yield the same result. LabBridge does
  not assess this either.

Package verification proves integrity and reports completeness. It is not an experimental
certification.

## File-to-Package path

| Capability | Status | Evidence and boundary |
|---|---|---|
| Opaque source intake, exact-byte retrieval, and integrity verification | `demonstrated` | [`artifacts/source-capture`](../artifacts/source-capture). Retention and checksum verification of exact bytes only; assigns no CSV semantics |
| Explicit generic CV CSV ingestion and closed normalisation lineage | `demonstrated` | [`artifacts/cv-ingestion`](../artifacts/cv-ingestion). Parser, mapping, unit, structural, identity, and lineage behaviour for one committed profile; claims no electrochemical validity |
| Append-only Experiment assertions and deterministic release validation | `implemented` | [`artifacts/experiment-passport`](../artifacts/experiment-passport) |
| JSON/HTML Experiment Passport and independently verified Experiment Package | `implemented` | Same artifact: initial and superseding Passports, independent CLI verification, and byte-level proof that one user assertion changed neither the source assertion nor the prior release |
| Bounded Gamry DTA Framework 7.07 CV ingestion and retained parser diagnostics | `implemented` | [`artifacts/gamry-dta-cv`](../artifacts/gamry-dta-cv) and its [`SUPPORT.md`](../artifacts/gamry-dta-cv/SUPPORT.md). One pinned textual variant; every other Framework version, technique, and table layout fails closed |
| Galvanostatic electrolysis electrical time series and Package schema `3` | `implemented` | [`artifacts/galvanostatic-electrolysis`](../artifacts/galvanostatic-electrolysis). Time, current, and potential series only; chemical analysis reported as unavailable |
| EchemDB-aligned CV export | `implemented` | [`artifacts/echemdb-cv-exchange`](../artifacts/echemdb-cv-exchange) and its [`MAPPING.md`](../artifacts/echemdb-cv-exchange/MAPPING.md). Validated against one pinned EchemDB metadata-schema commit and one pinned Frictionless version; not a general EchemDB compatibility claim |
| Single-user interactive CV Passport demo | `implemented` | [`artifacts/cv-passport-demo`](../artifacts/cv-passport-demo): browser trace, built-asset manifest, downloaded Package, independent CLI verification. Two human acceptance records remain outstanding — see [`ROADMAP.md`](ROADMAP.md) |

Every `implemented` capability above holds a committed candidate artifact. Each is held below
`demonstrated` by outstanding clean-checkout reproduction, not by missing code. The demo is
additionally held by the two human acceptance records in [`ROADMAP.md`](ROADMAP.md).

## Campaign runtime

| Capability | Status | Evidence and boundary |
|---|---|---|
| Process-boundary campaign fault injection | `demonstrated` | [`artifacts/fault-campaign`](../artifacts/fault-campaign) |
| Deployment restore and the seeded synthetic-replay fault campaign | `demonstrated` | Same artifact |
| HER source acquisition, checksums, and landing provenance | `implemented` | Code and tests; the acquired archive is git-ignored |
| Licence and data-use gate, resolved only by a recorded decision | `implemented` | ADR-009; the licence parser always yields `unresolved`, so a record that stops declaring the licence reopens the gate |
| Archive inspection and versioned dataset inventory | `implemented` | Records actual paths, member tables, and column shapes rather than inferring a schema |
| Independently generated schema-compatible HER fixture | `implemented` | Copies no source values |
| HER replay adapter, with origin decided by the evidence on disk | `implemented` | A `fixture_manifest.json` means `synthetic + replay`; a `provenance.json` means `observed + replay`; a root holding both fails |
| Typed quantities, candidates, observations, outcomes, and derived metrics | `implemented` | Domain tests |
| Canonical serialisation and content-derived identity | `implemented` | Property tests |
| Campaign, work-item, and attempt state machines | `implemented` | Transition tests |
| PostgreSQL schema, constraints, and migrations | `implemented` | Migration tests |
| Object storage with read-back checksum verification and a pending/committed lifecycle | `implemented` | Integration tests against MinIO |
| Durable jobs with atomic claim, lease fencing, heartbeats, and lease recovery | `implemented` | Integration tests against PostgreSQL |
| Constraint-arbitrated idempotency for submission, enqueueing, and outcome acceptance | `implemented` | Each decided by a database constraint reached through a conflict-safe insert, tested under real concurrency |
| Worker-startup and CLI reconciliation, including non-deleting object classification | `implemented` | Unexplained bytes are classified, never deleted |
| Retry scheduling | `implemented` | Durable and bounded; on-demand rather than a running scheduler |
| Typed, version-checked event append with aggregate and campaign ordering | `implemented` | Unknown types and unsupported schema versions fail explicitly |
| Explicit legacy/incomplete event-stream boundary and validated replay input | `implemented` | Pre-contract streams are excluded rather than guessed |
| Deterministic state reconstruction from the event log | `implemented` | Replay reproduces logical state; infrastructure timing may differ |
| Append-only budget ledger, written in the outcome transaction | `implemented` | Attempt and actual cost retained even for suppressed duplicates |
| Budget reservation and hard stopping rules | `implemented` | Transactional under concurrent workers |
| Evidence bundles, local bundle checks, and full stored-object verification | `implemented` | `--mode bundle-only` reports `partial`; `--mode full` reports `complete` only when every referenced object matches |
| Campaign submission API with idempotency keys | `implemented` | API tests |
| Campaign control endpoints, observability, and operator runbook | `implemented` | [`OPERATOR_RUNBOOK.md`](OPERATOR_RUNBOOK.md) |
| Enzymatic-biosensor simulator | `deferred` | Contract retained in [`SIMULATOR_MODEL.md`](SIMULATOR_MODEL.md); no adapter exists |
| Model-based selection policy beyond a seeded random baseline | `deferred` | — |

### What the campaign reliability artifact measured

The committed run executed 100 seeded campaigns covering all six declared process-termination
boundaries: after lease acquisition, after adapter return before upload, during object upload, after
upload before the outcome transaction, after commit before acknowledgement, and during evidence
export.

It recorded zero lost accepted observations, zero unintended duplicate acceptances, zero hard-budget
overspends, zero projection mismatches, and zero Package-verification failures. It also recorded 51
`lease_lost` failures and 100 suppressed redeliveries. The deployment restore reverified 100 stored
objects, replayed 100 campaigns, and fully verified 100 restored campaign Packages.

Campaign cancellation with a leased job is exercised across the same kind of process boundary: an
already-leased job may finish, while new claims and retries are rejected and any received bytes
remain evidence ([`FAILURE_MATRIX.md`](FAILURE_MATRIX.md) F-034).

These are software-behaviour measurements on generated synthetic bytes in replay mode. They are not
scientific results, not observed-data results, and not live-instrument results. Observed HER replay
and live execution were not run.

## Known limitations

- No scientific number in this repository comes from a released physical measurement run.
- The end-to-end campaign path runs on the generated fixture. `labbridge demo her` completes and its
  bundle verifies, but every record it produces is synthetic.
- At-least-once delivery with idempotent effect handling is the worker protocol. Duplicate submission,
  enqueueing, and acceptance are each suppressed by a database constraint, and that suppression is
  tested under real concurrency. An accepted work item can still be re-executed by a redelivery,
  costing an adapter call and an object upload before the acceptance claim refuses it. Suppression is
  proven; avoiding the wasted execution is not attempted.
- A suppressed duplicate retains its received bytes as a non-accepted observation under its own
  attempt, so identical and divergent reads stay distinguishable.
- Experiment Passport validation reports declared evidence completeness. A released Package may retain
  warnings and unknowns; release asserts neither scientific validity, data quality, nor experimental
  reproducibility.
- Gamry support covers one pinned Framework 7.07 CV layout. It is not general Gamry support. The
  parser retains source-field locations and diagnostics without inferring a reference scale, a
  potential correction, or a working-electrode area, and it does not normalise current to current
  density.
- The EchemDB export is validated against pinned external versions. It is not general EchemDB
  compatibility, and it is not EchemDB submission.
- Galvanostatic electrolysis covers generic CSV electrical time series. The Passport and Package
  report no conversion, selectivity, yield, product assignment, or Faradaic efficiency, and the
  capability excludes instrument control and chromatography ingestion.
- The acquired HER archives and the generated fixture are git-ignored. A clean checkout has no data
  until `labbridge fetch-her` or `labbridge build-her-fixture` produces it.
- The integration suite requires PostgreSQL and MinIO and skips loudly when they are absent rather
  than passing vacuously.
- Nothing here has been deployed or operated outside local Docker Compose.

## Scientific boundaries

- Observed and synthetic data are never conflated. Every observation and derived artifact carries
  `data_origin`, `execution_mode`, `environment_id`, and provenance linking it to source files or to a
  seeded simulator configuration.
- Synthetic output may be exported and visualised, but every human-readable and machine-readable
  representation identifies it as synthetic. It is never described as measured, experimental, or
  observed.
- User-supplied metadata stays identified as user-supplied. Unknown metadata stays unknown. An
  `inferred` value carries its method, version, evidence, and confidence representation, and cannot be
  relabelled as `source_file` or `user_supplied`.
- Structural validation is not scientific validation. LabBridge checks that a value resolves to a
  source column, a declared transformation, and a unit. It does not check that the value is
  physically correct.
- The demo's `RHE` value is an operator declaration. LabBridge does not infer it, validate it as
  physically correct, or convert potential values to that reference scale.

Full definitions are in [`DATA_STRATEGY.md`](DATA_STRATEGY.md).

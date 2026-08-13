# LabBridge — product and implementation roadmap

**Status:** normative public roadmap
**Product thesis:** turn experimental electrochemistry files into provenance-backed,
reproducibility-checked, publication-ready experiment packages
**Active wedge:** file ingestion and Experiment Passport
**Secondary path:** fault-aware campaign execution

This roadmap orders work by scientist-visible vertical slices. A phase is not complete because its
classes, tables, or commands exist. Completion requires the stated automated proofs and an inspectable
artifact. Capability status uses the vocabulary defined in ADR-008: `planned`, `implemented`,
`demonstrated`, or `deferred`.

---

## Product paths

```text
File path
source file → immutable raw artifact → explicit mapping → validation → normalisation
            → Experiment Passport → manifest-verified Experiment Package

Campaign path
campaign → durable job → adapter → observation → metric → evidence bundle

Shared integrity layer
content identity · provenance · versions · append-only relations · manifests · verification
```

The file path is the primary product wedge. The campaign path remains a supported source of
observations and a reliability test bed. It does not gate the first useful file-to-package slice
unless a specific runtime mechanism is required to uphold source retention, lineage, or package
verification.

## Current capability map

| Capability | Current status | Evidence boundary |
|---|---|---|
| Pinned HER acquisition and archive inspection | `implemented` | Code and tests exist; no committed demonstration package |
| Typed observations, provenance, canonical identity, and object checksums | `implemented` | Existing domain and storage tests |
| Durable campaign jobs, lease fencing, heartbeats, and reconciliation | `implemented` | Existing local/integration tests; no released fault report |
| Campaign evidence bundle verification | `implemented` | Current bundle schema is campaign-specific |
| Opaque source capture and exact-byte integrity verification | `demonstrated` | Reproducible `synthetic + replay` artifact under `artifacts/source-capture` |
| Generic CV CSV ingestion | `demonstrated` | Reproducible artifact under `artifacts/cv-ingestion` |
| Experiment metadata origin/transformation model | `implemented` | Versioned append-only schema, migration, application service, and tests |
| Deterministic validation report and Experiment Passport | `implemented` | JSON/HTML reports and reproducible candidate artifact under `artifacts/experiment-passport` |
| Experiment Package | `implemented` | Closed ZIP manifest, independent CLI verifier, tamper tests, and candidate artifact |
| Single-user interactive CV demo | `planned` | No frontend, demo API, browser proof, screenshot, or demo asset |
| Gamry DTA ingestion | `implemented` | Bounded Framework 7.07 CV parser, retained diagnostics, shared pipeline, and candidate artifact under `artifacts/gamry-dta-cv` |
| Galvanostatic electrolysis support | `planned` | Technique contract not implemented |
| EchemDB-compatible export | `planned` | Mapping and validation proof do not exist |
| Enzymatic-biosensor simulator | `deferred` | Scientific contract retained in `SIMULATOR_MODEL.md` |

The source-capture capability is `demonstrated` only at the opaque byte-retention boundary. The
artifact assigns no CSV semantics and does not demonstrate generic CV ingestion.

---

## Phase 0 — publish the product contract

**Status:** `implemented` as documentation in the current worktree; not a product demonstration.

### Objective

Make the file-first product thesis, the secondary campaign path, evidence statuses, and ordered
delivery plan consistent across the public documentation.

### Implementation work

- publish this roadmap and include it in the documentation checksum manifest;
- align the README, technical specification, data strategy, failure matrix, simulator status, and
  architecture decisions;
- keep historical ADRs unchanged and record superseding decisions as new ADRs;
- preserve current runtime implementation work without presenting it as file-ingestion support.

### Scientific and data work

- define the difference between metadata origin and transformation;
- make explicit mapping and units mandatory for generic CSV ingestion;
- bound the meaning of validation, reproducibility checks, and publication readiness;
- document EchemDB alignment as a future mapping exercise rather than present compatibility.

### Acceptance criteria

- all public documents resolve their local links;
- no current capability is strengthened beyond its artifact evidence;
- `docs/ROADMAP.md` is tracked rather than ignored;
- `SHA256SUMS.txt` covers all six public specification documents and verifies byte-for-byte.

### Automated proofs

- `python scripts/check_docs.py --strict`;
- `sha256sum -c SHA256SUMS.txt` or an equivalent SHA-256 check on Windows;
- `git diff --check`.

### Artifact

The six checksummed public specification documents and this roadmap's manifest entry.

### Out of scope

Product code, schema migrations, parser code, UI, deployment, commit, and push.

---

## Phase 1 — minimum file-integrity seam

**Status:** `demonstrated` by the reproducible `synthetic + replay` source-capture artifact.

### Objective

Expose only the existing integrity mechanisms required by file intake through reusable application
services, and prove that arbitrary source bytes can be retained and verified without a campaign or a
dataset-specific parser.

### Implementation work

- introduce a `SourceArtifact` identity from exact bytes, media type, size, and SHA-256;
- define application-service ports for source intake, object persistence, source-artifact lookup, and
  manifest verification so CLI and future HTTP adapters call the same use cases;
- reuse the existing object-store read-back checksum and manifest verification mechanisms rather than
  creating a file-demo path;
- define idempotent intake semantics for the same bytes and stable conflict semantics for a reused
  request identity with different bytes;
- keep persistence and object-storage transactions explicit, including staging, commit, quarantine,
  and reconciliation after partial failure;
- provide one independently generated synthetic CV-shaped byte fixture as an opaque source artifact;
  Phase 1 does not interpret its columns.

### Scientific and data work

- label the fixture `synthetic + replay` in its filename, metadata, manifest, and human-readable
  description;
- retain filename and media type as descriptive metadata, never as scientific semantics;
- confirm that no Phase 1 service assigns column meaning, units, reference scale, or technique
  validity.

### Acceptance criteria

- exact input bytes are retrievable after intake and match the recorded size and SHA-256;
- the same bytes under the same intake identity do not create a conflicting second source artifact;
- changed bytes produce a different content identity and fail an earlier manifest;
- source intake works through an application service with no `Campaign`, HER archive, parser, or UI
  dependency;
- a CLI/test adapter and the HTTP adapter can call the same application use case;
- an interrupted store/database boundary leaves retained, classifiable evidence rather than deleted
  bytes;
- the fixture is visibly synthetic on every committed surface.

### Automated proofs

- property tests for stable byte identity and changed-byte identity;
- offline tests for idempotent intake and canonical metadata;
- object-store read-back and tamper tests;
- integration tests for the persistence/object boundary when PostgreSQL and MinIO are available;
- contract tests proving the use case imports no FastAPI, Typer, ORM, filesystem path, or cloud SDK.

### Artifact

The synthetic source fixture, its source-artifact record, integrity manifest, verification output, and
a reproduction command are committed under [`../artifacts/source-capture`](../artifacts/source-capture).
This artifact demonstrates source capture only, not CSV interpretation.

### Out of scope

CSV parsing, column mapping, normalisation, metadata validation, Passport, frontend, campaign retry,
budget completion, and broad runtime hardening unrelated to file intake.

---

## Phase 2 — generic CV CSV ingestion

**Status:** `demonstrated` by the reproducible `synthetic + replay` artifact under
[`../artifacts/cv-ingestion`](../artifacts/cv-ingestion).

### Objective

Transform the Phase 1 synthetic source fixture into a typed, normalised CV observation while
retaining the exact input bytes and every explicit mapping decision.

### Implementation work

- define a versioned import profile that explicitly maps source columns to quantity roles and units;
- accept delimiter, decimal convention, header location, and missing-value tokens only through the
  profile, never through silent defaults that affect scientific meaning;
- parse one CV signal and create a versioned normalised observation without mutating the raw file;
- record each transformation step, implementation version, inputs, parameters, and outputs;
- expose bounded application/API contracts for source inspection, import-profile creation,
  normalisation, and plot-series retrieval;
- make the CLI and API call the same intake and normalisation services.

### Scientific and data work

- define the minimum CV signal contract without inventing electrochemical values or units;
- require explicit potential-axis, current-axis, time-axis where used, and unit mappings;
- represent reference scale, applied versus corrected potential, current versus current-density basis,
  electrode role, geometric/contact area, scan rate, and cycle information as explicit metadata states;
- permit `unknown`, `unavailable`, and `not_applicable` where the source cannot support a value;
- limit Phase 2 findings to parsing, mapping, unit, and structural validity; experiment-metadata
  release rules belong to Phase 3.

### Acceptance criteria

- an unmapped scientific column or unit blocks normalisation;
- the same bytes plus the same canonical import profile produce the same normalised identity;
- changing a mapping produces a new normalised observation linked to the same source artifact;
- every normalised value resolves to a source column, a declared transformation, and a unit;
- the normalised observation and transformation graph close to the retained Phase 1 source artifact;
- the API plot series contains the backend-approved values, axis roles, units, observation identity,
  and provenance references without display-specific transformations.

### Automated proofs

- offline unit tests for import-profile canonicalisation and validation;
- table-driven parser tests for delimiter, decimal, missing token, extra column, duplicate header,
  non-numeric cell, non-finite value, and row-length mismatch;
- property tests for stable identity and mapping-sensitive identity changes;
- API/CLI parity tests using the same source bytes and import profile;
- one end-to-end offline test from fixture bytes to a normalised observation and closed lineage.

### Artifact

The committed synthetic CV CSV fixture, its explicit import profile, normalised observation,
transformation graph, structural findings, and documented reproduction command are under
[`../artifacts/cv-ingestion`](../artifacts/cv-ingestion).

### Out of scope

Experiment Passport, Experiment Package, automatic column detection, inferred units, Gamry DTA,
galvanostatic electrolysis, scientific quality scoring, peak interpretation, background subtraction,
iR correction, campaign orchestration, and UI.

---

## Phase 3 — Experiment Passport and verified package

**Status:** `implemented`. The reproducible candidate artifact is under
[`../artifacts/experiment-passport`](../artifacts/experiment-passport); promotion to `demonstrated`
requires that artifact to be committed and verified from the resulting clean checkout.

### Objective

Turn the Phase 2 experiment into a scientist-readable Passport and manifest-verified Package that
distinguish available evidence, transformations, missing context, and release-blocking ambiguity.

### Implementation work

- define versioned `Experiment`, `MetadataAssertion`, `TransformationRecord`, `ValidationFinding`,
  `ExperimentPassport`, and `ExperimentPackage` schemas;
- keep metadata `origin` independent from `transformation` and validation state;
- support append-only user supplements and corrections that create new package versions;
- render machine-readable JSON and a self-contained human-readable report from the same findings;
- link every displayed value and warning to its assertion, transformation, or source artifact;
- make report generation deterministic apart from declared release metadata such as release time;
- expose application/API contracts for assertions, validation runs, Passport preview and release,
  Package creation, and Package download;
- require idempotency keys and expected experiment versions on mutating HTTP operations.

### Scientific and data work

- classify fields as `required`, `conditional`, `recommended`, or `optional` per technique profile;
- define conditional requirements for CV without assuming that unavailable context can be recovered;
- require an evidence note for every `inferred` origin and prevent it from being re-labelled as
  `source_file` or `user_supplied`;
- separate reproducibility-relevant completeness from scientific validity and data quality.

### Acceptance criteria

- origin, transformation, requirement class, and value state are independently queryable;
- adding user-supplied metadata does not rewrite the source-file assertion it supplements;
- every inferred value identifies the method, version, evidence, and confidence representation used;
- blocking findings prevent a release status while warnings remain visible in a released package;
- HTML and JSON reports contain the same finding identifiers and status decisions;
- a downloaded package verifies through the CLI and contains the same released Passport shown by the
  API;
- the API never accepts `source_file` or `inferred` as a client-selected origin for a user edit.

### Automated proofs

- schema round-trip and backward-compatibility tests;
- exhaustive transition tests for assertion supersession and correction;
- report contract tests for visible blockers, warnings, unknowns, and origin labels;
- lineage-closure tests from each report field to retained evidence;
- deterministic-render tests with volatile release fields isolated explicitly;
- API/CLI parity tests for findings, release decision, Passport identity, and Package checksum;
- package tamper tests for missing, modified, unexpected, and checksum-mismatched members.

### Artifact

Versioned machine-readable and HTML Passports for the Phase 2 fixture, the initial and superseding
Experiment Packages, their manifests, independent CLI verification output, and evidence that adding
one user-supplied assertion did not mutate the source assertion or prior release are generated under
[`../artifacts/experiment-passport`](../artifacts/experiment-passport).

### Out of scope

Automated claims of reproducibility, journal-specific formatting, collaborative review, electronic
signatures, a scalar quality score, and frontend implementation.

---

## Phase 3.5 — single-user CV Passport demo

**Status:** `planned`.

### Objective

Expose the demonstrated Phase 2–3 pipeline through one polished local workflow that makes the
scientific chain of custody understandable in 60–90 seconds. The CLI, application services, domain
models, deterministic validation, and Package verifier remain authoritative.

### Technology decision

Use one React application built with Vite and TypeScript as a bounded presentation adapter consuming
the FastAPI contracts from Phases 2–3. Serve its production assets from FastAPI so the demo has one
local entry point. Use Plotly.js for the ordered, potentially non-monotonic CV trace.

The frontend has one page, no router, no global state library, no SSR framework, no component suite,
and no general design system. TypeScript API types are generated from the versioned OpenAPI contract.
No browser code parses scientific values, converts units, assigns metadata origin, evaluates a
validation rule, generates a Passport, or assembles a Package.

### Demo flow

1. load the committed synthetic fixture or upload one CV CSV;
2. show source filename, byte size, SHA-256, origin, and synthetic/observed label;
3. show source columns without assigning their meaning;
4. submit explicit potential/current roles and units;
5. display the backend-provided normalised CV series with its observation identity and units;
6. display metadata with separate origin and transformation indicators;
7. display deterministic blockers, warnings, unknowns, and their rule identifiers;
8. add or confirm one missing field as a new `user_supplied` assertion;
9. request and preview a new Passport version;
10. create, download, and verify the Experiment Package.

### Implementation work

- add a bounded frontend workspace with a locked dependency graph and reproducible production build;
- implement upload, source identity, explicit mapping, plot, metadata, finding, Passport preview, and
  Package download components for the single CV fixture path;
- preserve backend responses as the authoritative state; keep only transient form and interaction
  state in the browser;
- render provenance indicators from `origin` and ordered transformation records without collapsing
  their meanings;
- allow metadata edits only through the user-assertion endpoint; the client cannot select
  `source_file` or `inferred` as the origin of an edit;
- bundle every runtime asset locally; the clean-checkout demo does not depend on a CDN;
- expose one minimal documented demo command after the capability is implemented;
- add a README screenshot or short demo asset only after the browser acceptance artifact exists.

### Scientific and data work

- design the synthetic fixture specifically for the demonstration: valid CV-shaped data, some
  explicit metadata, some deliberately absent metadata, and at least one finding a user can resolve;
- identify the fixture as synthetic in its filename, UI banner, plot, metadata, Passport, Package,
  and README asset;
- obtain domain review for which missing field is a blocker or warning in the demo technique profile;
- ensure the UI distinguishes completeness, integrity, scientific validity, and reproducibility.

### Acceptance criteria

- the demo starts from a clean checkout through one documented local command and requires no
  proprietary data, live instrument, external asset CDN, or manual database edits;
- an unfamiliar viewer can complete the fixture workflow and understand the raw-to-package chain in
  60–90 seconds;
- every plotted value, axis role, unit, finding, provenance indicator, Passport field, and release
  decision is returned by the backend rather than recomputed by the frontend;
- the same experiment yields the same findings, Passport identity, and Package checksum through CLI
  and UI paths;
- submitting a mapping creates no implicit inference, and an unconfirmed inferred proposal cannot be
  normalised;
- a UI edit creates a new `user_supplied` assertion and superseding Passport without mutating the
  source assertion or prior release;
- the downloaded Package passes `labbridge package verify` byte-for-byte;
- the synthetic fixture cannot be mistaken for measured data on any screen or released artifact;
- keyboard operation, focus visibility, semantic labels, contrast, loading state, and error state are
  verified for the single workflow;
- no authentication, tenancy, history browser, campaign monitor, or unrelated dashboard surface is
  present.

### Automated proofs

- frontend unit tests for the presentation-only state transitions and provenance rendering;
- OpenAPI/type drift gate;
- API contract tests proving the browser cannot choose protected origins or bypass validation;
- browser test covering fixture load, explicit mapping, plot, finding resolution, Passport refresh,
  Package download, and independent CLI verification;
- browser tests for a blocked mapping, stale experiment version, backend error, and synthetic label;
- deterministic frontend build and offline asset check.

### Artifact

A committed synthetic fixture, browser-test trace, screenshot or short demo recording, exact demo
command, built-asset manifest, downloaded Experiment Package, and independent CLI verification output.
Only this artifact can promote the demo from `implemented` to `demonstrated`.

### Out of scope

Authentication, multi-user tenancy, organisations, billing, collaboration, experiment history,
campaign monitoring, live instruments, mobile application, generic dashboard framework, elaborate
design system, autonomous-agent chat, and admin console.

---

## Phase 4 — Gamry DTA CV ingestion

**Status:** `implemented`. The reproducible candidate artifact is under
[`../artifacts/gamry-dta-cv`](../artifacts/gamry-dta-cv); promotion to `demonstrated` requires a
commit followed by clean-checkout reproduction and verification.

The accepted boundary is the project-owned synthetic variant documented in the artifact's
[`SUPPORT.md`](../artifacts/gamry-dta-cv/SUPPORT.md). ADR-017 records the decision to implement a
bounded parser after evaluating `echemdb-converters` 0.4.1. The runtime adds no converter
dependency.

### Objective

Add one vendor-specific CV ingestion path without weakening raw-byte retention, explicit semantics,
or failure visibility.

### Implementation work

- evaluate the documented
  [`echemdb-converters` Gamry loader](https://echemdb.github.io/echemdb-converters/) and its
  dependencies before deciding whether to reuse, wrap, or implement a bounded parser;
- pin the accepted DTA variants with sanitised or redistributable fixtures;
- retain the original DTA bytes and parser diagnostics;
- map parsed fields into the same import-profile, metadata-assertion, observation, passport, and
  package contracts used by generic CSV;
- reject unsupported blocks and versions explicitly.

### Scientific and data work

- review vendor field meanings against primary vendor documentation or a qualified domain reviewer;
- keep source-declared units and reference information distinct from user supplements and
  normalisation;
- document which DTA constructs are preserved but not interpreted.

### Acceptance criteria

- every accepted parsed field cites its DTA location and parser version;
- unsupported or ambiguous technique blocks fail closed while retaining source bytes;
- equivalent explicit mappings yield the same normalised domain representation as the generic CSV
  path where the source evidence is equivalent;
- no vendor-specific field bypasses the common validation and lineage contracts.

### Automated proofs

- offline fixture tests for each supported DTA variant;
- malformed, truncated, mixed-technique, encoding, and locale tests;
- differential contract tests between vendor and generic representations;
- package and report tests reused from Phases 1 and 2.

### Artifact

One redistributable or sanitised DTA CV fixture and its verified Experiment Package, including a
parser support statement and explicit exclusions.

### Out of scope

All Gamry techniques, proprietary binary formats, automatic scientific interpretation, live control,
and compatibility claims beyond the tested variants.

---

## Phase 5 — galvanostatic electrolysis package

**Status:** `planned`.

### Objective

Support a second experimental class so that the package model proves it is technique-aware without
becoming an unrestricted workflow engine.

### Implementation work

- define a galvanostatic electrolysis technique profile and explicit time/current/potential mapping;
- support source artifacts, metadata assertions, normalised time series, validation findings,
  passport rendering, and package verification through the common contracts;
- represent auxiliary analytical results only through explicit linked source artifacts and methods;
- add no technique-independent abstraction until both CV and electrolysis require it.

### Scientific and data work

- obtain domain review for current sign, electrode area basis, cell geometry, reference scale,
  compensation/correction state, sampling, interruptions, and product-quantification semantics;
- distinguish electrical time-series completeness from conversion, selectivity, yield, or Faradaic
  efficiency claims;
- require declared equations, inputs, units, and analysis versions for derived quantities.

### Acceptance criteria

- a time series can be packaged without claiming product quantification;
- any derived efficiency or yield is blocked unless all technique-specific inputs and provenance are
  present;
- CV-only fields are `not_applicable`, not silently absent or reused;
- common package verification remains unchanged across both technique profiles.

### Automated proofs

- offline parser and schema tests for one generic electrolysis fixture;
- conditional-requirement tests for technique-specific metadata;
- dimensional and lineage tests for any approved derived quantity;
- cross-technique package contract tests.

### Artifact

One independently generated galvanostatic electrolysis fixture and a verified package whose report
clearly separates recorded electrical data from any unavailable chemical analysis.

### Out of scope

Instrument control, gas/liquid chromatography ingestion, automatic product assignment, mechanism
attribution, and unsupported efficiency calculations.

---

## Phase 6 — EchemDB-aligned exchange package

**Status:** `planned`.

### Objective

Provide a tested mapping from LabBridge's evidence model into the applicable EchemDB/Frictionless
schema without making LabBridge's internal model depend on one external schema.

### Implementation work

- pin the [EchemDB metadata-schema](https://github.com/echemdb/metadata-schema) and package versions
  used by the exporter;
- publish a field-by-field mapping, including unmapped and lossy fields;
- generate a Frictionless-compatible package through a versioned export adapter;
- validate the export using the pinned external schema and supported tooling;
- retain LabBridge source identities and provenance in a documented extension or companion manifest
  when the target schema cannot express them.

### Scientific and data work

- compare technique metadata requirements with the active EchemDB schema and examples;
- require domain review for semantic mappings involving reference electrodes, electrolyte, cell,
  electrode area, measurement type, and scan rate;
- never convert `inferred` metadata into a source-declared external field without an explicit,
  machine-visible qualifier.

### Acceptance criteria

- the exported package validates against the pinned schema version;
- every exported value maps back to a LabBridge assertion or observation;
- every omitted or lossy mapping is listed in the export report;
- no README or package claims general EchemDB compatibility beyond the tested technique and schema
  versions.

### Automated proofs

- pinned-schema validation in an offline test fixture;
- mapping completeness and collision tests;
- round-trip identity tests where the external format preserves the required semantics;
- explicit lossy-mapping tests where round-trip equality is impossible.

### Artifact

A validated EchemDB-aligned export for one CV package, the mapping table, validation output, schema
version, and reproduction command.

### Out of scope

Submission to EchemDB, universal external-schema compatibility, silent semantic coercion, and support
for techniques not demonstrated in earlier phases.

---

## Phase 7 — campaign reliability demonstration

**Status:** `demonstrated` for synthetic replay in the recorded environment; observed replay and
future live execution were not run.

### Objective

Complete and demonstrate the existing fault-aware campaign path as a second producer of the same
evidence model.

### Implementation work

- finish retry scheduling, budget reservation, campaign control, state reconstruction, backup and
  restore, and the operator runbook;
- adapt campaign evidence into the shared Experiment Package contract without erasing attempts,
  failures, or origin/mode distinctions;
- run the process-boundary fault campaign defined in `FAILURE_MATRIX.md`;
- publish actual results and exclusions rather than target language.

### Scientific and data work

- keep HER observed replay, generated synthetic replay, and any future live data distinct;
- verify that campaign-derived metrics close lineage through observations to source artifacts;
- retain unfavourable valid results and corrupted receipts according to the same package rules.

### Acceptance criteria

- every targeted failure scenario has inspectable durable state and a reproducible proof;
- campaign observations produce a package conforming to the same integrity contract as file intake;
- reliability claims cite the released fault report and package manifests;
- deployment restore and full object verification are exercised in the stated environment.

### Automated proofs

- offline, integration, data, and slow fault suites with their prerequisites recorded;
- at least 100 seeded campaigns with injected process termination, as specified in PO-10;
- migration and backup-restore checks;
- replay-versus-persisted state comparison;
- full stored-object verification.

### Artifact

The released fault-campaign report, raw result table, environment manifest, logs needed for audit,
and verified campaign-derived Experiment Package.

The released evidence is under [`artifacts/fault-campaign`](../artifacts/fault-campaign). Its 100
seeded campaigns cover all six declared process-termination boundaries. The recorded run reports
zero lost accepted observations, unintended duplicate acceptances, hard-budget overspends,
projection mismatches, or Package-verification failures; it also records 51 `lease_lost` failures
and 100 suppressed redeliveries. The deployment restore reverified 100 stored objects, replayed 100
campaigns, and fully verified 100 restored campaign Packages. These are synthetic-replay reliability
measurements, not scientific or live-instrument results.

### Out of scope

No exactly-once claims, continuous instrument scheduling, Kubernetes, multi-fidelity optimisation, or
model-performance claims.

---

## Deferred track — enzymatic-biosensor simulator

**Status:** `deferred`.

The scientific contract remains in [`SIMULATOR_MODEL.md`](SIMULATOR_MODEL.md). Work resumes only after
the file-ingestion/passport wedge and the shared package contract are demonstrated, and only after
the literature and domain-review requirements in that document are satisfied. Deferred status does
not convert its hypotheses into accepted science or its planned interfaces into implemented
capabilities.

## Release discipline

For every phase:

1. implementation and tests may justify `implemented` only after the relevant gates pass;
2. `demonstrated` requires a committed, reproducible artifact with a manifest and verification
   instructions;
3. schema, parser, analysis, and report versions are part of the evidence;
4. missing evidence remains explicit; no default fills a scientific gap;
5. a superseding package never mutates a previously released package;
6. external compatibility is scoped to pinned versions and tested mappings;
7. a failed or scaffolded gate is reported as such, never as passing.

## Principal risks

| Risk | Consequence | Control |
|---|---|---|
| Silent CSV semantics | Plausible but scientifically wrong signal | Mandatory explicit mapping and units; fail closed |
| Origin/transformation conflation | Inference appears source-declared | Orthogonal fields and lineage tests |
| Passport overclaim | Report mistaken for scientific certification | Bounded language and separate blocker/warning dimensions |
| Frontend scientific logic | UI and CLI disagree while both look valid | Backend-owned rules and API/CLI parity tests |
| Frontend scope expansion | Demo becomes a second product platform | One-page Phase 3.5 boundary and explicit exclusions |
| Demo asset drift | Screenshot or fixture no longer matches the package | Browser acceptance artifact and checksum manifest |
| Vendor-format drift | Incorrect DTA parsing | Versioned fixtures, explicit supported variants, fail closed |
| External-schema drift | False compatibility claim | Pin schema/tool versions and publish mapping evidence |
| Infrastructure-first expansion | No scientist-visible value | Vertical artifacts gate each phase |
| Runtime claims outrun proof | Reliability described from code alone | Status discipline and released fault report |
| Deferred simulator treated as science | Synthetic hypotheses presented as validated | Persistent synthetic labels and domain/literature gates |

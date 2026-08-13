# Single-user CV Passport Demo Design

## Authority and status

`docs/ROADMAP.md` section "single-user CV Passport demo" defines the scope, technology,
acceptance criteria, evidence requirements, and exclusions. `AI_CONTRACT.md`, `docs/SPEC.md`,
`docs/DATA_STRATEGY.md`, `docs/FAILURE_MATRIX.md`, and accepted architecture decisions continue to
govern scientific and durability semantics.

The bounded adapter is `implemented` after its relevant offline tests and gates pass. It remains
not `demonstrated` until the complete inspectable artifact is committed, its manifest verifies, the
missing-reference-scale severity has received the required human electrochemistry domain review, and
the recorded unfamiliar-viewer acceptance run satisfies the 60-90 second comprehension criterion.

## Selected approach

A Docker Compose application service builds a locked React/Vite workspace and packages its
production assets with the FastAPI application. PostgreSQL and MinIO remain the authoritative local
stores. From a clean checkout, the exact demo command is:

```text
docker compose --profile demo up -d --build --wait
```

This single command builds the application, starts all three services, waits for readiness, and exposes
the application at `http://localhost:8000/`. A clean-checkout acceptance script copies only tracked
files into a temporary directory, runs this command, completes the browser flow, verifies the downloaded
Package, and tears down the isolated Compose project.

The acceptance copy is formed from the union returned by
`git ls-files --cached --others --exclude-standard`. This includes every tracked and non-ignored
project file while excluding build products and machine-local state.

This approach provides a clean-checkout path and isolates Node and Python build dependencies from the
host. A host bootstrap script would need to install, coordinate, and stop two language runtimes plus
the durable services. A separate Vite development server would create a second application entry
point and would not prove that FastAPI serves the production build.

## Runtime architecture

The frontend is a presentation adapter with one page, no router, and no global state library. A local
reducer tracks only workflow position, pending requests, transient form values, and the latest API
responses. Reloading the page deliberately clears this interaction state; PostgreSQL and MinIO retain
the scientific records and releases.

FastAPI registers all API routes before exposing the built `index.html` and local assets. The static
adapter contains no fallback that can shadow an API error. The Docker image uses a Node build stage
for the frontend and a Python runtime stage for the API. The application service waits for healthy
PostgreSQL and MinIO, applies Alembic migrations, and then starts one Uvicorn process.

The frontend dependency graph is locked. Plotly is imported from the installed package and bundled by
Vite; no runtime asset references a CDN. The build emits a Vite manifest, and an artifact script records
the SHA-256 and byte size of every built file.

## Versioned API contract

The repository retains a deterministic OpenAPI document generated from `create_app().openapi()`. The
frontend generates TypeScript types from that document with a pinned generator. A drift gate regenerates
both files in temporary locations and compares them byte-for-byte with the tracked contract and types.

The frontend uses one typed request module. It does not duplicate Pydantic models by hand and does not
accept an `origin` field for user assertions. Stable HTTP error bodies are converted into display states
without parsing their prose.

The existing source, CV, Experiment, Passport, Package, and download endpoints remain authoritative.
The CV plot response is extended only if necessary to return an explicit ordered trace with axis roles,
units, labels, identity, and provenance. Any such shaping occurs in the application/API boundary, not in
React.

## Synthetic fixture and explicit mapping

The fixture is a dedicated, visibly named synthetic CSV with an ordered forward and reverse potential
sweep and a synthetic current response. Its values are illustrative only. They are not presented as a
physical model, a measurement, or evidence of scientific validity.

The fixture bytes and a separate metadata declaration are committed once and copied into the Vite build
without modification. Loading the fixture fetches those bytes and submits them to `POST /source-artifacts`
with the explicit `synthetic + replay` pair. Uploading a user CSV requires the operator to choose the data
origin and execution mode; the browser does not derive either from its filename or contents.

Source inspection returns headers without roles. The mapping form requires one explicit potential column,
one explicit current or current-density column, a decision for every other column, and source/target units
for scientific columns. The form may prevent an obviously incomplete submission, but backend validation
is decisive. The browser sends strings as entered and never parses, converts, sorts, filters, or repairs
scientific values.

## Workflow and page composition

The page presents the chain of custody as one vertical dossier with a persistent progress rail:

1. **Source** loads the synthetic fixture or one upload and shows filename, size, SHA-256, origin, mode,
   retained state, and a prominent synthetic banner when applicable.
2. **Columns and mapping** shows inspection headers first, then explicit role and unit controls. A rejected
   or incomplete mapping remains visible with the backend error.
3. **Normalised observation** shows the backend response identity, units, structural findings,
   transformation records, and a Plotly line trace in source order. The title and plot annotation identify
   synthetic data.
4. **Metadata and validation** renders assertion origin, transformation, value state, requirement class,
   evidence links, finding severity, stable rule code, resolution text, and separate counts for blockers,
   warnings, and unknowns. Explanatory copy keeps completeness, integrity, scientific validity, and
   reproducibility distinct.
5. **Correction and Passport** first releases the eligible initial Passport so the later release can
   supersede it. The only editable demonstration field is the unresolved reference scale. Submitting it
   appends a `user_supplied` assertion that supplements the retained source/profile assertion. The page
   revalidates, previews the new Passport, and releases it with the latest expected version.
6. **Experiment Package** creates the Package from the superseding Passport, downloads the exact ZIP,
   and reports the package identity and backend checksum. The browser does not claim independent
   verification; the browser test invokes `labbridge package verify` on the downloaded bytes and retains
   that output in the demonstration artifact.

The `RHE` value used by the fixture flow is an operator declaration retained as demonstration evidence.
LabBridge neither infers nor verifies it from the CSV. The workflow does not convert the plotted potential
to another reference scale, validate `RHE` as physically correct, or interpret the current response.

The current validation rules render the unresolved `reference_scale` according to the existing Phase 3
requirement class. Whether the demo technique profile should classify that missing field as a blocker or
warning requires a recorded human electrochemistry domain review; implementation does not settle that
scientific policy. The demo cannot be promoted to `demonstrated` until the review approves the
classification and the resulting rule is represented consistently in the API, UI, Passport, Package, and
acceptance artifact.

## Failure handling

Every network transition has idle, pending, succeeded, and failed presentation states. Buttons remain
disabled only while their request is pending or a prerequisite response is absent. Errors remain beside
the affected step and expose the stable backend code plus concise recovery guidance.

- A blocked or incomplete mapping cannot advance to normalisation.
- A stale experiment version refreshes the latest Experiment and requires the user to retry; it never
  silently resubmits against a different version.
- A backend failure preserves all successful prior responses and offers a scoped retry.
- A blocking validation cannot release a Passport or Package.
- A failed download or verification never changes the released Package.

Idempotency keys are generated once per user intent and retained across retries of that intent. Editing a
request creates a new key. The browser makes no exactly-once claim.

## Accessibility and visual treatment

The layout uses semantic regions, a single heading hierarchy, labelled controls, explicit error summaries,
keyboard-reachable actions, visible focus rings, and text labels in addition to colour. The palette uses a
quiet laboratory-notebook surface with high-contrast navy text, teal provenance accents, and an amber
synthetic-data treatment. Responsive wrapping protects narrow desktop windows, but mobile support is not
claimed.

Loading indicators use text and `aria-live`; Plotly has an adjacent textual series summary. Browser tests
cover keyboard progression, focus visibility, labels, contrast, loading, and error states for this one
workflow.

## Verification

The implementation follows test-first slices:

- reducer and component tests for allowed transitions, provenance rendering, synthetic labels, pending
  controls, and structured errors;
- API contract tests for protected assertion origin, backend-owned findings, plot values, and stale-version
  conflicts;
- OpenAPI and generated-TypeScript drift checks;
- deterministic double builds and a scan rejecting remote runtime assets;
- Playwright fixture flow through mapping, initial release, user assertion, superseding Passport, Package
  download, and CLI verification;
- Playwright failure cases for blocked mapping, stale version, backend error, and missing synthetic labels;
- parity assertions that first complete the mutating workflow through the UI and then drive validation,
  Passport release, Package creation, download, and verification through CLI commands for the same stored
  experiment and equivalent inputs. The CLI path uses new idempotency keys and must converge on the same
  finding identifiers, persisted Passport identity, and Package checksum rather than merely reading the
  UI responses;
- the existing F-058 offline Package tamper matrix for missing, changed, unexpected, duplicate, unsafe,
  and checksum-mismatched archive members;
- existing offline Python gates plus PostgreSQL/MinIO integration tests protecting F-054 normalised-object
  integrity, F-055 interrupted normalisation publication, and F-059 interrupted Package publication;
- the isolated clean-checkout acceptance script using only the documented command.

## Demonstration artifact

`artifacts/cv-passport-demo/` contains the browser trace, final screenshot, built-asset manifest,
downloaded Package, CLI verification JSON, exact command, limitations, and a closed artifact manifest.
The manifest records `data_origin=synthetic`, `execution_mode=replay`, producing versions, every file hash,
and the source, observation, Passport, and Package identities.

The reproduction script verifies the closed manifest and `labbridge validate-artifacts` verifies the
committed artifact. A single recorded unfamiliar-viewer acceptance run must capture both elapsed time of
60–90 seconds and confirmation that the raw-to-Package chain and the four evidence concepts were
understood. The automated browser trace measures the interaction path but does not substitute for that
human evidence. If the recorded human evidence is unavailable or outside the time range, the capability
remains `implemented` and the unsatisfied criterion is reported.

The artifact demonstrates only this local synthetic fixture workflow and the tested failure cases. It does
not establish scientific validity, data quality, experimental reproducibility, journal readiness,
production readiness, authentication, tenancy, collaboration, instrument connectivity, or mobile support.
Without the required domain review of the missing-reference-scale classification, the artifact supports at
most `implemented`, even when every automated test and reproduction command passes.

## Authority boundary

- Convention applied: origin and execution mode remain separate; potential values retain the explicit
  source and target units supplied through the mapping.
- Requires inspection: no observed archive quantity or unit is used by this synthetic fixture.
- Requires literature support: no physical mechanism, coefficient, or empirical range is asserted.
- Requires domain review: a human electrochemistry reviewer must approve whether a missing reference scale
  is blocking or warning-level evidence for this demo technique profile. A separate review would be
  required before treating the fixture shape or declared `RHE` value as physically representative; the
  demo makes neither claim.

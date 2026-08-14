# LabBridge — open work

**Status:** current statement of what is unfinished
**Companion:** [`PROJECT_STATUS.md`](PROJECT_STATUS.md) states what LabBridge does today and the
evidence for each claim

This document lists what is open, not what is promised. LabBridge has no approved product roadmap
beyond the work below. Adding one is a product decision that has not been taken, and no date,
sequence, or delivery commitment should be inferred from this page.

The numbered delivery plan that built the current system is retained as a historical record in
[`archive/2026-implementation-roadmap.md`](archive/2026-implementation-roadmap.md). It is no longer a
statement of current capability or future intent.

---

## Completed

The file-to-Package path and the fault-aware campaign runtime are both integrated. Opaque source
capture, generic CV CSV ingestion, and synthetic-replay campaign reliability carry committed,
reproducible artifacts. The Experiment Passport and Package, bounded Gamry DTA CV ingestion,
galvanostatic electrolysis support, the EchemDB-aligned CV export, and the single-user CV Passport
demo are implemented with committed candidate artifacts.

Per-capability status, the artifact behind each claim, and the boundary each artifact does not cross
are in [`PROJECT_STATUS.md`](PROJECT_STATUS.md).

## Open evidence gaps

These capabilities are `implemented`. Each is held below `demonstrated` by a specific missing piece
of evidence, not by missing code.

| Capability | What is missing |
|---|---|
| Experiment Passport and Package | Reproduction and verification of the committed artifact from a resulting clean checkout |
| Bounded Gamry DTA CV ingestion | The same clean-checkout reproduction and verification |
| Galvanostatic electrolysis | The same clean-checkout reproduction and verification |
| EchemDB-aligned CV export | The same clean-checkout reproduction and verification, within the pinned schema and tool versions only |

The committed `LIMITATIONS.md` inside the Gamry, electrolysis, and EchemDB artifacts describes each
as an "uncommitted candidate". That wording was accurate when the artifact bytes were produced and is
now stale: the artifacts are committed. It is preserved because those files are covered by closed
SHA-256 manifests and a released artifact is immutable. Correcting the wording requires regenerating
the artifacts and their manifests, which is a separate change.

## Open human-acceptance gaps

Two records require a human and cannot be produced by automation or by any additional test.

- **Reference-scale severity.** A recorded electrochemistry domain review must decide whether a
  missing `reference_scale` is a blocking or warning-level finding in the demo technique profile, and
  confirm that the decision is represented consistently across the API, UI, Passport, Package, and
  artifact. Until it exists, the single-user CV Passport demo stays `implemented`.
- **Unfamiliar-viewer comprehension.** A recorded run by someone unfamiliar with the project must
  show both 60–90 second completion of the fixture workflow and comprehension of the raw-to-Package
  chain and of the distinction between completeness, integrity, scientific validity, and
  reproducibility. The automated browser trace measures the interaction path and does not substitute
  for this.

## Open operational gaps

- No cloud deployment has been performed. Local Docker Compose execution, backup and restore
  verification, a migration exercise, the crash-recovery campaign experiment, and the operator
  runbook are done; a deployment, an incident postmortem, and a tagged release with checksummed
  public artifacts are not.
- Campaign reliability is measured for generated synthetic bytes in replay mode only. The same
  campaign has not been run against the observed HER replay environment.
- `SourceArtifactService.reconcile` implements recovery for a source left `pending` by an interrupted
  intake ([`FAILURE_MATRIX.md`](FAILURE_MATRIX.md) F-048) but has no operator command. Its `commit`
  and `quarantine` writes are unguarded against a concurrent `intake()` of the same content identity;
  that race must be closed before the command is exposed.
- Reconciliation and retry scheduling are durable and bounded but on-demand. No continuously running
  scheduler or daemon is provided.

## Deferred tracks

Deferred means intentionally outside the current release. Deferral does not convert a hypothesis into
accepted science or a planned interface into an implemented capability.

- **Enzymatic-biosensor simulator.** Its scientific contract is retained in
  [`SIMULATOR_MODEL.md`](SIMULATOR_MODEL.md). Work would resume only after the literature support and
  domain review that document requires are satisfied.
- **Model-based selection policy.** The campaign runtime uses a seeded random baseline. Anything
  beyond it is deferred.
- **Live instrument execution.** `execution_mode="live"` is reserved in the data model and has no
  adapter. Instrument control is out of scope.

## What is not planned

No commitment exists for additional vendor formats, additional techniques, EchemDB submission,
authentication, multi-user tenancy, collaboration, a hosted service, or a general workflow engine.
Any of these would require a new product decision and a new set of acceptance criteria before work
started.

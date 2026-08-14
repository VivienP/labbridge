# LabBridge

[![CI](https://github.com/VivienP/labbridge/actions/workflows/ci.yml/badge.svg)](https://github.com/VivienP/labbridge/actions/workflows/ci.yml)

**Turn an electrochemistry data file into a verifiable evidence package, without ever guessing what
the file means.**

## Why LabBridge exists

Two things go wrong quietly in experimental data work.

The first is meaning. A CSV column named `V` might be an applied potential, a corrected potential, or
a potential against an unrecorded reference. An ordinary pipeline picks the plausible reading and
produces a chart that looks right. Months later, nobody can say which reading was used, or whether
the reference scale was ever known.

The second is failure. An instrument times out before returning bytes. A retry writes a second copy
of a result that was already accepted. A worker dies between uploading a signal and committing the
row that describes it. An ordinary pipeline absorbs these as log lines, and the dataset ends up
missing a record that nothing will ever notice.

LabBridge refuses to guess and refuses to forget. Column roles and units come from an explicit,
versioned import profile or normalisation fails. Every execution attempt produces a durable outcome
record, including the ones that failed. Every accepted value resolves to a retained observation, to a
source checksum or a simulator seed, and to a named analysis version.

## What it does

LabBridge takes source bytes and produces an **Experiment Package**: a checksummed archive containing
the normalised observation, the transformation graph that produced it, the metadata assertions behind
it with their origin recorded separately from their transformation, the deterministic validation
findings, and a human-readable Passport. Anyone can verify that package independently.

Along the way it keeps four things apart that are easy to confuse: whether the declared evidence is
**complete**, whether the retained bytes still **hash correctly**, whether the measurement is
**scientifically valid**, and whether the experiment is **reproducible**. LabBridge decides the first
two. It does not assess the last two, and it never implies that it has.

A second path — a fault-aware campaign runtime — feeds the same evidence model from durable, leased,
recoverable jobs rather than from a file.

```text
FILE PATH                                  CAMPAIGN PATH

source file                                campaign declaration
  │ exact bytes retained, checksummed        │ durable job: leased, fenced, recoverable
  ▼                                          ▼
opaque SourceArtifact                      environment adapter
  │ explicit versioned import profile        │ every attempt yields a durable outcome
  ▼                                          ▼
normalised observation                     observation + attempt outcome
  │ closed transformation lineage            │ declared analysis version
  ▼                                          ▼
append-only metadata assertions            derived metrics
  │ origin, transformation, requirement      │
  │ class and value state stay independent   ▼
  ▼                                          evidence bundle
deterministic validation
  ▼
immutable Passport → verified Experiment Package

               shared integrity layer
  content identity · provenance · versions · append-only
  relations · manifests · independent verification
```

## Try the local demo

Requires Python 3.12+ and Docker with Compose v2. From a clean checkout:

```bash
docker compose --profile demo up -d --build --wait
```

Open `http://localhost:8000/`. The page loads a committed **synthetic** CV fixture, shows its
SHA-256, origin, and execution mode, then makes you declare column roles and units before anything is
normalised. It plots backend-provided values, shows the deterministic findings, lets you append one
`user_supplied` assertion, releases a superseding Passport, and downloads the exact Package.

Verify that Package — or any committed artifact — independently:

```bash
labbridge validate-artifacts
```

Installation, the equivalent command-line workflow, and artifact reproduction are in
[`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md).

## Evidence status

Every capability carries exactly one status: `planned`, `implemented` (code exists and its relevant
local tests pass), `demonstrated` (a committed, reproducible artifact proves it), or `deferred`. Code
existing is never enough for `demonstrated`; only an inspectable artifact is.

What is `demonstrated` today, and by what:

| Capability | Artifact | What it proves — and does not |
|---|---|---|
| Opaque source capture | [`artifacts/source-capture`](artifacts/source-capture) | Exact-byte retention and checksum verification. Assigns no CSV semantics |
| Generic CV CSV ingestion | [`artifacts/cv-ingestion`](artifacts/cv-ingestion) | Parser, mapping, unit, structural, identity, and lineage behaviour for one committed profile. Claims no electrochemical validity |
| Campaign reliability under injected faults | [`artifacts/fault-campaign`](artifacts/fault-campaign) | 100 seeded campaigns across six process-termination boundaries: zero lost accepted observations, zero unintended duplicate acceptances, zero budget overspends, zero projection mismatches, zero Package-verification failures. Synthetic bytes in replay mode — not a scientific or live-instrument result |

The Experiment Passport and Package, bounded Gamry DTA CV ingestion, galvanostatic electrolysis, the
EchemDB-aligned export, and the interactive demo are `implemented` with committed candidate
artifacts. The biosensor simulator is `deferred`.

Full per-capability status, evidence, and boundaries:
**[`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md)**.

## Architecture

```text
Scientist
   │
   ▼
FastAPI / CLI ──── same application services behind both
   │
   ├──▶ PostgreSQL      campaigns · work items · events · jobs · attempts · outcomes
   │                    budget ledger · idempotency keys · object metadata · relations
   │
   ├──▶ Durable worker ──▶ Environment adapter
   │                       └─ HER replay (observed · replay | synthetic · replay)
   │
   └──▶ S3-compatible object storage
        raw observations · Parquet exports · manifests · evidence bundles
```

The domain layer holds campaign rules, state transitions, budget arithmetic, canonical serialisation,
and typed scientific quantities. It imports no framework, ORM session, filesystem path, or cloud SDK.
Normative behaviour is in [`docs/SPEC.md`](docs/SPEC.md); the decisions and what each one cost are in
[`docs/ARCHITECTURE_DECISIONS.md`](docs/ARCHITECTURE_DECISIONS.md).

## Boundaries

**Observed and synthetic data are never conflated.** Every observation and derived artifact carries
`data_origin`, `execution_mode`, `environment_id`, and provenance linking it to source files or to a
seeded simulator configuration. Synthetic output may be exported and visualised, but every
human-readable and machine-readable representation identifies it as synthetic, and it is never
described as measured, experimental, or observed.

Two environments run behind one adapter interface and are never merged into one scientific candidate
space:

| Environment | `data_origin` | `execution_mode` | What it is |
|---|---|---|---|
| Au–Ir–Rh HER | `observed` | `replay` | A published electrocatalyst dataset, replayed from its archived measurements |
| Enzymatic biosensor | `synthetic` | `simulation` | A seeded, mechanistically informed phenomenological simulator. `deferred`: no adapter exists, it produces no experimental numbers, and it asserts no physical validation |

Replaying a generated fixture instead of the acquired archive yields `synthetic + replay` (ADR-010).
The replay adapter decides which pair applies from what the landing root contains — a `fixture_manifest.json`
means generated bytes, a `provenance.json` means acquired bytes — never from a caller-supplied flag,
and a root holding both fails rather than preferring one.

Also true, and worth stating plainly:

- No scientific number in this repository comes from a released physical measurement run.
- Package verification proves integrity and reports declared completeness. It is not an experimental
  certification, and release asserts neither scientific validity nor reproducibility.
- Gamry support covers one pinned Framework 7.07 CV layout, not Gamry in general. The EchemDB export
  is validated against pinned external versions, not EchemDB in general.
- The worker protocol is at-least-once delivery with idempotent effect handling. Duplicate
  submission, enqueueing, and acceptance are each suppressed by a database constraint under real
  concurrency; nothing here is exactly-once, and a redelivery can still waste an execution before
  acceptance refuses it.
- Nothing here has been deployed or operated outside local Docker Compose.

The complete list is in [`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md).

## Data source

The observed environment replays the Au–Ir–Rh HER dataset published as Zenodo record
[`10.5281/zenodo.20439519`](https://doi.org/10.5281/zenodo.20439519):

> Thelen F, Kim M, Arruda de Oliveira G, Bürgel JL, Schuhmann W, Ludwig A. Dataset — Autonomous
> scanning electrochemical cell microscopy enables rapid exploration of large compositionally
> complex material spaces. Zenodo, 2026. doi:10.5281/zenodo.20439519. Licensed CC BY 4.0.

Redistribution is permitted with attribution and an indication of changes. That permission comes from
a dated decision pinned to the DOI and the declared licence identifier, not from parsing the record:
the parser always yields `unresolved`, so a record that stops declaring that licence reopens the gate
on its own. Boundaries and layer definitions are in
[`docs/DATA_STRATEGY.md`](docs/DATA_STRATEGY.md).

## Documentation

**[`docs/`](docs/README.md)** is the index, organised by what you are trying to do. The shortest
paths from here:

| I want to… | Go to |
|---|---|
| Run it | [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) |
| Know what is proven | [`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md) |
| Read the normative behaviour | [`docs/SPEC.md`](docs/SPEC.md) |
| Understand a design choice | [`docs/ARCHITECTURE_DECISIONS.md`](docs/ARCHITECTURE_DECISIONS.md) |
| See how failures are handled | [`docs/FAILURE_MATRIX.md`](docs/FAILURE_MATRIX.md) |
| Contribute | [`CONTRIBUTING.md`](CONTRIBUTING.md) |

## Contributing and licence

Contributions are welcome; read [`CONTRIBUTING.md`](CONTRIBUTING.md) first, since a change is
expected to arrive with evidence at the layer its claim requires. LabBridge is maintained as time
allows and carries no support or response-time commitment.

To report a suspected vulnerability, use GitHub's private reporting rather than a public issue —
see [`SECURITY.md`](SECURITY.md).

Python 3.12 or later. FastAPI, Pydantic v2, SQLAlchemy 2 with Alembic, PostgreSQL, MinIO, pandas and
pyarrow, NumPy and SciPy, Typer and Rich.

[MIT](LICENSE). The licence covers LabBridge's own source; it grants nothing over any third-party
dataset. The Au–Ir–Rh HER dataset remains under CC BY 4.0 and keeps its own attribution requirement.

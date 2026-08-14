# Getting started

**Status:** operational guide
**Audience:** anyone running LabBridge locally for the first time

This guide covers installation, the local demo, the command-line file-to-Package workflow, and how to
verify or reproduce a committed artifact. It describes only commands that exist. For what each
capability is proven to do, see [`PROJECT_STATUS.md`](PROJECT_STATUS.md).

## Prerequisites

- Python 3.12 or later.
- Docker with Compose v2, for PostgreSQL and MinIO. The demo image additionally builds the frontend,
  so it needs network access on first build.

Linux, macOS, and Windows are all supported. Shell examples below are POSIX; on Windows use
PowerShell and substitute its line-continuation and path conventions. One repository script,
`scripts/check_clean_demo.ps1`, is PowerShell-only.

Every Compose service belongs to a profile, so a bare `docker compose up` starts nothing. Always pass
`--profile demo` or `--profile infrastructure`, or name the services explicitly.

## Install

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Activating the environment matters: without it, the editable install and the `labbridge` command
land in whichever interpreter is first on `PATH`.

Contributors should also enable the shared pre-commit gate:

```bash
git config core.hooksPath scripts/hooks
```

## Run the demo

From a clean checkout, one command builds the application, starts PostgreSQL and MinIO, applies
migrations, and waits for readiness:

```bash
docker compose --profile demo up -d --build --wait
```

Open `http://localhost:8000/`. The page loads a committed synthetic CV fixture, shows its filename,
byte size, SHA-256, origin, and execution mode, then requires explicit column roles and units before
anything is normalised. It displays backend-provided plot values and findings, appends a
`user_supplied` assertion, releases a superseding Passport, and downloads the exact Experiment
Package.

Every runtime asset is bundled locally; the demo needs no CDN. The browser does not parse or repair
scientific values, infer metadata origin, or evaluate validation rules — the backend owns all of
that.

Stop and remove the stack with:

```bash
docker compose --profile demo down -v
```

## Run the workflow from the command line

The CLI and the HTTP API call the same application services, so either path produces the same
findings, Passport identity, and Package checksum.

Start the durable services first:

```bash
docker compose --profile infrastructure up -d
```

PostgreSQL listens on 55432 and MinIO on 59000. Those ports are deliberately non-standard so a test
cannot silently connect to another local instance. Override the defaults with the `LABBRIDGE_DB_*`
and `LABBRIDGE_S3_*` environment variables.

### 1. Retain the source bytes

```bash
labbridge source intake fixtures/source/synthetic-replay-cv-opaque.csv \
  --intake-id synthetic-replay-cv-v1 \
  --media-type text/csv \
  --data-origin synthetic \
  --execution-mode replay
labbridge source verify <source-artifact-id>
```

Origin and execution mode are supplied explicitly. Nothing is inferred from the filename or contents.
This step assigns no column meaning, unit, reference scale, or technique validity.

### 2. Normalise through an explicit import profile

```bash
labbridge cv inspect <source-artifact-id> --encoding utf-8 --delimiter , --header-row 1 --json
labbridge cv profile-create fixtures/import-profiles/synthetic-replay-cv-v1.json --json
labbridge cv normalise <source-artifact-id> --profile-id <profile-id> --json
labbridge cv plot <normalised-observation-id> --json
```

The profile — not the headers — assigns roles, units, delimiter, decimal convention, and
missing-value tokens. An unmapped scientific column or an undeclared unit blocks normalisation rather
than defaulting.

For a Gamry DTA source, select the format explicitly and inspect the retained parser diagnostics:

```bash
labbridge cv normalise <source-artifact-id> --profile-id <profile-id> \
  --source-format gamry_dta --json
labbridge cv parser-record <parser-record-id> --json
```

Galvanostatic electrolysis uses the parallel commands `labbridge electrolysis profile-create` and
`labbridge electrolysis normalise`.

### 3. Release a Passport and build a Package

```bash
labbridge experiment create <normalised-observation-id> \
  --expected-version 0 --idempotency-key <key> --json
labbridge experiment assert <experiment-id> <assertion.json> \
  --expected-version <version> --idempotency-key <key> --json
labbridge experiment validate <experiment-id> \
  --expected-version <version> --idempotency-key <key> --json
labbridge experiment passport-preview <experiment-id> --json
labbridge experiment passport-release <experiment-id> \
  --expected-version <version> --idempotency-key <key> --json
labbridge package create <experiment-id> --passport-id <passport-id> \
  --expected-version <version> --idempotency-key <key> --json
labbridge package download <package-id> --output experiment-package.zip
labbridge package verify experiment-package.zip --json
```

Mutating operations require an expected version and an idempotency key. A user assertion supplements
the source assertion; it never rewrites it, and it never mutates a prior release.

## Verify the committed artifacts

Every committed artifact carries a closed manifest of file hashes and producing versions. Verify one,
or all of them:

```bash
labbridge validate-artifacts --bundle artifacts/source-capture
labbridge validate-artifacts
```

With no `--bundle`, the command verifies every closed manifest under `artifacts/` and under the local
`data/bundles/` root, and exits non-zero when it finds nothing — "nothing to check" and "everything
checks out" are different answers. It defaults to `--mode bundle-only`, verifies members locally, and
reports `partial`. `--mode full` additionally checks every referenced object for existence, byte
size, and SHA-256 against object storage, and reports `complete` only when those pass.

To regenerate an artifact rather than only verify it, run the command recorded in its
`REPRODUCE.txt`. For example:

```bash
python scripts/reproduce_cv_ingestion.py --output build/cv-ingestion
```

Reproductions that exercise the persistence and object-store boundary need the `infrastructure`
profile running.

## Campaign runtime

The campaign path is a second producer of the same evidence model. It runs end to end on the
generated fixture:

```bash
labbridge build-her-fixture
labbridge demo her
labbridge reconcile
```

`build-her-fixture` generates an independently produced, schema-compatible fixture with a seeded
manifest, so the replay adapter can be exercised offline without copying source values. A campaign
run against that fixture records `data_origin=synthetic`, `execution_mode=replay` — the replay
adapter reads generated bytes, so every record it produces is synthetic and none of it is evidence
about the physical system.

`reconcile` reclaims expired leases, closes abandoned attempts with durable outcomes, and classifies
stored objects without deleting unexplained bytes; a worker runs the same pass at startup.

To work with the observed dataset instead, acquire it first:

```bash
labbridge fetch-her --record-id 20439519 --dry-run
labbridge inspect-her
```

`--dry-run` reads the pinned Zenodo record, writes an archive inventory, and downloads nothing.
Without it, only explicitly named files are acquired, each is checksummed, and `provenance.json` is
written beside them in the git-ignored landing root. Source bytes are never edited after landing.
`inspect-her` records actual paths, member tables, and column shapes so that no later code infers the
schema from an article's prose.

Operating a campaign for real — migrations, backup and restore, projection rebuilds, stop conditions
— is covered in [`OPERATOR_RUNBOOK.md`](OPERATOR_RUNBOOK.md).

## Tests

```bash
pytest -q -m "not slow and not data and not integration"    # offline
pytest -q -m integration                                    # needs the infrastructure profile
```

The integration suite skips loudly when PostgreSQL and MinIO are absent rather than passing
vacuously. The full contributor gate list is in [`../CONTRIBUTING.md`](../CONTRIBUTING.md).

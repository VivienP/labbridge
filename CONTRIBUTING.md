# Contributing to LabBridge

LabBridge accepts focused changes that strengthen experimental-data integrity, reproducibility,
durability, or maintainability. The repository is provenance-first: a smaller change with explicit
failure semantics and adequate evidence is preferable to a broader unverified feature.

## Before changing code

1. Read the relevant sections of [`docs/SPEC.md`](docs/SPEC.md),
   [`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md), and
   [`docs/ARCHITECTURE_DECISIONS.md`](docs/ARCHITECTURE_DECISIONS.md).
2. Confirm the change does not contradict an accepted decision, and check
   [`docs/ROADMAP.md`](docs/ROADMAP.md) for whether it touches an open gap or a deferred track.
3. Inspect the existing implementation, migrations, tests, fixtures, and actual source data before
   assuming an interface or schema.
4. Keep the change to the smallest coherent unit that satisfies a falsifiable acceptance criterion.

[`docs/README.md`](docs/README.md) indexes the rest of the documentation. Use
[`docs/DEVELOPMENT_WORKFLOW.md`](docs/DEVELOPMENT_WORKFLOW.md) for branch, worktree, and integration
conventions. Automated contributors also follow [`AGENTS.md`](AGENTS.md); Claude Code uses the shared
adapter in [`CLAUDE.md`](CLAUDE.md).

Discuss a proposed dependency or architecture change before implementing it. LabBridge intentionally
avoids infrastructure and abstractions that do not serve a current proof obligation.

## Development setup

LabBridge requires Python 3.12 or later.

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
git config core.hooksPath scripts/hooks
```

PostgreSQL and MinIO are required for integration tests. Every Compose service belongs to a profile,
so the profile must be named:

```bash
docker compose --profile infrastructure up -d
```

Fetched research data, local object-store volumes, secrets, and generated evidence bundles must remain
outside version control.

## Implementation expectations

- Preserve immutable source bytes and append-only scientific history.
- Keep observed and synthetic data explicitly distinguished.
- Use typed scientific quantities and explicit units; never guess a missing unit or source schema.
- Record derived values with their producing version, inputs, parameters, and lineage.
- Enforce concurrency and idempotency through durable constraints at the appropriate layer.
- Retain received corrupted observations even when no scientific metric is accepted.
- Keep domain rules independent of API frameworks, ORM models, filesystem paths, and cloud SDKs.
- Add or update tests at the layer required by the claim. A mocked store does not establish storage or
  transaction behaviour.

Do not weaken an invariant, assertion, or failure classification to make a test pass.

## Verification

Run the focused tests while developing, then the relevant repository gates:

```bash
ruff format --check src/ tests/ scripts/ migrations/ .claude/hooks/ .claude/tools/
ruff check src/ tests/ scripts/ migrations/ .claude/hooks/ .claude/tools/
mypy --strict src/
pytest -q -m "not slow and not data and not integration"
python scripts/check_docs.py --strict
python .claude/tools/check_agent_system.py
python -m pytest .claude/hooks/ -q -o addopts= -o testpaths=
sha256sum -c SHA256SUMS.txt
```

Run `pytest -q -m integration` for persistence, object storage, leases, transactions, migrations, or
cross-process behaviour. It requires PostgreSQL and MinIO. Run dataset and artifact checks when the
change touches their inputs or outputs.

Report gates that were not run and why. Do not describe a capability as verified, deployed, or
demonstrated without current inspectable evidence.

## Documentation and evidence

Update documentation only when an interface, behaviour, scientific interpretation, limitation, or
evidence status changes. Public prose must be concise, technically accurate, and useful to an external
reader.

Capability status uses the four values defined in
[`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md) — `planned`, `implemented`, `demonstrated`,
`deferred` — and that document owns the current value for every capability. Changing a status there
requires the evidence it describes; changing it anywhere else is a finding.

Do not publish temporary plans, implementation diaries, machine-local paths, or claims unsupported by
a committed artifact. New normative documents and shared instruction files must be added to
`SHA256SUMS.txt` in the same change.

## Commits

Prefer one coherent, independently reviewable change per commit. Use Conventional Commits:

```text
<type>(<scope>): <imperative summary>
```

Keep the title concise. Use a short body when the rationale or consequence is not evident from the
diff. Avoid WIP messages, generic summaries, implementation diaries, unrelated formatting, and
irrelevant attribution.

## Pull requests

A pull request should state:

- what changed;
- why the change is needed;
- important design or integrity decisions;
- the exact validation performed;
- material limitations or follow-up work.

Keep the title precise and the description reviewable. Separate unrelated changes. Include migrations,
documentation, manifests, and tests when they are required by the behaviour being introduced.

Before requesting review, inspect the complete diff, run `git diff --check`, and confirm that no secret,
fetched dataset, generated result without a manifest, temporary file, or unrelated change is included.

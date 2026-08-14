# LabBridge documentation

Start with the [project README](../README.md) for what LabBridge is and why it exists. This page
routes you to the rest by what you are trying to do.

## Run LabBridge

| Document | Use it for |
|---|---|
| [`GETTING_STARTED.md`](GETTING_STARTED.md) | Prerequisites, install, the local demo, the command-line file-to-Package workflow, artifact verification and reproduction |
| [`OPERATOR_RUNBOOK.md`](OPERATOR_RUNBOOK.md) | Operating the campaign runtime: preconditions, stop conditions, migrations, backup and restore, projection rebuilds |

## Understand what it does today

| Document | Use it for |
|---|---|
| [`PROJECT_STATUS.md`](PROJECT_STATUS.md) | The canonical status of every capability, the artifact behind each claim, known limitations, and scientific boundaries |
| [`ROADMAP.md`](ROADMAP.md) | What is still open, which gaps need a human rather than more code, and what is deferred |

If a status stated anywhere else disagrees with `PROJECT_STATUS.md`, that document is correct.

## Inspect the architecture and its invariants

| Document | Use it for |
|---|---|
| [`SPEC.md`](SPEC.md) | Normative behaviour: domain models, persistence, event model, worker protocol, state machines, HTTP API, evidence bundle, proof obligations |
| [`ARCHITECTURE_DECISIONS.md`](ARCHITECTURE_DECISIONS.md) | Accepted decisions and the consequences each one accepted |
| [`FAILURE_MATRIX.md`](FAILURE_MATRIX.md) | Every failure scenario, its expected durable outcome, retention rule, and the proof that covers it |
| [`../AI_CONTRACT.md`](../AI_CONTRACT.md) | The engineering invariants the implementation must not weaken |

## Understand the data and its limits

| Document | Use it for |
|---|---|
| [`DATA_STRATEGY.md`](DATA_STRATEGY.md) | Observed and synthetic environments, the licence gate, raw/normalised/derived layers, lineage and correction policy |
| [`SIMULATOR_MODEL.md`](SIMULATOR_MODEL.md) | The deferred biosensor simulator's scientific contract, its parameter hypotheses, and the language required for synthetic output |

## Verify the evidence

Committed artifacts under [`../artifacts/`](../artifacts) are inspectable evidence rather than
documentation. Each directory holds a closed `manifest.json` of file hashes and producing versions, a
`LIMITATIONS.md` stating what it does not prove, and usually a `REPRODUCE.txt` with the exact command
that regenerates it.

```bash
labbridge validate-artifacts
```

`PROJECT_STATUS.md` maps each capability to the artifact that supports it.

## Contribute

| Document | Use it for |
|---|---|
| [`../CONTRIBUTING.md`](../CONTRIBUTING.md) | Expectations for a change, development setup, the verification gates, commit and pull-request conventions |
| [`../SECURITY.md`](../SECURITY.md) | How to report a suspected vulnerability privately, and what is in and out of scope |
| [`DEVELOPMENT_WORKFLOW.md`](DEVELOPMENT_WORKFLOW.md) | Branch, worktree, and integration conventions |
| [`../AGENTS.md`](../AGENTS.md) | Repository-wide instructions for automated contributors |
| [`../CLAUDE.md`](../CLAUDE.md) | The Claude Code adapter over those shared instructions |

## Design records and history

| Document | Use it for |
|---|---|
| [`designs/cv-passport-demo.md`](designs/cv-passport-demo.md) | How the single-user CV Passport demo is built, and the boundaries its design accepted |
| [`archive/2026-implementation-roadmap.md`](archive/2026-implementation-roadmap.md) | The numbered delivery plan that built the current system, retained for decision traceability |

Normative documents sometimes refer to "Phase 1", "Phase 2", and so on. Those are the delivery phases
defined in the archived roadmap; they are not a current plan.

## Checksums

`SHA256SUMS.txt` covers the normative documents. Verify with:

```bash
sha256sum -c SHA256SUMS.txt
```

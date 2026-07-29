# LabBridge

> *Name is a placeholder — rename in `pyproject.toml` + these docs if you prefer.*

**An experimental-data infrastructure and fault-aware campaign runtime for electrochemical R&D.**

LabBridge turns raw, noisy, failure-prone electrochemical measurement runs — the kind where the
parameters that most affect the result are usually *unreported* — into **validated, provenance-tracked,
immutable datasets** that models and scientists can trust, and it runs experimental campaigns as an
**auditable, resumable state machine that treats failures as first-class outcomes**.

It is demonstrated on two interchangeable environments behind one interface:

1. **Real** — a public electrocatalyst dataset (Au–Ir–Rh hydrogen-evolution, the electrochemistry-at-scale
   domain of autonomous-lab materials discovery). Proves LabBridge handles genuine, messy experimental data.
2. **Simulated** — a transparent, physically-grounded **enzymatic-biosensor simulator** that encodes the
   parameter→performance relationships from the author's own wet-lab R&D (carbon loading, surfactant,
   membrane, 2-vs-3-electrode setup, reference placement, cleaning protocol, immersion time →
   LOD / linearity / sensitivity / selectivity / durability), with configurable noise, batch drift, and
   **injectable instrument failures**. Its infra purpose is *deterministic fault/edge-case injection* to
   test the runtime; its scientific purpose is to carry real domain expertise without publishing
   proprietary data.

> **Status:** initialised — brief and specs frozen; implementation to be driven by AI dev agents against
> `AI_CONTRACT.md` and `docs/`.

---

## Why this project (positioning)

This is a **hiring-signal artifact** targeting the **Software Engineer, Infrastructure** role at an
AI-native materials-discovery company whose thesis is closing the simulation→reality gap for
electrocatalysts. For that role the signal is not novel ML — it is the **data + reliability layer** between
messy physical experiments and trustworthy datasets, models, and scientists. LabBridge makes every one of
that role's surfaces a first-class feature: Python services (FastAPI/Pydantic), raw experimental-data
pipelines, scientist-facing interfaces, containers, observability, job orchestration, and simulation
integration. Full mapping in [`docs/ROLE_FIT.md`](docs/ROLE_FIT.md).

The author's authentic bridge: enzymatic biosensors **are** electrochemical measurements (amperometry /
voltammetry, electrodes, reference, linear-sweep) — so the raw-signal handling and the "unreported
parameters move the result" problem transfer directly from the author's in-vitro R&D to the target's
solid-state electrocatalyst data. LabBridge connects *what the author knows* to *what the target does*.

## The integrity line (this project's #1 rule)

Because the author's real biosensor data cannot be published, one environment is **simulated**. Therefore:

> **Real and simulated data are never conflated.** Every record is indelibly tagged `source: real |
> simulated` in its schema and provenance. Simulated data is never exported, visualised, or reported as a
> real measurement. The simulator's assumptions are documented; it embodies *modelled physics*, never
> fabricated experimental numbers.

This is the credibility backbone — see `AI_CONTRACT.md` invariant #1 and [`docs/DATA_STRATEGY.md`](docs/DATA_STRATEGY.md).

## What it does (surfaces)

```bash
labbridge demo her                 # end-to-end campaign on the real HER replay environment
labbridge demo biosensor           # end-to-end campaign on the simulated biosensor environment
labbridge run campaign.yaml        # run a declared campaign (durable, resumable, auditable)
labbridge replay artifacts/run-001 # replay a past campaign from its event log
labbridge serve                    # FastAPI service: submit campaigns, query state, fetch evidence bundle
labbridge validate-artifacts       # verify committed result artifacts against their checksummed manifests
```

A campaign is one durable `campaign.yaml` (declared state) + an append-only event log (what happened),
so any run can be **paused, resumed, reproduced, and audited** — including its failures and recoveries.

## Docs

| Doc | What it is |
|-----|------------|
| [`AI_CONTRACT.md`](AI_CONTRACT.md) | Engineering contract for AI dev agents: invariants, stack, DoD, forbidden patterns |
| [`docs/SPEC.md`](docs/SPEC.md) | Architecture: data layer, oracles, fault-aware runtime, API, observability |
| [`docs/DATA_STRATEGY.md`](docs/DATA_STRATEGY.md) | The real HER dataset + the transparent biosensor simulator + integrity rules |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Dependency-ordered steps (no schedule), de-risk gate first |
| [`docs/ROLE_FIT.md`](docs/ROLE_FIT.md) | 1:1 mapping of features → the Infrastructure role, and the positioning narrative |

## Constraints

Python 3.12+ · CPU-friendly · public data only for the real environment · simulated data always labelled ·
provenance-first · failures are first-class · reproducible + checksummed artifacts.

## License

MIT.

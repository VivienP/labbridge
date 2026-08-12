---
description: Review scientific data handling — schemas, units, raw versus derived, origin propagation, lineage, corrections, and simulator claims.
argument-hint: [module, diff scope, or artifact]
---

Invoke `@data-integrity-reviewer`.

**Target:** $ARGUMENTS

Run this whenever the change touches an observation, a derived metric, a unit, a quantity definition,
HER ingestion or replay, the simulator, an export, a manifest, an evidence bundle, or a report.

## What it checks

- **Actual schema** — no column, path, unit, or identifier used without a corresponding entry in the
  inventory produced by `scripts/inspect_her.py`. Never inferred from memory or from article prose.
- **Units and definitions** — typed quantities with explicit units; one operational definition per
  metric, recorded; unknown units fail rather than being guessed.
- **Raw / normalised / derived** — the three layers stay distinct; source-provided fits and
  LabBridge recomputations use distinct `analysis_name` values.
- **Origin and execution mode** — present, propagated, enforced by validation, and proven by a test; no
  synthetic output described as measured; no replay described as a live experiment.
- **Observed versus predicted** — measured XPS and GP-predicted XPS never merged.
- **Lineage closure** — every accepted metric resolves to an observed source root or a synthetic seed
  and model configuration; the traversal actually fails on a missing parent.
- **Invalidation and supersession** — corrections create new records and relations; nothing is
  overwritten; released bundles are not mutated.
- **Real versus synthetic integrity** — every committed archive-derived artifact carries the ADR-009
  attribution on the artifact itself; fixtures independently generated.
- **Content addressing** — canonical serialisation covering bytes, dtype, shape, units, axes, schema
  version, and source identifiers.
- **Simulator claims** — each parameter effect has a hypothesis, a citation or an explicit synthetic
  label, a domain of validity, and a test that permits the documented non-monotonicity; failure injection
  stays outside the signal equations; the mandatory report language is present.

## Authority boundary

This lens verifies consistency with the repository's **own declared contract**. It does not certify that
a physical hypothesis is true.

Its report must always end by classifying what it checked as `Contract-checked`,
`Requires literature support` (naming the missing citation from `docs/SIMULATOR_MODEL.md` §12 or
`docs/DATA_STRATEGY.md` §9), or `Requires domain review` (naming the specific question for an
electrochemistry or biosensor reviewer, per `docs/SIMULATOR_MODEL.md` §11.5).

A `Requires literature support` or `Requires domain review` result blocks promotion of the associated
scientific claim. It does not by itself block infrastructure work that does not depend on that claim.

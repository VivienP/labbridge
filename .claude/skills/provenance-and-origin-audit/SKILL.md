---
name: provenance-and-origin-audit
description: Use when writing or reviewing anything that creates, transforms, exports, projects, plots, or reports a scientific record — observations, derived metrics, Parquet exports, manifests, evidence bundles, HTML reports. Enforces data-origin separation, lineage closure, append-only correction, and content addressing.
paths: src/labbridge/domain/provenance.py, src/labbridge/domain/quantities.py, src/labbridge/environments/**, src/labbridge/evidence/**, src/labbridge/infrastructure/her_ingestion/**
---

# Provenance and origin audit

Authority: `AI_CONTRACT.md` invariants 1, 3, 7, 8, 11; `docs/DATA_STRATEGY.md` §1, §5, §6, §7;
`docs/SPEC.md` §3; `docs/ARCHITECTURE_DECISIONS.md` ADR-003, ADR-005, ADR-006.

This is the credibility backbone. A provenance gap is not a documentation problem — it makes a result
uninterpretable.

## 1. Origin and execution mode

Two independent fields, never collapsed into one:

| Environment | `data_origin` | `execution_mode` |
|---|---|---|
| Au–Ir–Rh HER replay | `observed` | `replay` |
| Enzymatic biosensor simulator | `synthetic` | `simulation` |
| Future instrument adapter | `observed` | `live` (outside V1) |

Check:

- both fields are present on every observation, attempt outcome, derived metric, export row, manifest
  entry, and report section;
- they **propagate** through every transformation. Never re-derive, default, infer from a filename, or
  reconstruct them downstream — carry them.
- an adapter cannot emit an incompatible pair. This must be enforced by the type system or by
  validation, and proven by a test (invariant 1). A convention is not enforcement.
- no human-readable surface calls synthetic output measured, experimental, observed, or real;
- no surface calls a replay a live experiment, a physical-lab execution, or a new measurement.

The withdrawn vocabulary `source: real | simulated` and a universal `Fidelity = simulation | experiment`
must not reappear anywhere (ADR-003). If you find them, they are stale — report them.

## 2. Synthetic labelling

Every synthetic export must be identifiable both by a human and by a machine
(`docs/SIMULATOR_MODEL.md` §13, F-045):

- chart title or subtitle contains "Synthetic";
- tables expose `data_origin` and the simulator version as columns, not as a caption;
- filenames and object keys make it visible;
- metadata blocks carry the model version, canonical configuration hash, and seed;
- the report carries the mandatory limitation paragraph.

A synthetic export with no visible and machine-readable label is an integrity-gate failure, not a
cosmetic issue. Refuse the release.

## 3. Source-type distinctions inside observed data

The HER archive mixes categories that must never merge:

- measured XPS versus GP-predicted XPS (F-046);
- observed LSV data;
- source-provided fitted parameters;
- LabBridge-derived metrics.

Check: a distinct source-type field exists and is validated; nothing merges them into one column, one
metric, or one undifferentiated chart series; a GP-predicted property is never substituted for an
observed measurement in the replay path.

Source-provided fits and LabBridge recomputations use **distinct** `analysis_name` values
(`docs/SPEC.md` §3.6). A source-provided fit presented as independently reproduced requires a validation
artifact; without one, the claim is forbidden (`docs/DATA_STRATEGY.md` §2.5).

## 4. Lineage closure

Every accepted derived metric resolves to exactly one root (`docs/DATA_STRATEGY.md` §6):

**Observed root** — Zenodo record and version · source filename and checksum · internal source path or
record identifier · raw observation hash · parsing version · analysis version.

**Synthetic root** — simulator model version · canonical configuration hash · seed · component-model
versions · generated observation hash · analysis version.

Check the traversal actually runs and actually fails: a missing parent, an unknown version, or an
ambiguous origin must make the lineage test fail. A metric that resolves to neither root, or to both, is
a blocking defect (PO-06).

Check that no derived value can be created without an `analysis_name`, an `analysis_version`, and a
`parameter_hash`. An unversioned derived value is forbidden — it cannot be reinterpreted later.

## 5. Append-only correction

Raw observations are never overwritten (invariant 3, ADR-006).

Corrections, invalidations, reinterpretations, and reprocessing create **new** records plus explicit
relations: `supersedes`, `superseded_by`, `invalidates`, `derived_from`, `replaces`.

Check:

- no `UPDATE` or in-place mutation touches a raw observation row or object;
- an invalidation records a reason and a timestamp;
- current views may exclude invalidated records, but the original remains retrievable;
- a released evidence bundle is never mutated — a corrected result is a new bundle with an explicit
  relation to the superseded one (`docs/SPEC.md` §4.3, F-037, F-038);
- replay after a correction still reconstructs the state that was released at the time;
- projections and indexes are rebuildable from the authoritative records (F-040).

## 6. Retention of corrupted data

If bytes were received, an `Observation` exists and is content-addressed — even when the observation is
corrupted, malformed, or scientifically rejected (invariant 2, ADR-005).

The failure mode to hunt for: a validation check that returns or raises **before** the observation is
persisted. Persist first, classify second.

A corrupted observation must be visible in the evidence bundle with its hash and its validation error
(PO-05, F-011 to F-016).

## 7. Content addressing

An observation hash incorporates (`docs/DATA_STRATEGY.md` §5): canonical array bytes · dtype and byte
order · shape · quantity names and units · ordered axes · schema version · relevant source identifiers ·
normalisation version when values were transformed.

A candidate hash incorporates: candidate schema version · all typed parameter values in canonical units ·
explicit nulls · environment ID.

Check the canonicalisation is defined somewhere inspectable — how decimals, NaN, infinities, missing
values, string normalisation, and mapping order are serialised. Then check the properties:

- reordering a mapping does not change the identity;
- changing units, dtype, shape, or schema version does change it;
- the identity is stable across processes and platforms;
- nothing hashes a `repr`, a `str(dict)`, a `json.dumps` without `sort_keys`, a platform-dependent float
  format, or a mutable location (invariant 7).

## 8. Typed quantities

Scientific quantities, uncertainty, cost, instrument metadata, and environment parameters use typed
structures with explicit units — never `dict[str, Any]` (invariant 8).

Check: unit conversion is deliberate and tested; an unsupported conversion fails explicitly; an unknown
or missing unit fails validation or remains explicitly unknown and is never guessed (F-015);
environment-specific candidates use discriminated unions rather than one universal mapping.

## 9. Public observed data

Downloaded HER source files are fetched, checksummed, recorded, and retained unchanged in the raw
landing zone (invariant 11). Nothing may silently edit source bytes, impute an unavailable measurement,
replace a failed measurement, remove rows without recording the filtering operation, present a derived
value as source-provided, or present a source-provided fit as independently reproduced.

Missing, unavailable, excluded, and failed locations remain explicit. See the `her-source-discipline`
skill for the fetch and inspection protocol.

## Quick scan

These greps produce candidates, not verdicts. Inspect each hit.

```bash
rg -n "data_origin|execution_mode" src/ | rg -v "propagat|test_"   # sites that set rather than carry
rg -n "\"real\"|'real'|simulated|Fidelity" src/ docs/               # withdrawn vocabulary
rg -n "dict\[str, Any\]" src/labbridge/domain/                      # untyped scientific values
rg -n "json\.dumps" src/ | rg -v "sort_keys"                        # non-canonical serialisation
rg -n "UPDATE .*observation|\.observation.*=" src/                  # mutation of a raw record
```

---
name: her-source-discipline
description: Use before writing, changing, or reviewing any code that reads the Au–Ir–Rh HER archive, any fetch or inspection script, any HER fixture, or any replay-adapter lookup. Enforces the Gate 0 licence gate, byte-for-byte source preservation, and the rule that the schema is inspected, never remembered.
paths: scripts/fetch_her.py, scripts/inspect_her.py, src/labbridge/infrastructure/her_ingestion/**, src/labbridge/environments/her_replay.py
---

# HER source discipline

Authority: `AI_CONTRACT.md` invariant 11 and §7; `docs/DATA_STRATEGY.md` §2; `docs/ROADMAP.md` Gate 0.

Pinned sources: Zenodo DOI `10.5281/zenodo.20439519` (dataset, authoritative for archive contents) and
arXiv `2606.00779` (preprint, authoritative for method and interpretation).

## The rule that matters most

**Never infer the schema from memory, from the preprint prose, or from this file.**

`AI_CONTRACT.md` §7: *"Column names, file paths, types, and units MUST NOT be copied from memory or
inferred solely from article prose."*

Published metadata is sufficient for planning. It is never sufficient for a parser. Before any
dataset-specific code exists, `scripts/inspect_her.py` must have produced a versioned inventory of the
**actual** extracted files, and the code must reference that inventory.

If you find yourself typing a column name you have not seen in an inventory entry, stop.

## Licence gate — closed by ADR-009

The record declares `cc-by-4.0` with open access, read from the Zenodo REST API on 2026-07-30.
Redistribution is permitted **with attribution and an indication of changes**
(`docs/DATA_STRATEGY.md` §2.3, ADR-009).

What that changes: archive-derived rows, transformed subsets, fixtures, plots, and evidence bundles may
be committed. What it does not change:

- every such artifact carries the attribution from `data_use.HER_DATA_USE`, **on the artifact**, and
  states what LabBridge changed. An attribution in a commit message does not satisfy CC BY;
- the bulk archive and extracted data stay git-ignored and fetched on demand. That is repository
  hygiene, not a licence limit — do not cite it as a prohibition;
- offline fixtures stay independently generated, because a test suite must not depend on a
  multi-hundred-megabyte download.

**The structural rule survives the gate closing.** A licence string on a record is evidence, not a
decision. `parse_record` must keep returning `unresolved`; only a recorded decision pinned to the DOI
*and* to the licence identifier may widen it. If you are about to make a parser read a licence field and
conclude something, stop — that is the thing this design exists to prevent.

A decision expires by itself: if the record stops declaring `cc-by-4.0`, `resolve_redistribution` returns
`unresolved` again with no edit to any file. Never work around that by loosening the match.

## Fetch protocol

`scripts/fetch_her.py` must:

- use the pinned Zenodo record and version;
- download only explicitly selected files, never the whole archive by default;
- support a dry-run inventory mode;
- avoid the large measurement video unless explicitly requested;
- record for each file: source URL, DOI, record version, access timestamp, filename, byte size,
  source-provided checksum, and the locally computed SHA-256;
- write a machine-readable `provenance.json`;
- retain the downloaded bytes **unchanged** in an immutable raw landing prefix;
- fail on checksum mismatch and stop ingestion (F-018).

Fetching belongs in `scripts/` or a dedicated ingestion command — never inside a pure scientific
function (`AI_CONTRACT.md` §8).

Full fetched datasets are git-ignored.

## Inspection protocol

`scripts/inspect_her.py` must produce a versioned inventory covering: archive paths · file formats ·
table names and columns · inferred and declared units · row and array dimensions · missing-value
summaries · identifier ranges · duplicate checks · relationships among coordinates, libraries, LSVs,
compositions, and fitted parameters.

Gate 0 exits only when the inventory exists, `provenance.json` records the exact source and checksums,
no implementation assumption depends on an uninspected column or path, the redistribution status is
explicit, and an offline fixture can exercise the adapter independently of the archive.

## Category separation

The archive mixes categories that must stay distinct in every downstream record:

- measured XPS values;
- GP-predicted XPS values (13 measured locations per library; predictions cover the full grid);
- observed LSV data (potential vs RHE, mean current density, current-density standard deviation);
- source-provided fitted parameters (limiting current density, transfer coefficient, standard rate
  constant);
- any LabBridge-derived metric.

No predicted composition may be labelled as measured (F-046). A source-provided fit is ingested as a
source-provided derived value and is never presented as independently reproduced without a validation
artifact.

## Replay adapter semantics

The adapter maps a typed location candidate to the actual recorded observation for that source location,
or to a **structured unavailable outcome**.

It must never: interpolate a missing location; impute a failed or excluded measurement; substitute a
GP-predicted property for an observed one; fabricate uncertainty; or represent a replay as a newly
performed experiment (`docs/DATA_STRATEGY.md` §2.6).

An unavailable or excluded location produces `failed_terminal` with a source-unavailable code, and
preserves the source's own reason (F-017). Twenty areas per library are excluded from SECCM because of
collision risk — that exclusion is data, not an error to work around.

Repeated queries to the same source location return the same source observation identity. A
campaign-level repeat creates a new attempt record, never a fictitious independent measurement.

## Schema change

An archive schema the parser does not recognise must fail explicitly with an unsupported-schema error.
Never coerce, never default, never guess a column (F-019). Raw source is retained; ingestion stops until
an adapter version is added.

## Fixtures

Offline fixtures are independently generated and schema-compatible. They must be reproducible from a
recorded generator and seed, and they must be labelled as fixtures — not as observed data. They exist to
exercise the adapter's structure, not to stand in for measurements.

ADR-009 permits copying archive values into a fixture. Do not. A fixture built from archive rows makes
the test suite depend on a download, and it makes a schema regression indistinguishable from a data
change. Independence here is an engineering choice that outlived its original licence reason.

## Before you write HER code, answer these

1. Does `scripts/inspect_her.py` output exist, and does it cover the file I am about to parse?
2. Is every column, path, unit, and identifier I use present in that inventory?
3. If I am committing anything archive-derived, does the artifact itself carry the attribution?
4. Does my code preserve missing, excluded, and failed locations as explicit states?
5. Does every record I produce carry `observed` + `replay`, the source checksum, and the source path?
6. Does my parser fail loudly on an unrecognised schema?

An unanswered question means the code is not ready to write.

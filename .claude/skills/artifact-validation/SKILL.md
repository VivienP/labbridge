---
name: artifact-validation
description: Use when producing, changing, releasing, or verifying a committed result artifact, a manifest, an evidence bundle, a Parquet export, or the normative-document checksum manifest. Enforces manifest completeness, tamper detection, reproducibility, and the rule that a released artifact is immutable.
paths: artifacts/**, SHA256SUMS.txt, src/labbridge/evidence/**
---

# Artifact validation

Authority: `AI_CONTRACT.md` invariant 12 and §6; `docs/SPEC.md` §4.2, §4.3, §12, PO-07;
`docs/FAILURE_MATRIX.md` F-027, F-028, §5.

## The rules

1. Every released evidence bundle and every committed result artifact has a manifest containing hashes
   and the relevant producing versions.
2. A released artifact set is **immutable**. A correction is a new release with a new manifest and an
   explicit relation to the superseded release — never an edit.
3. Verification fails deterministically when any released file is missing, changed, unexpectedly added
   where the manifest is closed, or when the manifest itself does not match.
4. An output that is not reproducible from recorded inputs and versions is not released as scientific
   evidence.
5. A checksum is never "refreshed" to make verification pass. A mismatch is an incident (F-028).

## Evidence bundle contents

`docs/SPEC.md` §12 fixes the contents. Verify each is present before calling a bundle complete:

- immutable campaign declaration (canonical JSON or `campaign.yaml`);
- campaign metadata and environment identity;
- ordered `events.jsonl` export;
- attempts and outcomes table;
- observation inventory with hashes and object references — **including corrupted observations**;
- derived metrics table with analysis versions;
- invalidation and supersession relations;
- budget reservation and consumption summary;
- failure and recovery summary;
- code, schema, adapter, and dependency versions;
- object manifest with SHA-256 hashes;
- a self-contained HTML report;
- bundle-level checksum and release identifier;
- evidence-status limitations.

The report must visibly distinguish `observed + replay` from `synthetic + simulation`
(`docs/SPEC.md` §12). A bundle whose report does not is not releasable (F-045).

`events.jsonl` inside a bundle is a deterministic evidence **export**. It is never the concurrent event
store (invariant 4).

## Object states

Distinguish and verify: `pending` metadata · `committed` metadata · unreferenced or orphaned objects ·
released immutable objects (`docs/FAILURE_MATRIX.md` §5).

A database row must not declare an artifact committed until the object exists and its checksum has been
verified (`docs/SPEC.md` §4.2). Reconciliation verifies pending objects by checksum, commits only those
a completed transaction references, identifies unreferenced objects older than a threshold, quarantines
ambiguous objects rather than deleting them, and never mutates a released evidence object.

## Tamper tests

Artifact integrity is `demonstrated` only when a test does this, not when a checksum helper exists:

- mutate exactly one byte of a released file → verification fails (F-028);
- delete a released file → verification fails (F-027);
- alter one manifest entry → verification fails;
- add an unexpected file where the manifest is closed → verification fails or reports explicitly.

Each must fail for the right reason, with a specific error, not a generic exception.

## Reproducibility

Before releasing, confirm: every input is recorded (source checksums, or simulator configuration hash,
model version, and seed); every producing version is recorded (code, schema, adapter, analysis,
dependencies); regenerating from those inputs yields the same canonical content.

For a synthetic artifact, a test must regenerate the observation and compare its canonical content hash
(`docs/SIMULATOR_MODEL.md` §10).

## The normative-document manifest

`SHA256SUMS.txt` at the repository root checksums the normative specification set. It is the same
discipline applied to the documents themselves.

Verify:

```bash
sha256sum -c SHA256SUMS.txt
```

When a normative document changes deliberately, regenerate the manifest in the same change:

```bash
git ls-files -co --exclude-standard -- AGENTS.md AI_CONTRACT.md CLAUDE.md 'docs/*.md' \
  | xargs sha256sum > SHA256SUMS.txt
```

A mismatch you did not cause is a signal, not a nuisance. Investigate before regenerating.

## Before claiming an artifact is valid

1. Run the verification command and read its output. `labbridge validate-artifacts` once it exists;
   `sha256sum -c SHA256SUMS.txt` for the document manifest today.
2. Confirm the manifest covers every released file, not only the ones you touched.
3. Confirm every number quoted anywhere in the repository that comes from this artifact matches what the
   artifact contains today.
4. Confirm the bundle states what it does **not** cover.
5. Use the `evidence-status-discipline` wording. An artifact that exists makes a capability
   `demonstrated` only if the artifact is reproducible and verifiable — otherwise it is still
   `implemented`.

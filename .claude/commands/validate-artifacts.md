---
description: Validate manifests, checksums, evidence bundles, and the normative-document manifest.
argument-hint: [artifact path or bundle id]
---

Validate the following artifacts. Load the `artifact-validation` skill.

**Target:** $ARGUMENTS (default: every committed artifact and the normative-document manifest)

## Available now

```bash
sha256sum -c SHA256SUMS.txt
```

`SHA256SUMS.txt` checksums the normative specification set. A mismatch you did not cause is a signal:
investigate before regenerating. When a normative document changed deliberately, regenerate the manifest
in the same change:

```bash
git ls-files -co --exclude-standard -- AGENTS.md AI_CONTRACT.md CLAUDE.md 'docs/*.md' \
  | xargs sha256sum > SHA256SUMS.txt
```

## Available once the CLI exists

```bash
labbridge validate-artifacts
labbridge evidence verify <bundle>
```

Until then, report artifact validation as `NOT RUN — command not implemented`, never as passing.
`python .claude/tools/gates.py` reports the current status.

## What a valid bundle requires

Every item in `docs/SPEC.md` §12 — declaration, ordered `events.jsonl` export, attempts and outcomes,
observation inventory **including corrupted observations**, derived metrics with analysis versions,
invalidation and supersession relations, budget summary, failure and recovery summary, code and schema
versions, object manifest with SHA-256 hashes, self-contained HTML report, bundle checksum, and the
evidence-status limitations.

The report must visibly distinguish `observed + replay` from `synthetic + simulation`. A bundle whose
report does not is not releasable (F-045).

## Tamper checks

Artifact integrity is `demonstrated` only when these fail for the right reason, not when a checksum
helper exists:

- mutate one byte of a released file → verification fails (F-028);
- delete a released file → verification fails (F-027);
- alter one manifest entry → verification fails;
- add an unexpected file where the manifest is closed → verification fails or reports explicitly.

## Never

Refresh a checksum to make verification pass. A mismatch is an incident, and F-028 requires an alert and
an invalid-bundle classification — not a silent update.

Mutate a released evidence object. A correction is a new release with an explicit relation to the
superseded one.

## Report

The command run, its exit code, its output, and — for each claim that depends on an artifact — whether
the artifact exists today and matches. Close with the artifacts that could not be validated and why.

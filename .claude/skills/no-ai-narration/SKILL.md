---
name: no-ai-narration
description: Use when editing comments, docstrings, string literals, README.md, or documentation. Prevents formatting-only churn, development narration, unsupported review provenance, and internal workflow labels from leaking into committed prose.
paths: src/**, docs/**, scripts/**, tests/**, README.md, AI_CONTRACT.md
---

# no-ai-narration

## Rule

Committed prose describes the current system and its evidence, not how an agent or a session produced
it. Comments explain non-obvious constraints and invariants; Git carries the implementation history.
Once prose is accurate, clear, and compliant, stop touching it. Its wrapping, spacing, and punctuation
are not yours to polish.

## Formatting-only churn is forbidden

The commonest waste is re-editing prose that already says the right thing. Never make an edit whose only
effect on a comment, docstring, or string literal is to:

- re-wrap the same words across different line breaks;
- redistribute text across adjacent or f-string pieces when the concatenated runtime value is unchanged;
- move whitespace, or add or remove a trailing space;
- swap equivalent punctuation (`,` ↔ `;`, an em-dash reflow, `... so` ↔ `..., so`).

If the wording and every runtime value are unchanged, the edit is a provable no-op that costs tokens and
reviewer attention for zero meaning. This applies with full force to prose written earlier in the *same*
task: the first time it is correct is the last time you touch it.

```diff
# Pure re-wrap — the runtime string is byte-identical. Forbidden.
- msg = (
-     "attempt outcome, event, projection, and budget update must commit "
-     "together; partial success leaves the campaign inconsistent"
- )
+ msg = (
+     "attempt outcome, event, projection, and budget update must "
+     "commit together; partial success leaves the campaign inconsistent"
+ )

# Equivalent-punctuation churn — meaning unchanged. Forbidden.
- Retain the bytes first, then classify; a rejected metric never deletes an observation.
+ Retain the bytes first, then classify. A rejected metric never deletes an observation.
```

A PreToolUse hook (`.claude/hooks/no_prose_churn.py`) blocks the provable cases: an `Edit` whose
canonical form — wording, code, and every string value — is unchanged, and whose original lines already
fit the line limit. The hook is a floor, not the rule. It cannot judge a word or punctuation swap, so
those remain your responsibility under the threshold below. Codex has no such hook; there the rule is
entirely manual.

## Development narration

Committed prose must not contain: dates, week or phase or step labels, agent or session references,
model or vendor names, first-person change history ("I added", "previously this"), or internal review
labels (`REQUEST-CHANGES`, "reviewer finding", "blocking item 3").

Factual dated history is legitimate in `UPDATE_NOTES.md`, in `docs/ARCHITECTURE_DECISIONS.md`, in
experiment records, and in commit messages. It does not belong in source comments, docstrings, or the
README.

## Non-project operating context

Tracked prose must stand on project facts. Do not publish prompts, conversation history, private
deliberation, owner-specific reminders, workstation paths, private service state, or operator hardware,
time, budget, hosting, and execution constraints that are not intrinsic LabBridge constraints.

A private constraint may guide local execution. It does not become a product requirement, scientific
eligibility rule, public limitation, or contributor obligation unless the project independently
requires it and the public rationale stands on its own. Authorship, maintainer contact, repository
ownership, licence attribution, and reproducible environment requirements are legitimate public project
metadata.

## Review provenance

Do not describe a review as `independent`, `external`, or performed by a reviewer unless a real,
identifiable source supports that attribution. Self-review, tool-assisted analysis, or work performed
for the repository owner does not become independent because another model or session produced it.

`docs/SIMULATOR_MODEL.md` §11.5 requires a plausibility review by someone with electrochemistry or
biosensor expertise. Only an actual such review may be described as one.

Public documentation states the result and its evidence directly:

```diff
- An independent audit confirmed the replay guarantee.
+ Replay reconstructs the persisted terminal state for the campaigns in `artifacts/replay-check.json`;
+ campaigns with an unsupported event schema version are excluded and listed there.
```

## Useful comments

Comments earn their place by recording a constraint that the code cannot express:

- `# Persist the observation before validation: received bytes are retained even when rejected (ADR-005).`
- `# Single conditional UPDATE: a read-then-update lets two workers claim the same job.`
- `# Hash over canonical bytes + dtype + shape + units + schema_version; repr() is not stable.`
- `# Lease ownership is re-checked inside the transaction; a late worker must not commit.`

If a comment only restates self-explanatory code, delete it when introducing or materially changing that
code. Do not scan unrelated lines for cleanup.

## Rewrite threshold

Rewrite existing prose only when at least one is true:

- it contains development narration or an internal workflow label;
- it claims unsupported review provenance;
- it is factually wrong or contradicts current behaviour;
- it is ambiguous enough to misstate a constraint, invariant, input, output, or failure mode;
- it makes a claim stronger than the evidence supports (see `evidence-status-discipline`);
- a required formatter, linter, or documentation check rejects it — for example a line over the
  100-character limit.

Otherwise leave it unchanged. Do not edit merely to improve vocabulary, fluency, tone, or subjective
style preference.

## Before completing a documentation change

1. Scan committed prose for provenance claims, agent or vendor names, internal review labels,
   first-person narration, non-project context, operating constraints, and local paths.
2. Verify every `independent` or `external` review claim against an identifiable source.
3. Replace unsupported attribution with the current status and the evidence-based reason.
4. Inspect every match manually so legitimate history, authorship, and literal test identifiers survive.
5. For each rewrite, name the applicable threshold condition. Drop the cosmetic ones.

A useful first-pass scan — candidates, not violations:

```bash
rg -n -i "independent .*(audit|review)|external review|reviewer finding|REQUEST-CHANGES|Claude|Codex|AI-generated|as an AI|private deliberation|owner-specific|workstation path" README.md docs/ src/ scripts/
```

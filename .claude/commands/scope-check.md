---
description: Classify whether a proposed task is in scope for the active roadmap slice.
argument-hint: <task description>
---

Invoke `@scope-guard` for the following task.

**Task:** $ARGUMENTS

Use this only when scope is genuinely uncertain: the request reads two ways, may cross a V1 boundary,
introduces a technology outside `AI_CONTRACT.md` §4, touches something listed as deferred in
`docs/SPEC.md` §2, depends on an unfinished slice, would change an accepted ADR without a superseding
one, or would promote a documentation claim.

Do not run it for an already-scoped step or a small local change.

Verdicts:

- **GREEN** — eligible to proceed. This says nothing about whether the design is correct.
- **YELLOW** — valid in principle, but a named prerequisite or narrowing condition is required first.
- **RED** — necessarily violates an invariant, a V1 exclusion, an accepted ADR, or a licence boundary.
  Refuse, with the quoted boundary.
- **CLARIFY** — two readings would get different verdicts. Answer the one focused question before
  continuing.

`YELLOW` and `RED` both stop the work as proposed. Take the smallest in-scope alternative the verdict
names, or return to the author with it.

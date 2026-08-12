---
description: Plan a roadmap-scoped task — establish the active slice, check scope, and produce a spec.
argument-hint: <task description>
---

Plan the following task against the LabBridge roadmap. Do not write implementation code.

**Task:** $ARGUMENTS

## 1. Establish the active roadmap slice from evidence

Read `docs/ROADMAP.md`. Determine the active slice from what exists on disk — deliverables present, exit
criteria met by inspectable evidence, stop conditions cleared — not from what the task assumes. If you
cannot establish it, say so and treat timing as unresolved.

## 2. Scope

Run the `scope-guard` lens only if the task is genuinely ambiguous, may cross a V1 boundary, adds a
dependency, touches a deferred feature, or depends on an unfinished slice. Skip it for an already-scoped
step or a small local change.

A `RED` or `YELLOW` verdict stops planning. Report it and the smallest in-scope alternative.

## 3. State assumptions and risks

Before any design: list the assumptions the plan depends on, the invariants (`AI_CONTRACT.md` §3) the
work touches, the proof obligations (`docs/SPEC.md` §15) it advances, and the failure-matrix rows
(`docs/FAILURE_MATRIX.md`) it must satisfy. If a scientific question is unresolved, name it rather than
choosing an answer.

## 4. Define acceptance criteria

Write the criteria as falsifiable statements tied to the slice's exit criteria. Each must name the
command or artifact that would prove it, and the layer that proof has to run at.

## 5. Spec or no spec

Require an `architect` spec when the task introduces a new module, a persistence schema, an event type
or `schema_version`, a state machine, a transaction boundary, an object-storage boundary, or a migration
with compatibility implications.

Skip the spec for a small change inside an already-specified area, and say why.

When a spec is required, invoke `@architect`. Update a public specification or ADR only when the
decision changes a durable repository contract.

## 6. Report

- active slice and the evidence that establishes it;
- scope verdict, or why the scope lens was not needed;
- assumptions, risks, and unresolved questions;
- acceptance criteria with their proof commands;
- specification authority, or the justification for proceeding without a separate design;
- the smallest first implementation unit.

Do not implement. Planning ends here.

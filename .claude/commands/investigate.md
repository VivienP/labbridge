---
description: Investigate a bug, failure, flake, lost or duplicated result, or unexpected value — root cause first, then a fix at the right layer.
argument-hint: <symptom, failing command, or observed wrong state>
---

Investigate the following. Load the `systematic-debugging` skill and follow its four phases. Do not
propose a fix before Phase 1 is complete.

**Symptom:** $ARGUMENTS

## Phase 1 — Root cause

1. Read the actual failure: full traceback, exit code, line number. For a wrong value, read the value.
2. Reproduce deterministically. Fix the seed, pin the lease clock, name the exact command. If it only
   happens sometimes, that is a concurrency or ordering signal — gather data, do not guess.
3. Check recent changes: `git diff`, recent migrations, a new constraint, a changed default, a bumped
   `schema_version`.
4. Instrument the pipeline boundaries and read **durable state**, not only logs:

   ```text
   API command → durable job → adapter → object staging → outcome transaction → derived metric → evidence
   ```

   At each: is there a row, and what is its status? An event, with what `sequence` and `schema_version`?
   An object, `pending` or `committed`, and does its checksum match? Does the projection agree with a
   replay of the events? Is the budget arithmetic consistent?

5. Trace backward to where the bad value first existed. Fix there.

## Phase 2 — Pattern

Find a working analogue in the repository. Read it completely. List every difference.

## Phase 3 — Hypothesis

State one hypothesis in falsifiable terms — a mechanism, not a category. Test it with the smallest
possible change, one variable at a time.

## Phase 4 — Fix

1. Write a failing test **at the layer where it actually fails**. A race gets a concurrency test; a
   crash gets a process boundary; a rollback gets a real transaction. A unit test for a durability bug
   proves nothing and lets the bug return.
2. One fix, addressing the root cause. No adjacent cleanup.
3. Verify: the new test passes, nothing else broke, and the durable state is now correct — read it.
4. Check `docs/FAILURE_MATRIX.md`. If this bug matches an existing row, that row's proof was inadequate
   — say so. If it matches no row, propose the row rather than adding one silently.
5. After three failed fixes, stop and question the design.

## Report

- the reproduction command;
- the boundary where the pipeline first went wrong, with the durable state you read;
- the root cause, stated as a mechanism;
- the fix, and the test that proves it, with its layer;
- fresh verification output;
- which failure-matrix row this corresponds to, and whether its existing proof was adequate;
- what remains unexplained.

Never resolve this by weakening an assertion, widening a tolerance, or skipping a test.

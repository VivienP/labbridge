---
name: systematic-debugging
description: Use when encountering any bug, test failure, flaky test, stuck job, lost or duplicated result, replay mismatch, or unexpected scientific value — before proposing a fix. Root cause first; a symptom fix in a durability path hides data loss.
---

# Systematic debugging

Random fixes waste time and create new bugs. In a durable, provenance-carrying system they do worse:
they can make an incorrect result look correct, and they can hide a lost observation behind a green
suite.

**Core principle:** find the root cause before attempting a fix. A symptom fix is a failure.

## The iron law

```
NO FIX WITHOUT ROOT-CAUSE INVESTIGATION FIRST
```

## When to use

Any technical issue: test failure, wrong output, stuck job, flaky integration test, import failure — and
especially any of these LabBridge-specific surprises:

- an accepted observation is missing after a restart;
- two accepted outcomes exist for one intended attempt;
- replay produces a state that differs from the persisted projection;
- the budget shows a value that is not reserved − released + incurred;
- a job sits `LEASED` with an expired lease and is never reclaimed;
- a corrupted observation is not in the evidence bundle;
- a metric appears with no resolvable lineage;
- a derived value changes between runs with identical inputs;
- a test passes locally and fails under concurrency, or the reverse.

Use it especially when under time pressure, when "one quick fix" seems obvious, when you have already
tried several fixes, or when you do not fully understand the issue.

## Phase 1 — Root-cause investigation

1. **Read the actual failure.** Full traceback, exit code, line number. For a wrong value, read the
   value, not your expectation of it.
2. **Reproduce deterministically.** Exact command, every time. Fix the seed. Pin the lease clock. If it
   only happens sometimes, you have a concurrency or ordering problem — gather more data rather than
   guessing.
3. **Check recent changes.** `git diff`, recent migrations, a new constraint, a changed configuration
   default, a changed `schema_version`.
4. **Instrument the boundaries, then narrow.** LabBridge has an explicit pipeline. Find *where* it
   breaks before asking *why*:

   ```text
   API command
     → durable job (claim, lease, heartbeat)
       → environment adapter (bytes or structured failure)
         → object staging (pending → checksum → committed)
           → outcome transaction (outcome + event + projection + budget)
             → derived metric (analysis version, lineage)
               → evidence export (manifest, checksums)
   ```

   Ask at each boundary, from durable state rather than from logs alone:
   - is there a row for this attempt, and what is its status?
   - is there an event, and what is its `sequence` and `schema_version`?
   - is there an object, and is it `pending` or `committed`, and does its checksum match?
   - does the projection agree with a replay of the events?
   - is the budget arithmetic consistent?

   Query the database and the object store directly. The log is a narrative; the durable state is the
   fact.

5. **Trace backward to the origin.** Where did the bad value first exist? What produced it? Keep going
   up until you reach the source, and fix there.

   Recurring LabBridge root causes worth checking early:
   - a commit boundary placed between two effects that must be atomic;
   - an idempotency key derived from something non-canonical (a timestamp, a per-call UUID, an
     unordered mapping);
   - a `SELECT`-then-`INSERT` standing in for a unique constraint;
   - a worker committing after losing its lease;
   - an object marked committed before its checksum was verified;
   - a validation `return` that skips persisting received bytes;
   - replay ordered by timestamp instead of aggregate sequence;
   - `dict` or `set` iteration order reaching a persisted or hashed value;
   - a unit dropped in a conversion, or a missing unit silently defaulted;
   - `data_origin` or `execution_mode` defaulted rather than propagated.

## Phase 2 — Pattern analysis

Find a working analogue in the repository. Read it completely. List every difference between the working
and broken paths. Enumerate the assumptions and dependencies each one makes.

## Phase 3 — Hypothesis and test

State one specific hypothesis, in terms that can be false. Not "there is a race" but "worker B claims
the job because the claim is a read followed by an update rather than a single conditional update, so
both workers see `AVAILABLE`."

Test it with the smallest possible change, one variable at a time. Verify before continuing. If it was
wrong, form a new hypothesis rather than stacking fixes.

## Phase 4 — Fix

1. Write a failing test that reproduces the bug **at the layer where it actually fails**. A race gets a
   concurrency test; a crash gets a process-boundary test; a rollback gets a real transaction. A unit
   test for a durability bug proves nothing and will let the bug return.
2. Implement one fix addressing the root cause. No "while I'm here" changes.
3. Verify: the new test passes, nothing else broke, and the durable state is now what it should be —
   read it.
4. Check the failure matrix: does this bug correspond to an existing row in `docs/FAILURE_MATRIX.md`?
   If yes, that row's proof was inadequate — say so. If no, the matrix needs a row; propose it rather
   than adding one silently.
5. If the fix fails and you have tried three, stop and question the design. Do not attempt a fourth.

## The green-result corollary

A result that looks right is suspect until you have ruled out the ways it could be spuriously right:

- the test asserted on a mock rather than on durable state;
- the concurrency test ran sequentially;
- the "crash" was an exception inside the same process;
- the assertion was loosened in the same change;
- the observation compared was regenerated rather than read back;
- the replay compared against the same in-memory object it was built from.

When a result looks too clean, debug it exactly like a bug: find the root cause of the cleanliness
before believing it.

## Rationalisations that are false

| Excuse | Reality |
|---|---|
| "It's simple, skip the process" | Simple bugs have root causes too; the process is fast for them. |
| "No time" | Systematic is faster than guess-and-check thrashing. |
| "It's probably a flake" | A flaky test in a concurrency path is usually a real race. |
| "I'll write the test after" | Untested fixes do not stick, and the test is what proves the fix. |
| "The state is probably fine" | Read the row. A plausible state from a broken path is the worst outcome. |

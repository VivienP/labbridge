---
name: receiving-code-review
description: Use when acting on findings from a reviewer, a data-integrity or reliability lens, a verification audit, or a human review. How feedback is received matters as much as how it is given.
---

# Receiving code review

LabBridge uses several review lenses — code, scientific data, reliability, verification. Each emits
findings that an implementing agent or a human must act on.

## The rules

1. **Verify before implementing.** Reproduce or confirm each finding against the actual code before
   changing anything. A reviewer can be wrong. Do not apply a fix built on a misread.

2. **No performative agreement.** Do not reply "good catch, fixing" to every item. State, per finding:
   `confirmed` / `partially valid` / `disagree` — with the evidence.

3. **Push back with technical reasoning.** If a finding is wrong, or the suggested fix would violate an
   invariant, say so and explain. Examples of suggestions that must be refused here:
   - "just cache the idempotency keys in memory" — breaks invariant 5;
   - "skip persisting the observation when validation fails" — breaks invariant 2 and ADR-005;
   - "update the row instead of creating a supersession" — breaks invariant 3 and ADR-006;
   - "mock the session so the test is fast" — the test then proves nothing about durability;
   - "refresh the checksum so verification passes" — that is F-028 handled backwards.

   Deference that ships a worse design is not collaboration.

4. **Clarify before partial implementation.** If a finding is ambiguous or a fix has options, resolve
   the ambiguity first rather than implementing half of it and leaving the rest inconsistent.

5. **Re-verify after fixing.** Apply `verification-before-completion`: run the command, read the
   output, then say the finding is resolved. Never mark a finding fixed on "should be fine now".

6. **Fix the root cause, not the finding.** A finding is a symptom the reviewer could see. Apply
   `systematic-debugging` when the cause is not obvious from the finding text. A fix that makes the
   reported line correct while leaving the underlying race, gap, or non-atomic update intact is not a
   fix.

## Severity discipline

- **BLOCKING** — address before the change is considered complete.
- **WARNING** — schedule it; record where.
- **SUGGESTION** — judge on merit. A suggestion that adds complexity for little gain is correctly
  declined. Say why.

A `REQUEST-CHANGES` verdict from any lens means the change is not complete, regardless of what the other
lenses said.

A reliability verdict marking a failure-matrix row `PARTIAL` or `UNCOVERED` means the corresponding
guarantee stays `implemented`, never `demonstrated`, until the row is covered at an adequate layer.

A data-integrity verdict of `Requires literature support` or `Requires domain review` blocks promotion
of the associated scientific claim, but does not by itself block infrastructure work that does not
depend on the claim. Say which of the two applies.

## The one thing never to do

Never resolve a finding by weakening the check that produced it: loosening an assertion, widening a
tolerance, deleting a test, adding a `skip`, relaxing a constraint, or downgrading a validation to a
warning — unless that change is itself the correct fix and you say so explicitly and justify it.

The verification auditor checks the diff for exactly this pattern. Making the reviewer quiet is not the
same as making the code right.

## Reporting back

Per finding: the identifier, your verdict, what you changed (or why you did not), and the fresh
verification output that shows it. Group them; do not narrate each one.

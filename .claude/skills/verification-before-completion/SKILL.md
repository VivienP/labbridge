---
name: verification-before-completion
description: Use when about to claim work is complete, fixed, passing, or demonstrated — before committing, before reporting success, and before promoting a documentation claim. Requires running the verification command in this session and reading its output first.
---

# Verification before completion

Claiming completion without fresh verification is not efficiency. It is a false report.

**Core principle:** evidence before claims, always. Satisfying the letter of this rule while evading its
spirit is still a false report.

## The iron law

```
NO COMPLETION CLAIM WITHOUT FRESH VERIFICATION EVIDENCE
```

If you have not run the command in this session, you cannot say it passes.

## The gate

```
BEFORE claiming any status or expressing satisfaction:
1. IDENTIFY  — what command or artifact proves this exact claim?
2. RUN       — execute it fresh, in full.
3. READ      — full output, exit code, failure count.
4. VERIFY    — does the output confirm the claim? If not, report the actual status with the evidence.
5. CLAIM     — only then, and always with the evidence attached.
Skipping a step is misreporting, not verifying.
```

## Which gates exist right now

Run this first. It is the authoritative inventory and it distinguishes gates that can run today from
gates scaffolded for a module that does not exist yet:

```bash
python .claude/tools/gates.py
```

Never report a scaffolded or deferred gate as passing. Report it as `NOT RUN — <reason>`.

## Claims and their required evidence

| Claim | Requires | Not sufficient |
|---|---|---|
| "tests pass" | fresh `pytest` output, `0 failed` | a previous run, "should pass" |
| "types clean" | fresh `mypy --strict src/`, 0 errors | `ruff` passing |
| "formatting clean" | fresh `ruff format --check` | `ruff check` alone |
| "the agent system is consistent" | fresh `python .claude/tools/check_agent_system.py` | having just edited it |
| "the guard hooks work" | fresh `pytest .claude/hooks/ -o addopts= -o testpaths=` | the hook file existing |
| "durable" / "crash-safe" | an integration test crossing a real process boundary | a unit test with a mocked session |
| "idempotent" | a test racing a real database unique constraint | one process calling the handler twice |
| "budget cannot overspend" | concurrent transactions against real PostgreSQL | sequential arithmetic |
| "replay reconstructs state" | replay from persisted events compared to the persisted terminal state | replay of an in-memory list |
| "corrupted data is retained" | the stored object and its row, read back after the run | a validator returning `corrupted` |
| "artifacts verify" | a tamper test that mutates one released byte and fails verification | a checksum helper unit test |
| "the migration is safe" | an upgrade test from the previous tagged schema | the migration file existing |
| "provenance closes" | a lineage traversal reaching an observed source file or a synthetic seed and model config | the fields being present |
| "the demo runs" | a real `docker compose` run producing the expected output | the compose file existing |
| "the number is X" | reading it from the artifact file today | memory, a commit message, a hoped-for value |
| "`demonstrated`" | a named, inspectable, reproducible artifact | passing tests |

## The distinction this project lives on

`implemented` means code exists and its relevant local automated tests pass.
`demonstrated` means a reproducible artifact or operational experiment proves the claim.

They are not interchangeable, and no amount of code inspection converts one into the other
(`AI_CONTRACT.md` §10). The `evidence-status-discipline` skill governs the wording.

## Red flags — stop

- "should", "probably", "seems to", "I believe", "looks correct".
- Expressing satisfaction before the command ran.
- About to commit or report without running the gate.
- Trusting a sub-agent's success report without reading its output or the diff.
- Reporting a number you did not just read from a file.
- Skipping a gate because it is slow.
- A test that was marked `skip`/`xfail`, deleted, or loosened inside the same change that made the suite
  green. Check the diff for this specifically — it is the most damaging failure mode here, and it is
  the one `AI_CONTRACT.md` §9 and §10 exist to prevent.

## Bottom line

Run the command. Read the output. Then claim the result, with the evidence. A guarantee that no test at
an adequate layer proves is not a guarantee — it is a plan.

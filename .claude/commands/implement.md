---
description: The default LabBridge implementation workflow — inspect, spec, test, implement narrowly, verify, review claims.
argument-hint: <feature or change>
---

Implement the following change. Read `AI_CONTRACT.md` first.

**Change:** $ARGUMENTS

Do not generate a ceremonial plan for a trivial edit. Do perform every step below for anything touching
persistence, concurrency, scientific semantics, event schemas, migrations, artifacts, or public claims.

## 1. Inspect

Read the existing code, migrations, schemas, fixtures, and tests the change touches. Read the actual
fetched data before writing any dataset-specific code (`AI_CONTRACT.md` §7). Never design against a
remembered layout.

## 2. Identify the applicable specification and roadmap gate

Name the slice, the exit criterion this advances, the section of `docs/SPEC.md` that governs the
interface, and the approved task specification if one exists. If a spec is required and missing, stop
and run `/plan-slice`.

## 3. State assumptions and risks

The invariants touched, the transaction and process boundaries involved, the failure-matrix rows in
play, and the most likely way this change fails silently. Two or three sentences, not an essay.

## 4. Define acceptance criteria

Falsifiable, each with the command or artifact that proves it, at the layer it must run at.

## 5. Design or update the tests first

Write the failing tests. Run them. Confirm they fail for the right reason — a real assertion failure,
not an import error.

Pick the layer deliberately: a durability claim gets an `integration` test against real PostgreSQL or
MinIO; a crash-safety claim gets a real process boundary; a concurrency claim gets real concurrent
actors. See the `offline-tests` skill.

## 6. Implement narrowly

The minimum that makes the tests pass. Nothing else.

Stop and escalate rather than deciding silently if the change turns out to need: a different transaction
boundary, a new table or constraint, a new event type or `schema_version`, a new state transition, a new
failure code, or a dependency outside `AI_CONTRACT.md` §4.

## 7. Run focused verification

The tests for the change. Read the output.

## 8. Run broader regression verification

```bash
ruff format --check src/ tests/ scripts/ migrations/ .claude/hooks/ .claude/tools/
ruff check src/ tests/ scripts/ migrations/ .claude/hooks/ .claude/tools/
mypy --strict src/
pytest -q -m "not slow and not data and not integration"
```

Plus `pytest -q -m integration` when the change claims durability, and the artifact and migration gates
when they apply. Run `python .claude/tools/gates.py` to see which gates are live now.

## 9. Review documentation and claims

Update documentation only when a public interface, behaviour, scientific interpretation, or claim status
changed. Apply `evidence-status-discipline` to every claim word. Add the failure-matrix row or SPEC
section a new failure code or interface requires. Regenerate `SHA256SUMS.txt` if a normative document
changed.

Then run the review lenses that apply:

- `@reviewer` for any source change;
- `@data-integrity-reviewer` for observations, units, lineage, origin, HER ingestion, or the simulator;
- `@reliability-reviewer` for workers, leases, retries, failures, or a failure-matrix scenario.

Act on findings with the `receiving-code-review` discipline.

## 10. Report exact evidence and remaining limitations

- files changed with line counts;
- fresh gate output per gate, and the explicit list of gates not run, with the reason;
- which invariant each new test proves;
- which failure-matrix rows this now exercises, and which remain uncovered;
- the claim status this change legitimately supports;
- what this change does **not** prove.

Do not stage or commit. Use `/prepare-commit` when the author asks only for commit preparation. Use
`$finish-worktree` when the author explicitly requests completed-worktree finalization, including the
task-branch push and pull request.

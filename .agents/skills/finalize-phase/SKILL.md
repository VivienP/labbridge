---
name: finalize-phase
description: Use when a completed roadmap phase or other substantial coherent LabBridge implementation is ready for end-of-phase review, verification, and integration preparation.
---

# Finalize Phase

Orchestrate the repository's existing review, verification, and Git-finalization mechanisms. Do not
duplicate their specialist rules or weaken their gates.

## Authorities

- `.claude/agents/reviewer.md` owns the complete read-only review and its handoff requests.
- `verification-before-completion` owns the final evidence decision.
- `finish-worktree` owns branch preparation, staging, commit, push, and pull-request creation.
- `git-commit-rules` owns every Git authorization and hygiene checkpoint.
- The specialist named by a reviewer handoff owns that specialist decision.

Follow `AI_CONTRACT.md` and `docs/DEVELOPMENT_WORKFLOW.md`. Report contradictions between these
authorities instead of choosing a convenient interpretation.

## 1. Establish the coherent scope

Inspect the current worktree, `HEAD`, tracked and untracked changes, and commits in scope. Resolve the
base against `main`: prefer the merge base with `origin/main` when available, otherwise use local
`main`. Record the exact base.

Fetch `origin` before treating generated contracts or release evidence as final:

```bash
git fetch origin
git rev-parse HEAD
git rev-parse origin/main
git merge-base HEAD origin/main
```

The task branch contains the current `origin/main` only when the merge-base equals `origin/main`.
If it does not, STOP. Do not merge or rebase as part of this check. Require an explicit
synchronization decision before regenerating OpenAPI, generated TypeScript, or release evidence.

Review the union of:

- committed changes from the base through `HEAD`;
- every staged, unstaged, and untracked working-tree change, classified as attributable or unrelated.

Identify the approved task specification, roadmap exit criterion, affected invariants, and relevant
failure-matrix rows. If the tree mixes a substantial unrelated change with the task, stop and report
the boundary. Do not stash, restore, stage, or clean the unrelated work.

Record recent validation evidence with its command, scope, exit code, and the repository state it
covered. This evidence may be reused only under the rules in section 5.

## 2. Invoke the repository reviewer

Invoke the reviewer defined by `.claude/agents/reviewer.md` over the complete scope and pass the exact
base. Include all working-tree changes so none are silently excluded as unrelated. The reviewer remains
read-only and must emit its defined report, handoffs, and verdict.

- In Claude Code, invoke `@reviewer`.
- In Codex, delegate a read-only review that follows `.claude/agents/reviewer.md` as its complete
  contract. Do not pretend that the Claude alias exists.

Do not replace this invocation with an informal self-review.

## 3. Honor handoffs selectively

Read the reviewer's `Handoffs` section before acting on its verdict.

- If `Scientific data` contains a specific question rather than `none`, invoke the reviewer defined by
  `.claude/agents/data-integrity-reviewer.md` with that question and the same scope.
- If `Failure coverage` contains a specific question rather than `none`, invoke the reviewer defined by
  `.claude/agents/reliability-reviewer.md` with that question and the same scope.
- If a handoff is `none`, do not invoke that specialist.

Do not invoke architecture, migration, scientific-data, reliability, or other reviewers merely because
their lens might be relevant. The repository reviewer owns these two handoff decisions for this
workflow. Preserve material specialist limitations and findings in the final report.

## 4. Resolve the verdict and repeat review

Validate every finding against the current code, exact repository rule, and complete execution path.
Never change code merely to silence a reviewer.

### `REQUEST-CHANGES`

Use `receiving-code-review` only now, because acting on review findings is its documented trigger.
Return the validated `BLOCKING` and material `WARNING` findings to the implementation agent. Fix only
findings attributable to the current coherent task; do not broaden into unrelated cleanup.

Rerun checks invalidated by the fixes, then invoke the complete repository reviewer again on the new
state. Repeat the review, conditional handoffs, and fix loop until no validated `BLOCKING` finding
remains. If a blocker is unrelated, cannot be fixed safely in scope, or requires a specification
decision, stop and report it.

### `APPROVE-WITH-WARNINGS`

Preserve every material warning and verification limitation in the final report. Continue unless a
warning represents an explicit repository merge gate; if it does, stop at that gate.

### `APPROVE`

Continue to final verification.

If the verdict contradicts the report's severity sections or a specialist returns a validated blocker,
do not silently reinterpret it. Resolve the contradiction through another reviewer pass or stop and
report it.

## 5. Verify the final post-review state

Invoke `verification-before-completion` for the precise completion claim and final diff.

Reuse a prior result only when all are true:

- the exact command and complete output were read in the current working session;
- the result covers the final diff and the required proof layer;
- no subsequent code, test, configuration, migration, documentation, or artifact change could affect
  it;
- the verification authority accepts it for the claim.

Do not rerun an expensive integration check solely for ceremony when those conditions hold. Rerun every
missing, stale, partial, failed, or invalidated check. A skipped, unavailable, or scaffolded gate is not
a pass.

If verification does not support the claim, stop and report the exact limitation. Do not proceed to
integration preparation.

## 6. Delegate integration preparation

Invoke `finish-worktree` for the verified final state. It must apply `git-commit-rules` before its first
staging operation, commit, and push, and must preserve the commit-specific title approval checkpoint.

Git authorization may be granted progressively. At every missing checkpoint, pause with one concise
request naming exactly the next operation the user must authorize. Do not treat completion, invocation
of this skill, or an earlier Git approval as broader authorization.

After `finish-worktree` creates the pull request, report the branch, commit, validation evidence,
warnings, limitations, and pull-request URL or identifier. Stop there. Never merge the pull request.

## Conditional skills only

Do not invoke these skills unless their own documented trigger occurs:

- `constrained-refactor` remains explicitly requested; module size never triggers it;
- `systematic-debugging` requires an actual bug, failure, or unexpected behavior;
- `electrochemistry-expert` requires electrochemical or bioanalytical meaning to determine correctness;
- `receiving-code-review` requires acting on a concrete review finding.

## Stop conditions

Stop at the current stage and report the blocking condition when scope is incoherent, a required
reviewer cannot run, a blocker remains, verification is unsupported, or the next Git operation lacks
authorization. Never skip a stage, weaken a gate, expand scope, or merge automatically.

---
name: finish-worktree
description: Finalize a completed LabBridge worktree by performing targeted validation, creating a clean commit, pushing its branch, and opening a pull request to main. Use only when the user considers the current coherent task complete.
---

# Finish Worktree

Finalize the current LabBridge worktree for integration.

The objective is to make the completed change reviewable and open a pull request to `main` without performing an unnecessary repository-wide re-audit.

**REQUIRED SUB-SKILL:** Apply `git-commit-rules` before any Git state mutation. Load it before a branch
change or the first staging operation, keep it active through the commit and push, and reload it after
context compaction or a repository-rule change. Its authorisation checkpoints are not optional.

## 1. Establish current state

Inspect:

- the current Git worktree and HEAD;
- the base branch;
- tracked and untracked changes;
- the diff produced by this task;
- commits already created for this task.

Confirm that the current worktree contains a coherent completed change.

Record which branch, staging, commit, push, and pull-request operations the user has explicitly
authorised. Authorisation may be progressive. Treating a task as complete or invoking this skill is a
readiness signal, not authorisation for Git state changes. If the next operation is not authorised,
pause and request exactly that operation. Preserve the commit-specific title approval required by
`git-commit-rules` even when broader finalization was requested.

Do not modify `main`.

## 2. Review the final diff

Review the current task's diff for:

- accidental files;
- temporary/debug artifacts;
- secrets or local configuration;
- obvious unrelated changes;
- incomplete modifications.

Do not perform a broad repository audit.

Do not introduce unrelated cleanup or refactors.

If a substantial unrelated change is present, stop and report it instead of silently including it.

## 3. Perform targeted validation

Determine validation from the files and behavior changed by this task.

Run the relevant targeted:

- tests;
- linting;
- formatting checks;
- type checks;
- documentation validation;

according to repository instructions.

Prefer the narrowest validation that provides meaningful confidence.

When `finalize-phase` supplies fresh evidence for the exact unchanged diff, reuse it and run only
missing or invalidated checks. Do not repeat an expensive check solely for ceremony.

Do not rerun the entire repository test suite unless:

- repository policy explicitly requires it;
- the change affects shared/core behavior;
- targeted validation is insufficient.

If validation fails, fix failures caused by this work and rerun the relevant checks.

If failures appear unrelated to this work, report them rather than expanding scope.

## 4. Verify documentation completeness

Check whether the completed behavior requires updates to:

- public documentation;
- specifications;
- architecture documentation;
- examples;
- roadmap status.

Only update documentation directly affected by this task.

Do not perform a general documentation rewrite.

## 5. Prepare the branch

Before changing branches, apply `git-commit-rules` and confirm explicit authorisation for that branch
change.

Before staging or committing, ensure the worktree is on a non-`main` task branch.

If the worktree is in detached HEAD state or currently on `main`, create a descriptive branch for the
completed task.

Infer a concise branch name from the task when possible.

Name the durable outcome or capability. Do not use roadmap phase, slice, gate, sprint, or milestone
coordinates in the branch name unless the user explicitly requires that literal name.

Use conventional prefixes where appropriate:

- `feat/`
- `fix/`
- `docs/`
- `refactor/`
- `test/`
- `chore/`

Do not switch to `main`.

If a suitable non-`main` task branch already exists, use it.

## 6. Commit

Before the first staging operation, apply `git-commit-rules` and confirm explicit staging
authorisation. After staging, inspect the staged diff and complete its per-commit title approval
checkpoint. Immediately before committing, apply the still-current `git-commit-rules`; do not commit
until the exact title and that one commit are authorised.

Stage only the intended task changes.

Create a clear commit message describing the repository change, not the agent that produced it.

The subject describes the durable outcome and does not use roadmap phase, slice, gate, sprint, or
milestone coordinates unless explicitly required by the user.

Prefer conventional commit messages where appropriate.

Examples:

- `feat: add generic CSV ingestion`
- `fix: reject invalid artifact hashes`
- `docs: consolidate development workflow`
- `refactor: isolate experiment normalization`

Do not use messages such as:

- `Codex changes`
- `worktree changes`
- `WIP`
- `fix stuff`

If the task already contains clean commits, do not unnecessarily rewrite them.

## 7. Push

Before pushing, apply the `git-commit-rules` push check and obtain explicit push authorisation.

Push the task branch to the configured remote.

Never push directly to `main`.

Set the upstream branch when necessary.

## 8. Create the pull request

Obtain explicit pull-request creation authorisation if it has not already been granted.

Create a pull request targeting `main`.

The PR should concisely include:

- what changed;
- why;
- relevant implementation/design decisions;
- validation performed;
- known limitations or follow-up work, if any.

Do not include internal chain-of-thought, agent conversation history, temporary planning notes, or irrelevant implementation narration.

Use a clear PR title describing the outcome.
Do not use a roadmap phase, slice, gate, sprint, or milestone coordinate as the title.

## 9. Do not merge

Do NOT merge the pull request into `main`.

Do NOT bypass repository protections.

The final merge remains a separate integration decision.

## 10. Final response

Return a concise completion report containing:

- branch name;
- commit(s) created;
- validation performed and result;
- PR title;
- PR URL or identifier if available;
- any issue that should be reviewed before merge.

If the workflow cannot safely complete, stop at the failing step and explain exactly what remains.

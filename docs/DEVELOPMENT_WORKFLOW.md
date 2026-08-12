# Development and Worktree Workflow

This document defines the Git worktree, branch, and integration workflow for LabBridge.

The objective is to keep parallel development isolated, reviewable, reproducible, and easy to integrate, whether changes are made manually or by coding agents.

## Core Principle

A worktree represents an **independent unit of work**, not a category of files.

Use:

> **one coherent task → one branch → one worktree → one reviewable change set**

A task may include source code, tests, documentation, schemas, fixtures, migrations, or configuration when those changes are required to deliver the same outcome.

Do not separate worktrees merely because one change affects documentation and another affects code.

---

## Main Checkout

The primary checkout should remain the stable integration workspace.

Prefer not to perform unrelated feature development directly on `main`.

Use it primarily for:

- synchronizing the repository;
- inspecting integrated changes;
- final validation when appropriate;
- creating new task branches or worktrees;
- resolving integration-level issues;
- maintaining a clean view of the current repository state.

`main` should remain deployable or otherwise satisfy the repository's normal validation requirements.

---

## When to Create a Worktree

Create a separate worktree when work can proceed as an independently reviewable change.

Typical examples include:

- implementing a feature;
- fixing a bug;
- performing an independent refactor;
- redesigning or substantially updating documentation;
- performing a migration;
- investigating an experimental implementation;
- addressing an independent issue;
- running a parallel coding-agent task.

Example:

```text
main
├── feat/source-artifact
├── feat/csv-ingestion
├── fix/package-validation
└── docs/public-documentation
```

Each branch may have its own worktree and agent session.

---

## Do Not Segment by File Type

The worktree boundary is the task, not whether files contain code or documentation.

For example, this should normally remain one worktree:

```text
feat/csv-ingestion
├── implementation
├── tests
├── public API documentation
└── specification changes required by the feature
```

All of these changes contribute to the same outcome.

By contrast, a repository-wide documentation cleanup and an unrelated CSV ingestion implementation should normally use separate worktrees.

---

## Parallelism Rule

Parallelize work only when the tasks are sufficiently independent.

Good candidates:

```text
feat/csv-ingestion
fix/package-verification
docs/public-cleanup
```

Poor candidates:

```text
refactor/domain-model
feat/domain-model-v2
feat/domain-model-serialization
```

when all three require simultaneous incompatible changes to the same core abstractions.

File overlap alone is not forbidden. Semantic overlap is the more important consideration.

If two tasks require competing decisions about the same architecture, API, schema, or invariant, prefer sequencing them rather than resolving avoidable conflicts later.

---

## Dependency Rule

Before creating parallel worktrees, determine whether the tasks are:

1. independent;
2. dependent;
3. mutually conflicting.

### Independent tasks

Branch both from the latest suitable base, normally `main`.

```text
main
├── task-A
└── task-B
```

They can proceed and merge independently.

### Dependent tasks

If task B requires task A, make the dependency explicit.

Prefer:

```text
main
└── task-A
    └── task-B
```

or complete and integrate task A before beginning task B.

Do not pretend that dependent work is independent merely to maximize parallelism.

After task A reaches `main`, update task B against the new `main` and verify the complete result.

### Conflicting tasks

If two tasks redesign the same architectural boundary in incompatible ways, do not run them as independent implementation work.

Resolve the architecture first, then implement against the resulting decision.

---

## Agent Isolation

Concurrent coding-agent sessions that modify the repository should normally operate in separate worktrees.

Do not allow two independent agent sessions to modify the same working directory concurrently.

Each implementation session should have:

- a clearly defined objective;
- a dedicated worktree;
- an isolated branch or Codex-managed worktree state;
- explicit validation requirements;
- a bounded change scope.

Agents may inspect repository-wide context, but their writes should remain within their assigned worktree.

---

## Worktree Creation

For manually managed worktrees, create the branch from an up-to-date base.

Example:

```bash
git switch main
git pull --ff-only

git worktree add -b feat/csv-ingestion ../labbridge-worktrees/csv-ingestion main
```

A sibling worktree directory is preferred for manually managed worktrees because it keeps temporary checkouts outside the main repository tree.

Example layout:

```text
projects/
├── labbridge/
└── labbridge-worktrees/
    ├── csv-ingestion/
    ├── package-validation/
    └── public-documentation/
```

When Codex manages the worktree itself, prefer its native worktree lifecycle rather than duplicating that management manually.

---

## Branch Naming

Use short branch names describing the outcome.

Recommended prefixes:

```text
feat/
fix/
refactor/
docs/
test/
chore/
```

Examples:

```text
feat/csv-ingestion
fix/package-validation
refactor/domain-model
docs/public-documentation
```

Branch names should describe the actual change rather than the agent, developer, session, or worktree implementing it.

Avoid:

```text
codex-task-1
developer-worktree
agent-docs
new-changes
```

---

## Scope Discipline

A worktree should produce one coherent reviewable change set.

If an unrelated issue is discovered while working:

1. determine whether it is required for the current task;
2. if it is not required, do not silently expand the current branch;
3. record or create a separate task;
4. use another worktree if it should be addressed immediately.

Avoid opportunistic unrelated refactors inside feature branches.

Small changes are easier to understand, test, review, revert, and integrate.

---

## Documentation Changes

Documentation belongs in the same worktree as an implementation when it documents or specifies that implementation.

Example:

```text
feat/passport-generation
├── passport implementation
├── tests
├── SPEC changes
└── README changes
```

Use a separate documentation worktree when documentation itself is the independent objective.

Example:

```text
docs/public-documentation
```

for a repository-wide documentation restructuring unrelated to one particular feature.

---

## Finishing a Worktree

A worktree represents a coherent line of development, not a single prompt or development phase.
Implementation, relevant tests, documentation, fixes, and targeted validation should remain in the
same worktree when they belong to the same task.

When that task is complete and the required Git operations are explicitly authorised, invoke the
repository-local `finish-worktree` skill:

```text
$finish-worktree
```

The skill owns the executable finalization procedure. It:

1. reviews the task diff for accidental or unrelated changes;
2. runs validation proportional to the scope of the change;
3. checks directly affected documentation;
4. creates or uses an appropriate task branch;
5. commits the intended changes;
6. pushes the task branch;
7. opens a pull request targeting `main`.

Targeted validation must provide meaningful confidence in the changed behavior. A repository-wide
audit or full test run is required only when repository policy, shared behavior, or the proof
obligation makes narrower validation insufficient.

The skill never pushes directly to `main` and never merges the pull request. Review, CI, and merging
remain separate integration decisions. Worktrees themselves are not merged; their branches or commits
are integrated.

The normal lifecycle is:

```text
worktree
→ implementation
→ tests and documentation as needed
→ targeted validation
→ $finish-worktree
→ task branch
→ commit
→ push
→ pull request
→ review / CI
→ merge into main
→ worktree cleanup
```

Do not create separate worktrees merely for implementation, testing, documentation, or review when
those activities are part of the same coherent task. Create separate worktrees when independent tasks
benefit from isolation or parallel execution.

---

## Merge Order

### Independent branches

Independent branches may merge in whichever order becomes ready.

After another branch changes nearby code, update and revalidate the remaining branch before integration when appropriate.

### Dependent branches

Merge dependencies first.

For:

```text
main
└── A
    └── B
```

integrate A first.

Then update B against the resulting `main`, resolve any differences, rerun validation, and integrate B.

### High-overlap architectural changes

Do not rely on merge conflict resolution to reconcile architectural disagreements.

Merge or establish the foundational change first, then adapt dependent work to it.

---

## Updating Long-Lived Worktrees

Worktrees should be short-lived whenever practical.

For a branch that has diverged materially from `main`, synchronize it before final integration.

For example:

```bash
git fetch origin
git rebase origin/main
```

or use the repository's chosen merge-based synchronization strategy.

After synchronization:

```text
resolve conflicts
→ rerun validation
→ review resulting diff
→ continue integration
```

Never resolve conflicts mechanically without verifying semantics.

---

## Commit Discipline

Commits should represent intentional repository changes.

Do not use worktrees as justification for low-quality commit history.

Avoid commits such as:

```text
wip
changes
fix stuff
codex output
more changes
```

Prefer meaningful commits describing the repository change.

Examples:

```text
feat: add deterministic CSV source ingestion
fix: reject packages with invalid artifact hashes
docs: clarify passport provenance model
refactor: isolate experiment normalization
```

Temporary local commits may be useful during complex work, but the branch should have a clean history before final integration when the repository workflow permits rewriting it.

---

## Worktree Cleanup

A worktree is temporary development infrastructure.

After its changes are safely integrated and no further work is required:

```bash
git worktree remove ../labbridge-worktrees/csv-ingestion
git branch -d feat/csv-ingestion
```

Periodically inspect registered worktrees:

```bash
git worktree list
```

Remove stale worktrees and prune obsolete metadata when necessary:

```bash
git worktree prune
```

Do not accumulate abandoned worktrees indefinitely.

---

## Local and Ignored Files

A new worktree contains tracked repository files but should not be assumed to contain every local or ignored development artifact.

Environment setup may therefore need to be reproduced for each worktree.

Do not commit:

- secrets;
- local credentials;
- machine-specific configuration;
- private environment files;
- temporary agent state;
- local build artifacts,

unless the repository explicitly defines them as version-controlled assets.

Prefer reproducible setup scripts and documented development commands over relying on hidden machine state.

### Repository instructions and automation

Repository-level instructions and reusable automation are shared project assets, not machine state.
The following paths are public and version-controlled:

- `AI_CONTRACT.md` for normative engineering invariants and proof requirements;
- `AGENTS.md` for concise repository-wide automated-contributor instructions;
- `CLAUDE.md` for shared Claude Code guidance;
- `.claude/settings.json`, `.claude/agents/`, `.claude/commands/`, `.claude/hooks/`,
  `.claude/skills/`, and `.claude/tools/` for shared Claude configuration and workflows;
- `.agents/skills/` for the mirrored Codex skill set.

Apply ignore rules narrowly. Machine-local overrides, logs, caches, credentials, temporary plans, and
session state remain ignored, including `.claude/settings.local.json`, `.claude/logs/`, equivalent
local state under `.agents/` or `.codex/`, and normal tool caches. Do not ignore an entire tool
directory when it contains reusable project configuration.

Shared instruction files contain only durable repository rules. They must not contain workstation
paths, credentials, operator-specific preferences, conversation history, temporary decisions, or
session logs.
When two instruction documents overlap, consolidate the rule into the narrowest canonical authority
and link to it instead of maintaining parallel copies.

Enable the shared pre-commit gate with:

```bash
git config core.hooksPath scripts/hooks
```

---

## Choosing Between Local Checkout and Worktree

Use the local checkout when:

- no parallel repository modification is occurring;
- interactive debugging requires the primary local environment;
- final integration or inspection is being performed;
- the task is trivial and isolated parallelism provides no benefit.

Use a separate worktree when:

- another task is already modifying the local checkout;
- another coding agent is running concurrently;
- the task should remain isolated;
- an experiment should not disturb current work;
- several independently reviewable tasks are progressing in parallel.

Creating worktrees unnecessarily adds management overhead.

The goal is isolation where isolation has value, not maximizing the number of worktrees.

---

## Decision Checklist

Before starting a new task, ask:

```text
Is this the same coherent outcome as my current task?
│
├── Yes
│   └── Keep it in the existing worktree.
│
└── No
    │
    ├── Can it proceed independently?
    │   ├── Yes → Create another worktree.
    │   └── No
    │       └── Sequence it or make the dependency explicit.
```

Then ask:

```text
Will another agent or developer modify this repository concurrently?
│
├── Yes → Prefer worktree isolation.
└── No  → A separate worktree is optional unless isolation is otherwise useful.
```

---

## LabBridge Default

For LabBridge, the default policy is:

> Independent concurrent changes SHOULD use separate Git worktrees.

> Each worktree SHOULD correspond to one coherent, reviewable task.

> Code, tests, and documentation required by the same task SHOULD remain together.

> Concurrent tasks that make competing changes to the same architecture SHOULD be sequenced instead of parallelized.

> Dependent work SHOULD make its branch dependency explicit and SHOULD be rebased or otherwise updated after its dependency reaches `main`.

> Non-trivial changes SHOULD be integrated through reviewed pull requests.

> A worktree SHOULD be removed after its work has been safely integrated.

These rules apply equally to human development and coding-agent sessions.

---

## Agent Instructions

Coding agents working in this repository must respect the following constraints:

1. Do not create a new worktree solely because different file types are involved.
2. Treat the coherent task or pull request as the unit of isolation.
3. Do not mix unrelated changes into an existing worktree.
4. Do not run independent write-capable agent sessions in the same working tree.
5. Before parallelizing dependent work, identify and preserve the dependency.
6. Avoid parallel implementation when tasks require conflicting architectural decisions.
7. Validate the complete change before declaring a worktree ready for integration.
8. Never merge a branch solely because it is conflict-free.
9. After updating a branch against `main`, rerun relevant validation.
10. Clean up completed worktrees after their changes are safely integrated.

When uncertain whether a newly discovered change belongs in the current task, prefer preserving scope and separating the unrelated work.

---
name: constrained-refactor
description: Use when explicitly asked to restructure an oversized module without changing behaviour. One target, one extracted responsibility, one new private sibling module, verified before and after. Refuses when no low-risk seam exists or the baseline is not green.
---

# Constrained refactor

Structural cleanup is behaviour-preserving or it is not a refactor. In a system whose correctness lives
in transaction boundaries, execution order, and durable constraints, an "improvement" that reorders
initialisation or moves code across a boundary is a defect with a tidy diff.

Use this only when restructuring is explicitly requested. It never auto-runs.

## Scope of one run

Exactly two files may change:

1. the explicitly named target module;
2. one newly created **private sibling** module.

Name the new module after the responsibility: `_lease_recovery.py`, `_object_staging.py`,
`_budget_arithmetic.py`, `_event_upcasting.py`. Never `utils.py`, `helpers.py`, `common.py`, or
`types.py`.

Do not modify tests, configuration, documentation, unrelated modules, or formatting outside the
extracted region. Never split a file under `tests/`.

## Eligibility

- Explicit invocation naming one concrete path. Refuse ambiguous, directory-wide, or multi-file
  requests.
- The target exists, is a Python file under `src/labbridge/`, is not generated, and is not a test.
- Size: **600+ physical LOC** is eligible for seam analysis; **400–599** only with at least two clearly
  distinct responsibilities and a strong justification; **below 400**, refuse. LOC is an eligibility
  signal, never sufficient reason.
- Baseline is green. Before touching anything, run the project gates:

  ```bash
  ruff format --check src/ tests/ scripts/
  ruff check src/ tests/ scripts/
  mypy --strict src/
  pytest -q -m "not slow and not data and not integration"
  ```

  If any fails, stop without modifying files and report `REFACTOR NOT STARTED` with the existing
  failure. Do not fix baseline failures.

## Protected boundaries

Treat these as boundaries code may not cross in a refactor:

- a transaction boundary — what is inside and outside a `with session.begin()` block;
- the point at which received bytes are persisted, relative to validation;
- lease-ownership checks relative to the commit that depends on them;
- idempotency-key derivation and the constraint that enforces it;
- object staging, checksum verification, and the commit that marks bytes durable;
- canonical serialisation used for a content identity;
- `data_origin` / `execution_mode` propagation;
- seeds, sampling order, and any ordering that reaches a persisted or hashed value;
- numerical operation order where floating-point results could change;
- import-time side effects and module initialisation order.

If a proposed extraction touches one of these and exact preservation is not obvious, refuse.

## Hard constraints

Preserve: observable behaviour · execution order · return and yielded values · mutation behaviour ·
exception types and failure timing · warnings and logging · random-number usage and deterministic
ordering · default arguments, annotations, and signatures · import-time side effects · every supported
import from the original module path · `__all__` exactly.

Do not: rename, remove, or redefine a public symbol; change an algorithm, condition, constant, or
numerical operation; modify tests to accommodate the refactor; introduce a dependency cycle; introduce a
new dependency; overwrite an existing destination module; use `git checkout`, `git reset`,
`git restore`, or `git clean`; require a clean worktree — preserve pre-existing changes exactly.

## Choosing a seam

Do not take the first match. Inventory the module's top-level symbols, search the repository for every
reference (including package re-exports, monkeypatch strings, dependency injection, serialisation, and
string-qualified import paths), identify module-level mutable state and import-time registration, and
read the covering tests.

Prefer extracting **private collaborators** and keeping public functions and classes in the original
module as the stable façade. Moving a public symbol changes `__module__`, pickle paths, registry
identities, introspection output, and monkeypatch targets — do not move one unless analysis shows those
are irrelevant.

Preferred seam categories: a responsibility boundary (lease management vs job claiming; object staging
vs checksum verification; event append vs projection update; canonical serialisation vs hashing); pure
computation extracted from orchestration and I/O; a cohesive cluster of private collaborators.

A clean extraction normally moves 100+ substantive LOC, leaves both modules with a clear responsibility,
requires minimal data passing, and never imports the original module from the extracted one.

## Refuse when

The eligibility gate fails · the baseline is not green · no cohesive extraction of meaningful size
exists · the seam needs bidirectional imports · the extracted module would import the original · public
runtime identity cannot be preserved · import-time side effects would move or reorder · a protected
boundary becomes less explicit · the split creates substantial parameter plumbing · the result merely
distributes one tightly coupled implementation across two files · the destination already exists · exact
rollback cannot be guaranteed.

```text
REFACTOR NOT APPLICABLE

Target: <path>
LOC: <count>
Reason: <specific reason>
Recommendation: <manual alternative, or accept the current module size>
```

## Process

1. Record `git status --short` and the target's current diff. Make an exact temporary backup outside the
   repository. Confirm the destination does not exist. Record the public surface and signatures.
2. State the plan in one block: target and LOC · seam · destination · rationale · public API unchanged ·
   protected boundaries unchanged. Then proceed without asking for confirmation.
3. Perform exactly one extraction. Preserve internal code order where it may matter. Import the private
   collaborators back. Make only the minimal import adjustments. Do not opportunistically rename,
   rewrite control flow, or reformat.
4. Verify dependency direction is `original → extracted`, one-way. If a cycle appears, abandon rather
   than introducing `TYPE_CHECKING` tricks or delayed imports to force the split.
5. Compile the package, import the original module, confirm every recorded public symbol is still
   reachable from the original path, compare signatures, confirm `__all__` is unchanged, inspect the
   diff for accidental edits, and run `git diff --check`.
6. Re-run the full baseline gate. Do not report success unless every command passes.
7. On failure, fix only a structural import, export, or annotation issue caused by the extraction, then
   re-run the whole gate. If it cannot pass without a behavioural change, restore the target from the
   backup, remove the new module, confirm nothing else changed, and report `REFACTOR ABANDONED`. Never
   use a destructive Git command to roll back.

## Reports

```text
REFACTOR COMPLETE

Target
- <original>: <before> -> <after> LOC
- <new>: 0 -> <after> LOC

Seam
- <responsibility extracted>
- Dependency direction: <original> -> <new>

Compatibility
- Original import path: preserved
- Public symbols moved: <none, or explicit list>
- __all__: <unchanged / not defined>
- Protected boundaries: unchanged

Verification
- ruff format / ruff check / mypy / pytest: PASS — <commands>
- Compile and import checks: PASS
- Diff check: PASS
```

```text
REFACTOR ABANDONED

Target: <path>
Attempted seam: <description>
Failure: <specific verification or compatibility problem>
Rollback: target restored exactly; new module removed; unrelated files untouched
Recommendation: <manual alternative, or reason to keep the current structure>
```

A safe refusal or an exact rollback is a correct outcome. A partially verified or behaviour-changing
split is not.

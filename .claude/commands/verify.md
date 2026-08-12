---
description: Pre-completion audit — prove a change, slice, or claim with fresh command output before reporting it complete.
argument-hint: <the claim being made>
---

Invoke `@verification-auditor`.

**Claim:** $ARGUMENTS

Run this before reporting any change, slice, exit criterion, or capability status as complete. The
auditor is read-only: it runs the gates, reads their output, and rules on whether the evidence supports
the claim. It does not fix anything.

## The law

```
NO COMPLETION CLAIM WITHOUT FRESH VERIFICATION OUTPUT READ IN THIS SESSION
```

A previous run, a CI badge, an agent's success report, or "it should pass" is not evidence.

## Sequence

1. Restate the claim precisely and classify it: change complete · slice complete · capability
   `implemented` · capability `demonstrated` · proof obligation met. Different claims need different
   evidence.
2. Run `python .claude/tools/gates.py` to get the gate inventory — live, scaffolded, deferred.
3. Run every live applicable gate and read its output:

   ```bash
   ruff format --check src/ tests/ scripts/ migrations/ .claude/hooks/ .claude/tools/
   ruff check src/ tests/ scripts/ migrations/ .claude/hooks/ .claude/tools/
   mypy --strict src/
   pytest -q -m "not slow and not data and not integration"
   python .claude/tools/check_agent_system.py
   python scripts/check_docs.py --strict
   python -m pytest .claude/hooks/ -q -o addopts= -o testpaths=
   ```

   Plus, when the claim depends on them: `pytest -q -m integration`, `pytest -q -m data`,
   `labbridge validate-artifacts`, migration tests, and `docker compose up --build`.

4. Map each component of the claim to the exact command output or artifact path that supports it. Any
   component with no evidence is `NOT SUPPORTED`.
5. Audit the claim language with `evidence-status-discipline` — `exactly-once`, `deterministic
   execution`, `guarantees`, `production-ready`, `demonstrates`, and any number without an artifact.
6. Check documentation consistency: interfaces against `docs/SPEC.md`, new failure codes against
   `docs/FAILURE_MATRIX.md`, changed decisions against `docs/ARCHITECTURE_DECISIONS.md`, and
   `sha256sum -c SHA256SUMS.txt`.

## Verdict

`SUPPORTED` requires every live applicable gate to have passed in this session and every claim component
to map to concrete evidence. A scaffolded or deferred gate the claim depends on caps the verdict at
`PARTIALLY SUPPORTED`, with the dependency named.

The *Remaining limitations* section is never omitted.

## Check the diff for this specifically

An invariant assertion loosened, a threshold widened, a validation downgraded to a warning, or a test
skipped or deleted **inside the same change that made the suite green**. That is the most damaging
failure mode in this repository, and it is what this audit exists to catch.

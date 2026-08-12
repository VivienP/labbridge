---
description: Full repository audit — documentation consistency, agent-system integrity, claim status, roadmap position, failure coverage, and gate health.
argument-hint: [focus area]
---

Run a full audit of the repository. Read-only: report, do not fix.

**Focus:** $ARGUMENTS (default: everything below)

## 1. Automated checks

```bash
python .claude/tools/check_agent_system.py
python scripts/check_docs.py --strict
python .claude/tools/gates.py
sha256sum -c SHA256SUMS.txt
```

Report each command's exit code and output. These run today regardless of whether application code
exists.

## 2. Documentation consistency

- Do `AI_CONTRACT.md`, `docs/SPEC.md`, `docs/ARCHITECTURE_DECISIONS.md`, `docs/DATA_STRATEGY.md`,
  `docs/SIMULATOR_MODEL.md`, `docs/FAILURE_MATRIX.md`, and `docs/ROADMAP.md` agree? Resolve conflicts by
  the precedence in `AI_CONTRACT.md`, section *"When documents conflict"*, and **report** the conflict —
  never pick the convenient reading silently.
- Do `CLAUDE.md`, `AGENTS.md`, `.claude/`, and `.agents/` contradict `AI_CONTRACT.md` anywhere?
- Do `CLAUDE.md` and `AGENTS.md` express the same invariants? `check_agent_system.py` compares the
  non-negotiables blocks byte-for-byte.
- Is any withdrawn vocabulary still present — `source: real | simulated`, a universal
  `Fidelity = simulation | experiment`, JSONL described as the event store, or an "immutable dataset"
  assumption that contradicts append-only correction?

## 3. Claim status

Every material capability in `README.md` and `docs/` carries exactly one of `planned`, `implemented`,
`demonstrated`, `deferred`. For each `implemented`, name the passing tests. For each `demonstrated`, name
the artifact — and confirm it exists today.

Flag: `guarantees`, `production-ready`, `demonstrates`, `proven`, `exactly-once`,
`deterministic execution`, `fault-tolerant`, `calibrated`, `digital twin`, and every number with no
artifact behind it. Use the substitution table in `evidence-status-discipline`.

## 4. Roadmap position

Establish the active slice from what exists on disk, not from what the documents intend. For each of
Gate 0 and Slices 1–7: deliverables present, exit criteria met, stop conditions cleared. Report the
first slice whose exit criteria are not met — that is where work belongs.

Check the V1 release blockers in `docs/ROADMAP.md` and report which are currently open.

## 5. Failure and proof coverage

For each row in `docs/FAILURE_MATRIX.md`: `COVERED` / `PARTIAL` / `UNCOVERED`, with the test that proves
it and the layer that test runs at. For each PO-01 to PO-10: `not started` / `implemented` /
`demonstrated`, with the artifact for anything demonstrated.

Invoke `@reliability-reviewer` for this section when application code exists.

## 6. Data and licence

- Is the HER redistribution licence gate still open? Is any archive-derived content committed?
- Is the dataset inventory present, and does the code reference it rather than hardcoded columns?
- Are the simulator assumptions still listed as requiring literature support, and does any code assert
  one as fact?
- Is `.gitignore` still excluding fetched data, secrets, and local volumes?

## 7. Agent-system health

- Every agent's `skills:` frontmatter names a skill that exists.
- Every command references an agent, skill, or tool that exists.
- `.claude/skills/` and `.agents/skills/` are byte-identical.
- Every hook in `.claude/settings.json` points at a script that exists and parses.
- No file in the agent system duplicates a rule that belongs to `AI_CONTRACT.md`.
- Shared instruction and automation paths are visible to Git; machine-local settings, logs, caches,
  and session state are ignored through narrow rules.
- No decorative file with no operational purpose.

## 8. Secrets and hygiene

- Does any tracked file contain prompts, private deliberation, owner-specific reminders, workstation
  details, or execution constraints that are not intrinsic LabBridge requirements?
- Would every tracked file materially help a user, contributor, maintainer, or reviewer understand,
  use, verify, or maintain the project?
- Are obsolete files, duplicate documentation, stale TODOs, commented-out code, temporary scripts,
  debug artifacts, and unnecessary generated files absent?

```bash
rg -n "sk-[A-Za-z0-9]{20,}|AWS_SECRET|-----BEGIN [A-Z ]*PRIVATE KEY-----|password\s*=|token\s*=" -g '!.git' .
git ls-files | rg -n "\.env|\.pem$|\.key$"
```

## Output

```text
## REPOSITORY AUDIT

Automated checks:  <command → exit code → summary>
Active slice:      <slice> — evidence: <what establishes it>
Open V1 blockers:  <list>

### Contradictions
- <source A> vs <source B> — <the conflict, and which wins by precedence>

### Unsupported claims
- <file:line> — <claim> → <required wording or the missing artifact>

### Coverage gaps
- <F-row or PO> — <status and what is missing>

### Agent-system findings
- <finding>

### Hygiene
- <finding, or clean>

### Priority actions
1. <highest-value correction>
2. ...
```

Report. Do not fix, do not commit.

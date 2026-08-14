#!/usr/bin/env python3
"""Report which LabBridge verification gates can run right now, and which cannot.

The repository is currently a specification with no application code. Several gates named in
AI_CONTRACT.md section 10 therefore have nothing to check yet. Reporting them as "passing"
would be a false claim, and reporting them as "failing" would be noise.

This tool resolves that by inspecting the repository and classifying every gate as:

  LIVE        the target exists and the tool is installed; the command is runnable now
  BLOCKED     the target exists but the tool is missing; install it before claiming the gate
  SCAFFOLDED  the command is defined but its target module does not exist yet
  DEFERRED    the gate needs infrastructure or a release step that is out of the current slice

Exit code is 0 unless --require-live names a gate that is not LIVE. The tool never runs a gate;
use it to decide what to run and what to report as NOT RUN.

Usage:
    python .claude/tools/gates.py
    python .claude/tools/gates.py --json
    python .claude/tools/gates.py --require-live ruff-format,ruff-check,agent-system
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

LIVE = "LIVE"
BLOCKED = "BLOCKED"
SCAFFOLDED = "SCAFFOLDED"
DEFERRED = "DEFERRED"


@dataclass
class Gate:
    key: str
    command: str
    status: str
    reason: str


def _has(tool: str) -> bool:
    return shutil.which(tool) is not None


def _any_py(root: Path, *parts: str) -> bool:
    target = root.joinpath(*parts)
    return target.is_dir() and any(target.rglob("*.py"))


def _has_marked_test(root: Path, marker: str) -> bool:
    """Whether any test actually carries `marker`.

    `pytest -m <marker>` with nothing to collect exits 5, so a directory of tests is not evidence
    that a marked gate can run. Reporting it LIVE would be the false pass this tool exists to stop.
    """
    tests = root / "tests"
    if not tests.is_dir():
        return False
    needle = f"pytest.mark.{marker}"
    return any(
        needle in path.read_text(encoding="utf-8", errors="replace") for path in tests.rglob("*.py")
    )


def _has_selected_test(root: Path, marker: str, keyword: str) -> bool:
    """Whether a marked test would also survive `-k <keyword>`.

    A gate narrowed with `-k` can collect nothing even when the marker exists, and `pytest` then
    exits 5 having run no assertion. Requiring a matching test name is what stops that from being
    reported as a pass.

    The marker and the keyword must meet in the **same file**: a domain test whose name happens to
    contain the keyword does not make an integration gate runnable.
    """
    tests = root / "tests"
    if not tests.is_dir():
        return False
    needle = f"pytest.mark.{marker}"
    for path in tests.rglob("*.py"):
        body = path.read_text(encoding="utf-8", errors="replace")
        if needle not in body:
            continue
        # `-k` matches the node id, so the filename counts as well as the test name.
        if keyword in path.name:
            return True
        if any(
            keyword in line for line in body.splitlines() if line.lstrip().startswith("def test")
        ):
            return True
    return False


def _command_responds(argv: list[str], *, env: dict[str, str] | None = None) -> bool:
    """Whether a subcommand actually exists, probed via its own `--help`.

    A file existing is not the same as a command being implemented: `src/labbridge/cli.py` appears
    in Gate 0, while `validate-artifacts` arrives in Slice 3. Probing `--help` is a capability
    check, not a gate run, so this tool still never executes a gate.
    """
    if not _has(argv[0]):
        return False
    try:
        return (
            subprocess.run(
                [*argv, "--help"], capture_output=True, timeout=15, check=False, env=env
            ).returncode
            == 0
        )
    except Exception:
        return False


def _checkout_env(root: Path) -> dict[str, str]:
    """Environment that resolves `labbridge` to this checkout rather than to an editable install.

    An editable install can point at a different worktree, so a probe without this would report on
    someone else's bytes.
    """
    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH")
    source = str(root / "src")
    environment["PYTHONPATH"] = f"{source}{os.pathsep}{existing}" if existing else source
    return environment


def _tool_gate(key: str, command: str, tool: str, present: bool, missing_reason: str) -> Gate:
    if not present:
        return Gate(key, command, SCAFFOLDED, missing_reason)
    if not _has(tool):
        return Gate(key, command, BLOCKED, f"`{tool}` is not on PATH")
    return Gate(key, command, LIVE, "target and tool present")


def collect(root: Path) -> list[Gate]:
    src = _any_py(root, "src", "labbridge")
    tests = _any_py(root, "tests")
    scripts = _any_py(root, "scripts")
    code = src or tests or scripts
    migrations = any((root / d).is_dir() for d in ("alembic", "migrations"))
    compose = any(
        (root / f).exists() for f in ("docker-compose.yml", "docker-compose.yaml", "compose.yaml")
    )
    artifacts_cmd = _command_responds(
        [sys.executable, "-m", "labbridge.cli", "validate-artifacts"], env=_checkout_env(root)
    )
    has_integration = _has_marked_test(root, "integration")
    has_replay = _has_selected_test(root, "integration", "test_replay_determinism")
    has_migration_test = _has_selected_test(root, "integration", "migration")
    has_fault_campaign = _has_selected_test(root, "slow", "fault_campaign")
    has_backup_restore = (
        root / "src" / "labbridge" / "reliability" / "backup_restore.py"
    ).exists() and has_fault_campaign
    has_data = _has_marked_test(root, "data")
    manifest = (root / "SHA256SUMS.txt").exists()

    no_code = "no Python under src/labbridge, tests/, or scripts/ yet"

    gates = [
        # --- repository-level gates: these work today ---------------------------------------------
        Gate(
            "agent-system",
            "python .claude/tools/check_agent_system.py",
            LIVE,
            "agent system present",
        ),
        Gate("docs", "python scripts/check_docs.py --strict", LIVE, "documentation present"),
        Gate(
            "hook-tests",
            "python -m pytest .claude/hooks/ -q -o addopts= -o testpaths=",
            LIVE if _has("pytest") else BLOCKED,
            "guard hooks have unit tests" if _has("pytest") else "`pytest` is not on PATH",
        ),
        Gate(
            "doc-manifest",
            "sha256sum -c SHA256SUMS.txt",
            LIVE if manifest and _has("sha256sum") else BLOCKED if manifest else SCAFFOLDED,
            "SHA256SUMS.txt present"
            if manifest and _has("sha256sum")
            else "`sha256sum` is not on PATH"
            if manifest
            else "SHA256SUMS.txt does not exist",
        ),
        # --- source gates -------------------------------------------------------------------------
        _tool_gate(
            "ruff-format",
            "ruff format --check src/ tests/ scripts/ migrations/ .claude/hooks/ .claude/tools/",
            "ruff",
            code,
            no_code,
        ),
        _tool_gate(
            "ruff-check",
            "ruff check src/ tests/ scripts/ migrations/ .claude/hooks/ .claude/tools/",
            "ruff",
            code,
            no_code,
        ),
        _tool_gate("mypy", "mypy --strict src/", "mypy", src, "no Python under src/labbridge yet"),
        _tool_gate(
            "pytest-offline",
            'pytest -q -m "not slow and not data and not integration"',
            "pytest",
            tests,
            "no tests/ directory with Python files yet",
        ),
        # --- gates needing real infrastructure ----------------------------------------------------
        Gate(
            "pytest-integration",
            "pytest -q -m integration",
            LIVE if has_integration and _has("pytest") else SCAFFOLDED,
            "requires `docker compose --profile infrastructure up -d`"
            if has_integration
            else "no test carries @pytest.mark.integration yet",
        ),
        Gate(
            "pytest-data",
            "pytest -q -m data",
            LIVE if has_data and _has("pytest") else SCAFFOLDED,
            "requires the fetched HER archive on disk (`labbridge fetch-her`)"
            if has_data
            else "no test carries @pytest.mark.data yet",
        ),
        Gate(
            "migrations",
            "pytest -q -m integration -k migration",
            LIVE if migrations and has_migration_test else SCAFFOLDED,
            "migration directory and matching integration test present"
            if migrations and has_migration_test
            else "no alembic/ or migrations/ directory yet"
            if not migrations
            else "no integration test matches -k migration",
        ),
        Gate(
            "artifacts",
            "PYTHONPATH=src python -m labbridge.cli validate-artifacts",
            LIVE if artifacts_cmd else SCAFFOLDED,
            "command responds to --help; verifies the committed artifacts/ tree"
            if artifacts_cmd
            else "`validate-artifacts` is not implemented yet",
        ),
        Gate(
            "compose",
            "docker compose --profile demo up --build",
            LIVE if compose and _has("docker") else SCAFFOLDED,
            "compose file present" if compose and _has("docker") else "no compose file yet",
        ),
        # --- release-level gates ------------------------------------------------------------------
        Gate(
            "replay-determinism",
            # Not `-k replay`: that also selects a parametrised case whose id contains "replay",
            # so the command would exit 0 having run something else entirely.
            "pytest -q -m integration -k test_replay_determinism",
            LIVE if has_replay and _has("pytest") else SCAFFOLDED,
            "proves PO-01"
            if has_replay
            else "no integration test named test_replay_determinism* yet",
        ),
        Gate(
            "fault-campaign",
            "pytest -q -m slow -k fault_campaign",
            LIVE if has_fault_campaign and _has("pytest") else SCAFFOLDED,
            "process-boundary checkpoint proof; release command runs at least 100 seeded campaigns"
            if has_fault_campaign
            else "no slow test named fault_campaign yet",
        ),
        Gate(
            "backup-restore",
            "pytest -q -m slow -k fault_campaign",
            LIVE if has_backup_restore and _has("pytest") else SCAFFOLDED,
            "PO-09 restores PostgreSQL and objects into distinct environments"
            if has_backup_restore
            else "backup/restore implementation and slow proof are incomplete",
        ),
    ]
    return gates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", default=".", help="repository root (default: .)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--require-live",
        default="",
        help="comma-separated gate keys that must be LIVE; exit 1 otherwise",
    )
    args = parser.parse_args()

    root = Path(args.project_dir).resolve()
    gates = collect(root)

    if args.json:
        print(json.dumps([asdict(g) for g in gates], indent=2))
    else:
        width = max(len(g.key) for g in gates)
        print(f"LabBridge verification gates  ({root})\n")
        for status in (LIVE, BLOCKED, SCAFFOLDED, DEFERRED):
            group = [g for g in gates if g.status == status]
            if not group:
                continue
            print(f"{status}")
            for g in group:
                print(f"  {g.key.ljust(width)}  {g.command}")
                print(f"  {' ' * width}  -> {g.reason}")
            print()
        print(
            "Report a SCAFFOLDED, BLOCKED, or DEFERRED gate as `NOT RUN — <reason>`.\n"
            "Never report one as passing (AI_CONTRACT.md section 10)."
        )

    required = [k.strip() for k in args.require_live.split(",") if k.strip()]
    if required:
        by_key = {g.key: g for g in gates}
        failures = [k for k in required if k not in by_key or by_key[k].status != LIVE]
        if failures:
            for k in failures:
                got = by_key[k].status if k in by_key else "UNKNOWN GATE"
                print(f"required gate not live: {k} ({got})", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

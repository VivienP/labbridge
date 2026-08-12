from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PUBLIC_HOOK = Path(__file__).parents[2] / "scripts" / "hooks" / "pre-commit"
DOC_CHECKER = Path(__file__).parents[2] / "scripts" / "check_docs.py"


def _run(*args: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _git_bash(git: str) -> str | None:
    git_executable = Path(git).resolve()
    candidates = (
        git_executable.parents[1] / "usr" / "bin" / "bash.exe",
        git_executable.parents[1] / "bin" / "bash.exe",
    )
    return next((str(path) for path in candidates if path.is_file()), None)


def _bash_path(path: Path) -> str:
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").lower()
    tail = resolved.as_posix().split(":", 1)[1]
    return f"/{drive}{tail}"


@pytest.fixture
def hook_repo(tmp_path: Path) -> tuple[Path, str, str, dict[str, str]]:
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is required for the public hook test")
    bash = _git_bash(git)
    if bash is None:
        pytest.skip("Git Bash is required for the public hook test")

    repo = tmp_path / "repo"
    (repo / "scripts" / "hooks").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "scripts" / "hooks" / "pre-commit").write_bytes(PUBLIC_HOOK.read_bytes())
    (repo / "scripts" / "check_docs.py").write_bytes(DOC_CHECKER.read_bytes())
    (repo / "README.md").write_text("# Project\n", encoding="utf-8")
    (repo / "CONTRIBUTING.md").write_text("# Contributing\n", encoding="utf-8")
    normative = {
        "AGENTS.md": "# Repository instructions\n",
        "CLAUDE.md": "# Claude instructions\n",
        "AI_CONTRACT.md": "# Engineering contract\n",
        "docs/DEVELOPMENT_WORKFLOW.md": "# Development workflow\n",
        "docs/SPEC.md": "# Specification\n",
    }
    manifest_lines = []
    for name, content in normative.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        manifest_lines.append(f"{digest}  {name}\n")
    (repo / "SHA256SUMS.txt").write_text("".join(manifest_lines), encoding="utf-8", newline="\n")
    assert subprocess.run([git, "init", "--quiet"], cwd=repo, check=False).returncode == 0

    shim_dir = repo / ".test-bin"
    shim_dir.mkdir()
    (shim_dir / "python").write_text(
        f'#!/usr/bin/env bash\nexec "{_bash_path(Path(sys.executable))}" "$@"\n',
        encoding="utf-8",
    )
    (shim_dir / "ruff").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")

    index = tmp_path / "disposable.index"
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    assert _run(git, "read-tree", "--empty", cwd=repo, env=env).returncode == 0
    command = 'PATH="$PWD/.test-bin:/usr/bin:$PATH"; scripts/hooks/pre-commit'
    return repo, git, bash, env | {"LABBRIDGE_HOOK_COMMAND": command}


def test_public_hook_runs_from_clone_without_private_tooling(
    hook_repo: tuple[Path, str, str, dict[str, str]],
) -> None:
    repo, _git, bash, env = hook_repo
    result = _run(bash, "-c", env["LABBRIDGE_HOOK_COMMAND"], cwd=repo, env=env)

    assert result.returncode == 0, result.stdout
    assert "pre-commit: OK" in result.stdout


def test_public_hook_validates_staged_public_document(
    hook_repo: tuple[Path, str, str, dict[str, str]],
) -> None:
    repo, git, bash, env = hook_repo
    assert _run(git, "add", "--", "README.md", cwd=repo, env=env).returncode == 0

    result = _run(bash, "-c", env["LABBRIDGE_HOOK_COMMAND"], cwd=repo, env=env)

    assert result.returncode == 0, result.stdout
    assert "[run ] documentation" in result.stdout
    assert "errors: 0" in result.stdout


def test_public_hook_accepts_shared_agent_instructions(
    hook_repo: tuple[Path, str, str, dict[str, str]],
) -> None:
    repo, git, bash, env = hook_repo
    assert _run(git, "add", "--", "AGENTS.md", cwd=repo, env=env).returncode == 0

    result = _run(bash, "-c", env["LABBRIDGE_HOOK_COMMAND"], cwd=repo, env=env)

    assert result.returncode == 0, result.stdout
    assert "[run ] documentation" in result.stdout


def test_public_hook_lints_shared_python_tooling(
    hook_repo: tuple[Path, str, str, dict[str, str]],
) -> None:
    repo, git, bash, env = hook_repo
    shared_hook = repo / ".claude" / "hooks" / "shared.py"
    shared_hook.parent.mkdir(parents=True)
    shared_hook.write_text("VALUE = 1\n", encoding="utf-8")
    assert _run(git, "add", "--", ".claude/hooks/shared.py", cwd=repo, env=env).returncode == 0

    result = _run(bash, "-c", env["LABBRIDGE_HOOK_COMMAND"], cwd=repo, env=env)

    assert result.returncode == 0, result.stdout
    assert "[run ] ruff format --check" in result.stdout

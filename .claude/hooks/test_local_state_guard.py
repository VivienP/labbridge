from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

LOCAL_ONLY_PATHS = (
    ".claude/settings.local.json",
    ".claude/logs/agent-usage.jsonl",
    ".claude/state/session.json",
    ".agents/settings.local.json",
    ".agents/logs/session.jsonl",
    ".agents/state/session.json",
    ".codex/settings.local.json",
    ".codex/logs/session.jsonl",
    ".codex/state/session.json",
)


def _git_bash(git: str) -> str | None:
    git_executable = Path(git).resolve()
    candidates = (
        git_executable.parents[1] / "bin" / "bash.exe",
        git_executable.parents[1] / "usr" / "bin" / "bash.exe",
    )
    return next((str(path) for path in candidates if path.is_file()), None)


def _run(
    *args: str, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
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


@pytest.mark.parametrize("local_path", LOCAL_ONLY_PATHS)
def test_forced_local_state_is_rejected_by_public_hook(tmp_path: Path, local_path: str) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is required for the local hook test")
    bash = _git_bash(git)
    if bash is None:
        pytest.skip("Git Bash is required for the local hook test")

    repo = tmp_path / "repo"
    (repo / "scripts" / "hooks").mkdir(parents=True)
    assert _run(git, "init", "--quiet", cwd=repo).returncode == 0

    local_file = repo / local_path
    local_file.parent.mkdir(parents=True, exist_ok=True)
    local_file.write_text("local\n", encoding="utf-8")

    source_hook = Path(__file__).parents[2] / "scripts" / "hooks" / "pre-commit"
    target_hook = repo / "scripts" / "hooks" / "pre-commit"
    target_hook.write_bytes(source_hook.read_bytes())

    disposable_index = tmp_path / "disposable.index"
    env = {**os.environ, "GIT_INDEX_FILE": str(disposable_index)}
    assert _run(git, "add", "-f", "--", local_path, cwd=repo, env=env).returncode == 0

    result = _run(bash, str(target_hook), cwd=repo, env=env)

    assert result.returncode == 1
    assert "forbidden file(s) staged" in result.stdout

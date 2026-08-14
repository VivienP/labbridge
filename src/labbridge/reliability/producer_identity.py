"""Fail-closed producer identity for release evidence runs."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TypedDict

_INHERITED_GIT_DIRECTORY_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_PREFIX",
)


def _git_subprocess_env() -> dict[str, str]:
    """Ignore hook-inherited Git directory variables so cwd is the inspected tree."""
    env = os.environ.copy()
    for key in _INHERITED_GIT_DIRECTORY_VARS:
        env.pop(key, None)
    return env


class ProducerIdentity(TypedDict):
    git_head: str
    origin_main: str
    merge_base_with_origin_main: str
    origin_main_contained: bool
    working_tree: str


def _git_invocation(repo_root: Path) -> list[str]:
    """Pin linked worktrees so a bare common dir is not inspected as the producer."""
    git_meta = repo_root / ".git"
    if git_meta.is_file():
        for raw in git_meta.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.lower().startswith("gitdir:"):
                pointed = Path(line.split(":", 1)[1].strip())
                if not pointed.is_absolute():
                    pointed = (repo_root / pointed).resolve()
                return ["git", f"--git-dir={pointed}", f"--work-tree={repo_root}"]
    return ["git"]


def _git(repo_root: Path, *args: str) -> str:
    completed = subprocess.run(
        [*_git_invocation(repo_root), *args],
        cwd=repo_root,
        env=_git_subprocess_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip() or f"exit {completed.returncode}"
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def require_clean_committed_producer(
    repo_root: Path, *, allow_dirty: bool = False
) -> ProducerIdentity:
    """Refuse to start unless HEAD exists and the tree contains origin/main.

    A dirty worktree is refused unless `allow_dirty` is set. That escape hatch still records
    `working_tree=dirty` so a release artifact cannot pretend it came from a committed producer.
    """
    head = _git(repo_root, "rev-parse", "--verify", "HEAD")
    dirty = bool(_git(repo_root, "status", "--porcelain"))
    if dirty and not allow_dirty:
        raise RuntimeError(
            "producer source tree is not clean; commit or discard local changes before "
            "the evidence run"
        )
    origin_main = _git(repo_root, "rev-parse", "origin/main")
    merge_base = _git(repo_root, "merge-base", "HEAD", "origin/main")
    if merge_base != origin_main:
        raise RuntimeError(
            "producer HEAD does not contain origin/main; synchronize before generating "
            "release evidence"
        )
    return {
        "git_head": head,
        "origin_main": origin_main,
        "merge_base_with_origin_main": merge_base,
        "origin_main_contained": True,
        "working_tree": "dirty" if dirty else "clean",
    }


__all__ = ["ProducerIdentity", "require_clean_committed_producer"]

"""Fault-campaign evidence must start from a clean committed producer tree."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from labbridge.reliability.producer_identity import require_clean_committed_producer


def _fixture_git_env() -> dict[str, str]:
    """Keep fixture repos independent of the hook or worktree that launched pytest."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_AUTHOR_NAME"] = "Producer"
    env["GIT_AUTHOR_EMAIL"] = "producer@example.test"
    env["GIT_COMMITTER_NAME"] = "Producer"
    env["GIT_COMMITTER_EMAIL"] = "producer@example.test"
    return env


def _run(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-c", "core.hooksPath=", *args],
        cwd=repo,
        env=_fixture_git_env(),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "producer"
    repo.mkdir()
    _run(repo, "init", "-b", "main")
    _run(repo, "config", "user.email", "producer@example.test")
    _run(repo, "config", "user.name", "Producer")
    (repo / "README").write_text("producer\n", encoding="utf-8")
    _run(repo, "add", "README")
    _run(repo, "commit", "-m", "init")
    head = _run(repo, "rev-parse", "HEAD")
    _run(repo, "update-ref", "refs/remotes/origin/main", head)
    return repo


def test_clean_tree_records_head_and_origin_main(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    head = _run(repo, "rev-parse", "HEAD")

    identity = require_clean_committed_producer(repo)

    assert identity["git_head"] == head
    assert identity["origin_main"] == head
    assert identity["merge_base_with_origin_main"] == head
    assert identity["origin_main_contained"] is True
    assert identity["working_tree"] == "clean"


def test_dirty_worktree_fails_closed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="clean"):
        require_clean_committed_producer(repo)


def test_allow_dirty_records_the_dirty_tree_instead_of_refusing(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")
    head = _run(repo, "rev-parse", "HEAD")

    identity = require_clean_committed_producer(repo, allow_dirty=True)

    assert identity["git_head"] == head
    assert identity["working_tree"] == "dirty"


def test_inherited_git_dir_does_not_redirect_the_inspected_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    head = _run(repo, "rev-parse", "HEAD")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "missing.git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "missing.index"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "missing-worktree"))

    identity = require_clean_committed_producer(repo)

    assert identity["git_head"] == head
    assert identity["working_tree"] == "clean"


def test_stale_base_that_does_not_contain_origin_main_fails_closed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _run(repo, "checkout", "-b", "task")
    (repo / "README").write_text("task\n", encoding="utf-8")
    _run(repo, "add", "README")
    _run(repo, "commit", "-m", "task")
    _run(repo, "checkout", "main")
    (repo / "README").write_text("advanced main\n", encoding="utf-8")
    _run(repo, "add", "README")
    _run(repo, "commit", "-m", "advance main")
    advanced = _run(repo, "rev-parse", "HEAD")
    _run(repo, "update-ref", "refs/remotes/origin/main", advanced)
    _run(repo, "checkout", "task")

    with pytest.raises(RuntimeError, match="origin/main"):
        require_clean_committed_producer(repo)

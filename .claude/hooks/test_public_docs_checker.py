from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

CHECKER = Path(__file__).parents[2] / "scripts" / "check_docs.py"


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


@pytest.fixture
def public_repo(tmp_path: Path) -> Path:
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is required for the documentation checker tests")

    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    assert _run(git, "init", "--quiet", cwd=repo).returncode == 0
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
    return repo


def _check(repo: Path) -> subprocess.CompletedProcess[str]:
    return _run(sys.executable, str(CHECKER), "--strict", cwd=repo)


def _refresh_manifest(repo: Path) -> None:
    lines = []
    for name in (
        "AGENTS.md",
        "CLAUDE.md",
        "AI_CONTRACT.md",
        "docs/DEVELOPMENT_WORKFLOW.md",
        "docs/SPEC.md",
    ):
        target = repo / name
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        lines.append(f"{digest}  {name}\n")
    (repo / "SHA256SUMS.txt").write_text("".join(lines), encoding="utf-8", newline="\n")


def test_readme_and_contributing_are_public_but_non_normative(public_repo: Path) -> None:
    result = _check(public_repo)

    assert result.returncode == 0, result.stdout
    assert "not covered" not in result.stdout


def test_public_document_cannot_link_to_ignored_local_file(public_repo: Path) -> None:
    local_settings = public_repo / ".claude" / "settings.local.json"
    local_settings.parent.mkdir()
    local_settings.write_text("{}\n", encoding="utf-8")
    exclude = public_repo / ".git" / "info" / "exclude"
    exclude.write_text(".claude/settings.local.json\n", encoding="utf-8")
    (public_repo / "README.md").write_text(
        "[local settings](.claude/settings.local.json)\n", encoding="utf-8"
    )

    result = _check(public_repo)

    assert result.returncode == 1
    assert "links to local-only path" in result.stdout


def test_shared_instruction_files_are_normative(public_repo: Path) -> None:
    manifest = public_repo / "SHA256SUMS.txt"
    manifest.write_text(
        "".join(
            line
            for line in manifest.read_text(encoding="utf-8").splitlines(keepends=True)
            if not line.endswith("  AGENTS.md\n")
        ),
        encoding="utf-8",
        newline="\n",
    )

    result = _check(public_repo)

    assert result.returncode == 1
    assert "AGENTS.md: not covered" in result.stdout


def test_checksum_drift_fails_in_strict_mode(public_repo: Path) -> None:
    (public_repo / "docs" / "SPEC.md").write_text("changed\n", encoding="utf-8")

    result = _check(public_repo)

    assert result.returncode == 1
    assert "SHA-256 differs" in result.stdout


def test_package_member_uniqueness_is_not_an_exactly_once_delivery_claim(
    public_repo: Path,
) -> None:
    (public_repo / "docs" / "SPEC.md").write_text(
        "Every manifest member is present exactly once.\n", encoding="utf-8"
    )
    _refresh_manifest(public_repo)

    result = _check(public_repo)

    assert result.returncode == 0, result.stdout
    assert "claim word needs evidence" not in result.stdout


def test_exactly_once_delivery_claim_is_flagged(public_repo: Path) -> None:
    (public_repo / "docs" / "SPEC.md").write_text(
        "A result is delivered exactly once.\n", encoding="utf-8"
    )
    _refresh_manifest(public_repo)

    result = _check(public_repo)

    assert result.returncode == 0, result.stdout
    assert "claim word needs evidence" in result.stdout

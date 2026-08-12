#!/usr/bin/env python3
"""Validate the LabBridge agent system for internal consistency.

This runs today, with no application code, and catches the failure modes that make a
multi-agent setup quietly stop working:

  1. an agent or skill whose frontmatter `name` does not match its file or directory;
  2. an agent whose `skills:` frontmatter names a skill that does not exist;
  3. a command, agent, or skill referencing an `@agent` that is not defined;
  4. `.claude/skills/` and `.agents/skills/` drifting apart, so Claude and Codex would follow
     different rules;
  5. CLAUDE.md or AGENTS.md omitting its canonical repository authority;
  6. a hook wired in settings.json pointing at a script that does not exist or does not parse;
  7. a markdown link in any agentic file pointing at a path that does not exist;
  8. a referenced `.claude/tools/*.py` helper that does not exist;
  9. shared instructions being ignored, or machine-local agent state being visible to Git.

Exit code 0 when there are no errors. Warnings do not fail the check.

Usage:
    python .claude/tools/check_agent_system.py [--project-dir .]
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
from pathlib import Path

FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
MD_LINK = re.compile(r"\[[^\]]*\]\(([^)#\s]+)(?:#[^)\s]*)?\)")
AGENT_REF = re.compile(r"@([a-z][a-z0-9-]{2,})\b")
TOOL_REF = re.compile(r"\.claude/tools/([A-Za-z0-9_]+\.py)")
SHARED_PATHS = (
    "AGENTS.md",
    "CLAUDE.md",
    "AI_CONTRACT.md",
    "docs/DEVELOPMENT_WORKFLOW.md",
    ".claude/settings.json",
    ".claude/agents/reviewer.md",
    ".claude/commands/verify.md",
    ".claude/hooks/guard_bash.py",
    ".claude/skills/verification-before-completion/SKILL.md",
    ".claude/tools/check_agent_system.py",
    ".agents/skills/verification-before-completion/SKILL.md",
)
LOCAL_STATE_SAMPLES = (
    ".claude/settings.local.json",
    ".claude/logs/agent-usage.jsonl",
    ".agents/settings.local.json",
    ".agents/logs/session.jsonl",
    ".codex/settings.local.json",
    ".codex/logs/session.jsonl",
    ".codex/state/session.json",
)

# `@` tokens that are not agent references.
AGENT_REF_IGNORE = {
    "pytest",
    "staticmethod",
    "classmethod",
    "property",
    "dataclass",
    "override",
    "cached",
}


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def note(self, msg: str) -> None:
        self.notes.append(msg)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _frontmatter_field(text: str, field: str) -> str | None:
    match = FRONTMATTER.match(text)
    if not match:
        return None
    for line in match.group(1).splitlines():
        if line.startswith(f"{field}:"):
            return line.split(":", 1)[1].strip().strip("\"'")
    return None


def _frontmatter_list(text: str, field: str) -> list[str]:
    match = FRONTMATTER.match(text)
    if not match:
        return []
    lines = match.group(1).splitlines()
    out: list[str] = []
    collecting = False
    for line in lines:
        if line.startswith(f"{field}:"):
            collecting = True
            continue
        if collecting:
            stripped = line.strip()
            if stripped.startswith("- "):
                out.append(stripped[2:].strip().strip("\"'"))
                continue
            if line[:1] not in (" ", "\t") and stripped:
                break
    return out


def check_agents(root: Path, report: Report) -> tuple[set[str], set[str]]:
    agents_dir = root / ".claude" / "agents"
    skills_dir = root / ".claude" / "skills"
    agent_names: set[str] = set()
    skill_names = (
        {p.name for p in skills_dir.iterdir() if p.is_dir() and (p / "SKILL.md").exists()}
        if skills_dir.is_dir()
        else set()
    )

    if not agents_dir.is_dir():
        report.error(".claude/agents/ does not exist")
        return agent_names, skill_names

    for path in sorted(agents_dir.glob("*.md")):
        text = _read(path)
        rel = path.relative_to(root).as_posix()
        if not FRONTMATTER.match(text):
            report.error(f"{rel}: missing YAML frontmatter")
            continue
        name = _frontmatter_field(text, "name")
        if not name:
            report.error(f"{rel}: frontmatter has no `name`")
        elif name != path.stem:
            report.error(f"{rel}: frontmatter name `{name}` does not match filename `{path.stem}`")
        else:
            agent_names.add(name)
        if not _frontmatter_field(text, "description") and "description: |" not in text:
            report.error(f"{rel}: frontmatter has no `description`")
        for skill in _frontmatter_list(text, "skills"):
            if skill not in skill_names:
                report.error(
                    f"{rel}: declares skill `{skill}` which does not exist under .claude/skills/"
                )

    if not agent_names:
        report.error(".claude/agents/ contains no valid agent definitions")
    return agent_names, skill_names


def check_skills(root: Path, report: Report) -> None:
    skills_dir = root / ".claude" / "skills"
    if not skills_dir.is_dir():
        report.error(".claude/skills/ does not exist")
        return
    for path in sorted(skills_dir.iterdir()):
        if not path.is_dir():
            continue
        skill_md = path / "SKILL.md"
        rel = skill_md.relative_to(root).as_posix()
        if not skill_md.exists():
            report.error(f".claude/skills/{path.name}/: has no SKILL.md")
            continue
        text = _read(skill_md)
        if not FRONTMATTER.match(text):
            report.error(f"{rel}: missing YAML frontmatter")
            continue
        name = _frontmatter_field(text, "name")
        if name != path.name:
            report.error(f"{rel}: frontmatter name `{name}` does not match directory `{path.name}`")
        if not _frontmatter_field(text, "description"):
            report.error(f"{rel}: frontmatter has no `description`")


def check_mirror(root: Path, report: Report) -> None:
    claude = root / ".claude" / "skills"
    codex = root / ".agents" / "skills"
    if not codex.is_dir():
        report.error(".agents/skills/ does not exist; Codex would have no skills")
        return
    names = {p.name for p in claude.iterdir() if p.is_dir()} | {
        p.name for p in codex.iterdir() if p.is_dir()
    }
    for name in sorted(names):
        a = claude / name / "SKILL.md"
        b = codex / name / "SKILL.md"
        if not a.exists():
            report.error(f"skill `{name}` exists under .agents/ but not .claude/")
        elif not b.exists():
            report.error(
                f"skill `{name}` exists under .claude/ but not .agents/ (Codex would miss it)"
            )
        elif a.read_bytes() != b.read_bytes():
            report.error(
                f"skill `{name}` differs between .claude/ and .agents/; "
                "Claude and Codex would follow different rules"
            )


def check_adapter_authorities(root: Path, report: Report) -> None:
    """Adapters must point to shared authorities instead of restating them."""
    requirements = {
        "AGENTS.md": ("AI_CONTRACT.md", "docs/DEVELOPMENT_WORKFLOW.md"),
        "CLAUDE.md": ("AGENTS.md", "AI_CONTRACT.md"),
    }
    for name, references in requirements.items():
        path = root / name
        if not path.exists():
            report.error(f"{name} does not exist")
            continue
        text = _read(path)
        for reference in references:
            if reference not in text:
                report.error(f"{name}: does not reference `{reference}`")


def check_settings(root: Path, report: Report) -> None:
    settings = root / ".claude" / "settings.json"
    if not settings.exists():
        report.error(".claude/settings.json does not exist; no hooks would be wired")
        return
    try:
        data = json.loads(_read(settings))
    except json.JSONDecodeError as exc:
        report.error(f".claude/settings.json: invalid JSON ({exc})")
        return

    referenced: set[str] = set()
    for event, entries in (data.get("hooks") or {}).items():
        for entry in entries:
            for hook in entry.get("hooks", []):
                command = str(hook.get("command") or "")
                for match in re.finditer(r"\.claude/hooks/([A-Za-z0-9_.-]+\.py)", command):
                    referenced.add(match.group(1))
                if not command:
                    report.error(f".claude/settings.json: {event} hook has an empty command")

    hooks_dir = root / ".claude" / "hooks"
    for script in sorted(referenced):
        path = hooks_dir / script
        if not path.exists():
            report.error(
                f".claude/settings.json references .claude/hooks/{script}, which is missing"
            )
            continue
        try:
            ast.parse(_read(path))
        except SyntaxError as exc:
            report.error(f".claude/hooks/{script}: syntax error at line {exc.lineno}")

    present = {p.name for p in hooks_dir.glob("*.py") if not p.name.startswith("test_")}
    for orphan in sorted(present - referenced):
        report.warn(f".claude/hooks/{orphan} exists but no settings.json hook references it")


def check_references(
    root: Path, agent_names: set[str], skill_names: set[str], report: Report
) -> None:
    targets = [
        root / "AGENTS.md",
        root / "CLAUDE.md",
        root / "AI_CONTRACT.md",
        root / "docs" / "DEVELOPMENT_WORKFLOW.md",
    ]
    for sub in (".claude/agents", ".claude/commands", ".claude/skills"):
        base = root / sub
        if base.is_dir():
            targets.extend(sorted(base.rglob("*.md")))

    tools_dir = root / ".claude" / "tools"
    for path in targets:
        rel = path.relative_to(root).as_posix()
        text = _read(path)

        for link in MD_LINK.findall(text):
            if link.startswith(("http://", "https://", "mailto:")):
                continue
            candidate = (path.parent / link).resolve()
            if not candidate.exists() and not (root / link.lstrip("/")).exists():
                report.error(f"{rel}: markdown link `{link}` does not resolve")

        for ref in AGENT_REF.findall(text):
            if ref in AGENT_REF_IGNORE or ref in agent_names:
                continue
            if ref in skill_names:
                continue
            report.warn(f"{rel}: references `@{ref}`, which is not a defined agent")

        for tool in set(TOOL_REF.findall(text)):
            if not (tools_dir / tool).exists():
                report.error(f"{rel}: references .claude/tools/{tool}, which does not exist")


def check_tools(root: Path, report: Report) -> None:
    tools_dir = root / ".claude" / "tools"
    if not tools_dir.is_dir():
        report.error(".claude/tools/ does not exist")
        return
    for path in sorted(tools_dir.glob("*.py")):
        try:
            ast.parse(_read(path))
        except SyntaxError as exc:
            report.error(f".claude/tools/{path.name}: syntax error at line {exc.lineno}")


def check_repository_policy(root: Path, report: Report) -> None:
    for shared in SHARED_PATHS:
        path = root / shared
        if not path.is_file():
            report.error(f"shared repository instruction is missing: {shared}")
            continue
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "-v", "--", shared],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            report.error(f"shared repository instruction is ignored: {shared}")

    for local in LOCAL_STATE_SAMPLES:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "-v", "--", local],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            report.error(f"machine-local agent state is not ignored: {local}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", default=".", help="repository root (default: .)")
    args = parser.parse_args()
    root = Path(args.project_dir).resolve()

    report = Report()
    agent_names, skill_names = check_agents(root, report)
    check_skills(root, report)
    check_mirror(root, report)
    check_adapter_authorities(root, report)
    check_settings(root, report)
    check_tools(root, report)
    check_references(root, agent_names, skill_names, report)
    check_repository_policy(root, report)

    report.note(f"agents: {len(agent_names)}")
    report.note(f"skills: {len(skill_names)}")
    commands = root / ".claude" / "commands"
    report.note(f"commands: {len(list(commands.glob('*.md'))) if commands.is_dir() else 0}")

    print(f"agent-system check  ({root})\n")
    for line in report.notes:
        print(f"  {line}")
    print()
    for line in report.warnings:
        print(f"  WARN  {line}")
    for line in report.errors:
        print(f"  ERROR {line}")
    if not report.errors and not report.warnings:
        print("  no findings")
    print()
    print(f"errors: {len(report.errors)}  warnings: {len(report.warnings)}")
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

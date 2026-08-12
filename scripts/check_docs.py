#!/usr/bin/env python3
"""Validate public LabBridge documentation links, claims, and checksums."""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MD_LINK = re.compile(r"\[[^\]]*\]\(([^)#\s]+)(?:#[^)\s]*)?\)")
MANIFEST = "SHA256SUMS.txt"
MANIFEST_FIELDS = 2
PUBLIC_ROOT_DOCUMENTS = {
    "AGENTS.md",
    "AI_CONTRACT.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "README.md",
}
NON_NORMATIVE = {"README.md", "CONTRIBUTING.md"}
VOCAB_EXEMPT = {
    "docs/ARCHITECTURE_DECISIONS.md",
    "docs/DATA_STRATEGY.md",
    "docs/SPEC.md",
}
WITHDRAWN_VOCAB = (
    (re.compile(r"source:\s*real\s*\|\s*simulated", re.I), "withdrawn origin vocabulary"),
    (
        re.compile(r"\bFidelity\s*=\s*simulation\s*\|\s*experiment", re.I),
        "withdrawn universal fidelity type",
    ),
    (re.compile(r"\bdigital twin\b", re.I), "the simulator is not a digital twin"),
)
CLAIM_WORDS = (
    (
        re.compile(
            r"\b(?:exactly-once|"
            r"(?:deliver(?:y|ed)|execut(?:ion|ed)|process(?:ing|ed)|effect(?:s)?)\s+exactly once|"
            r"exactly once\s+(?:deliver(?:y|ed)|execut(?:ion|ed)|"
            r"process(?:ing|ed)|effect(?:s)?))\b",
            re.I,
        ),
        "say: at-least-once delivery with idempotent effect handling",
    ),
    (re.compile(r"\bdeterministic execution\b", re.I), "say: deterministic state reconstruction"),
    (re.compile(r"\bproduction[- ]ready\b", re.I), "requires operational deployment evidence"),
    (re.compile(r"\bfault[- ]tolerant\b", re.I), "say: fault-aware"),
    (
        re.compile(r"\bcalibrated uncertainty\b", re.I),
        "requires a calibration procedure and evaluation",
    ),
    (
        re.compile(r"\bguarantees\s+(?:that\b|no\b|zero\b|exactly\b|every\b)", re.I),
        "name the tested boundary instead of a universal guarantee",
    ),
)
PROHIBITION = re.compile(
    r"\b(?:MUST NOT|SHOULD NOT|never|not\b|no\b|without|avoid|forbid|forbidden|prohibit|"
    r"refuse|do not|don't|until|before|unless|withdrawn|deferred|instead of|rather than|say:)\b",
    re.I,
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _git_paths(root: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return [line.replace("\\", "/") for line in result.stdout.splitlines()]


def _docs(root: Path) -> list[Path]:
    paths = _git_paths(root)
    if paths:
        selected = {
            path
            for path in paths
            if path in PUBLIC_ROOT_DOCUMENTS or (path.startswith("docs/") and path.endswith(".md"))
        }
        return [root / path for path in sorted(selected) if (root / path).is_file()]

    fallback = [root / name for name in sorted(PUBLIC_ROOT_DOCUMENTS)]
    docs = root / "docs"
    if docs.is_dir():
        fallback.extend(sorted(docs.rglob("*.md")))
    return [path for path in fallback if path.is_file()]


def _relative(root: Path, path: Path) -> str | None:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _ignored(root: Path, rel: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--", rel],
        cwd=root,
        capture_output=True,
        timeout=10,
        check=False,
    )
    return result.returncode == 0


def check_links(root: Path, errors: list[str]) -> None:
    for path in _docs(root):
        rel = path.relative_to(root).as_posix()
        for link in MD_LINK.findall(_read(path)):
            if link.startswith(("http://", "https://", "mailto:")):
                continue
            target = path.parent / link
            if not target.exists():
                target = root / link.lstrip("/")
            if not target.exists():
                errors.append(f"{rel}: markdown link `{link}` does not resolve")
                continue
            target_rel = _relative(root, target)
            if target_rel is not None and _ignored(root, target_rel):
                errors.append(f"{rel}: links to local-only path `{link}`")


def check_manifest(root: Path, drift: list[str], errors: list[str]) -> None:
    manifest = root / MANIFEST
    if not manifest.exists():
        drift.append(f"{MANIFEST} does not exist; normative documents are unchecksummed")
        return

    covered: set[Path] = set()
    for raw in _read(manifest).splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != MANIFEST_FIELDS:
            errors.append(f"{MANIFEST}: unparseable line `{line}`")
            continue
        expected, name = parts[0], parts[1].lstrip("*").strip()
        target = root / name.lstrip("./")
        if not target.exists():
            errors.append(f"{MANIFEST}: `{name}` is listed but does not exist")
            continue
        covered.add(target.resolve())
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != expected:
            drift.append(f"{name}: SHA-256 differs from {MANIFEST}")

    for path in _docs(root):
        rel = path.relative_to(root).as_posix()
        if rel in NON_NORMATIVE or path.resolve() in covered:
            continue
        drift.append(f"{rel}: not covered by {MANIFEST}")


def check_language(root: Path, warnings: list[str], errors: list[str]) -> None:
    for path in _docs(root):
        rel = path.relative_to(root).as_posix()
        for line_number, line in enumerate(_read(path).splitlines(), 1):
            if rel not in VOCAB_EXEMPT:
                for pattern, reason in WITHDRAWN_VOCAB:
                    if pattern.search(line) and not PROHIBITION.search(line):
                        warnings.append(f"{rel}:{line_number}: {reason}")
            for pattern, guidance in CLAIM_WORDS:
                if pattern.search(line) and not PROHIBITION.search(line):
                    warnings.append(f"{rel}:{line_number}: claim word needs evidence - {guidance}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", default=".", help="repository root (default: .)")
    parser.add_argument("--strict", action="store_true", help="fail on checksum drift")
    args = parser.parse_args()
    root = Path(args.project_dir).resolve()

    errors: list[str] = []
    warnings: list[str] = []
    drift: list[str] = []
    check_links(root, errors)
    check_manifest(root, drift, errors)
    check_language(root, warnings, errors)

    print(f"documentation check  ({root})\n")
    for line in drift:
        print(f"  DRIFT {line}")
    for line in warnings:
        print(f"  WARN  {line}")
    for line in errors:
        print(f"  ERROR {line}")
    if not (drift or warnings or errors):
        print("  no findings")
    print(f"\nerrors: {len(errors)}  drift: {len(drift)}  warnings: {len(warnings)}")
    print("\nWarnings require human review; they are not automatic violations.")

    if errors or (args.strict and drift):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Verify that two production frontend builds are byte-identical and offline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


class BuildGateError(RuntimeError):
    """The frontend output is non-deterministic or references a remote runtime asset."""


def _inventory(root: Path) -> list[dict[str, object]]:
    return [
        {
            "name": path.relative_to(root).as_posix(),
            "byte_size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    ]


def compare_builds(first: Path, second: Path) -> list[dict[str, object]]:
    first_inventory = _inventory(first)
    second_inventory = _inventory(second)
    if first_inventory != second_inventory:
        first_by_name = {str(item["name"]): item for item in first_inventory}
        second_by_name = {str(item["name"]): item for item in second_inventory}
        changed = sorted(
            name
            for name in first_by_name.keys() | second_by_name.keys()
            if first_by_name.get(name) != second_by_name.get(name)
        )
        raise BuildGateError(f"frontend builds differ: {', '.join(changed)}")
    return first_inventory


_REMOTE_RUNTIME_PATTERNS = (
    re.compile(r"(?:src|href)\s*=\s*['\"](?:https?:)?//", re.IGNORECASE),
    re.compile(r"url\(\s*['\"]?(?:https?:)?//", re.IGNORECASE),
    re.compile(r"(?:fetch|import)\(\s*['\"]https?://", re.IGNORECASE),
    re.compile(r"\bsrc\s*:\s*['\"]https?://", re.IGNORECASE),
)


def scan_runtime_assets(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".html", ".js", ".css"}:
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        if any(pattern.search(content) for pattern in _REMOTE_RUNTIME_PATTERNS):
            raise BuildGateError(f"remote runtime reference in {path.relative_to(root).as_posix()}")


def _npm_command() -> str:
    return "npm.cmd" if os.name == "nt" else "npm"


def _build(frontend: Path, destination: Path) -> None:
    completed = subprocess.run(
        [
            _npm_command(),
            "run",
            "build",
            "--",
            "--outDir",
            str(destination),
            "--emptyOutDir",
        ],
        cwd=frontend,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        raise BuildGateError(f"frontend build exited {completed.returncode}")


def check_frontend(frontend: Path, manifest_output: Path | None = None) -> list[dict[str, object]]:
    frontend = frontend.resolve()
    if not (frontend / "node_modules").is_dir():
        raise BuildGateError("frontend/node_modules is absent; run npm ci first")
    with tempfile.TemporaryDirectory(prefix="labbridge-frontend-build-") as temp:
        root = Path(temp)
        first = root / "first"
        second = root / "second"
        _build(frontend, first)
        _build(frontend, second)
        inventory = compare_builds(first, second)
        scan_runtime_assets(first)
        if manifest_output is not None:
            manifest_output.parent.mkdir(parents=True, exist_ok=True)
            manifest_output.write_text(
                json.dumps(inventory, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
        shutil.rmtree(second)
    return inventory


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frontend", type=Path, default=Path("frontend"))
    parser.add_argument("--manifest-output", type=Path)
    arguments = parser.parse_args()
    inventory = check_frontend(arguments.frontend, arguments.manifest_output)
    print(f"verified deterministic offline frontend build: {len(inventory)} files")


if __name__ == "__main__":
    main()

"""The source-intake use case owns no adapter or infrastructure details."""

from __future__ import annotations

import ast
from pathlib import Path


def test_source_intake_imports_no_framework_or_infrastructure_adapter() -> None:
    path = Path("src/labbridge/application/source_intake.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}

    forbidden = ("fastapi", "typer", "sqlalchemy", "pathlib", "boto", "labbridge.infrastructure")
    assert not any(name.startswith(forbidden) for name in imports)

import os
import subprocess
import sys
from pathlib import Path

import pytest
from scripts.reproduce_cv_passport_demo import (
    EXACT_COMMAND,
    LIMITATIONS,
    _npm_command,
    _require_empty,
    _run_cli_verifier,
)

ROOT = Path(__file__).parents[1]


def test_reproduction_limitations_preserve_status_and_domain_boundary() -> None:
    limitations = " ".join(LIMITATIONS.split())
    assert EXACT_COMMAND == "docker compose --profile demo up -d --build --wait"
    assert "Capability status is `implemented`, not `demonstrated`" in limitations
    assert "human electrochemistry domain review" in limitations
    assert "blocker or warning" in limitations
    assert "does not infer it from the CSV" in limitations
    assert "validate it as physically correct" in limitations
    assert "60-90 second" in limitations


def test_reproduction_refuses_non_empty_output(tmp_path: Path) -> None:
    (tmp_path / "retained.txt").write_text("retained", encoding="utf-8")

    with pytest.raises(FileExistsError, match="refusing to replace"):
        _require_empty(tmp_path)


def test_reproduction_uses_the_windows_npm_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scripts.reproduce_cv_passport_demo.os.name", "nt")

    assert _npm_command() == "npm.cmd"


def test_reproduction_script_resolves_the_checkout_source_from_any_working_directory(
    tmp_path: Path,
) -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "reproduce_cv_passport_demo.py"), "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--skip-browser" in completed.stdout


def test_current_module_cli_independently_verifies_a_committed_package() -> None:
    package = ROOT / "artifacts" / "experiment-passport" / "superseding-package.zip"

    verification = _run_cli_verifier(package)

    assert verification["verified"] is True
    assert verification["data_origin"] == "synthetic"
    assert verification["execution_mode"] == "replay"

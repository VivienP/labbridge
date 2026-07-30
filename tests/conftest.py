"""Shared fixtures. The doubles and payload builders live in `tests/helpers.py`."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from helpers import FIXED_NOW


@pytest.fixture
def fixed_clock() -> Callable[[], datetime]:
    """An injected clock, so assertions on `provenance.json` bytes are stable."""
    return lambda: FIXED_NOW


@pytest.fixture
def landing_root(tmp_path: Path) -> Path:
    return tmp_path / "her" / "raw"

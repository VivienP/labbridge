from __future__ import annotations

import csv
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "frontend/public/demo-fixtures/synthetic-cv-passport-demo.csv"


def test_demo_fixture_is_visibly_synthetic_and_cv_shaped() -> None:
    assert "synthetic" in FIXTURE.name
    rows = list(csv.reader(FIXTURE.read_text(encoding="utf-8").splitlines()))
    assert rows[0] == ["sample_index", "channel_a", "channel_b"]
    assert all(len(row) == len(rows[0]) for row in rows[1:])

    potential = [Decimal(row[1]) for row in rows[1:]]
    assert any(right > left for left, right in pairwise(potential))
    assert any(right < left for left, right in pairwise(potential))


def test_demo_fixture_contains_no_non_finite_value() -> None:
    rows = list(csv.reader(FIXTURE.read_text(encoding="utf-8").splitlines()))
    for row in rows[1:]:
        assert all(Decimal(value).is_finite() for value in row)

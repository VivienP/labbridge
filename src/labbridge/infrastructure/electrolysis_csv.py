"""Fail-closed CSV parsing for the bounded galvanostatic-electrolysis profile."""

from __future__ import annotations

from labbridge.domain.electrolysis import ElectrolysisImportProfile

from .cv_csv import ParsedCV, parse_mapped_csv


def parse_electrolysis_csv(data: bytes, profile: ElectrolysisImportProfile) -> ParsedCV:
    """Parse exact bytes using only declared electrolysis syntax, roles, and units."""
    return parse_mapped_csv(data, profile)


__all__ = ["parse_electrolysis_csv"]

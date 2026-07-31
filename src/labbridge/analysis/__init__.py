"""Derived metrics.

Versioned separately from ingestion (`docs/SPEC.md` §3.6): changing an analysis produces new derived
values without changing the identity of the observation it read. Nothing here writes to a database
or reads a file — an analysis takes bytes and returns a result, so it is testable without either.
"""

from __future__ import annotations

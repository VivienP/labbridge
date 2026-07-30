"""PostgreSQL and object-storage adapters.

Everything framework-specific lives here so `labbridge.domain` stays free of it
(`AI_CONTRACT.md` §5).
"""

from __future__ import annotations

from .config import DatabaseSettings, ObjectStoreSettings
from .tables import metadata

__all__ = ["DatabaseSettings", "ObjectStoreSettings", "metadata"]

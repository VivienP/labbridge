"""The HTTP surface.

Only the endpoints Slice 1 needs exist. `docs/SPEC.md` §11.1 lists more; a stub returning 501 for
one with no code behind it would be a claim without evidence (`AI_CONTRACT.md` invariant 10).
"""

from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]

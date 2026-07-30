"""Environment adapters.

One adapter per environment, implementing the protocol in `docs/SPEC.md` §9. An adapter turns a
typed candidate into bytes plus a lineage root, or into a structured unavailable outcome. It never
persists anything and never returns a database object.
"""

from __future__ import annotations

"""The domain layer: scientific and operational meaning, with no infrastructure.

`AI_CONTRACT.md` §5 bars this layer from depending on filesystem paths, frameworks, database
sessions, or object storage. Nothing here imports SQLAlchemy, boto3, FastAPI, or `pathlib` — that is
what makes these types testable without a container, and what keeps a persistence decision from
leaking into scientific meaning.

Pydantic is present because validation *is* domain logic here: an inadmissible origin/mode pair and
a provenance resolving to two roots must be unconstructable, not merely discouraged.
"""

from __future__ import annotations

"""The durable runtime: jobs, leases, the worker, and the event log.

Everything here needs a real PostgreSQL to mean anything. `AI_CONTRACT.md` §9 is explicit that a
test mocking away the database transaction does not prove the corresponding operational guarantee,
so this package's claims rest on the integration suite rather than on unit tests.
"""

from __future__ import annotations

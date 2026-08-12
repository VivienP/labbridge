---
name: offline-tests
description: Use when writing, changing, or reviewing tests. The default suite must run fully offline and fast — no network, no PostgreSQL, no MinIO, no fetched archive. Heavier layers go behind explicit markers, and a durability claim still needs the real dependency.
paths: tests/**
---

# Offline tests

## The rule

`pytest` with no marker filter must pass **offline, in seconds**: no network call, no PostgreSQL, no
MinIO, no Docker, no fetched HER archive.

Anything heavier carries a marker:

| Marker | Requires | Typical use |
|---|---|---|
| `integration` | PostgreSQL and/or MinIO, usually via Docker Compose | transactions, constraints, leases, object staging, replay against persisted events |
| `data` | the fetched HER archive on disk | real-schema parsing, inventory checks |
| `slow` | a long run | the seeded fault campaign, load tests |

The default gate is:

```bash
pytest -q -m "not slow and not data and not integration"
```

Markers are registered in `pyproject.toml` under `[tool.pytest.ini_options] markers`. `--strict-markers`
is on, so an unregistered marker fails rather than silently doing nothing.

## Why

The offline suite is what keeps the invariant guards runnable on every edit. A "unit" test that quietly
starts a container or downloads an archive turns a two-second check into a two-minute one, couples
correctness to network availability, and gets skipped under pressure — which is exactly when the guards
matter.

## The counter-rule that matters more

Fast is not a licence to mock away the thing under test.

`AI_CONTRACT.md` §9: *"A test that mocks away the database transaction, object store, or process
boundary does not prove the corresponding operational guarantee."*

So:

- **Offline unit tests** prove domain logic: state transitions, budget arithmetic, canonical hashing,
  unit conversion, failure classification, candidate validation, event ordering rules.
- **Integration tests** prove durability: uniqueness constraints, transaction atomicity, row locking,
  lease reclaim, object staging and commit, replay against persisted events.
- **Process-boundary tests** prove crash safety: real termination, real restart, real state read back.

If a guarantee needs a real dependency, the test gets the real dependency and a marker. It does not get
a mock and a fast label. `AI_CONTRACT.md` §8 prefers database and object-store integration tests over
mocks when proving durability or transaction guarantees.

## Writing them

```python
def test_attempt_outcome_is_created_for_timeout() -> None:
    """Pure domain: no I/O, no clock, no randomness."""


@pytest.mark.integration      # real PostgreSQL: the unique constraint is the thing under test
def test_duplicate_delivery_yields_one_accepted_outcome() -> None: ...


@pytest.mark.integration      # real MinIO: staging and checksum verification
def test_pending_object_is_not_marked_committed_before_checksum() -> None: ...


@pytest.mark.data             # requires scripts/fetch_her.py to have run
def test_inventory_matches_actual_archive_columns() -> None: ...


@pytest.mark.slow             # the seeded fault campaign
def test_100_campaigns_with_injected_termination() -> None: ...
```

Rules for the offline tier:

- no `datetime.now()` and no unseeded randomness in the test or the code under test — inject a clock and
  a seed;
- no reliance on `dict` or `set` iteration order;
- no sleeping for a production backoff interval; make the backoff and lease clocks configurable;
- fixtures are independently generated and schema-compatible, never derived from the HER archive — ADR-009
  permits copying archive values, but a fixture that does makes the suite depend on a download
  (see `her-source-discipline`).

## Detecting a violation

These greps produce candidates, not verdicts. Inspect each hit and check for a marker.

```bash
rg -n "httpx\.|requests\.|urlopen|boto3|create_engine|psycopg|Minio|docker" tests/
rg -n "@pytest\.mark\." tests/ | rg -v "integration|data|slow"
rg -n "time\.sleep\(" tests/
```

A network, database, object-store, or archive dependency inside an unmarked test is `BLOCKING` in
review.

## The other failure mode

A test that runs offline because the durability it claims to test was mocked away is worse than a slow
test. When reviewing, ask of every fast test attached to a durability claim: *what would still pass if
the constraint, the transaction, or the process boundary were removed?* If the answer is "this test",
the test does not prove the claim.

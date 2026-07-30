"""The in-memory store, offline.

These prove the contract's *logic*: immutability, typed failures, read-back. They prove nothing
about durability across a process or a network — that claim belongs to
`tests/integration/test_minio.py` running against real MinIO (`AI_CONTRACT.md` §9).
"""

from __future__ import annotations

import pytest

from labbridge.infrastructure.objectstore import (
    InMemoryObjectStore,
    ObjectAlreadyExistsError,
    ObjectNotFoundError,
    digest,
)

PAYLOAD = b"Potential vs. RHE [V],Current density [A/cm^2]\n-0.1,-1.5\n"


def test_a_stored_object_reports_the_digest_of_what_was_stored() -> None:
    store = InMemoryObjectStore()

    stored = store.put_and_verify("obs/1.csv", PAYLOAD, media_type="text/csv")

    assert stored.sha256 == digest(PAYLOAD)
    assert stored.byte_size == len(PAYLOAD)
    assert stored.uri == "s3://labbridge/obs/1.csv"


def test_the_bytes_come_back_unchanged() -> None:
    store = InMemoryObjectStore()
    store.put_and_verify("obs/1.csv", PAYLOAD, media_type="text/csv")

    assert store.get("obs/1.csv") == PAYLOAD


def test_restoring_identical_bytes_is_an_idempotent_retry() -> None:
    """A retried upload after an ambiguous failure must not be an error."""
    store = InMemoryObjectStore()

    first = store.put_and_verify("obs/1.csv", PAYLOAD, media_type="text/csv")
    second = store.put_and_verify("obs/1.csv", PAYLOAD, media_type="text/csv")

    assert first == second


def test_storing_different_bytes_under_one_key_is_refused() -> None:
    """Content addressing makes this a defect, not an overwrite: the key names the content."""
    store = InMemoryObjectStore()
    store.put_and_verify("obs/1.csv", PAYLOAD, media_type="text/csv")

    with pytest.raises(ObjectAlreadyExistsError):
        store.put_and_verify("obs/1.csv", PAYLOAD + b"tampered\n", media_type="text/csv")

    assert store.get("obs/1.csv") == PAYLOAD


def test_a_missing_object_raises_a_typed_error_rather_than_returning_none() -> None:
    store = InMemoryObjectStore()

    with pytest.raises(ObjectNotFoundError) as caught:
        store.get("obs/absent.csv")

    assert caught.value.code == "object_not_found"
    assert not store.exists("obs/absent.csv")

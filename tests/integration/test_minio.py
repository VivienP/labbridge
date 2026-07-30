"""Object storage against real MinIO.

`AI_CONTRACT.md` §9: a test that mocks away the object store does not prove the storage guarantee.
The in-memory store's tests establish the contract's logic; these establish that bytes survive a
round trip through a real S3-compatible service, which is the only thing that lets a database row
say `committed`.
"""

from __future__ import annotations

import uuid

import pytest

from labbridge.infrastructure.objectstore import (
    ObjectAlreadyExistsError,
    ObjectNotFoundError,
    S3ObjectStore,
    digest,
)

pytestmark = pytest.mark.integration

PAYLOAD = b"Potential vs. RHE [V],Current density [A/cm^2]\r\n-0.1,-1.5\r\n"


def _key() -> str:
    return f"tests/{uuid.uuid4().hex}.csv"


def test_bytes_survive_a_round_trip_through_minio(object_store: S3ObjectStore) -> None:
    key = _key()

    stored = object_store.put_and_verify(key, PAYLOAD, media_type="text/csv")

    assert stored.sha256 == digest(PAYLOAD)
    assert object_store.get(key) == PAYLOAD


def test_crlf_payloads_are_not_translated_in_transit(object_store: S3ObjectStore) -> None:
    """The archive mixes CR, CRLF and LF. A store that normalised them would corrupt observations
    while every checksum still appeared to match at the sending end."""
    key = _key()

    object_store.put_and_verify(key, PAYLOAD, media_type="text/csv")

    assert b"\r\n" in object_store.get(key)


def test_a_repeated_upload_of_identical_bytes_is_idempotent(object_store: S3ObjectStore) -> None:
    """After an ambiguous network failure a worker retries; that must not be an error."""
    key = _key()

    first = object_store.put_and_verify(key, PAYLOAD, media_type="text/csv")
    second = object_store.put_and_verify(key, PAYLOAD, media_type="text/csv")

    assert first == second


def test_different_bytes_under_an_existing_key_are_refused(object_store: S3ObjectStore) -> None:
    key = _key()
    object_store.put_and_verify(key, PAYLOAD, media_type="text/csv")

    with pytest.raises(ObjectAlreadyExistsError):
        object_store.put_and_verify(key, PAYLOAD + b"tampered\r\n", media_type="text/csv")

    assert object_store.get(key) == PAYLOAD


def test_a_missing_key_raises_a_typed_error(object_store: S3ObjectStore) -> None:
    with pytest.raises(ObjectNotFoundError):
        object_store.get(_key())


def test_exists_reports_absence_without_raising(object_store: S3ObjectStore) -> None:
    key = _key()

    assert not object_store.exists(key)
    object_store.put_and_verify(key, PAYLOAD, media_type="text/csv")
    assert object_store.exists(key)


def test_an_empty_payload_is_storable_and_distinguishable_from_absence(
    object_store: S3ObjectStore,
) -> None:
    """A zero-byte observation is a real outcome; it must not read as a missing object."""
    key = _key()

    stored = object_store.put_and_verify(key, b"", media_type="text/csv")

    assert stored.byte_size == 0
    assert object_store.exists(key)
    assert object_store.get(key) == b""

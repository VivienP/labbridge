"""S3-compatible object storage with an explicit pending/committed lifecycle.

`docs/SPEC.md` §4.2: objects progress through `pending`, `committed`, and `orphaned`, and *"a
database record MUST NOT declare an artifact committed until the expected object exists and its
checksum has been verified"*.

That sentence is the whole design. `put_and_verify` uploads, **reads the object back**, and compares
the digest of the returned bytes with the digest of what was sent. Trusting the upload response
would verify that the client thinks it succeeded, not that the bytes are retrievable and intact —
and the failure this guards against is exactly the one where those two differ.

One client serves MinIO locally and S3 in production (`AI_CONTRACT.md` §4), so the storage boundary
does not change shape between environments.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Protocol

from botocore.exceptions import ClientError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mypy_boto3_s3.client import S3Client


class ObjectStoreError(Exception):
    """Base class. Every failure here is typed; none is signalled by a None return."""

    code: ClassVar[str] = "object_store_error"


class ObjectIntegrityError(ObjectStoreError):
    """What came back is not what went in. The object is left for inspection, never overwritten."""

    code: ClassVar[str] = "object_integrity_mismatch"

    def __init__(self, key: str, expected: str, actual: str) -> None:
        self.key = key
        self.expected = expected
        self.actual = actual
        super().__init__(f"object `{key}` read back as sha256:{actual}, expected sha256:{expected}")


class ObjectNotFoundError(ObjectStoreError):
    code: ClassVar[str] = "object_not_found"

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"object `{key}` does not exist")


class ObjectAlreadyExistsError(ObjectStoreError):
    """A committed object is immutable. Re-storing different bytes under one key is a defect."""

    code: ClassVar[str] = "object_already_exists"

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"object `{key}` already exists with different content")


@dataclass(frozen=True)
class StoredObject:
    """An object whose bytes have been written and read back intact."""

    bucket: str
    key: str
    uri: str
    byte_size: int
    sha256: str


class ObjectStore(Protocol):
    """The boundary the application depends on. Neither implementation leaks a client object."""

    bucket: str

    def put_and_verify(self, key: str, data: bytes, *, media_type: str) -> StoredObject: ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _uri(bucket: str, key: str) -> str:
    return f"s3://{bucket}/{key}"


class InMemoryObjectStore:
    """For the offline suite.

    It is a real implementation of the protocol, not a mock: it stores bytes, reads them back, and
    enforces the same immutability rule. What it does **not** prove is durability across a process
    or a network. `AI_CONTRACT.md` §9 is explicit that a test mocking away the object store does
    not establish the storage guarantee, so that claim rests on the MinIO integration tests.
    """

    def __init__(self, bucket: str = "labbridge") -> None:
        self.bucket = bucket
        self._objects: dict[str, bytes] = {}

    def put_and_verify(self, key: str, data: bytes, *, media_type: str) -> StoredObject:
        del media_type  # recorded by the database row, not by this store
        expected = digest(data)
        existing = self._objects.get(key)
        if existing is not None and digest(existing) != expected:
            raise ObjectAlreadyExistsError(key)
        self._objects[key] = data
        actual = digest(self._objects[key])
        if actual != expected:  # pragma: no cover - unreachable in memory, kept for parity
            raise ObjectIntegrityError(key, expected, actual)
        return StoredObject(
            bucket=self.bucket,
            key=key,
            uri=_uri(self.bucket, key),
            byte_size=len(data),
            sha256=expected,
        )

    def get(self, key: str) -> bytes:
        try:
            return self._objects[key]
        except KeyError as error:
            raise ObjectNotFoundError(key) from error

    def exists(self, key: str) -> bool:
        return key in self._objects


class S3ObjectStore:
    """MinIO locally, S3 in production. The client is injected so tests construct it explicitly."""

    def __init__(self, client: S3Client, bucket: str) -> None:
        self._client = client
        self.bucket = bucket

    def ensure_bucket(self) -> None:
        """Create the bucket when absent. Idempotent, and safe to call on every start."""
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except ClientError:
            self._client.create_bucket(Bucket=self.bucket)

    def put_and_verify(self, key: str, data: bytes, *, media_type: str) -> StoredObject:
        expected = digest(data)
        if self.exists(key):
            # Immutability: the same bytes under the same key is a no-op retry, different bytes is
            # a defect. Distinguishing them is what makes an upload retry safe.
            if digest(self.get(key)) != expected:
                raise ObjectAlreadyExistsError(key)
            return StoredObject(
                bucket=self.bucket,
                key=key,
                uri=_uri(self.bucket, key),
                byte_size=len(data),
                sha256=expected,
            )

        self._client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=media_type)
        # Read back rather than trusting the response: this is the only check that proves the bytes
        # are retrievable, which is what "committed" is going to mean in the database.
        actual = digest(self.get(key))
        if actual != expected:
            raise ObjectIntegrityError(key, expected, actual)
        return StoredObject(
            bucket=self.bucket,
            key=key,
            uri=_uri(self.bucket, key),
            byte_size=len(data),
            sha256=expected,
        )

    def get(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            raise ObjectNotFoundError(key) from error
        body: bytes = response["Body"].read()
        return body

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
        except ClientError:
            return False
        return True

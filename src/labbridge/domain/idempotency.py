"""Idempotency identities: what makes two requests, or two deliveries, the same one.

Three identities live here, and they are deliberately different things:

* **the client's idempotency key** — an opaque token the caller chooses. It says "this is my retry
  of that submission". It carries no meaning the runtime can verify;
* **the request fingerprint** — a digest of the *canonical* request, computed through
  `labbridge.domain.canonical`. It is what lets the runtime tell a genuine retry from a key reused
  with a different body. Without it, a key is only a promise;
* **the instruction key** — the identity of a unit of durable work. It is derived from the work
  item and the command version, never from a client token, so a redelivery is recognisable as the
  same instruction even when the delivery that carries it is new.

Nothing here touches a database, a clock, or a framework. The uniqueness these identities imply is
enforced by PostgreSQL constraints (`AI_CONTRACT.md` invariant 5); this module only says what is
supposed to be equal to what.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import ClassVar, Final

from labbridge.domain.canonical import canonical_bytes

#: The width of `idempotency_keys.idempotency_key` and `jobs.idempotency_key`. Enforced here so an
#: oversized client token becomes a typed rejection rather than a driver error at INSERT time.
MAX_IDEMPOTENCY_KEY_LENGTH: Final = 255

#: Prefix for the durable-work identity, so an instruction key cannot be confused with a client
#: token that happens to look like a UUID.
INSTRUCTION_SCOPE: Final = "execute_work_item"


class IdempotencyKeyError(ValueError):
    """A client-supplied key the runtime will not store."""

    code: ClassVar[str] = "idempotency_key_invalid"


class MissingIdempotencyKeyError(IdempotencyKeyError):
    """No key, or a key that is only whitespace."""

    code: ClassVar[str] = "idempotency_key_required"


class OversizedIdempotencyKeyError(IdempotencyKeyError):
    """Longer than the column that has to hold it.

    Refused here rather than at INSERT: a key the database cannot store is a client error with a
    stable code, not a 500 from a driver.
    """

    code: ClassVar[str] = "idempotency_key_too_long"

    def __init__(self, length: int) -> None:
        self.length = length
        super().__init__(
            f"idempotency key is {length} characters; the maximum is {MAX_IDEMPOTENCY_KEY_LENGTH}"
        )


class IdempotencyConflictError(ValueError):
    """The same key was offered with a different canonical request.

    Returning the first result would silently discard this request; creating a second aggregate
    would break the key's promise. Neither is acceptable, so the caller is told (F-001).
    """

    code: ClassVar[str] = "idempotency_key_reused"

    def __init__(self, key: str, *, stored: str, offered: str) -> None:
        self.key = key
        self.stored = stored
        self.offered = offered
        super().__init__(
            f"idempotency key {key!r} was recorded against request fingerprint {stored}, "
            f"and this request fingerprints as {offered}"
        )


class InstructionConflictError(ValueError):
    """One instruction key already names a different unit of durable work.

    Only reachable when a caller builds an instruction key itself instead of deriving it, which is
    a programming error rather than a race — so it is typed and raised rather than absorbed.
    """

    code: ClassVar[str] = "instruction_key_reused"

    def __init__(self, key: str, *, stored: uuid.UUID, offered: uuid.UUID) -> None:
        self.key = key
        self.stored = stored
        self.offered = offered
        super().__init__(f"instruction key {key!r} already names work item {stored}, not {offered}")


def normalise_idempotency_key(raw: str | None) -> str:
    """Return the storable form of a client key, or raise a typed error.

    Surrounding whitespace is stripped because HTTP header values carry optional whitespace that no
    client intends as part of the token; a key that is *only* whitespace is absent, not empty.
    """
    candidate = (raw or "").strip()
    if not candidate:
        raise MissingIdempotencyKeyError(
            "every mutating request requires an Idempotency-Key header"
        )
    if len(candidate) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise OversizedIdempotencyKeyError(len(candidate))
    return candidate


def request_fingerprint(payload: object) -> str:
    """Digest the canonical form of a request.

    Canonical, so two byte-different encodings of the same request — reordered mapping keys, other
    whitespace — fingerprint alike, and a change to any recorded value does not. The rules the
    digest covers are the ones in `labbridge.domain.canonical`; read them before assuming what a
    matching fingerprint proves.
    """
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def work_item_instruction_key(*, work_item_id: uuid.UUID, command_version: str) -> str:
    """The identity of one unit of durable work.

    Derived from the work item and the command version rather than from the client token that
    happened to create it. That is what makes a redelivery recognisable: the same work item under
    the same command version is the same instruction, whatever delivery carries it, and enqueuing
    it again is a no-op decided by the uniqueness constraint rather than by a prior read.

    The command version is part of the identity on purpose. A new command version is a different
    instruction — the work item is the same, but what the worker is being asked to do is not.
    """
    return f"{INSTRUCTION_SCOPE}:{work_item_id}:{command_version}"


def check_request_fingerprint(*, key: str, stored: str, offered: str) -> None:
    """Raise when a stored key is being reused for a different canonical request."""
    if stored != offered:
        raise IdempotencyConflictError(key, stored=stored, offered=offered)


__all__ = [
    "INSTRUCTION_SCOPE",
    "MAX_IDEMPOTENCY_KEY_LENGTH",
    "IdempotencyConflictError",
    "IdempotencyKeyError",
    "InstructionConflictError",
    "MissingIdempotencyKeyError",
    "OversizedIdempotencyKeyError",
    "check_request_fingerprint",
    "normalise_idempotency_key",
    "request_fingerprint",
    "work_item_instruction_key",
]

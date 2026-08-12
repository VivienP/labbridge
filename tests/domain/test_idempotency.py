"""The identities idempotency is built on, offline.

The database enforces the uniqueness; these tests check the thing being made unique. A fingerprint
that moved when the encoding moved would report every retry as a conflict, and one that did not move
when a value moved would accept a different request as a replay of the first. Neither failure is
visible in an integration test that always sends the same bytes.
"""

from __future__ import annotations

import uuid

import pytest

from labbridge.domain.idempotency import (
    MAX_IDEMPOTENCY_KEY_LENGTH,
    IdempotencyConflictError,
    InstructionConflictError,
    MissingIdempotencyKeyError,
    OversizedIdempotencyKeyError,
    check_request_fingerprint,
    normalise_idempotency_key,
    request_fingerprint,
    work_item_instruction_key,
)

DIGEST_LENGTH = 64


def _request(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "a campaign",
        "environment_id": "her_auirrh",
        "data_origin": "synthetic",
        "execution_mode": "replay",
        "candidates": [{"library_id": "Au-rich", "measurement_area_id": "1"}],
    }
    payload.update(overrides)
    return payload


def test_a_fingerprint_is_a_full_sha256_digest() -> None:
    """Full width, not truncated: this one decides whether two requests are the same, and a
    shortened digest trades that decision for readability nobody needs here."""
    assert len(request_fingerprint(_request())) == DIGEST_LENGTH


def test_the_same_request_fingerprints_the_same_way_twice() -> None:
    assert request_fingerprint(_request()) == request_fingerprint(_request())


def test_reordering_the_mapping_does_not_change_the_fingerprint() -> None:
    """A retry serialised by a different client library is still the same request."""
    forwards = _request()
    backwards = dict(reversed(list(forwards.items())))

    assert request_fingerprint(backwards) == request_fingerprint(forwards)


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "a different campaign"),
        ("environment_id", "another_environment"),
        ("data_origin", "observed"),
        ("execution_mode", "simulation"),
        ("candidates", [{"library_id": "Ir-rich", "measurement_area_id": "1"}]),
    ],
)
def test_changing_a_meaningful_value_changes_the_fingerprint(field: str, value: object) -> None:
    """Every field the runtime acts on has to be inside the digest. One left out would let a key
    replay a request that asked for something else."""
    assert request_fingerprint(_request(**{field: value})) != request_fingerprint(_request())


def test_adding_a_candidate_changes_the_fingerprint() -> None:
    extended = _request()
    extended["candidates"] = [
        *list(extended["candidates"]),  # type: ignore[arg-type]
        {"library_id": "Au-rich", "measurement_area_id": "2"},
    ]

    assert request_fingerprint(extended) != request_fingerprint(_request())


def test_an_explicit_null_is_not_the_same_as_an_absent_field() -> None:
    """`canonical` keeps explicit nulls, and that distinction has to survive into the fingerprint:
    a field recorded as unknown is not a field never sent."""
    assert request_fingerprint(_request(note=None)) != request_fingerprint(_request())


def test_an_instruction_key_is_stable_for_one_work_item_and_command_version() -> None:
    work_item_id = uuid.uuid4()

    first = work_item_instruction_key(work_item_id=work_item_id, command_version="1")
    second = work_item_instruction_key(work_item_id=work_item_id, command_version="1")

    assert first == second
    assert str(work_item_id) in first


def test_a_different_work_item_or_command_version_is_a_different_instruction() -> None:
    """The command version is part of the identity: the same work item under a new command is a new
    instruction, not a redelivery of the old one."""
    work_item_id = uuid.uuid4()

    baseline = work_item_instruction_key(work_item_id=work_item_id, command_version="1")

    assert baseline != work_item_instruction_key(work_item_id=uuid.uuid4(), command_version="1")
    assert baseline != work_item_instruction_key(work_item_id=work_item_id, command_version="2")


def test_an_instruction_key_fits_the_column_that_stores_it() -> None:
    key = work_item_instruction_key(work_item_id=uuid.uuid4(), command_version="1")

    assert len(key) <= MAX_IDEMPOTENCY_KEY_LENGTH


@pytest.mark.parametrize("raw", [None, "", "   ", "\t"])
def test_a_missing_or_blank_key_is_a_typed_refusal(raw: str | None) -> None:
    with pytest.raises(MissingIdempotencyKeyError) as raised:
        normalise_idempotency_key(raw)

    assert raised.value.code == "idempotency_key_required"


def test_a_key_longer_than_the_column_is_a_typed_refusal() -> None:
    with pytest.raises(OversizedIdempotencyKeyError) as raised:
        normalise_idempotency_key("k" * (MAX_IDEMPOTENCY_KEY_LENGTH + 1))

    assert raised.value.code == "idempotency_key_too_long"


def test_a_key_at_the_column_width_is_accepted() -> None:
    """The boundary belongs in a test: off by one here refuses keys the database would store."""
    exact = "k" * MAX_IDEMPOTENCY_KEY_LENGTH

    assert normalise_idempotency_key(exact) == exact


def test_surrounding_whitespace_is_stripped_rather_than_stored() -> None:
    """Header values carry optional whitespace no client intends as part of the token; storing it
    would make a retry a different key."""
    assert normalise_idempotency_key("  key:1  ") == "key:1"


def test_a_matching_fingerprint_is_not_a_conflict() -> None:
    check_request_fingerprint(key="key:1", stored="a" * 64, offered="a" * 64)


def test_a_differing_fingerprint_raises_a_stable_conflict_code() -> None:
    with pytest.raises(IdempotencyConflictError) as raised:
        check_request_fingerprint(key="key:1", stored="a" * 64, offered="b" * 64)

    assert raised.value.code == "idempotency_key_reused"
    assert raised.value.key == "key:1"


def test_the_instruction_conflict_code_is_stable() -> None:
    """Callers branch on `code`, so it is part of the contract and changing it is a breaking
    change — which is why it is asserted rather than left to the exception's name."""
    conflict = InstructionConflictError(
        "execute_work_item:x:1", stored=uuid.uuid4(), offered=uuid.uuid4()
    )

    assert conflict.code == "instruction_key_reused"

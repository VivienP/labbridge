"""Content addressing: what must change an identity, and what must not.

`AI_CONTRACT.md` invariant 7 is a list of things that must never determine an identity. These tests
assert the properties that follow, because a hash whose coverage is only documented is a hash nobody
can rely on.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from labbridge.domain.canonical import (
    CanonicalisationError,
    canonical_bytes,
    canonicalise,
    content_id,
)


def test_reordering_a_mapping_does_not_change_the_identity() -> None:
    a = {"alpha": 1, "beta": 2, "gamma": 3}
    b = {"gamma": 3, "alpha": 1, "beta": 2}

    assert content_id("x", a) == content_id("x", b)


def test_nesting_order_does_not_change_the_identity() -> None:
    a = {"outer": {"one": [1, 2], "two": {"z": 1, "a": 2}}}
    b = {"outer": {"two": {"a": 2, "z": 1}, "one": [1, 2]}}

    assert content_id("x", a) == content_id("x", b)


def test_sequence_order_does_change_the_identity() -> None:
    """Axes are ordered. A list is not a set, and collapsing the two would lose the axis order."""
    assert content_id("x", [1, 2]) != content_id("x", [2, 1])


def test_the_kind_prefix_separates_two_things_hashing_the_same_payload() -> None:
    assert content_id("obs", {"a": 1}) != content_id("cand", {"a": 1})
    assert content_id("obs", {"a": 1}).startswith("obs:")


def test_exponent_notation_and_plain_notation_agree() -> None:
    """`1E+2` and `100` are the same number recorded two ways, so they must hash alike."""
    assert canonicalise(Decimal("1E+2")) == canonicalise(Decimal("100"))


def test_trailing_zeros_change_the_identity() -> None:
    """Deliberate: 1.10 is recorded to two decimals and 1.1 to one. Precision is information."""
    assert content_id("x", Decimal("1.10")) != content_id("x", Decimal("1.1"))


def test_a_float_is_refused_rather_than_rounded() -> None:
    with pytest.raises(CanonicalisationError, match="not canonically representable"):
        canonical_bytes({"value": 0.1})


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_decimals_are_refused(literal: str) -> None:
    with pytest.raises(CanonicalisationError, match="non-finite"):
        canonical_bytes(Decimal(literal))


def test_an_explicit_null_differs_from_an_absent_key() -> None:
    assert content_id("x", {"a": 1, "b": None}) != content_id("x", {"a": 1})


def test_equivalent_unicode_spellings_produce_one_identity() -> None:
    """NFC normalisation: a decomposed and a composed `é` are the same text."""
    assert content_id("x", "café") == content_id("x", "café")


def test_a_datetime_is_canonicalised_by_its_isoformat() -> None:
    moment = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)

    assert canonicalise(moment) == moment.isoformat()


def test_raw_bytes_are_refused_rather_than_encoded_as_a_list_of_integers() -> None:
    """Falling through to the sequence branch gave `b"ab"` the identity of `[97, 98]`."""
    with pytest.raises(CanonicalisationError, match="hash it and pass the digest"):
        canonical_bytes(b"ab")


@pytest.mark.parametrize("value", [bytearray(b"ab"), memoryview(b"ab")])
def test_the_other_buffer_types_are_refused_too(value: object) -> None:
    """A `memoryview` is a mutable location, which invariant 7 names explicitly."""
    with pytest.raises(CanonicalisationError):
        canonical_bytes(value)


def test_a_non_string_mapping_key_is_refused() -> None:
    """`str(key)` collapsed `{1: "a"}` and `{"1": "a"}` into one identity."""
    with pytest.raises(CanonicalisationError, match="not str"):
        canonical_bytes({1: "a"})


def test_a_mixed_key_mapping_is_refused_with_a_typed_error() -> None:
    """It has no canonical order at all, and a bare TypeError would not say why."""
    with pytest.raises(CanonicalisationError, match="not str"):
        canonical_bytes({1: "a", "b": 2})


def test_a_set_with_unorderable_members_is_refused_with_a_typed_error() -> None:
    with pytest.raises(CanonicalisationError, match="cannot be ordered"):
        canonical_bytes({1, "a"})


def test_an_unknown_type_is_refused_rather_than_repr_ed() -> None:
    """A `repr` fallback is exactly the identity source invariant 7 forbids."""

    class Opaque:
        pass

    with pytest.raises(CanonicalisationError, match="no canonical form"):
        canonical_bytes(Opaque())


def test_canonical_bytes_are_stable_across_calls() -> None:
    payload = {"b": Decimal("2.50"), "a": ["x", None, 3]}

    assert canonical_bytes(payload) == canonical_bytes(payload)
    assert canonical_bytes(payload) == b'{"a":["x",null,3],"b":"2.50"}'

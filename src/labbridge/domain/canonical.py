"""Canonical serialisation and content addressing.

`AI_CONTRACT.md` invariant 7 forbids deriving an identity from a `repr`, a `str(dict)`, a
`json.dumps` without `sort_keys`, a platform-dependent float format, or a mutable location. This
module is the one place identity is computed, so those rules are checkable in a single file.

The rules, stated because a hash is worthless if nobody can say what it covers:

* **mapping order is not significant** — keys are sorted, so reordering a mapping cannot change an
  identity;
* **decimals** are written with `format(d, "f")`: plain notation, no exponent, trailing zeros kept.
  `1E+2` and `100` therefore hash alike, while `1.1` and `1.10` do not. That is deliberate — a value
  recorded to two decimals is a different measurement from one recorded to one, and significant
  figures are scientific information, not formatting;
* **floats are refused.** A binary float has no canonical decimal form across platforms. Scientific
  values are `Decimal`;
* **NaN and infinities are refused.** They have no meaningful identity and must fail validation
  rather than silently acquire one;
* **explicit nulls are kept.** A field recorded as absent differs from a field never recorded;
* **strings are NFC-normalised**, so two spellings of the same text do not produce two identities;
* the payload is UTF-8, with `:` and `,` separators and no insignificant whitespace.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Final

from pydantic import BaseModel

#: Separates the kind of a thing from its digest, so an identity cannot be read as a bare hash and
#: two kinds hashing the same payload never collide.
ID_SEPARATOR: Final = ":"
_DIGEST_CHARS: Final = 32


class CanonicalisationError(ValueError):
    """A value that has no canonical form. Raised rather than guessed at."""


def _decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise CanonicalisationError(f"non-finite decimal has no canonical form: {value!r}")
    return format(value, "f")


def canonicalise(value: object) -> object:  # noqa: PLR0911
    """Reduce a value to JSON-encodable primitives under the rules in the module docstring.

    One return per handled type. A dispatch table would satisfy the branch limit while hiding which
    types are handled, and which are deliberately refused, behind a lookup.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return _decimal(value)
    if isinstance(value, float):
        raise CanonicalisationError(
            f"float {value!r} is not canonically representable; use Decimal for scientific values"
        )
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Enum):
        return canonicalise(value.value)
    if isinstance(value, BaseModel):
        return canonicalise(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {str(key): canonicalise(item) for key, item in sorted(value.items())}
    if isinstance(value, Sequence | set | frozenset):
        items = sorted(value) if isinstance(value, set | frozenset) else value
        return [canonicalise(item) for item in items]
    raise CanonicalisationError(f"no canonical form defined for {type(value).__name__}")


def canonical_bytes(value: object) -> bytes:
    """The exact bytes an identity is computed over. Inspect these when a hash surprises you."""
    return json.dumps(
        canonicalise(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def content_id(kind: str, value: object) -> str:
    """A stable `<kind>:<digest>` identity, truncated to keep records readable.

    Truncation is a display choice with a collision cost. 128 bits is far beyond the number of
    records this system will hold; widen `_DIGEST_CHARS` rather than reusing a shortened digest for
    anything security-bearing.
    """
    digest = hashlib.sha256(canonical_bytes(value)).hexdigest()[:_DIGEST_CHARS]
    return f"{kind}{ID_SEPARATOR}{digest}"

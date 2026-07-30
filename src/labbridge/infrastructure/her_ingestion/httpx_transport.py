"""The real network transport.

Deliberately free of policy: no retry, no authentication, no backoff. Every decision that could be
wrong lives in the tested pure modules instead, so there is nothing here for an untested branch to
hide in.

Its one responsibility beyond I/O is translating httpx exceptions into `SourceUnavailableError`, so
the acquisition layer speaks a single typed vocabulary and an ordinary 404 reaches the operator as a
classified failure rather than a traceback.

No offline test covers this class: mocking httpx would prove something about the mock, and a real
call is forbidden in the offline suite (skill `offline-tests`). It is `implemented`, never
`demonstrated`. Its behaviour was observed once against the live record during Gate 0 — a successful
read and a 404 — but a manual observation is not a committed reproducible artifact.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final

import httpx

from .errors import SourceUnavailableError

DEFAULT_TIMEOUT_SECONDS: Final = 30.0
STREAM_CHUNK_BYTES: Final = 1 << 20


class HttpxTransport:
    """A `ZenodoTransport` backed by httpx."""

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout

    def get_json(self, url: str) -> Mapping[str, object]:
        try:
            response = httpx.get(url, timeout=self._timeout, follow_redirects=True)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise SourceUnavailableError(
                url=url, detail=exc.response.reason_phrase, status=exc.response.status_code
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise SourceUnavailableError(url=url, detail=str(exc)) from exc
        if not isinstance(payload, Mapping):
            raise SourceUnavailableError(
                url=url, detail=f"expected a JSON object, got {type(payload).__name__}"
            )
        return payload

    def stream_to(self, url: str, sink: Callable[[bytes], None]) -> None:
        try:
            with httpx.stream("GET", url, timeout=self._timeout, follow_redirects=True) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes(STREAM_CHUNK_BYTES):
                    sink(chunk)
        except httpx.HTTPStatusError as exc:
            raise SourceUnavailableError(
                url=url, detail=exc.response.reason_phrase, status=exc.response.status_code
            ) from exc
        except httpx.HTTPError as exc:
            raise SourceUnavailableError(url=url, detail=str(exc)) from exc

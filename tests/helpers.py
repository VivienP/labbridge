"""Offline test doubles and payload builders for the HER acquisition layer.

Every payload here is obviously synthetic: record id `9999999`, and filenames that could not be
mistaken for archive content. The real DOI never appears in a fixture, so no future reader treats a
fixture as archive metadata and no remembered archive structure creeps in through the test suite.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

SYNTHETIC_RECORD_ID = "9999999"
SYNTHETIC_DOI = f"10.5281/zenodo.{SYNTHETIC_RECORD_ID}"
FIXED_NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

# Deliberately small, so a single served blob crosses several sink calls.
SINK_CHUNK = 7


class FakeTransport:
    """A `ZenodoTransport` that serves canned data and counts every call.

    Chunking the blob at 7 bytes drives the streaming hashers across chunk boundaries, so an
    implementation that hashes only the first chunk cannot pass the byte-identity tests.
    """

    def __init__(
        self, payload: Mapping[str, object], blobs: Mapping[str, bytes] | None = None
    ) -> None:
        self.payload = payload
        self.blobs: dict[str, bytes] = dict(blobs or {})
        self.get_json_urls: list[str] = []
        self.stream_urls: list[str] = []

    def get_json(self, url: str) -> Mapping[str, object]:
        self.get_json_urls.append(url)
        return self.payload

    def stream_to(self, url: str, sink: Callable[[bytes], None]) -> None:
        self.stream_urls.append(url)
        data = self.blobs[url]
        for start in range(0, len(data), SINK_CHUNK):
            sink(data[start : start + SINK_CHUNK])


def digest(content: bytes, algorithm: str) -> str:
    return hashlib.new(algorithm, content).hexdigest()


def build_file_entry(
    key: str,
    content: bytes,
    *,
    algorithm: str = "md5",
    checksum: str | None = None,
    size: int | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    """One entry of the Zenodo record `files` array, with a correct checksum unless overridden."""
    value = checksum if checksum is not None else f"{algorithm}:{digest(content, algorithm)}"
    return {
        "key": key,
        "size": len(content) if size is None else size,
        "checksum": value,
        "links": {"self": url or f"https://zenodo.example/api/files/{key}"},
    }


def build_payload(
    *,
    record_id: str = SYNTHETIC_RECORD_ID,
    doi: str | None = None,
    version_index: int | None = 0,
    concept_doi: str | None = "10.5281/zenodo.9999998",
    revision: int | None = 3,
    title: str = "Synthetic acquisition-layer record",
    licence: object = "cc-by-4.0",
    access_right: str | None = "open",
    publication_date: str | None = "2099-01-01",
    files: list[dict[str, Any]] | None = None,
    extra: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """A Zenodo record response shaped like the live one.

    The shape below was read from the live API during Gate 0, not recalled: the version lives in
    `metadata.relations.version[].index`, and there is no `metadata.version` string.
    """
    metadata: dict[str, Any] = {"title": title}
    if version_index is not None:
        metadata["relations"] = {
            "version": [{"index": version_index, "is_last": True, "parent": {"pid_value": "1"}}]
        }
    if licence is not None:
        metadata["license"] = licence
    if access_right is not None:
        metadata["access_right"] = access_right
    if publication_date is not None:
        metadata["publication_date"] = publication_date
    payload: dict[str, Any] = {
        "id": int(record_id),
        "doi": doi if doi is not None else f"10.5281/zenodo.{record_id}",
        "metadata": metadata,
        "files": [] if files is None else files,
    }
    if concept_doi is not None:
        payload["conceptdoi"] = concept_doi
    if revision is not None:
        payload["revision"] = revision
    if extra:
        payload.update(extra)
    return payload

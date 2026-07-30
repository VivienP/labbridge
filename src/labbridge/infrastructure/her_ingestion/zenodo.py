"""Pure interpretation of a Zenodo record payload. Imports no I/O library.

Tolerant at the boundary, strict internally: the real API response carries many fields this layer
does not model, so rejecting unknown keys would refuse a valid record. A required field that is
absent or the wrong type is never defaulted — that would be the silent coercion FAILURE_MATRIX F-019
exists to forbid.

The shape read here was taken from the live record `10.5281/zenodo.20439519`, not recalled. Two
observed facts drive it: there is no `metadata.version` string — the version index lives under
`metadata.relations.version[].index` — and file entries carry `key`, `size`, `checksum` and
`links.self`. Every required field still raises its own named error, so a future shape change
identifies precisely what to correct.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Final, Protocol, get_args

from .errors import (
    FileSelectionError,
    FileTooLargeError,
    UnsupportedRecordError,
)
from .records import (
    ArchiveInventory,
    ChecksumAlgorithm,
    LicenceStatus,
    RemoteFile,
)

ZENODO_API_ROOT: Final = "https://zenodo.org/api"
#: A drive-qualified path such as `C:` has its colon at index 1.
_DRIVE_PREFIX_LENGTH: Final = 2
SUPPORTED_ALGORITHMS: Final = frozenset(get_args(ChecksumAlgorithm))
_UNSAFE_FILENAME_PARTS: Final = ("/", "\\", "\x00")


class ZenodoTransport(Protocol):
    """The single network seam. Injected everywhere so the whole layer is testable offline.

    An implementation raises `SourceUnavailableError` for any transport failure — status, timeout,
    connection, or undecodable body — so callers never see a library-specific exception.
    """

    def get_json(self, url: str) -> Mapping[str, object]:
        """Fetch and decode a JSON document."""

    def stream_to(self, url: str, sink: Callable[[bytes], None]) -> None:
        """Stream the body to `sink` in chunks. The transport owns and closes the connection."""


def record_api_url(record_id: str) -> str:
    return f"{ZENODO_API_ROOT}/records/{record_id}"


def _require(payload: Mapping[str, object], key: str) -> object:
    if key not in payload:
        raise UnsupportedRecordError("required field is absent", field=key)
    return payload[key]


def _require_str(payload: Mapping[str, object], key: str) -> str:
    value = _require(payload, key)
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    raise UnsupportedRecordError(f"expected a string, got {type(value).__name__}", field=key)


def _require_int(payload: Mapping[str, object], key: str) -> int:
    value = _require(payload, key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise UnsupportedRecordError(f"expected an integer, got {type(value).__name__}", field=key)
    return value


def _require_mapping(payload: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = _require(payload, key)
    if not isinstance(value, Mapping):
        raise UnsupportedRecordError(f"expected an object, got {type(value).__name__}", field=key)
    return value


def parse_checksum(raw: str) -> tuple[ChecksumAlgorithm, str]:
    """Split `algorithm:digest`. A bare digest is never assumed to be any particular algorithm."""
    algorithm, separator, value = raw.partition(":")
    if not separator or not value:
        raise UnsupportedRecordError(
            f"checksum `{raw}` carries no algorithm prefix", field="checksum"
        )
    normalised = algorithm.strip().lower()
    if normalised not in SUPPORTED_ALGORITHMS:
        raise UnsupportedRecordError(
            f"checksum algorithm `{normalised}` is not supported; "
            f"supported: {', '.join(sorted(SUPPORTED_ALGORITHMS))}",
            field="checksum",
        )
    # `normalised` is a member of the Literal by the check above; mypy cannot narrow a set test.
    return normalised, value.strip().lower()  # type: ignore[return-value]


def safe_filename(raw: str) -> str:
    """Reject a remote name that could escape the landing root (docs/SPEC.md section 14)."""
    if not raw or raw in {".", ".."}:
        raise UnsupportedRecordError(f"unusable filename `{raw}`", field="key")
    if any(part in raw for part in _UNSAFE_FILENAME_PARTS):
        raise UnsupportedRecordError(
            f"filename `{raw}` contains a path separator or NUL", field="key"
        )
    if len(raw) >= _DRIVE_PREFIX_LENGTH and raw[1] == ":":
        raise UnsupportedRecordError(
            f"filename `{raw}` looks like a drive-qualified path", field="key"
        )
    return raw


def _parse_licence(metadata: Mapping[str, object]) -> LicenceStatus:
    """Read the declared rights verbatim. `redistribution` stays `unresolved` regardless."""
    declared = metadata.get("license", metadata.get("rights"))
    raw: str | None = None
    if isinstance(declared, str):
        raw = declared
    elif isinstance(declared, Mapping):
        identifier = declared.get("id", declared.get("identifier"))
        if isinstance(identifier, str):
            raw = identifier
    access = metadata.get("access_right")
    return LicenceStatus(raw_value=raw, access_right=access if isinstance(access, str) else None)


def _parse_version_index(metadata: Mapping[str, object]) -> str:
    """The zero-based version index Zenodo publishes under `metadata.relations.version`.

    The record carries no semantic version string, so nothing is renumbered or invented here. When
    the relation is absent the record cannot be identified by version, and that fails explicitly
    rather than defaulting to a plausible number.
    """
    relations = metadata.get("relations")
    if not isinstance(relations, Mapping):
        raise UnsupportedRecordError("no version relation", field="metadata.relations")
    versions = relations.get("version")
    if not isinstance(versions, Sequence) or isinstance(versions, str | bytes) or not versions:
        raise UnsupportedRecordError("empty version relation", field="metadata.relations.version")
    first = versions[0]
    if not isinstance(first, Mapping):
        raise UnsupportedRecordError(
            "version relation is not an object", field="metadata.relations.version"
        )
    index = first.get("index")
    if isinstance(index, bool) or not isinstance(index, int):
        raise UnsupportedRecordError(
            "version relation carries no integer index", field="metadata.relations.version.index"
        )
    return str(index)


def _parse_file(entry: Mapping[str, object]) -> RemoteFile:
    algorithm, value = parse_checksum(_require_str(entry, "checksum"))
    links = _require_mapping(entry, "links")
    return RemoteFile(
        filename=safe_filename(_require_str(entry, "key")),
        byte_size=_require_int(entry, "size"),
        checksum_algorithm=algorithm,
        checksum_value=value,
        download_url=_require_str(links, "self"),
    )


def parse_record(payload: Mapping[str, object], *, retrieved_at: datetime) -> ArchiveInventory:
    """Interpret a Zenodo record response. Unknown top-level fields are ignored deliberately."""
    files = _require(payload, "files")
    if not isinstance(files, Sequence) or isinstance(files, str | bytes):
        raise UnsupportedRecordError("expected an array of file entries", field="files")
    metadata = _require_mapping(payload, "metadata")

    parsed: list[RemoteFile] = []
    for entry in files:
        if not isinstance(entry, Mapping):
            raise UnsupportedRecordError("a file entry is not an object", field="files")
        parsed.append(_parse_file(entry))

    revision = payload.get("revision")
    concept_doi = payload.get("conceptdoi")
    return ArchiveInventory(
        doi=_require_str(payload, "doi"),
        record_id=_require_str(payload, "id"),
        record_version=_parse_version_index(metadata),
        concept_doi=concept_doi if isinstance(concept_doi, str) else None,
        revision=revision if isinstance(revision, int) and not isinstance(revision, bool) else None,
        title=_require_str(metadata, "title"),
        licence=_parse_licence(metadata),
        published_date=(
            metadata["publication_date"]
            if isinstance(metadata.get("publication_date"), str)
            else None
        ),
        files=tuple(parsed),
        retrieved_at=retrieved_at,
    )


def select_files(
    inventory: ArchiveInventory,
    *,
    filenames: Sequence[str],
    max_bytes: int,
    allow_large: Sequence[str],
) -> tuple[RemoteFile, ...]:
    """Resolve explicitly named files, in the order requested.

    There is no pattern language and no download-everything path: docs/DATA_STRATEGY.md section 2.4
    requires "download only explicitly selected files". An unknown name is an error rather than a
    silent skip, because a silent skip would let the operator believe a file had been downloaded.
    """
    by_name = {remote.filename: remote for remote in inventory.files}
    available = tuple(by_name)
    allowed = set(allow_large)
    selected: list[RemoteFile] = []
    for name in filenames:
        remote = by_name.get(name)
        if remote is None:
            raise FileSelectionError(requested=name, available=available)
        if remote.byte_size > max_bytes and name not in allowed:
            raise FileTooLargeError(
                filename=name, byte_size=remote.byte_size, limit_bytes=max_bytes
            )
        selected.append(remote)
    return tuple(selected)

"""Typed failures for HER source acquisition.

One class per failure mode, each with a stable `code` a future `FailureRecord.failure_code` can
adopt unchanged. AI_CONTRACT.md section 11 forbids collapsing infrastructure, programming,
scientific, and expected experimental failures into one generic error type; this is the opposite.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Literal

LandingConflictReason = Literal["landing_occupied", "reservation_occupied"]


class HerIngestionError(Exception):
    """Base for every acquisition failure. `code` is the stable machine-readable classification."""

    code: ClassVar[str] = "her_ingestion_error"


class UnsupportedRecordError(HerIngestionError):
    """The record envelope cannot be interpreted: a required field is absent or the wrong type.

    The same failure class as FAILURE_MATRIX F-019, one level up — the record envelope rather than
    the data files. A distinct code keeps the two from merging when the archive parser arrives.
    """

    code: ClassVar[str] = "unsupported_record"

    def __init__(self, detail: str, *, field: str | None = None) -> None:
        self.field = field
        location = f" (field `{field}`)" if field else ""
        super().__init__(f"unsupported Zenodo record{location}: {detail}")


class SourceUnavailableError(HerIngestionError):
    """The record or a file could not be retrieved: HTTP status, timeout, or connection failure.

    A transport implementation translates its own exceptions into this, so the acquisition layer
    speaks one vocabulary and the CLI never surfaces a raw traceback for an ordinary 404.
    """

    code: ClassVar[str] = "source_unavailable"

    def __init__(self, *, url: str, detail: str, status: int | None = None) -> None:
        self.url = url
        self.status = status
        where = f" (HTTP {status})" if status is not None else ""
        super().__init__(f"{url} could not be retrieved{where}: {detail}")


class SourceIntegrityError(HerIngestionError):
    """The provided checksum does not match the bytes received. FAILURE_MATRIX F-018.

    The bytes are retained at `quarantine_path` rather than deleted: F-018's retention column reads
    "Source file retained separately but not accepted into dataset".
    """

    code: ClassVar[str] = "source_checksum_mismatch"

    def __init__(
        self,
        *,
        filename: str,
        algorithm: str,
        expected: str,
        computed: str,
        quarantine_path: Path,
    ) -> None:
        self.filename = filename
        self.algorithm = algorithm
        self.expected = expected
        self.computed = computed
        self.quarantine_path = quarantine_path
        super().__init__(
            f"{filename}: {algorithm} checksum mismatch; expected {expected}, computed {computed}. "
            f"Bytes retained for diagnosis at {quarantine_path}; not accepted into the dataset."
        )


class LandingConflictError(HerIngestionError):
    """The immutable landing path, or its reservation, is already occupied.

    Raw source files are never overwritten (AI_CONTRACT.md invariant 11), so a re-fetch stops here
    and requires explicit operator action naming the path.
    """

    code: ClassVar[str] = "landing_occupied"

    def __init__(self, *, path: Path, reason: LandingConflictReason) -> None:
        self.path = path
        self.reason = reason
        if reason == "landing_occupied":
            detail = "a landed file already exists and raw source is never overwritten"
        else:
            detail = "a reservation from an interrupted fetch is present"
        super().__init__(
            f"{path}: {detail} ({reason}). Move or remove it deliberately to re-fetch."
        )


class FileSelectionError(HerIngestionError):
    """A requested filename is not in the record. Never a silent skip."""

    code: ClassVar[str] = "file_not_in_record"

    def __init__(self, *, requested: str, available: tuple[str, ...]) -> None:
        self.requested = requested
        self.available = available
        listing = ", ".join(available) if available else "(the record lists no files)"
        super().__init__(f"`{requested}` is not in the record. Available: {listing}")


class FileTooLargeError(HerIngestionError):
    """A requested file exceeds the size ceiling and was not explicitly allowed.

    This is how docs/DATA_STRATEGY.md section 2.4's "avoid downloading the large measurement
    video unless explicitly requested" is honoured without naming a file no inventory confirmed.
    """

    code: ClassVar[str] = "file_exceeds_size_limit"

    def __init__(self, *, filename: str, byte_size: int, limit_bytes: int) -> None:
        self.filename = filename
        self.byte_size = byte_size
        self.limit_bytes = limit_bytes
        super().__init__(
            f"{filename} is {byte_size} bytes, above the {limit_bytes}-byte ceiling. "
            f"Pass --allow-large {filename} to request it explicitly."
        )


class NoFilesRequestedError(HerIngestionError):
    """A non-dry-run fetch named no file. There is deliberately no download-everything path."""

    code: ClassVar[str] = "no_files_requested"

    def __init__(self, *, available: tuple[str, ...]) -> None:
        self.available = available
        listing = ", ".join(available) if available else "(the record lists no files)"
        super().__init__(
            "no file was requested; only explicitly selected files are downloaded. "
            f"Available: {listing}"
        )

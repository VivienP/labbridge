"""The immutable raw landing zone.

AI_CONTRACT.md invariant 11 requires downloaded HER source files to be "fetched, checksummed,
recorded, and retained unchanged". Two mechanisms carry that here:

* **Write-once by atomic reservation.** `open(..., "xb")` is an exclusive create on POSIX
  (`O_CREAT|O_EXCL`) and Windows (`CREATE_NEW`). A pre-existence check alone is check-then-act, and
  worse, it cannot distinguish a crashed download from a complete one. With a reservation, a crash
  leaves `<name>.part` and never a truncated file at the immutable landing path.
* **Quarantine, not deletion, on mismatch.** FAILURE_MATRIX F-018's retention column reads
  "Source file retained separately but not accepted into dataset", and AI_CONTRACT.md section 11
  forbids destructive removal where a reversible alternative exists.

The bytes are hashed in the same single pass that writes them: the source is a network stream and
cannot be re-read.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from .errors import LandingConflictError, SourceIntegrityError
from .records import FetchedFile, RemoteFile
from .zenodo import ZenodoTransport

RESERVATION_SUFFIX: Final = ".part"
QUARANTINE_DIRNAME: Final = "quarantine"
#: Enough of the computed digest to keep repeated quarantined attempts distinct.
_QUARANTINE_DIGEST_CHARS: Final = 12


@dataclass(frozen=True)
class LandingZone:
    """Paths in one immutable landing root. Filenames are pre-validated by `safe_filename`."""

    root: Path

    def landing_path(self, filename: str) -> Path:
        return self.root / filename

    def reservation_path(self, filename: str) -> Path:
        return self.root / f"{filename}{RESERVATION_SUFFIX}"

    def quarantine_path(self, filename: str, digest_prefix: str) -> Path:
        return self.root / QUARANTINE_DIRNAME / f"{digest_prefix}-{filename}"


def fetch_file(
    remote: RemoteFile,
    *,
    transport: ZenodoTransport,
    zone: LandingZone,
    retrieved_at: datetime,
) -> FetchedFile:
    """Download one file into the landing zone, verifying its provided checksum.

    Raises `LandingConflictError` when the landing path or its reservation is occupied, and
    `SourceIntegrityError` when the provided checksum does not match the bytes received.
    """
    landing = zone.landing_path(remote.filename)
    if landing.exists():
        raise LandingConflictError(path=landing, reason="landing_occupied")

    reservation = zone.reservation_path(remote.filename)
    reservation.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = reservation.open("xb")
    except FileExistsError as exc:
        raise LandingConflictError(path=reservation, reason="reservation_occupied") from exc

    sha256 = hashlib.sha256()
    # When the record provides sha256, one hasher serves both roles.
    provided = sha256 if remote.checksum_algorithm == "sha256" else hashlib.md5()
    written = 0

    def sink(chunk: bytes) -> None:
        nonlocal written
        handle.write(chunk)
        sha256.update(chunk)
        if provided is not sha256:
            provided.update(chunk)
        written += len(chunk)

    with handle:
        transport.stream_to(remote.download_url, sink)

    computed_sha256 = sha256.hexdigest()
    if provided.hexdigest() != remote.checksum_value:
        quarantine = zone.quarantine_path(
            remote.filename, computed_sha256[:_QUARANTINE_DIGEST_CHARS]
        )
        quarantine.parent.mkdir(parents=True, exist_ok=True)
        os.replace(reservation, quarantine)
        raise SourceIntegrityError(
            filename=remote.filename,
            algorithm=remote.checksum_algorithm,
            expected=remote.checksum_value,
            computed=provided.hexdigest(),
            quarantine_path=quarantine,
        )

    # Re-check immediately before the move: the reservation, not this check, is what makes the
    # write exclusive, but a landing file appearing mid-download is still worth refusing.
    if landing.exists():
        raise LandingConflictError(path=landing, reason="landing_occupied")
    os.replace(reservation, landing)

    return FetchedFile(
        filename=remote.filename,
        source_url=remote.download_url,
        byte_size=written,
        provided_checksum_algorithm=remote.checksum_algorithm,
        provided_checksum_value=remote.checksum_value,
        computed_sha256=computed_sha256,
        landing_path=remote.filename,
        retrieved_at=retrieved_at,
    )

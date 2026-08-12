"""Closed checksum manifests shared by released LabBridge artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Final

MANIFEST_FILENAME: Final = "manifest.json"


class ArtifactVerificationError(Exception):
    def __init__(self, problems: tuple[str, ...]) -> None:
        self.problems = problems
        super().__init__(f"artifact verification failed: {'; '.join(problems)}")


def canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_manifest(
    destination: Path,
    *,
    metadata: dict[str, object],
) -> dict[str, object]:
    """Close the flat file set already present in ``destination``."""
    reserved = {"files", "files_digest"}
    overlap = reserved.intersection(metadata)
    if overlap:
        raise ValueError(f"manifest metadata uses reserved keys: {', '.join(sorted(overlap))}")
    members = sorted(
        path for path in destination.iterdir() if path.is_file() and path.name != MANIFEST_FILENAME
    )
    files = [
        {"name": path.name, "sha256": digest(path.read_bytes()), "byte_size": path.stat().st_size}
        for path in members
    ]
    manifest: dict[str, object] = {
        **metadata,
        "files": files,
        "files_digest": digest(canonical_json(files)),
    }
    (destination / MANIFEST_FILENAME).write_bytes(canonical_json(manifest))
    return manifest


def verify_manifest(destination: Path) -> dict[str, object]:
    manifest_path = destination / MANIFEST_FILENAME
    if not manifest_path.exists():
        raise ArtifactVerificationError((f"{MANIFEST_FILENAME} is missing",))
    manifest: dict[str, object] = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ArtifactVerificationError(("manifest has no file list",))

    problems: list[str] = []
    if digest(canonical_json(files)) != manifest.get("files_digest"):
        problems.append("files_digest does not match the recorded file entries")

    listed: set[str] = set()
    for raw_entry in files:
        if not isinstance(raw_entry, dict):
            problems.append("manifest contains a non-object file entry")
            continue
        name = str(raw_entry.get("name", ""))
        listed.add(name)
        member = destination / name
        if not member.exists():
            problems.append(f"{name} is listed in the manifest but missing")
            continue
        data = member.read_bytes()
        actual = digest(data)
        if actual != raw_entry.get("sha256"):
            problems.append(
                f"{name}: sha256 {actual} does not match manifest {raw_entry.get('sha256')}"
            )
        if len(data) != raw_entry.get("byte_size"):
            problems.append(
                f"{name}: byte size {len(data)} does not match manifest "
                f"{raw_entry.get('byte_size')}"
            )

    present = {
        path.name
        for path in destination.iterdir()
        if path.is_file() and path.name != MANIFEST_FILENAME
    }
    for extra in sorted(present - listed):
        problems.append(f"{extra} is present but not listed in the manifest")
    if problems:
        raise ArtifactVerificationError(tuple(problems))
    return manifest


__all__ = [
    "MANIFEST_FILENAME",
    "ArtifactVerificationError",
    "build_manifest",
    "canonical_json",
    "digest",
    "verify_manifest",
]

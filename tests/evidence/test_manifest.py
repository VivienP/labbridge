"""Closed artifact manifests detect every relevant form of tampering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from labbridge.evidence.manifest import (
    ArtifactVerificationError,
    build_manifest,
    verify_manifest,
)


@pytest.fixture
def released(tmp_path: Path) -> Path:
    (tmp_path / "payload.bin").write_bytes(b"source bytes")
    (tmp_path / "record.json").write_text("{}\n", encoding="utf-8")
    build_manifest(
        tmp_path,
        metadata={
            "artifact_kind": "source_capture",
            "schema_version": "1",
            "producing_versions": {"labbridge": "0.1.0"},
            "data_origin": "synthetic",
            "execution_mode": "replay",
        },
    )
    return tmp_path


def test_an_untouched_closed_manifest_verifies(released: Path) -> None:
    assert verify_manifest(released)["artifact_kind"] == "source_capture"


def test_one_changed_byte_fails_verification(released: Path) -> None:
    member = released / "payload.bin"
    member.write_bytes(member.read_bytes()[:-1] + b"X")

    with pytest.raises(ArtifactVerificationError, match="sha256"):
        verify_manifest(released)


def test_a_deleted_member_fails_verification(released: Path) -> None:
    (released / "payload.bin").unlink()

    with pytest.raises(ArtifactVerificationError, match="missing"):
        verify_manifest(released)


def test_an_unexpected_member_fails_verification(released: Path) -> None:
    (released / "extra.txt").write_text("unexpected", encoding="utf-8")

    with pytest.raises(ArtifactVerificationError, match="not listed"):
        verify_manifest(released)


def test_an_altered_manifest_entry_fails_verification(released: Path) -> None:
    manifest_path = released / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ArtifactVerificationError, match="files_digest"):
        verify_manifest(released)

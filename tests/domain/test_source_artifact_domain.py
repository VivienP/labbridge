"""Opaque source-artifact identity and metadata."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from labbridge.domain.source_artifacts import (
    SourceArtifact,
    source_artifact_id,
)


@given(st.binary())
def test_identical_bytes_have_a_stable_source_identity(payload: bytes) -> None:
    digest = hashlib.sha256(payload).hexdigest()

    first = source_artifact_id(sha256=digest, byte_size=len(payload), media_type="text/csv")
    second = source_artifact_id(sha256=digest, byte_size=len(payload), media_type="text/csv")

    assert first == second


@given(st.binary(), st.binary())
def test_changed_bytes_have_a_changed_source_identity(first: bytes, second: bytes) -> None:
    if first == second:
        return

    first_id = source_artifact_id(
        sha256=hashlib.sha256(first).hexdigest(),
        byte_size=len(first),
        media_type="text/csv",
    )
    second_id = source_artifact_id(
        sha256=hashlib.sha256(second).hexdigest(),
        byte_size=len(second),
        media_type="text/csv",
    )

    assert first_id != second_id


def test_declared_media_type_is_part_of_the_identity() -> None:
    payload = b"opaque"
    digest = hashlib.sha256(payload).hexdigest()

    assert source_artifact_id(
        sha256=digest, byte_size=len(payload), media_type="text/csv"
    ) != source_artifact_id(
        sha256=digest, byte_size=len(payload), media_type="application/octet-stream"
    )


def test_filename_is_descriptive_and_does_not_change_the_identity() -> None:
    payload = b"opaque"
    digest = hashlib.sha256(payload).hexdigest()
    identity = source_artifact_id(sha256=digest, byte_size=len(payload), media_type="text/csv")
    created_at = datetime(2026, 8, 12, tzinfo=UTC)

    first = SourceArtifact(
        source_artifact_id=identity,
        filename="synthetic-replay-a.csv",
        media_type="text/csv",
        byte_size=len(payload),
        sha256=digest,
        data_origin="synthetic",
        execution_mode="replay",
        state="pending",
        object_uri="s3://labbridge/source-artifacts/sha256/opaque",
        created_at=created_at,
    )
    second = first.model_copy(update={"filename": "synthetic-replay-b.csv"})

    assert first.source_artifact_id == second.source_artifact_id


def test_quarantined_source_states_the_reason() -> None:
    with pytest.raises(ValidationError, match="quarantine_reason"):
        SourceArtifact(
            source_artifact_id="source:abc",
            filename="synthetic-replay.csv",
            media_type="text/csv",
            byte_size=0,
            sha256=hashlib.sha256(b"").hexdigest(),
            data_origin="synthetic",
            execution_mode="replay",
            state="quarantined",
            object_uri="s3://labbridge/source-artifacts/sha256/empty",
            created_at=datetime(2026, 8, 12, tzinfo=UTC),
        )


def test_pending_source_cannot_claim_a_commit_timestamp() -> None:
    with pytest.raises(ValidationError, match="only a committed"):
        SourceArtifact(
            source_artifact_id="source:pending",
            filename="synthetic-replay-opaque.bin",
            media_type="application/octet-stream",
            byte_size=0,
            sha256=hashlib.sha256(b"").hexdigest(),
            data_origin="synthetic",
            execution_mode="replay",
            state="pending",
            object_uri="s3://labbridge/source-artifacts/sha256/empty",
            created_at=datetime(2026, 8, 12, tzinfo=UTC),
            committed_at=datetime(2026, 8, 12, tzinfo=UTC),
        )


def test_observed_simulation_is_rejected_without_inferring_from_the_filename() -> None:
    with pytest.raises(ValidationError, match="inadmissible origin/mode"):
        SourceArtifact(
            source_artifact_id="source:abc",
            filename="observed.csv",
            media_type="text/csv",
            byte_size=0,
            sha256=hashlib.sha256(b"").hexdigest(),
            data_origin="observed",
            execution_mode="simulation",
            state="pending",
            object_uri="s3://labbridge/source-artifacts/sha256/empty",
            created_at=datetime(2026, 8, 12, tzinfo=UTC),
        )

"""Content-addressed opaque source files.

Source artifacts retain bytes before any parser assigns scientific meaning. Filename and media
type are descriptive declarations; neither is used to infer columns, units, reference scales, or
technique validity.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical import content_id
from .identity import ADMISSIBLE_PAIRS, DataOrigin, ExecutionMode

SourceArtifactState = Literal["pending", "committed", "quarantined"]


def source_artifact_id(*, sha256: str, byte_size: int, media_type: str) -> str:
    """Identify exact bytes under their declared media type."""
    return content_id(
        "source",
        {"sha256": sha256, "byte_size": byte_size, "media_type": media_type},
    )


class SourceArtifact(BaseModel):
    """One immutable source payload and its explicit, uninterpreted metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_artifact_id: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    byte_size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_origin: DataOrigin
    execution_mode: ExecutionMode
    state: SourceArtifactState
    object_uri: str = Field(min_length=1)
    created_at: datetime
    committed_at: datetime | None = None
    quarantine_reason: str | None = None

    @model_validator(mode="after")
    def _state_and_provenance_are_explicit(self) -> Self:
        if (self.data_origin, self.execution_mode) not in ADMISSIBLE_PAIRS:
            raise ValueError(
                f"inadmissible origin/mode pair {self.data_origin}+{self.execution_mode}"
            )
        if self.state == "quarantined" and not self.quarantine_reason:
            raise ValueError("quarantined source artifact requires a quarantine_reason")
        if self.state != "quarantined" and self.quarantine_reason is not None:
            raise ValueError("quarantine_reason is valid only for a quarantined source artifact")
        if (self.state == "committed") != (self.committed_at is not None):
            raise ValueError("only a committed source artifact may carry committed_at")
        return self


__all__ = ["SourceArtifact", "SourceArtifactState", "source_artifact_id"]

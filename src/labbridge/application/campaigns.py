"""Application boundary for optimistic, idempotent campaign control."""

from __future__ import annotations

import hashlib
import uuid
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from labbridge.domain.canonical import canonical_bytes
from labbridge.domain.idempotency import normalise_idempotency_key
from labbridge.domain.lifecycle import CampaignState

CampaignAction = Literal["pause", "resume", "cancel"]


class CampaignControlResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_id: uuid.UUID
    state: CampaignState
    version: int = Field(ge=1)
    replayed: bool


class CampaignControlRepository(Protocol):
    def control(
        self,
        campaign_id: uuid.UUID,
        *,
        action: CampaignAction,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
    ) -> CampaignControlResult: ...


class CampaignControlService:
    """Coordinate control commands without owning persistence or transition rules."""

    def __init__(self, repository: CampaignControlRepository) -> None:
        self._repository = repository

    def _control(
        self,
        campaign_id: uuid.UUID,
        *,
        action: CampaignAction,
        expected_version: int,
        idempotency_key: str,
    ) -> CampaignControlResult:
        if expected_version < 1:
            raise ValueError("expected_version must be at least 1")
        key = normalise_idempotency_key(idempotency_key)
        request_hash = hashlib.sha256(
            canonical_bytes(
                {
                    "campaign_id": str(campaign_id),
                    "action": action,
                    "expected_version": expected_version,
                }
            )
        ).hexdigest()
        return self._repository.control(
            campaign_id,
            action=action,
            expected_version=expected_version,
            idempotency_key=key,
            request_hash=request_hash,
        )

    def pause(
        self, campaign_id: uuid.UUID, *, expected_version: int, idempotency_key: str
    ) -> CampaignControlResult:
        return self._control(
            campaign_id,
            action="pause",
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

    def resume(
        self, campaign_id: uuid.UUID, *, expected_version: int, idempotency_key: str
    ) -> CampaignControlResult:
        return self._control(
            campaign_id,
            action="resume",
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

    def cancel(
        self, campaign_id: uuid.UUID, *, expected_version: int, idempotency_key: str
    ) -> CampaignControlResult:
        return self._control(
            campaign_id,
            action="cancel",
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )


__all__ = [
    "CampaignAction",
    "CampaignControlRepository",
    "CampaignControlResult",
    "CampaignControlService",
]

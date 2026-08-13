from __future__ import annotations

import uuid
from dataclasses import dataclass

from labbridge.application.campaigns import (
    CampaignControlResult,
    CampaignControlService,
)

SHA256_LENGTH = 64
CONTROL_ACTIONS = 3


@dataclass
class _Call:
    campaign_id: uuid.UUID
    action: str
    expected_version: int
    idempotency_key: str
    request_hash: str


class _Repository:
    def __init__(self) -> None:
        self.calls: list[_Call] = []

    def control(
        self,
        campaign_id: uuid.UUID,
        *,
        action: str,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
    ) -> CampaignControlResult:
        self.calls.append(
            _Call(campaign_id, action, expected_version, idempotency_key, request_hash)
        )
        return CampaignControlResult(
            campaign_id=campaign_id,
            state="paused" if action == "pause" else "active",
            version=expected_version + 1,
            replayed=False,
        )


def test_campaign_control_passes_expected_version_and_scoped_identity_to_the_port() -> None:
    repository = _Repository()
    service = CampaignControlService(repository)
    campaign_id = uuid.uuid4()

    result = service.pause(
        campaign_id,
        expected_version=7,
        idempotency_key="  operator-command-7  ",
    )

    assert result.state == "paused"
    assert repository.calls == [
        _Call(
            campaign_id,
            "pause",
            7,
            "operator-command-7",
            repository.calls[0].request_hash,
        )
    ]
    assert len(repository.calls[0].request_hash) == SHA256_LENGTH


def test_each_control_action_has_a_distinct_request_fingerprint() -> None:
    repository = _Repository()
    service = CampaignControlService(repository)
    campaign_id = uuid.uuid4()

    service.pause(campaign_id, expected_version=1, idempotency_key="same-key")
    service.resume(campaign_id, expected_version=1, idempotency_key="same-key")
    service.cancel(campaign_id, expected_version=1, idempotency_key="same-key")

    assert len({call.request_hash for call in repository.calls}) == CONTROL_ACTIONS

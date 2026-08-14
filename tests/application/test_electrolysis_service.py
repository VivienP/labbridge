from __future__ import annotations

from electrolysis_helpers import (
    FixedElectrolysisSourceReader,
    MemoryElectrolysisRecords,
    electrolysis_profile,
    electrolysis_source,
)
from labbridge.application.electrolysis_ingestion import ElectrolysisIngestionService


def test_service_persists_profile_and_normalisation_idempotently() -> None:
    records = MemoryElectrolysisRecords()
    service = ElectrolysisIngestionService(
        FixedElectrolysisSourceReader(), records, producing_version="0.1.0"
    )

    stored_profile = service.create_profile(electrolysis_profile())
    first = service.normalise(
        electrolysis_source().artifact.source_artifact_id, stored_profile.profile_id
    )
    second = service.normalise(
        electrolysis_source().artifact.source_artifact_id, stored_profile.profile_id
    )

    assert stored_profile.replayed is False
    assert first.replayed is False
    assert second.replayed is True
    assert service.get_normalisation(first.result.observation.observation_id).result == first.result

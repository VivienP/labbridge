from __future__ import annotations

from electrolysis_helpers import electrolysis_profile, electrolysis_source
from labbridge.application.electrolysis_ingestion import (
    ElectrolysisIngestionService,
    ElectrolysisRecordRepository,
)
from labbridge.domain.electrolysis import (
    ElectrolysisImportProfile,
    electrolysis_import_profile_id,
)
from labbridge.domain.electrolysis_observations import ElectrolysisNormalisationResult


class MemoryElectrolysisRecords(ElectrolysisRecordRepository):
    def __init__(self) -> None:
        self.profiles: dict[str, ElectrolysisImportProfile] = {}
        self.results: dict[str, ElectrolysisNormalisationResult] = {}

    def put_profile(
        self, item: ElectrolysisImportProfile, *, idempotency_key: str | None = None
    ) -> tuple[str, bool]:
        profile_id = electrolysis_import_profile_id(item)
        replayed = profile_id in self.profiles
        self.profiles.setdefault(profile_id, item)
        return profile_id, replayed

    def get_profile(self, profile_id: str) -> ElectrolysisImportProfile | None:
        return self.profiles.get(profile_id)

    def put_normalisation(
        self, result: ElectrolysisNormalisationResult, *, idempotency_key: str | None = None
    ) -> bool:
        observation_id = result.observation.observation_id
        replayed = observation_id in self.results
        self.results.setdefault(observation_id, result)
        return replayed

    def get_normalisation(self, observation_id: str) -> ElectrolysisNormalisationResult | None:
        return self.results.get(observation_id)


class FixedSourceReader:
    def retrieve(self, source_artifact_id: str):
        source = electrolysis_source()
        assert source_artifact_id == source.artifact.source_artifact_id
        return source


def test_service_persists_profile_and_normalisation_idempotently() -> None:
    records = MemoryElectrolysisRecords()
    service = ElectrolysisIngestionService(FixedSourceReader(), records, producing_version="0.1.0")

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

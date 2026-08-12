from __future__ import annotations

from dataclasses import dataclass

import pytest

from cv_helpers import cv_profile, cv_source
from labbridge.application.cv_ingestion import (
    CVIngestionService,
    CVRecordRepository,
    ImportProfileNotFoundError,
    NormalisedObservationNotFoundError,
)
from labbridge.domain.cv import CVImportProfile, import_profile_id
from labbridge.domain.cv_observations import NormalisationResult


@dataclass
class SourceReader:
    def retrieve(self, source_artifact_id: str):  # type: ignore[no-untyped-def]
        assert source_artifact_id == cv_source().artifact.source_artifact_id
        return cv_source()


class MemoryRecords(CVRecordRepository):
    def __init__(self) -> None:
        self.profiles: dict[str, CVImportProfile] = {}
        self.results: dict[str, NormalisationResult] = {}

    def put_profile(
        self, item: CVImportProfile, *, idempotency_key: str | None = None
    ) -> tuple[str, bool]:
        del idempotency_key
        identity = import_profile_id(item)
        replayed = identity in self.profiles
        self.profiles.setdefault(identity, item)
        return identity, replayed

    def get_profile(self, profile_id: str) -> CVImportProfile | None:
        return self.profiles.get(profile_id)

    def put_normalisation(
        self, result: NormalisationResult, *, idempotency_key: str | None = None
    ) -> bool:
        del idempotency_key
        identity = result.observation.observation_id
        replayed = identity in self.results
        self.results.setdefault(identity, result)
        return replayed

    def get_normalisation(self, observation_id: str) -> NormalisationResult | None:
        return self.results.get(observation_id)


def service() -> CVIngestionService:
    return CVIngestionService(SourceReader(), MemoryRecords(), producing_version="0.1.0")


def test_profile_creation_and_normalisation_are_content_idempotent() -> None:
    app = service()
    first_profile = app.create_profile(cv_profile())
    second_profile = app.create_profile(cv_profile())

    first = app.normalise(cv_source().artifact.source_artifact_id, first_profile.profile_id)
    second = app.normalise(cv_source().artifact.source_artifact_id, first_profile.profile_id)

    assert not first_profile.replayed
    assert second_profile.replayed
    assert not first.replayed
    assert second.replayed
    assert second.result.observation.observation_id == first.result.observation.observation_id


def test_plot_series_is_the_backend_observation_without_display_transformations() -> None:
    app = service()
    stored = app.create_profile(cv_profile())
    normalised = app.normalise(cv_source().artifact.source_artifact_id, stored.profile_id)

    plot = app.plot_series(normalised.result.observation.observation_id)

    assert plot.observation_id == normalised.result.observation.observation_id
    assert plot.data_origin == "synthetic"
    assert plot.execution_mode == "replay"
    assert [item.role for item in plot.series] == ["potential", "current"]
    assert plot.series[0].values == normalised.result.observation.series[0].values
    assert plot.provenance.source_artifact_id == cv_source().artifact.source_artifact_id


def test_unknown_profile_and_observation_have_typed_errors() -> None:
    app = service()

    with pytest.raises(ImportProfileNotFoundError):
        app.normalise(cv_source().artifact.source_artifact_id, "cv-profile:missing")
    with pytest.raises(NormalisedObservationNotFoundError):
        app.plot_series("cv-observation:missing")

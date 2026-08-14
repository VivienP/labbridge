"""The production Experiment service must be built with both normalisation readers.

`tests/test_experiment_adapter_parity.py` monkeypatches `cli._build_experiment_service`, so it
cannot see which readers that function passes on. Electrolysis observations were unreachable from
both adapters while every parity test passed. These tests call the real constructors instead.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine

from labbridge import cli
from labbridge.api import app as api_app
from labbridge.infrastructure import experiment_wiring
from labbridge.infrastructure.experiment_wiring import (
    CombinedNormalisationReader,
    build_experiment_service,
)
from labbridge.infrastructure.objectstore import InMemoryObjectStore


class _RecordingReader:
    def __init__(self, name: str) -> None:
        self.name = name
        self.observations: list[str] = []
        self.profiles: list[str] = []

    def get_normalisation(self, observation_id: str) -> str:
        self.observations.append(observation_id)
        return self.name

    def get_profile(self, profile_id: str) -> str:
        self.profiles.append(profile_id)
        return self.name


def _stub_infrastructure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace only the object store and the concrete services, never the wiring under test."""
    monkeypatch.setattr(experiment_wiring, "build_source_store", InMemoryObjectStore)


def test_wiring_dispatches_electrolysis_observations_to_the_electrolysis_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_infrastructure(monkeypatch)
    cv = _RecordingReader("cv")
    electrolysis = _RecordingReader("electrolysis")

    service = build_experiment_service(
        object(),  # type: ignore[arg-type]
        cv,  # type: ignore[arg-type]
        create_engine("sqlite://", future=True),
        electrolysis_service=electrolysis,  # type: ignore[arg-type]
    )
    reader = service._normalisations

    assert isinstance(reader, CombinedNormalisationReader)
    assert reader.get_normalisation("electrolysis-observation:abc") == "electrolysis"
    assert reader.get_normalisation("cv-observation:abc") == "cv"
    assert reader.get_profile("electrolysis-profile:abc") == "electrolysis"
    assert reader.get_profile("cv-profile:abc") == "cv"
    assert electrolysis.observations == ["electrolysis-observation:abc"]
    assert cv.observations == ["cv-observation:abc"]


def _capture_electrolysis_service(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[object]:
    captured: list[object] = []

    def record(*args: object, **kwargs: object) -> object:
        captured.append(kwargs.get("electrolysis_service"))
        return object()

    monkeypatch.setattr(module, "build_experiment_service", record)
    monkeypatch.setattr(module, "build_source_service", lambda *a, **k: object())
    monkeypatch.setattr(module, "build_cv_service", lambda *a, **k: object())
    monkeypatch.setattr(
        module, "build_electrolysis_service", lambda *a, **k: "electrolysis-service"
    )
    return captured


def test_cli_builds_the_experiment_service_with_an_electrolysis_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_electrolysis_service(monkeypatch, cli)

    cli._build_experiment_service()

    assert captured == ["electrolysis-service"]


def test_http_app_builds_the_experiment_service_with_an_electrolysis_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_electrolysis_service(monkeypatch, api_app)

    def unused_provider() -> Any:
        return object()

    provide = api_app._experiment_service_provider(
        lambda: create_engine("sqlite://", future=True),
        unused_provider,
        unused_provider,
        None,
    )
    provide()

    assert captured == ["electrolysis-service"]


def test_cli_builds_the_electrolysis_ingestion_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[object] = []

    def record(*args: object, **kwargs: object) -> str:
        captured.append("electrolysis-service")
        return "electrolysis-service"

    monkeypatch.setattr(cli, "build_electrolysis_service", record)
    monkeypatch.setattr(cli, "_build_source_service", lambda *a, **k: object())

    built = cli._build_electrolysis_service()

    assert built == "electrolysis-service"
    assert captured == ["electrolysis-service"]


def test_the_two_ingestion_services_own_disjoint_identity_namespaces() -> None:
    """The dispatcher is safe only while the prefixes cannot collide."""
    reader = CombinedNormalisationReader(
        _RecordingReader("cv"),  # type: ignore[arg-type]
        _RecordingReader("electrolysis"),  # type: ignore[arg-type]
    )

    assert reader.get_normalisation("electrolysis-observation:1") == "electrolysis"
    assert reader.get_normalisation("observation:1") == "cv"

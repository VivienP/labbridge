from __future__ import annotations

import json
from pathlib import Path
from runpy import run_path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts/openapi-v1.json"
EXPORTER = ROOT / "scripts/export_openapi.py"


def _contract() -> dict[str, object]:
    assert CONTRACT.is_file(), "the versioned OpenAPI contract has not been exported"
    document = json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def test_user_assertion_contract_has_no_origin_selector() -> None:
    document = _contract()
    schemas = document["components"]["schemas"]  # type: ignore[index]
    properties = schemas["UserAssertionRequest"]["properties"]

    assert "origin" not in properties
    assert {
        "expected_experiment_version",
        "field_name",
        "requirement_class",
        "transformation",
        "value",
        "evidence_note",
    } <= properties.keys()


def test_plot_contract_returns_backend_values_units_and_provenance() -> None:
    document = _contract()
    schemas = document["components"]["schemas"]  # type: ignore[index]
    properties = schemas["PlotSeriesView"]["properties"]

    assert {"observation_id", "data_origin", "execution_mode", "series", "provenance"} <= (
        properties.keys()
    )
    series = schemas["NormalisedSeries"]["properties"]
    assert {"values", "role", "source_unit", "unit", "series_id"} <= series.keys()


def test_normalisation_contract_retains_typed_result_properties() -> None:
    document = _contract()
    schemas = document["components"]["schemas"]  # type: ignore[index]

    assert {"observation", "graph", "findings", "parser_record"} <= schemas["NormalisationResult"][
        "properties"
    ].keys()
    assert {
        "observation_id",
        "data_origin",
        "execution_mode",
        "series",
        "metadata",
        "provenance",
    } <= schemas["NormalisedCVObservation"]["properties"].keys()


def test_source_inspection_contract_keeps_headers_semantically_unassigned() -> None:
    document = _contract()
    schemas = document["components"]["schemas"]  # type: ignore[index]
    properties = schemas["SourceInspectionView"]["properties"]

    assert set(properties) == {"source_artifact_id", "source_sha256", "headers", "row_count"}


def test_static_frontend_routes_are_not_part_of_the_api_contract() -> None:
    document = _contract()
    paths = document["paths"]  # type: ignore[index]

    assert "/" not in paths
    assert not any(str(path).startswith("/demo-fixtures") for path in paths)


def test_openapi_export_is_deterministic_and_current() -> None:
    _contract()
    render_openapi = run_path(str(EXPORTER))["render_openapi"]

    assert render_openapi() == CONTRACT.read_bytes()

"""Operator HTTP adapters must complete the electrolysis file-to-package path."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from electrolysis_helpers import electrolysis_profile
from labbridge.api import create_app
from labbridge.evidence.experiment_package import verify_experiment_package

pytestmark = pytest.mark.integration

CREATED = 201
OK = 200


def test_http_adapters_reach_an_independently_verified_electrolysis_package(
    migrated: Engine,
) -> None:
    marker = uuid.uuid4().hex
    payload = (
        "elapsed,applied_current,working_potential\n"
        f"0,10.0,-0.{int(marker[:8], 16):010d}\n"
        "60,10.0,-0.435\n120,10.0,-0.447\n"
    ).encode()
    client = TestClient(create_app(migrated))

    source = client.post(
        "/source-artifacts",
        params={
            "filename": f"synthetic-electrolysis-{marker}.csv",
            "data_origin": "synthetic",
            "execution_mode": "replay",
        },
        headers={"Idempotency-Key": f"electrolysis-source:{marker}", "Content-Type": "text/csv"},
        content=payload,
    )
    assert source.status_code == CREATED
    source_id = source.json()["source_artifact_id"]

    profile = client.post(
        "/electrolysis/import-profiles",
        json=electrolysis_profile().model_dump(mode="json"),
        headers={"Idempotency-Key": f"electrolysis-profile:{marker}"},
    )
    assert profile.status_code == CREATED, profile.text
    profile_id = profile.json()["profile_id"]

    normalised = client.post(
        "/electrolysis/normalisations",
        json={"source_artifact_id": source_id, "profile_id": profile_id},
        headers={"Idempotency-Key": f"electrolysis-normalise:{marker}"},
    )
    assert normalised.status_code == CREATED, normalised.text
    observation = normalised.json()["result"]["observation"]
    assert observation["technique"] == "galvanostatic_electrolysis"
    assert observation["metadata"]["chemical_analysis"]["state"] == "unavailable"
    assert observation["metadata"]["cell_geometry"]["state"] == "unknown"

    experiment = client.post(
        "/experiments",
        json={
            "observation_id": observation["observation_id"],
            "expected_experiment_version": 0,
        },
        headers={"Idempotency-Key": f"electrolysis-experiment:{marker}"},
    )
    assert experiment.status_code == CREATED, experiment.text
    body = experiment.json()["experiment"]
    assert body["technique"] == "galvanostatic_electrolysis"
    assertions = {item["field_name"]: item for item in body["assertions"]}
    assert assertions["scan_rate"]["value"]["state"] == "not_applicable"
    assert assertions["chemical_analysis"]["value"]["state"] == "unavailable"
    assert "faradaic_efficiency" not in assertions
    assert "conversion" not in assertions
    assert "selectivity" not in assertions
    experiment_id = body["experiment_id"]

    validation = client.post(
        f"/experiments/{experiment_id}/validations",
        json={"expected_experiment_version": 1},
        headers={"Idempotency-Key": f"electrolysis-validation:{marker}"},
    )
    assert validation.status_code == CREATED, validation.text

    passport = client.post(
        f"/experiments/{experiment_id}/passports",
        json={"expected_experiment_version": 1},
        headers={"Idempotency-Key": f"electrolysis-passport:{marker}"},
    )
    assert passport.status_code == CREATED, passport.text
    passport_id = passport.json()["passport"]["passport_id"]

    package = client.post(
        f"/experiments/{experiment_id}/packages",
        json={"passport_id": passport_id, "expected_experiment_version": 1},
        headers={"Idempotency-Key": f"electrolysis-package:{marker}"},
    )
    assert package.status_code == CREATED, package.text
    package_id = package.json()["package"]["package_id"]
    assert package.json()["package"]["schema_version"] == "3"

    download = client.get(f"/experiment-packages/{package_id}/download")
    assert download.status_code == OK
    verification = verify_experiment_package(download.content)
    assert verification.lineage_closed is True

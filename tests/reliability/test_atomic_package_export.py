from __future__ import annotations

from typing import Any, cast

import pytest

from labbridge.evidence.campaign_package import (
    BuiltCampaignExperimentPackage,
    CampaignPackageVerification,
)
from labbridge.evidence.experiment_package import ExperimentPackageVerificationError
from labbridge.infrastructure.objectstore import InMemoryObjectStore
from labbridge.reliability.package_export import publish_verified_campaign_package


def test_publish_is_atomic_idempotent_and_refuses_overwrite(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    destination = tmp_path / "campaign.zip"
    partial = destination.with_suffix(".zip.partial")
    partial.write_bytes(b"interrupted")
    package = BuiltCampaignExperimentPackage(
        metadata=cast(Any, None), archive_bytes=b"verified-package"
    )
    calls: list[bytes] = []

    verification = CampaignPackageVerification.model_construct(verified=True)

    def verify(package_bytes: bytes, *, object_store):  # type: ignore[no-untyped-def]
        calls.append(package_bytes)
        assert isinstance(object_store, InMemoryObjectStore)
        return verification

    monkeypatch.setattr("labbridge.reliability.package_export.verify_experiment_package", verify)
    store = InMemoryObjectStore()

    first = publish_verified_campaign_package(package, destination, object_store=store)
    second = publish_verified_campaign_package(package, destination, object_store=store)

    assert first == second == verification
    assert destination.read_bytes() == b"verified-package"
    assert not partial.exists()
    assert calls == [b"verified-package", b"verified-package", b"verified-package"]

    different = BuiltCampaignExperimentPackage(metadata=cast(Any, None), archive_bytes=b"different")
    with pytest.raises(ExperimentPackageVerificationError, match="immutable"):
        publish_verified_campaign_package(different, destination, object_store=store)

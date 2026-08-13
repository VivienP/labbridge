"""Atomic publication for already-built, fully verified campaign Packages."""

from __future__ import annotations

import os
from pathlib import Path

from labbridge.evidence.campaign_package import (
    BuiltCampaignExperimentPackage,
    CampaignPackageVerification,
)
from labbridge.evidence.experiment_package import (
    ExperimentPackageVerificationError,
    verify_experiment_package,
)
from labbridge.infrastructure.objectstore import ObjectStore


def publish_verified_campaign_package(
    package: BuiltCampaignExperimentPackage,
    destination: Path,
    *,
    object_store: ObjectStore,
) -> CampaignPackageVerification:
    """Verify before exposure and publish by one filesystem replacement."""
    verification = verify_experiment_package(package.archive_bytes, object_store=object_store)
    if not isinstance(verification, CampaignPackageVerification):
        raise ExperimentPackageVerificationError(
            "package_producer_mismatch", "campaign export produced a non-campaign Package"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.read_bytes() != package.archive_bytes:
            raise ExperimentPackageVerificationError(
                "package_release_immutable",
                "a released campaign Package is immutable and cannot be overwritten",
            )
        return verification

    partial = destination.with_suffix(destination.suffix + ".partial")
    with partial.open("wb") as handle:
        handle.write(package.archive_bytes)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, destination)
    published = destination.read_bytes()
    return verify_experiment_package(published, object_store=object_store)  # type: ignore[return-value]


__all__ = ["publish_verified_campaign_package"]

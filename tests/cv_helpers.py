from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from labbridge.application.cv_ingestion import CVIdempotencyConflictError, CVRecordRepository
from labbridge.application.source_intake import RetrievedSource
from labbridge.domain.cv import (
    ColumnMapping,
    CVImportProfile,
    CVMetadata,
    MetadataValue,
    import_profile_id,
)
from labbridge.domain.cv_observations import NormalisationResult
from labbridge.domain.parser_diagnostics import ParserRecord
from labbridge.domain.source_artifacts import SourceArtifact, source_artifact_id

CV_PAYLOAD = b"sample_index,channel_a,channel_b\n0,-0.240,0.012\n1,0.120,-0.031\n"
GAMRY_DTA_PAYLOAD = (
    b"EXPLAIN\r\n"
    b"TAG\tCV\r\n"
    b"TITLE\tLABEL\tSynthetic CV\tTest Identifier\r\n"
    b"FRAMEWORKVERSION\tQUANT\t7.07\tFramework Version\r\n"
    b"NOTES\tNOTES\t0\tSynthetic fixture\r\n"
    b"CURVE\tTABLE\t2\r\n"
    b"\tPt\tT\tVf\tIm\tVu\tSig\tAch\tIERange\tOver\tCycle\tTemp\r\n"
    b"\t#\ts\tV vs. Ref.\tA\tV\tV\tV\t#\tbits\t#\tdeg C\r\n"
    b"\t0\t0.00\t-0.240\t0.012\t0\t-0.24\t0\t9\t..........\t0\t25.0\r\n"
    b"\t1\t0.10\t0.120\t-0.031\t0\t0.12\t0\t9\t..........\t1\t25.0\r\n"
)


def cv_profile() -> CVImportProfile:
    return CVImportProfile(
        schema_version="1",
        technique="cyclic_voltammetry",
        environment_id="synthetic_cv_fixture",
        encoding="utf-8",
        delimiter=",",
        decimal_convention="point",
        header_row=1,
        missing_value_tokens=("", "NA"),
        columns=(
            ColumnMapping(source_column="sample_index", role="ignored"),
            ColumnMapping(
                source_column="channel_a", role="potential", source_unit="V", target_unit="V"
            ),
            ColumnMapping(
                source_column="channel_b", role="current", source_unit="A", target_unit="A"
            ),
        ),
        metadata=CVMetadata(
            reference_scale=MetadataValue(state="unknown"),
            potential_treatment=MetadataValue(state="unknown"),
            current_basis=MetadataValue(state="known", value="current"),
            electrode_role=MetadataValue(state="unknown"),
            geometric_area=MetadataValue(state="unavailable"),
            contact_area=MetadataValue(state="not_applicable"),
            scan_rate=MetadataValue(state="unknown"),
            cycle_information=MetadataValue(state="unavailable"),
        ),
    )


def cv_source() -> RetrievedSource:
    digest = hashlib.sha256(CV_PAYLOAD).hexdigest()
    artifact = SourceArtifact(
        source_artifact_id=source_artifact_id(
            sha256=digest, byte_size=len(CV_PAYLOAD), media_type="text/csv"
        ),
        filename="synthetic-replay-cv-opaque.csv",
        media_type="text/csv",
        byte_size=len(CV_PAYLOAD),
        sha256=digest,
        data_origin="synthetic",
        execution_mode="replay",
        state="committed",
        object_uri=f"s3://test/{digest}",
        created_at=datetime(2026, 8, 12, tzinfo=UTC),
        committed_at=datetime(2026, 8, 12, tzinfo=UTC),
    )
    return RetrievedSource(artifact=artifact, data=CV_PAYLOAD)


def gamry_dta_profile() -> CVImportProfile:
    return CVImportProfile(
        schema_version="1",
        technique="cyclic_voltammetry",
        environment_id="synthetic_gamry_cv_fixture",
        encoding="utf-8",
        delimiter="\t",
        decimal_convention="point",
        header_row=7,
        missing_value_tokens=("",),
        columns=(
            ColumnMapping(source_column="Pt", role="ignored"),
            ColumnMapping(source_column="T", role="time", source_unit="s", target_unit="s"),
            ColumnMapping(
                source_column="Vf",
                role="potential",
                source_unit="V vs. Ref.",
                target_unit="V",
            ),
            ColumnMapping(source_column="Im", role="current", source_unit="A", target_unit="A"),
            ColumnMapping(source_column="Vu", role="ignored"),
            ColumnMapping(source_column="Sig", role="ignored"),
            ColumnMapping(source_column="Ach", role="ignored"),
            ColumnMapping(source_column="IERange", role="ignored"),
            ColumnMapping(source_column="Over", role="ignored"),
            ColumnMapping(source_column="Cycle", role="cycle", source_unit="#", target_unit="1"),
            ColumnMapping(source_column="Temp", role="ignored"),
        ),
        metadata=CVMetadata.unknown(),
    )


def gamry_dta_source() -> RetrievedSource:
    digest = hashlib.sha256(GAMRY_DTA_PAYLOAD).hexdigest()
    artifact = SourceArtifact(
        source_artifact_id=source_artifact_id(
            sha256=digest,
            byte_size=len(GAMRY_DTA_PAYLOAD),
            media_type="application/vnd.gamry.dta",
        ),
        filename="synthetic-gamry-cv.dta",
        media_type="application/vnd.gamry.dta",
        byte_size=len(GAMRY_DTA_PAYLOAD),
        sha256=digest,
        data_origin="synthetic",
        execution_mode="replay",
        state="committed",
        object_uri=f"s3://test/{digest}",
        created_at=datetime(2026, 8, 12, tzinfo=UTC),
        committed_at=datetime(2026, 8, 12, tzinfo=UTC),
    )
    return RetrievedSource(artifact=artifact, data=GAMRY_DTA_PAYLOAD)


class MemoryCVRecords(CVRecordRepository):
    def __init__(self) -> None:
        self.profiles: dict[str, CVImportProfile] = {}
        self.results: dict[str, NormalisationResult] = {}
        self.idempotency: dict[tuple[str, str], str] = {}
        self.parser_records: dict[str, ParserRecord] = {}

    def _reserve(self, scope: str, key: str | None, identity: str) -> bool:
        if key is None:
            return False
        existing = self.idempotency.get((scope, key))
        if existing is not None and existing != identity:
            raise CVIdempotencyConflictError(key)
        self.idempotency[(scope, key)] = identity
        return existing is not None

    def put_profile(
        self, item: CVImportProfile, *, idempotency_key: str | None = None
    ) -> tuple[str, bool]:
        identity = import_profile_id(item)
        replayed = self._reserve("profile", idempotency_key, identity) or identity in self.profiles
        self.profiles.setdefault(identity, item)
        return identity, replayed

    def get_profile(self, profile_id: str) -> CVImportProfile | None:
        return self.profiles.get(profile_id)

    def put_normalisation(
        self, result: NormalisationResult, *, idempotency_key: str | None = None
    ) -> bool:
        identity = result.observation.observation_id
        replayed = self._reserve("normalise", idempotency_key, identity) or identity in self.results
        self.results.setdefault(identity, result)
        if result.parser_record is not None:
            self.parser_records.setdefault(
                result.parser_record.parser_record_id, result.parser_record
            )
        return replayed

    def get_normalisation(self, observation_id: str) -> NormalisationResult | None:
        return self.results.get(observation_id)

    def put_parser_record(self, record: ParserRecord) -> bool:
        replayed = record.parser_record_id in self.parser_records
        self.parser_records.setdefault(record.parser_record_id, record)
        return replayed

    def get_parser_record(self, parser_record_id: str) -> ParserRecord | None:
        return self.parser_records.get(parser_record_id)


class FixedSourceReader:
    def retrieve(self, source_artifact_id: str) -> RetrievedSource:
        retained = cv_source()
        assert source_artifact_id == retained.artifact.source_artifact_id
        return retained


class FixedGamrySourceReader:
    def retrieve(self, source_artifact_id: str) -> RetrievedSource:
        retained = gamry_dta_source()
        assert source_artifact_id == retained.artifact.source_artifact_id
        return retained

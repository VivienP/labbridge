from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from labbridge.application.cv_ingestion import (
    CVIngestionService,
    ParserRecordNotFoundError,
    normalise_cv,
)
from labbridge.application.source_intake import RetrievedSource
from labbridge.domain.cv import ColumnMapping, CVImportProfile, CVMetadata, import_profile_id
from labbridge.domain.cv_observations import NormalisationResult
from labbridge.domain.parser_diagnostics import ParserRecord
from labbridge.domain.source_artifacts import SourceArtifact, source_artifact_id
from labbridge.infrastructure.gamry_dta import GamryDtaParseError

DTA_PAYLOAD = (
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
CSV_PAYLOAD = b"potential,current,time,cycle\n-0.240,0.012,0.00,0\n0.120,-0.031,0.10,1\n"


def _source(data: bytes, filename: str, media_type: str) -> RetrievedSource:
    digest = hashlib.sha256(data).hexdigest()
    artifact = SourceArtifact(
        source_artifact_id=source_artifact_id(
            sha256=digest, byte_size=len(data), media_type=media_type
        ),
        filename=filename,
        media_type=media_type,
        byte_size=len(data),
        sha256=digest,
        data_origin="synthetic",
        execution_mode="replay",
        state="committed",
        object_uri=f"s3://test/{digest}",
        created_at=datetime(2026, 8, 12, tzinfo=UTC),
        committed_at=datetime(2026, 8, 12, tzinfo=UTC),
    )
    return RetrievedSource(artifact=artifact, data=data)


def _profile(*, dta: bool) -> CVImportProfile:
    columns = (
        (
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
        )
        if dta
        else (
            ColumnMapping(
                source_column="potential", role="potential", source_unit="V", target_unit="V"
            ),
            ColumnMapping(
                source_column="current", role="current", source_unit="A", target_unit="A"
            ),
            ColumnMapping(source_column="time", role="time", source_unit="s", target_unit="s"),
            ColumnMapping(source_column="cycle", role="cycle", source_unit="1", target_unit="1"),
        )
    )
    return CVImportProfile(
        schema_version="1",
        technique="cyclic_voltammetry",
        environment_id="equivalent_cv_fixture",
        encoding="utf-8",
        delimiter="\t" if dta else ",",
        decimal_convention="point",
        header_row=7 if dta else 1,
        missing_value_tokens=("",),
        columns=columns,
        metadata=CVMetadata.unknown(),
    )


def test_dta_and_generic_csv_converge_on_the_same_common_cv_representation() -> None:
    dta = normalise_cv(
        _source(DTA_PAYLOAD, "synthetic.dta", "application/vnd.gamry.dta"),
        _profile(dta=True),
        producing_version="0.1.0",
        source_format="gamry_dta",
    )
    csv = normalise_cv(
        _source(CSV_PAYLOAD, "synthetic.csv", "text/csv"),
        _profile(dta=False),
        producing_version="0.1.0",
        source_format="generic_csv",
    )

    assert dta.parser_record is not None
    assert dta.parser_record.status == "accepted"
    assert dta.graph.records[0].kind == "dta_parse"
    assert dta.observation.provenance.parser_record_id == dta.parser_record.parser_record_id
    assert [(item.role, item.unit, item.values) for item in dta.observation.series] == [
        (item.role, item.unit, item.values) for item in csv.observation.series
    ]
    assert dta.observation.metadata == csv.observation.metadata


@dataclass
class _Sources:
    source: RetrievedSource

    def retrieve(self, source_artifact_id: str) -> RetrievedSource:
        assert source_artifact_id == self.source.artifact.source_artifact_id
        return self.source


class _Records:
    def __init__(self, profile: CVImportProfile) -> None:
        self.profile = profile
        self.parser_records: dict[str, ParserRecord] = {}

    def put_profile(
        self, item: CVImportProfile, *, idempotency_key: str | None = None
    ) -> tuple[str, bool]:
        del idempotency_key
        self.profile = item
        return import_profile_id(item), False

    def get_profile(self, profile_id: str) -> CVImportProfile | None:
        return self.profile if profile_id == import_profile_id(self.profile) else None

    def put_normalisation(
        self, result: NormalisationResult, *, idempotency_key: str | None = None
    ) -> bool:
        del result, idempotency_key
        return False

    def get_normalisation(self, observation_id: str) -> NormalisationResult | None:
        del observation_id
        return None

    def put_parser_record(self, record: ParserRecord) -> bool:
        replayed = record.parser_record_id in self.parser_records
        self.parser_records.setdefault(record.parser_record_id, record)
        return replayed

    def get_parser_record(self, parser_record_id: str) -> ParserRecord | None:
        return self.parser_records.get(parser_record_id)


@pytest.mark.parametrize(
    "payload,error_code",
    [
        (
            DTA_PAYLOAD.replace(b"7.07", b"7.08"),
            "dta_unsupported_framework_version",
        ),
        (
            DTA_PAYLOAD.replace(b"\tIm\t", b"\tVf\t"),
            "dta_unsupported_table_schema",
        ),
    ],
)
def test_rejected_dta_retains_and_retrieves_its_diagnostic_record(
    payload: bytes, error_code: str
) -> None:
    source = _source(payload, "rejected.dta", "application/vnd.gamry.dta")
    records = _Records(_profile(dta=True))
    service = CVIngestionService(_Sources(source), records, producing_version="0.1.0")

    with pytest.raises(GamryDtaParseError) as caught:
        service.normalise(
            source.artifact.source_artifact_id,
            import_profile_id(records.profile),
            source_format="gamry_dta",
        )

    stored = service.get_parser_record(caught.value.parser_record_id)
    assert stored.record.status == "rejected"
    assert stored.record.diagnostics[-1].code == error_code
    with pytest.raises(ParserRecordNotFoundError):
        service.get_parser_record("parser-record:missing")

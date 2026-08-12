from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from labbridge.domain.cv import (
    ColumnMapping,
    CVImportProfile,
    CVMetadata,
    MetadataValue,
    import_profile_id,
)


def _metadata() -> CVMetadata:
    return CVMetadata(
        reference_scale=MetadataValue(state="unknown"),
        potential_treatment=MetadataValue(state="known", value="applied"),
        current_basis=MetadataValue(state="known", value="current"),
        electrode_role=MetadataValue(state="known", value="working"),
        geometric_area=MetadataValue(state="unavailable"),
        contact_area=MetadataValue(state="not_applicable"),
        scan_rate=MetadataValue(state="known", value=Decimal("50"), unit="mV/s"),
        cycle_information=MetadataValue(state="known", value="source_column:cycle"),
    )


def _profile(columns: tuple[ColumnMapping, ...] | None = None) -> CVImportProfile:
    return CVImportProfile(
        schema_version="1",
        technique="cyclic_voltammetry",
        environment_id="synthetic_cv_fixture",
        encoding="utf-8",
        delimiter=",",
        decimal_convention="point",
        header_row=1,
        missing_value_tokens=("", "NA"),
        columns=columns
        or (
            ColumnMapping(source_column="cycle", role="cycle", source_unit="1", target_unit="1"),
            ColumnMapping(
                source_column="potential_mV",
                role="potential",
                source_unit="mV",
                target_unit="V",
            ),
            ColumnMapping(
                source_column="current_mA",
                role="current",
                source_unit="mA",
                target_unit="A",
            ),
            ColumnMapping(source_column="note", role="ignored"),
        ),
        metadata=_metadata(),
    )


def test_profile_identity_is_stable_under_column_mapping_order() -> None:
    profile = _profile()
    reordered = _profile(tuple(reversed(profile.columns)))

    assert import_profile_id(profile) == import_profile_id(reordered)


@given(st.sampled_from(["mV", "V", "kV"]))
def test_changing_a_unit_changes_the_profile_identity(source_unit: str) -> None:
    profile = _profile()
    changed = profile.model_copy(
        update={
            "columns": tuple(
                item.model_copy(update={"source_unit": source_unit})
                if item.role == "potential"
                else item
                for item in profile.columns
            )
        }
    )

    if source_unit == "mV":
        assert import_profile_id(changed) == import_profile_id(profile)
    else:
        assert import_profile_id(changed) != import_profile_id(profile)


@pytest.mark.parametrize(
    "columns,match",
    [
        (
            (
                ColumnMapping(
                    source_column="potential", role="potential", source_unit="V", target_unit="V"
                ),
            ),
            "current",
        ),
        (
            (
                ColumnMapping(
                    source_column="potential", role="potential", source_unit="V", target_unit="V"
                ),
                ColumnMapping(
                    source_column="potential2", role="potential", source_unit="V", target_unit="V"
                ),
                ColumnMapping(
                    source_column="current", role="current", source_unit="A", target_unit="A"
                ),
            ),
            "exactly one potential",
        ),
        (
            (
                ColumnMapping(
                    source_column="potential", role="potential", source_unit="V", target_unit="V"
                ),
                ColumnMapping(
                    source_column="current", role="current", source_unit="A", target_unit="A"
                ),
                ColumnMapping(
                    source_column="density",
                    role="current_density",
                    source_unit="A/m^2",
                    target_unit="A/m^2",
                ),
            ),
            "current or current_density",
        ),
    ],
)
def test_profile_requires_the_minimum_cv_axes(
    columns: tuple[ColumnMapping, ...], match: str
) -> None:
    with pytest.raises(ValidationError, match=match):
        _profile(columns)


def test_every_source_column_is_explicit_even_when_ignored() -> None:
    with pytest.raises(ValidationError, match="ignored column"):
        ColumnMapping(
            source_column="note", role="ignored", source_unit="unknown", target_unit="unknown"
        )


def test_scientific_mapping_requires_both_source_and_target_units() -> None:
    with pytest.raises(ValidationError, match="source_unit and target_unit"):
        ColumnMapping(source_column="potential", role="potential")


def test_metadata_states_do_not_silently_carry_or_drop_values() -> None:
    with pytest.raises(ValidationError, match="known metadata requires a value"):
        MetadataValue(state="known")
    with pytest.raises(ValidationError, match="unknown metadata carries no value"):
        MetadataValue(state="unknown", value="RHE")


def test_known_numeric_metadata_requires_an_explicit_unit() -> None:
    with pytest.raises(ValidationError, match="numeric metadata requires an explicit unit"):
        MetadataValue(state="known", value=Decimal("50"))


def test_decimal_comma_cannot_share_the_comma_delimiter() -> None:
    with pytest.raises(ValidationError, match="decimal comma"):
        _profile().model_copy(update={"decimal_convention": "comma"}, deep=True).model_validate(
            {
                **_profile().model_dump(mode="python"),
                "decimal_convention": "comma",
            }
        )

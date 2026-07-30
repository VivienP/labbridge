"""Recorded data-use decisions: what LabBridge may redistribute from a source record.

A licence string on a record is evidence. It is not a decision. docs/DATA_STRATEGY.md section 2.3
requires the decision to be confirmed, dated, and recorded in an architecture decision before
anything archive-derived may be committed. That separation is enforced structurally: `parse_record`
always returns `unresolved`, and only a decision recorded in this module can widen it.

A decision is pinned to the DOI and the licence identifier it was made against. If the record stops
declaring that identifier — an upstream relicensing, or a metadata revision — the decision no longer
applies and the gate reopens on its own, rather than a stale decision outliving its evidence.

LabBridge's own source licence is unrelated to this. Making LabBridge open source grants nothing
over a third party's dataset; only the dataset's own terms do.
"""

from __future__ import annotations

from datetime import date
from typing import Final

from .records import PINNED_DOI, DataUseDecision, LicenceStatus

#: ADR-009. Read from the Zenodo REST API on 2026-07-30: `metadata.license.id == "cc-by-4.0"` and
#: `access == "open"`. CC BY 4.0 permits redistribution of the material and of adapted material,
#: provided attribution is given and changes are indicated.
HER_DATA_USE: Final = DataUseDecision(
    adr="ADR-009",
    doi=PINNED_DOI,
    licence_id="cc-by-4.0",
    verified_on=date(2026, 7, 30),
    verified_from="https://zenodo.org/api/records/20439519",
    redistribution="permitted_with_attribution",
    attribution=(
        "Thelen F, Kim M, Arruda de Oliveira G, Bürgel JL, Schuhmann W, Ludwig A. "
        "Dataset — Autonomous scanning electrochemical cell microscopy enables rapid exploration "
        "of large compositionally complex material spaces. Zenodo, 2026. "
        "doi:10.5281/zenodo.20439519. Licensed CC BY 4.0. "
        "Any changes made by LabBridge are indicated on the artifact that carries them."
    ),
)


def resolve_redistribution(
    licence: LicenceStatus, *, doi: str, decision: DataUseDecision
) -> LicenceStatus:
    """Apply a recorded decision to a declared licence, or leave the gate open.

    Both the DOI and the declared licence identifier must match the decision. A record that no
    longer declares the licence the decision was made against keeps `unresolved`, which is what
    stops a silent upstream relicensing from being ignored.
    """
    if doi != decision.doi or licence.raw_value != decision.licence_id:
        return licence
    return licence.model_copy(update={"redistribution": decision.redistribution})

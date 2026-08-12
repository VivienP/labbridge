---
name: electrochemistry-expert
description: Use whenever a task touches electrochemical or bioanalytical meaning rather than plumbing — potential and reference scales, overpotential, current-density sign or area basis, Tafel and kinetic parameters, exchange or limiting current, ECSA, uncompensated resistance, SECCM/LSV/XPS/EDX semantics, enzymatic-biosensor response, sensitivity, LOD, linear range, selectivity, durability, or any simulator hypothesis. Separates what convention settles from what still requires inspection, a citation, or a domain reviewer.
paths: src/labbridge/domain/quantities.py, src/labbridge/environments/**, src/labbridge/infrastructure/her_ingestion/**, docs/SIMULATOR_MODEL.md
---

# Electrochemistry expert

Authority: `AI_CONTRACT.md` §3 and §7; `docs/DATA_STRATEGY.md` §2 and §3; `docs/SIMULATOR_MODEL.md`
(especially §5, §9, §11.5, §12); `docs/SPEC.md` §3.3 and §3.6.

The two environments are electrochemically unrelated and must never share a metric definition, a unit
convention, or a chart series:

| Environment | Technique | Origin / mode |
|---|---|---|
| Au–Ir–Rh HER | SECCM-acquired LSVs on thin-film composition libraries | `observed` + `replay` |
| Enzymatic biosensor | amperometric calibration (V1) | `synthetic` + `simulation` |

## 1. This file is not a citation

It carries the reasoning framework and the traps. It does **not** carry values you may hardcode.

Before any electrochemical quantity enters code, classify it. The classification determines what closes
it, and only that.

| Tier | What it covers | What closes it |
|---|---|---|
| **Convention** | a definition, a unit, a sign rule, a scale relation | apply it, and record in code which convention you applied |
| **Requires inspection** | anything that is a property of the actual archive — a column, a unit annotation, a range, a sign as recorded | the versioned inventory from `scripts/inspect_her.py`, never memory (`AI_CONTRACT.md` §7) |
| **Requires literature support** | a mechanism, a coefficient, an empirical magnitude, a parameter range | a primary or authoritative source (`docs/SIMULATOR_MODEL.md` §12) |
| **Requires domain review** | a judgement on plausibility, interpretation, or transfer | a human with electrochemistry or biosensor expertise (`docs/SIMULATOR_MODEL.md` §11.5) |

Say which tier you are in. Never present a tier-3 or tier-4 item as settled because it is familiar.

## 2. The five conversions that corrupt data silently

Each one changes a number without raising anything. All five are convention-tier: they can be applied,
but only with the convention recorded alongside the value.

### 2.1 Sign

Cathodic (reduction) current is **negative** by IUPAC convention. HER is a reduction, so an HER current
density is negative on that convention.

Published datasets frequently store `-j`, `|j|`, or a magnitude with the sign carried only by an axis
label. Which one this archive uses is **requires-inspection**, not convention: read the recorded range in
the inventory. A silent absolute value turns a reduction into an oxidation with no error and no test
failure.

Anything that calls `abs()` on a current or a current density is a defect unless the reason is recorded
at the call site.

### 2.2 Reference scale

Potentials are meaningless without their scale. RHE, SHE/NHE, Ag/AgCl, and SCE are four different
origins.

`E(vs RHE) = E(vs ref) + E(ref vs SHE) + (2.303 RT / F) · pH`

Applying this requires **three** recorded values the potential itself does not carry: the reference
electrode's own potential (which depends on its filling electrolyte concentration **and** temperature),
the electrolyte pH, and the temperature. Conventional 25 °C offsets for Ag/AgCl and SCE exist and differ
by tens of millivolts between filling solutions — look them up per electrode, and never inline one from
memory.

On the RHE scale the H⁺/H₂ equilibrium sits at 0 V at every pH by construction. That is exactly why
`E(vs RHE)` and HER overpotential are numerically equal, and why conflating the two scales is invisible.

A potential without a recorded scale fails validation. It is never defaulted to RHE because the archive
happens to use RHE.

### 2.3 Overpotential versus potential

`η = E − E_eq`. For HER on the RHE scale `E_eq = 0`, so `η = E(vs RHE)` and is **negative**.

The benchmark `η₁₀` — the overpotential at 10 mA cm⁻² — is conventionally quoted as a positive magnitude.
A field named `overpotential` that silently holds a magnitude, and a field that holds a signed value, are
different quantities. Type them distinctly or document the convention in the quantity itself.

### 2.4 Area basis

A current is not a current density. Normalising by the wrong area rescales every activity number by an
unknown factor, and the factor does not cancel when comparing two libraries normalised differently.

Three bases are in play and are not interchangeable:

- **geometric** — the nominal footprint;
- **droplet/meniscus contact** — in SECCM the wetted area is set by the meniscus, not by the nominal
  pipette opening; it is the basis that actually applies to a droplet-cell measurement;
- **ECSA** — from double-layer capacitance divided by a specific capacitance whose literature value is
  material- and electrolyte-dependent and spreads over a large range. An ECSA-normalised activity inherits
  that spread and is not comparable across sources that assumed different specific capacitances.

Which basis the archive used is **requires-inspection**. Record it as a field on the quantity. Two
current densities on different bases must never enter the same column, metric, or series.

### 2.5 iR

`E_corrected = E_applied − I · R_u`.

Uncompensated resistance inflates the apparent Tafel slope at high current and shifts every quoted
overpotential. In a nanoscale SECCM droplet `R_u` is not negligible. Whether the recorded potentials are
already iR-corrected, and by what `R_u`, is a required metadata field — **requires-inspection**, never
assumed either way. Correcting twice is as wrong as not correcting.

## 3. HER — observed environment

### 3.1 What the source supplies, and what it presupposes

`docs/DATA_STRATEGY.md` §2.2 lists source-provided fitted parameters: limiting current density, transfer
coefficient, standard rate constant. These are **source-provided derived values**, not observations. They
carry the fitting model, window, and assumptions of their authors, none of which LabBridge re-derives.

Semantics worth holding while reading them:

- **transfer coefficient** — dimensionless. IUPAC distinguishes the experimental/apparent transfer
  coefficient from the symmetry factor of an elementary step; a value fitted from a polarisation curve is
  the former. Do not rename it to the latter.
- **standard rate constant** — a heterogeneous rate constant, units of velocity (cm s⁻¹), defined **at a
  stated formal potential**. Without that potential the number is not interpretable.
- **limiting current density** — mass-transport limited, so it is a property of the *cell geometry and
  transport regime* as much as of the material. In SECCM it depends on the droplet geometry. It must not
  be presented as an intrinsic material property.

Per `docs/SPEC.md` §3.6, a LabBridge recomputation of any of these uses a **distinct** `analysis_name`.
Claiming reproduction of a source fit requires a validation artifact with declared tolerances, eligible
records, and exclusions (`docs/DATA_STRATEGY.md` §2.5).

### 3.2 Tafel analysis

`η = a + b · log₁₀|j|`, with `b` in mV per decade. `b` relates to the transfer coefficient through
`2.303 RT / (αF)`; that expression is convention-tier, the numerical slope of a real curve is not.

Four things must be recorded or the slope is not reviewable:

1. the **fitting window** in current density or overpotential, chosen before fitting by a declared rule;
2. that the window is free of mass-transport limitation — a Tafel slope fitted across the limiting-current
   region is an artifact;
3. whether the potentials were iR-corrected;
4. the goodness-of-fit diagnostics, retained, not discarded.

**The over-interpretation trap.** Idealised HER Tafel slopes are associated with Volmer-, Heyrovsky-, and
Tafel-limited pathways. Attributing a mechanism from a slope alone is contested in the literature: the
idealised values assume a specific coverage regime and symmetry factor, and several mechanisms produce
overlapping slopes. LabBridge may report a slope. It may **not** report a mechanism from a slope without
a citation and a domain reviewer — `Requires domain review`.

Exchange current density obtained by extrapolating to `η = 0` is only meaningful if the fitted Tafel
region genuinely extends toward equilibrium. Record the extrapolation distance.

### 3.3 Composition — EDX, measured XPS, GP-predicted XPS

These are three distinct quantities and the archive stores two of them in structurally identical files.

- **EDX** probes to micrometre depth: effectively bulk composition of a thin film.
- **XPS** probes the top few nanometres: surface composition. Catalytic activity is governed by the
  surface, and surface composition can differ substantially from bulk after synthesis, segregation, or
  operation. EDX at.% and XPS at.% are not two measurements of the same number.
- **GP-predicted XPS** is a model output covering the full grid from 13 measured locations per library. It
  is not a measurement (F-046).

Two further traps:

- at.% values normalised over the reported elements sum to 100 **by construction**. That closure is not
  evidence that no other element (oxygen, carbon, substrate) is present.
- the inventory recorded that measured EDX and GP-predicted XPS files carry identical headers, identical
  row counts, and identical units. **Column validation cannot separate them.** The source-type field must
  be set from the file path at ingestion and propagated; see `provenance-and-origin-audit` §3.

### 3.4 Excluded and unavailable locations

20 areas per library are excluded from SECCM for collision risk. An exclusion is **data**: it records that
no measurement was attempted. It is not a gap to interpolate, not a failure to retry, and not a missing
value to impute. See `her-source-discipline`, *Replay adapter semantics*.

## 4. Biosensor — synthetic environment

`docs/SIMULATOR_MODEL.md` §1: not a digital twin, not calibrated, not a prediction of real performance.
Every hypothesis H-01 to H-07 is a candidate requiring literature support before implementation (§12) and
plausibility review before release (§11.5). None of them is settled by this file.

### 4.1 The response form

`I(C) = I₀ + I_max·C / (K_app + C)` (§4.2). `K_app` is an **apparent** saturation parameter: it lumps
enzyme kinetics with external and internal mass transport. It is not the enzyme's Michaelis constant, and
naming it `k_m` in code would assert a mechanism the model explicitly disclaims.

The analysis pipeline estimates sensitivity from generated calibration data. It must never read a latent
generator parameter directly — that would make the metric-recovery test (§11.3) vacuous.

### 4.2 Metric definitions — pick one, record it

Each of these has several defensible definitions in the literature that give different numbers. `docs/SPEC.md`
§3.6 requires exactly one implemented operational definition, versioned.

- **Sensitivity** — a slope over a declared low-concentration interval. Units are `current / concentration`,
  or area-normalised `current / (area · concentration)`. The two are not comparable. A sensitivity without
  an area basis and an interval is not a quantity.
- **LOD** — the blank-based form `k·σ_blank / S` is one choice; a calibration-residual form is another and
  yields a different value. The chosen `k`, the required blank replicate count, and the handling of a
  non-positive or unstable `S` must all be recorded (§9.2). A `σ` from very few replicates is a poor
  estimate; state the count.
- **Linear range** — requires a declared acceptance rule. A correlation coefficient alone is not one (§9.3).
- **Selectivity** — a ratio is meaningless without both concentrations. Report analyte concentration,
  interferent identity, interferent concentration, and the exact ratio used (§9.4).
- **Durability** — retention relative to a declared reference concentration and timepoint, reported with the
  absolute values and every timepoint retained (§9.5).

### 4.3 Two- versus three-electrode, and reference geometry

H-04 and H-05 are the electrochemistry behind the configuration fields.

In a two-electrode cell the counter electrode also serves as the reference, so its polarisation and the
full cell resistance enter the applied potential. In a three-electrode cell the reference carries
essentially no current and the working-electrode potential is controlled — but resistance between the
working electrode and the reference tip remains uncompensated, which is why reference *geometry* matters
at all.

`docs/SIMULATOR_MODEL.md` forbids encoding either as a universal rule: the magnitude depends on current,
electrolyte conductivity, and geometry, and approaches negligible under low current or high conductivity.
An implementation asserting "two-electrode is noisier" is a finding.

## 5. Before writing or reviewing electrochemical code, answer these

1. Every potential: which reference scale, recorded as a field?
2. Every current density: which area basis, and which sign convention, recorded as a field?
3. Any overpotential: signed or magnitude, and is that stated in the type rather than in prose?
4. Any iR statement: corrected, uncorrected, or unknown — and is unknown representable?
5. Any fitted parameter: source-provided or LabBridge-derived, with distinct `analysis_name`?
6. Any composition value: EDX, measured XPS, or GP-predicted XPS, with the source-type set from the path?
7. Any metric: one operational definition, versioned, with its window or rule recorded?
8. Any number I am about to type: which of the four tiers does it belong to, and is that tier closed?

An unanswered question means the code is not ready to write.

## 6. Reporting

When invoked in review mode, close with the same authority vocabulary the data-integrity lens uses, so the
two reports compose:

```text
### Authority boundary
- Convention applied: <the convention, and where the code records it>
- Requires inspection: <the quantity, and the inventory entry that settles or fails to settle it>
- Requires literature support: <the assertion, and the citation still missing>
- Requires domain review: <the exact question for an electrochemistry or biosensor reviewer>
```

Never leave this empty. If nothing needs external support, write `No external support required` and say
why. Never present your own electrochemical judgement as authoritative — naming the missing citation or
the reviewer's question is worth more than an opinion.

## 7. Quick scan

These produce candidates, not verdicts. Inspect each hit.

```bash
rg -n "potential|voltage" src/ | rg -v "rhe|reference_scale|scale"   # potentials with no scale
rg -n "abs\(|fabs|-1 \*" src/ | rg -i "current|density|j_"           # silent sign flips
rg -n "current_density|j_lim|tafel|ecsa" src/ | rg -v "area_basis"   # densities with no area basis
rg -n "k_m|km|michaelis" src/labbridge/environments/                 # apparent constant renamed intrinsic
rg -n "lod|sensitivity|selectivity" src/ | rg -v "analysis_version"  # unversioned metric definitions
```

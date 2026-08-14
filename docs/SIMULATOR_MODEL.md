# LabBridge — enzymatic-biosensor simulator model

**Status:** `deferred`; scientific assumptions require literature review before implementation  
**Model class:** mechanistically informed phenomenological simulator  
**Primary purpose:** deterministic scientific-signal generation and controlled fault injection for runtime testing

This document defines the scientific and integrity boundaries of the LabBridge biosensor simulator. The simulator is not a digital twin, not a substitute for unpublished experimental data, and not evidence that a particular biosensor formulation will perform as generated.

Its output is always:

- `data_origin="synthetic"`;
- `execution_mode="simulation"`.

---

## 1. Intended use

The simulator supports:

1. reproducible end-to-end campaign tests;
2. controlled generation of valid, poor, noisy, drifting, and corrupted signals;
3. testing of metric derivation and scientific-quality validation;
4. demonstration that experimental setup variables can be represented explicitly rather than omitted;
5. communication of domain knowledge through transparent assumptions.

The simulator MUST NOT be used to claim:

- experimentally validated sensor performance;
- optimisation of a real unpublished formulation;
- calibrated prediction of sensitivity, LOD, selectivity, or durability;
- transfer to HER electrocatalysis;
- a validated mechanistic account of every included parameter.

---

## 2. Architectural decomposition

The model is split into five independent components.

```text
Typed biosensor configuration
          │
          ▼
SignalGenerator ───────────────▶ ideal signal
          │
          ▼
NoiseModel ────────────────────▶ noisy valid signal
          │
          ▼
BatchEffectModel ──────────────▶ batch-conditioned valid signal
          │
          ▼
FailureInjector ───────────────▶ successful, corrupted, failed, or timed-out adapter result
          │
          ▼
AnalysisPipeline ──────────────▶ sensitivity, LOD, linear range, selectivity, durability
```

The `FailureInjector` MUST NOT alter hidden scientific model parameters without recording the injected failure. A poor valid signal remains a successful observation; an instrument-like corruption is classified separately.

---

## 3. Configuration model

A candidate configuration SHOULD include the following typed fields.

### 3.1 Fabrication parameters

- `carbon_loading` — mass per electrode area;
- `carbon_material_class` — categorical, initially one supported class;
- `surfactant_identity` — controlled vocabulary;
- `surfactant_concentration` — amount per formulation volume or mass;
- `enzyme_loading` — optional in V1 if needed to make the signal model interpretable;
- `membrane_present`;
- `membrane_class`;
- `membrane_thickness_proxy`;
- `mediator_present` and mediator parameters only if explicitly modelled.

### 3.2 Measurement parameters

- `electrode_setup` — `two_electrode` or `three_electrode`;
- `reference_geometry` — typed distance or geometry proxy;
- `cleaning_protocol` — controlled identifier linked to a declared sequence;
- `immersion_time`;
- `measurement_mode` — amperometry, LSV, or a single explicitly chosen V1 mode;
- `applied_potential` or scan definition as required;
- `temperature` only if the model includes a documented effect.

### 3.3 Analytical protocol

- analyte concentration schedule;
- blank replicate count;
- calibration replicate count;
- interferent identities and concentrations;
- durability timepoints;
- analysis configuration version.

### 3.4 Execution context

- `batch_id`;
- seed;
- simulator version;
- failure scenario and injection point when enabled.

Every numerical field MUST have explicit units and a validated range. V1 SHOULD support one coherent protocol rather than many partially defined modes.

---

## 4. Ideal signal model

### 4.1 Recommended V1 output

V1 SHOULD generate an amperometric calibration experiment because sensitivity, LOD, linearity, selectivity, and durability can be derived from one coherent protocol.

A voltammetric mode MAY be added later, but the first implementation should not attempt to reproduce both LSV and amperometry unless one is required by a concrete test.

### 4.2 Baseline concentration response

A bounded saturating response may use a Michaelis–Menten-like phenomenological form:

\[
I_{ideal}(C) = I_0 + \frac{I_{max} C}{K_{app} + C}
\]

where:

- \(C\) is analyte concentration;
- \(I_0\) is baseline current;
- \(I_{max}\) is a configured response scale;
- \(K_{app}\) is an apparent saturation parameter.

This equation is an idealised response shape. It MUST NOT be described as a complete mechanistic enzyme-electrode model.

The low-concentration slope follows from the chosen parameterisation and provides an idealised sensitivity region. The analysis pipeline still estimates sensitivity from generated calibration data rather than copying a hidden parameter directly.

### 4.3 Parameter transforms

Fabrication and operating parameters MAY alter a small set of latent phenomenological quantities:

- effective response scale;
- apparent saturation concentration;
- baseline current;
- response time;
- interferent response coefficient;
- drift coefficient;
- retention coefficient;
- noise amplitude.

Each transform MUST be:

- explicit;
- versioned;
- bounded;
- documented with units or dimensionless interpretation;
- linked to a qualitative hypothesis;
- accompanied by a domain of validity;
- covered by a sensitivity test;
- labelled synthetic when its numerical magnitude is not empirically calibrated.

Avoid direct hard-coded formulas in which every input independently and monotonically improves or degrades a headline metric. Interactions and saturation are preferable where scientifically justified, but V1 SHOULD remain interpretable.

---

## 5. Model hypotheses and limits

The following are candidate hypotheses, not accepted universal laws.

### H-01 — carbon loading and effective response

**Hypothesis:** increasing carbon loading within a declared low-to-moderate range may increase effective electroactive area and response scale.

**Required counter-effect:** the model SHOULD permit saturation and MAY increase capacitive baseline or noise at excessive loading.

**Implementation constraint:** no globally monotonic “more carbon is always better” rule.

**Evidence required:** primary literature or authoritative electrochemical source supporting the qualitative mechanism for the chosen sensor class.

### H-02 — surfactant-dependent wetting and immobilisation

**Hypothesis:** surfactant identity and concentration may modify wetting, enzyme environment, dispersion, or retention.

**Required counter-effect:** high or incompatible concentrations may reduce enzyme activity or stability.

**Implementation constraint:** effects are surfactant-specific and concentration-dependent; “surfactant present” is insufficient as a universal binary benefit.

### H-03 — membrane transport trade-off

**Hypothesis:** a membrane-like diffusion barrier may reduce analyte and interferent transport, changing sensitivity, apparent range, response time, and selectivity.

**Required counter-effect:** stronger transport limitation may lower sensitivity and slow response.

**Implementation constraint:** selectivity improvement is conditional on relative permeabilities; it is not guaranteed.

### H-04 — electrode configuration and potential-control proxy

**Hypothesis:** two- and three-electrode arrangements can differ in potential control and systematic bias under load.

**Required counter-effect:** the effect depends on geometry, current, electrolyte resistance, and device construction.

**Implementation constraint:** do not encode “two-electrode always noisier” as a universal rule.

### H-05 — reference geometry and uncompensated-resistance proxy

**Hypothesis:** reference position or geometry may influence apparent potential through an uncompensated-resistance proxy.

**Required counter-effect:** the effect approaches negligible values under low current or high conductivity.

**Implementation constraint:** represent this as an explicit simplified proxy, not a full electrochemical-field solution.

### H-06 — cleaning and fouling balance

**Hypothesis:** a compatible cleaning protocol may reduce fouling or baseline carryover.

**Required counter-effect:** aggressive cleaning may damage an enzyme, coating, mediator, or membrane.

**Implementation constraint:** cleaning protocol is a controlled categorical model with documented assumptions, not an ordinal “cleaner is better” score.

### H-07 — immersion conditioning and degradation

**Hypothesis:** immersion may produce an initial conditioning phase followed by leaching, swelling, fouling, or activity loss.

**Required counter-effect:** short immersion MAY improve stability before long-term decline.

**Implementation constraint:** permit non-monotonic behaviour and declare the supported time interval.

---

## 6. Noise model

Noise MUST be seeded and separated from batch effects and injected failures.

A minimal V1 model MAY combine:

- additive baseline noise;
- proportional signal noise;
- correlated baseline drift across a sequence;
- replicate-level variation.

Conceptually:

\[
I_{obs}(C,t) = I_{ideal}(C) + \epsilon_{add} + \epsilon_{prop} I_{ideal}(C) + d(t)
\]

where all stochastic terms are generated from a declared seed and versioned distributions.

Requirements:

- distribution choices and parameters are documented;
- random streams are derived deterministically from campaign, candidate, batch, and replicate identifiers;
- no unseeded global random state;
- heavy-tailed or heteroscedastic noise is optional and clearly named;
- the noise model does not produce an execution failure by itself.

---

## 7. Batch-effect model

Batch effects represent reproducible between-batch variation, not arbitrary corruption.

A batch MAY modify:

- baseline offset;
- response scale;
- apparent saturation parameter;
- drift coefficient;
- noise scale.

Requirements:

- batch parameters are seeded and recorded in provenance;
- the same batch context is reproducible;
- batch effects remain visible in the synthetic ground-truth metadata;
- the analysis pipeline does not automatically remove them unless a declared correction method is applied;
- derived metrics retain the batch ID.

The model SHOULD include at least one test showing that naive pooling across batches changes an estimated metric or uncertainty.

---

## 8. Failure injector

The failure injector operates after or around signal generation and returns a structured adapter result.

### Supported V1 scenarios

- timeout before any bytes are returned;
- empty response;
- malformed metadata;
- array length mismatch;
- clipping or saturation of the recorded signal;
- sparse spikes;
- axis reversal or non-monotonic concentration schedule;
- missing or invalid units;
- large reference-drift proxy causing scientific validation failure;
- sample or candidate collision;
- duplicate adapter delivery;
- delayed completion after the worker lease expires.

For any scenario in which bytes are returned, the bytes MUST be stored as an observation and classified. For a timeout or transport failure with no bytes, the attempt outcome contains no observation.

Failure probabilities are not intended to model real-world frequencies unless calibrated. Test campaigns SHOULD select scenarios explicitly rather than relying only on random occurrence.

---

## 9. Analysis pipeline and metric definitions

The analysis pipeline is versioned separately from the signal generator.

### 9.1 Sensitivity

Recommended V1 definition:

- fit a declared linear model over a pre-specified low-concentration interval;
- report slope and units;
- retain fit diagnostics;
- reject or warn when minimum point count or fit-quality criteria fail.

The interval MUST be configured before analysis or selected by a documented deterministic rule.

### 9.2 Limit of detection

Select exactly one V1 definition. A recommended blank-based form is:

\[
LOD = \frac{k\sigma_{blank}}{S}
\]

where:

- \(S\) is the estimated sensitivity;
- \(\sigma_{blank}\) is the standard deviation of blank replicates;
- \(k\) is a declared factor, commonly chosen according to the adopted protocol.

The project MUST document the chosen `k`, blank replicate requirements, units, and handling of non-positive or unstable sensitivity. It MUST NOT state that this is the only valid LOD definition.

### 9.3 Linear range

Recommended V1 definition:

- the largest contiguous concentration interval starting from the declared low end that meets a configured deviation or fit-quality criterion;
- report the rule and selected interval;
- do not infer linearity from correlation alone without a declared acceptance rule.

### 9.4 Selectivity

Recommended V1 definition:

- response to a declared interferent challenge relative to analyte response or baseline;
- include analyte and interferent concentrations;
- report the exact ratio or percentage error used.

A single synthetic scalar called “selectivity” without protocol context is forbidden.

### 9.5 Durability

Recommended V1 definition:

- retained sensitivity or retained response at a declared reference concentration after a configured immersion time;
- report relative retention and absolute values;
- preserve all timepoints used.

### 9.6 Scientific-quality validation

Validation MAY classify an observation as warning or rejected when:

- concentration axis is invalid;
- required blanks are absent;
- signal is clipped;
- fit is numerically unstable;
- sensitivity is non-positive where the protocol requires positive response;
- units are missing or incompatible;
- replicate count is insufficient.

A rejected metric does not delete the observation.

---

## 10. Determinism and provenance

A synthetic observation MUST be reproducible from:

- simulator version;
- component-model versions;
- canonical candidate configuration;
- analytical protocol;
- batch context;
- seed;
- explicitly selected failure scenario.

Provenance MUST include hashes of these inputs. A test MUST regenerate the observation and compare its canonical content hash.

Changing only the analysis version MAY create new derived metrics without changing the observation identity.

---

## 11. Validation plan

### 11.1 Structural tests

- all quantities have valid units;
- concentration schedules are ordered and within declared ranges;
- hashes are stable;
- seeds reproduce outputs;
- different model versions change provenance;
- synthetic labels propagate to every artifact.

### 11.2 Qualitative sensitivity tests

For each accepted hypothesis, vary one parameter within its declared domain while holding the rest fixed and assert only the behaviour justified by the model design.

Tests MUST allow saturation or non-monotonic effects where documented. They MUST NOT encode promotional conclusions such as “parameter X improves the sensor.”

### 11.3 Metric recovery tests

Generate signals with known latent characteristics and verify that the analysis pipeline recovers metrics within declared numerical tolerances under low-noise conditions.

This validates implementation consistency, not empirical realism.

### 11.4 Fault-classification tests

Each failure scenario must match `FAILURE_MATRIX.md`, retain any received observation, and produce the expected retry or terminal classification.

### 11.5 Plausibility review

Before release, a reviewer with electrochemistry or biosensor expertise SHOULD examine:

- equations;
- units;
- parameter ranges;
- qualitative interactions;
- metric definitions;
- chart labels;
- limitations.

The review outcome and unresolved objections SHOULD be recorded.

---

## 12. Literature requirements

Before implementation, add primary or authoritative sources for:

- the chosen idealised enzymatic response form;
- the adopted LOD procedure;
- membrane transport and interference mechanisms for the chosen membrane class;
- surfactant effects for the chosen enzyme/electrode system;
- carbon-loading effects for the chosen carbon material;
- two- versus three-electrode potential-control considerations;
- uncompensated-resistance or reference-geometry proxy;
- cleaning and fouling mechanisms;
- immersion conditioning and degradation;
- plausible noise, drift, and batch-effect scales.

Each model component SHOULD include a short source note that states what the citation supports and what remains an arbitrary synthetic modelling choice.

---

## 13. Required report language

Every simulator report MUST include language equivalent to:

> These results are generated by a seeded phenomenological simulator for infrastructure testing. They are not measured data, are not calibrated to the author’s proprietary experiments, and should not be interpreted as predicted performance of a real biosensor formulation.

Charts MUST include “Synthetic” in the title or subtitle. Tables MUST expose `data_origin` and simulator version.

---

## 14. Deferred scientific depth

Deferred until publishable calibration data and a separate validation plan exist:

- mechanistic enzyme kinetics coupled to mass transport and electrode kinetics;
- finite-element transport or electric-field simulation;
- parameter inference from real experiments;
- validated transfer across enzyme, membrane, or electrode families;
- uncertainty calibration against measured outcomes;
- decision claims about optimal sensor fabrication.

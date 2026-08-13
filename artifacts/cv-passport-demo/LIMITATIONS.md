# Limitations

This artifact covers one local, single-user, synthetic + replay CV Passport workflow. The CSV,
plot, Passport, and Package are demonstration evidence, not measured electrochemistry.

The operator-supplied `RHE` value is retained as a `user_supplied` declaration. LabBridge does not
infer it from the CSV, validate it as physically correct, or convert the plotted potential to that
reference scale.

Capability status is `implemented`, not `demonstrated`. A recorded human electrochemistry domain
review must decide whether the missing reference scale is a blocker or warning and approve
consistent API, UI, Passport, Package, and artifact semantics. A separate unfamiliar-viewer
acceptance run must
record both 60-90 second completion and comprehension of the raw-to-Package chain plus completeness,
integrity, scientific validity, and reproducibility. Neither human evidence is present here.

The artifact does not establish scientific validity, data quality, experimental reproducibility,
journal readiness, production readiness, authentication, tenancy, collaboration, instrument
connectivity, or mobile support.

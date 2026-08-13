# EchemDB CV exchange limitations

This candidate validates one project-owned `synthetic + replay` CV package against EchemDB
metadata-schema 0.8.3 at commit `f48f583f83b1de9f5601d05dae5e5fcd1c25a3f0`, the
Frictionless Data Package 2.0 profile, and Frictionless 5.19.0. It makes no compatibility claim for
other EchemDB schema, Data Package profile, or Frictionless versions.

Required EchemDB values absent from the DTA bytes are supplied only by the explicit `user_supplied`
assertions in `exchange-profile.json`. Their trace qualifier states that they are not
source-declared. No inferred assertion is projected as source metadata. Semantic EchemDB categories
are marked `fixture_declaration` in the mapping report: they define the project-owned synthetic
fixture and are not independently established as properties of a physical system.

The adapter applies no potential-reference conversion, current normalisation, sign-convention
change, area calculation, electrolyte-composition interpretation, electrode-role assignment, scan
rate derivation, or cycle interpretation. The normalised `V`, `A`, `s`, and dimensionless series are
copied without numeric conversion. The source unit `V vs. Ref.` remains in the LabBridge companion;
the reference scale remains unknown. No literature-dependent scientific claim is made.

Unknown reference scale, potential treatment, current basis, electrode role, geometric area,
contact area, scan rate, and cycle information are omitted and listed in `mapping.json`. The
EchemDB figure type is an explicitly asserted lossy projection; LabBridge `data_origin` and
`execution_mode` remain independently represented in `labbridge-provenance.json`.

The capability status is `implemented`; this uncommitted candidate is not evidence of clean-checkout
demonstration, EchemDB ingestion, EchemDB publication, or production deployment.

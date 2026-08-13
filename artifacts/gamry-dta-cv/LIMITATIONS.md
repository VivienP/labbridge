# Gamry DTA CV ingestion limitations

This candidate artifact exercises one project-owned `synthetic + replay` fixture. It supports only
the variant listed in `SUPPORT.md`. Other Framework versions, techniques, table schemas, multiple or
mixed table objects, missing or extra rows, undeclared encodings or decimal conventions, and
ambiguous unit mappings fail closed with a retained parser record.

The parser does not infer a reference electrode or potential scale, convert a potential to RHE or
SHE, infer working-electrode area, normalise current to current density, interpret temperature or
instrument-range fields, repair truncated files, select among multiple curves, or claim scientific
validity. Unknown metadata remains unknown. The capability status is `implemented`; this uncommitted
candidate is not evidence of clean-checkout demonstration or production deployment.

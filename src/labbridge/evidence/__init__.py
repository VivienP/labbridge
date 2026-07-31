"""Evidence bundles: the checksummed, immutable record of what a campaign produced.

A released bundle is never mutated. A correction is a new bundle with an explicit relation to the
one it supersedes (`docs/SPEC.md` §4.3, ADR-006).
"""

from __future__ import annotations

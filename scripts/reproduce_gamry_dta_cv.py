"""Reproduce and verify the Phase 4 Gamry DTA CV ingestion candidate artifact."""

from __future__ import annotations

import argparse
from pathlib import Path

from labbridge import __version__
from labbridge.evidence.gamry_dta_cv import reproduce_gamry_dta_cv_artifact


def reproduce(output: Path) -> dict[str, object]:
    return reproduce_gamry_dta_cv_artifact(output, producing_version=__version__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("build/gamry-dta-cv"))
    args = parser.parse_args()
    manifest = reproduce(args.output)
    print(f"verified {manifest['artifact_kind']} {manifest['parser_record_id']} at {args.output}")


if __name__ == "__main__":
    main()

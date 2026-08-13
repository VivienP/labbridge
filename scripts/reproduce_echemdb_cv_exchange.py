"""Reproduce and verify the Phase 6 EchemDB-aligned CV exchange candidate."""

from __future__ import annotations

import argparse
from pathlib import Path

from labbridge import __version__
from labbridge.evidence.echemdb_cv_artifact import reproduce_echemdb_cv_exchange_artifact


def reproduce(output: Path) -> dict[str, object]:
    return reproduce_echemdb_cv_exchange_artifact(output, producing_version=__version__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("build/echemdb-cv-exchange"))
    args = parser.parse_args()
    manifest = reproduce(args.output)
    print(f"verified {manifest['artifact_kind']} {manifest['observation_id']} at {args.output}")


if __name__ == "__main__":
    main()

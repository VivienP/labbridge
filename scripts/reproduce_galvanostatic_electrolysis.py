"""Reproduce and verify the galvanostatic-electrolysis candidate artifact."""

from __future__ import annotations

import argparse
from pathlib import Path

from labbridge import __version__
from labbridge.evidence.galvanostatic_electrolysis import (
    reproduce_galvanostatic_electrolysis_artifact,
)


def reproduce(output: Path) -> dict[str, object]:
    return reproduce_galvanostatic_electrolysis_artifact(output, producing_version=__version__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("build/galvanostatic-electrolysis"))
    args = parser.parse_args()
    manifest = reproduce(args.output)
    print(f"verified {manifest['artifact_kind']} {manifest['package_id']} at {args.output}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Acquire files from the pinned HER Zenodo record.

The name and requirements of this script are fixed by docs/DATA_STRATEGY.md section 2.4. Data
fetching belongs in `scripts/`, never inside a scientific pure function (AI_CONTRACT.md section 8).

The implementation is `labbridge.infrastructure.her_ingestion`; this file only provides the
documented entry point. Requires the package importable: `pip install -e ".[dev]"`, or run with
`PYTHONPATH=src`.

This script exposes the one command it is named after, so its options parse directly:

    python scripts/fetch_her.py --record-id 20439519 --dry-run

The installed console script keeps the subcommand form docs/SPEC.md section 11.2 fixes:

    labbridge fetch-her --record-id 20439519 --dry-run

Both routes call the same function; neither holds any logic of its own.
"""

from __future__ import annotations

import typer

from labbridge.cli import fetch_her

if __name__ == "__main__":
    typer.run(fetch_her)

#!/usr/bin/env python3
"""Inspect the acquired HER archives and write the versioned dataset inventory.

The name and required contents of this script are fixed by docs/DATA_STRATEGY.md section 2.4.
Nothing in the inspector knows a column, table, or filename pattern in advance: it reads the bytes
and reports them, which is what AI_CONTRACT.md section 7 requires before any dataset-specific code
is written.

    python scripts/inspect_her.py --landing-root data/her/raw
"""

from __future__ import annotations

import typer

from labbridge.cli import inspect_her

if __name__ == "__main__":
    typer.run(inspect_her)

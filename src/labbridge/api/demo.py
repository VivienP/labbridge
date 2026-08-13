"""Production entry point for the local single-user CV Passport demonstration."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI

from .app import create_app


def create_demo_app(frontend_dir: Path | None = None) -> FastAPI:
    root = frontend_dir or Path(os.environ.get("LABBRIDGE_FRONTEND_DIR", "/app/frontend/dist"))
    return create_app(frontend_dir=root)


app = create_demo_app()


__all__ = ["app", "create_demo_app"]

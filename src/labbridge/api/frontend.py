"""Serve the bounded production frontend without adding application routing."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


def register_frontend(app: FastAPI, root: Path | None) -> None:
    """Expose a built Vite root while leaving unknown paths as HTTP 404 responses."""
    if root is None:
        return
    index = root / "index.html"
    if not index.is_file():
        return

    assets = root / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="frontend-assets")

    @app.get("/", include_in_schema=False)
    def frontend_index() -> FileResponse:
        return FileResponse(index)

    @app.get("/demo-fixtures/{fixture_name}", include_in_schema=False)
    def frontend_fixture(fixture_name: str) -> FileResponse:
        if Path(fixture_name).name != fixture_name:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        fixture = root / "demo-fixtures" / fixture_name
        if not fixture.is_file():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return FileResponse(fixture)


__all__ = ["register_frontend"]

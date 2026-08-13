from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from labbridge.api.app import create_app

OK = 200
NOT_FOUND = 404


def _frontend(root: Path) -> None:
    assets = root / "assets"
    fixtures = root / "demo-fixtures"
    assets.mkdir()
    fixtures.mkdir()
    (root / "index.html").write_text(
        '<!doctype html><script type="module" src="/assets/app.js"></script>',
        encoding="utf-8",
    )
    (assets / "app.js").write_text("window.labbridgeDemo = true", encoding="utf-8")
    (fixtures / "synthetic-demo.csv").write_bytes(b"x,y\n0,1\n")


def test_frontend_root_assets_and_fixture_are_served_locally(tmp_path: Path) -> None:
    _frontend(tmp_path)
    client = TestClient(create_app(frontend_dir=tmp_path))

    assert client.get("/").status_code == OK
    assert client.get("/assets/app.js").text == "window.labbridgeDemo = true"
    fixture = client.get("/demo-fixtures/synthetic-demo.csv")
    assert fixture.status_code == OK
    assert fixture.content == b"x,y\n0,1\n"
    assert client.get("/health").json()["api_version"] == "1"


def test_missing_frontend_build_leaves_api_available(tmp_path: Path) -> None:
    client = TestClient(create_app(frontend_dir=tmp_path))

    assert client.get("/").status_code == NOT_FOUND
    assert client.get("/health").status_code == OK


def test_frontend_adapter_does_not_add_a_catch_all_route(tmp_path: Path) -> None:
    _frontend(tmp_path)
    client = TestClient(create_app(frontend_dir=tmp_path))

    assert client.get("/not-an-api-or-asset").status_code == NOT_FOUND

from pathlib import Path

import yaml

from labbridge.api.demo import create_demo_app

ROOT = Path(__file__).parents[1]


def test_demo_app_serves_explicit_production_directory(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<h1>CV Passport</h1>", encoding="utf-8")

    app = create_demo_app(tmp_path)

    paths = {route.path for route in app.routes}
    assert "/" in paths
    assert "/health" in paths
    assert "/ready" in paths


def test_compose_demo_profile_is_one_service_entrypoint() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    app = compose["services"]["app"]

    assert compose["name"] == "${LABBRIDGE_COMPOSE_PROJECT:-labbridge-cv-passport-demo}"
    assert app["profiles"] == ["demo"]
    assert app["ports"] == ["8000:8000"]
    assert app["depends_on"] == {
        "demo-postgres": {"condition": "service_healthy"},
        "demo-minio": {"condition": "service_healthy"},
    }
    assert app["healthcheck"]["test"][-1] == "http://localhost:8000/ready"
    assert "ports" not in compose["services"]["demo-postgres"]
    assert "ports" not in compose["services"]["demo-minio"]
    assert compose["services"]["postgres"]["profiles"] == ["infrastructure"]
    assert compose["services"]["minio"]["profiles"] == ["infrastructure"]


def test_demo_image_builds_local_frontend_and_runs_migrations() -> None:
    dockerfile = (ROOT / "Dockerfile.demo").read_text(encoding="utf-8")
    entrypoint = (ROOT / "scripts" / "run_demo.sh").read_text(encoding="utf-8")

    assert "npm ci" in dockerfile
    assert "npm run build" in dockerfile
    assert "frontend/dist" in dockerfile
    assert "alembic upgrade head" in entrypoint
    assert "labbridge.api.demo:app" in entrypoint


def test_browser_proof_drives_cli_parity_against_the_stored_experiment() -> None:
    browser_test = (ROOT / "frontend/e2e/cv-passport-demo.spec.ts").read_text(encoding="utf-8")

    assert '"experiment", "validate"' in browser_test
    assert '"experiment", "passport-release"' in browser_test
    assert '"package", "create"' in browser_test
    assert "cliValidation.validation.findings" in browser_test
    assert "cliPassport.passport.passport_id" in browser_test
    assert "cliPackage.package.archive_sha256" in browser_test
    assert browser_test.index("const experimentResponse") > browser_test.index(
        "demo_profile_unavailable"
    )

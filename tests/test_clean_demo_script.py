from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_clean_demo_script_copies_git_visible_union_and_uses_exact_command() -> None:
    script = (ROOT / "scripts" / "check_clean_demo.ps1").read_text(encoding="utf-8")

    assert "ls-files --cached --others --exclude-standard" in script
    assert "docker compose --profile demo up -d --build --wait" in script
    assert "LABBRIDGE_COMPOSE_PROJECT" in script
    assert "LABBRIDGE_DEMO_CONTAINER" in script
    assert "npm ci" in script
    assert "npm run e2e" in script
    assert "docker compose --profile demo down --volumes --remove-orphans" in script


def test_clean_demo_script_guards_copy_and_teardown_targets() -> None:
    script = (ROOT / "scripts" / "check_clean_demo.ps1").read_text(encoding="utf-8")

    assert "StartsWith" in script
    assert "[System.StringComparison]::OrdinalIgnoreCase" in script
    assert "try {" in script
    assert "finally {" in script

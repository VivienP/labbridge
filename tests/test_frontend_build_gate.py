from pathlib import Path

import pytest
from scripts.check_frontend_build import BuildGateError, compare_builds, scan_runtime_assets


def _write(root: Path, name: str, data: bytes) -> None:
    destination = root / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)


def test_identical_build_trees_pass(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write(first, "index.html", b"<script src='/assets/app.js'></script>")
    _write(second, "index.html", b"<script src='/assets/app.js'></script>")

    assert compare_builds(first, second)[0]["name"] == "index.html"


def test_changed_build_bytes_fail_with_exact_path(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write(first, "assets/app.js", b"one")
    _write(second, "assets/app.js", b"two")

    with pytest.raises(BuildGateError, match=r"assets/app\.js"):
        compare_builds(first, second)


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("index.html", '<script src="https://cdn.example/app.js"></script>'),
        ("assets/app.css", "body{background:url(//cdn.example/image.png)}"),
        ("assets/app.js", 'fetch("http://remote.example/data")'),
    ],
)
def test_remote_runtime_asset_fails(name: str, content: str, tmp_path: Path) -> None:
    _write(tmp_path, name, content.encode())

    with pytest.raises(BuildGateError, match="remote runtime reference"):
        scan_runtime_assets(tmp_path)


def test_non_runtime_xml_namespace_string_is_allowed(tmp_path: Path) -> None:
    _write(tmp_path, "assets/app.js", b'const ns="http://www.w3.org/2000/svg"')

    scan_runtime_assets(tmp_path)

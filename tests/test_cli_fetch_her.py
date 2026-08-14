"""The CLI wiring is real: the command is registered, options parse, typed errors map to exit 1.

The transport is monkeypatched at `labbridge.cli._build_transport`, so no test here opens a socket.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer.main
from typer.testing import CliRunner

from helpers import SYNTHETIC_DOI, FakeTransport, build_file_entry, build_payload
from labbridge import cli
from labbridge.infrastructure.her_ingestion.errors import SourceUnavailableError
from labbridge.infrastructure.her_ingestion.provenance import INVENTORY_FILENAME

ALPHA = b"potential,current\n-0.1,1.5\n"
ALPHA_URL = "https://zenodo.example/api/files/alpha_table.csv"

runner = CliRunner()


@pytest.fixture
def fake_transport(monkeypatch: pytest.MonkeyPatch) -> FakeTransport:
    payload = build_payload(files=[build_file_entry("alpha_table.csv", ALPHA, url=ALPHA_URL)])
    transport = FakeTransport(payload=payload, blobs={ALPHA_URL: ALPHA})
    monkeypatch.setattr(cli, "_build_transport", lambda: transport)
    # The pinned DOI belongs to the real record; the synthetic payload declares its own.
    monkeypatch.setattr(cli, "EXPECTED_DOI", SYNTHETIC_DOI)
    return transport


def test_fetch_her_is_registered_and_documents_its_options() -> None:
    result = runner.invoke(cli.app, ["fetch-her", "--help"])
    assert result.exit_code == 0

    # Rich help truncates option names when COLUMNS is 80, which is the GitHub Actions default.
    # The registration itself is what the command must keep, so inspect the Click parameters.
    fetch = typer.main.get_command(cli.app).commands["fetch-her"]
    opts = {flag for param in fetch.params for flag in param.opts}
    for option in ("--dry-run", "--file", "--max-bytes", "--allow-large", "--landing-root"):
        assert option in opts, option


def test_dry_run_exits_zero_and_writes_only_the_inventory(
    fake_transport: FakeTransport, tmp_path: Path
) -> None:
    result = runner.invoke(
        cli.app,
        ["fetch-her", "--record-id", "9999999", "--landing-root", str(tmp_path), "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert fake_transport.stream_urls == []
    assert sorted(p.name for p in tmp_path.iterdir()) == [INVENTORY_FILENAME]


def test_a_real_fetch_downloads_the_named_file(
    fake_transport: FakeTransport, tmp_path: Path
) -> None:
    result = runner.invoke(
        cli.app,
        [
            "fetch-her",
            "--record-id",
            "9999999",
            "--landing-root",
            str(tmp_path),
            "--file",
            "alpha_table.csv",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "alpha_table.csv").read_bytes() == ALPHA


def test_an_unknown_requested_filename_exits_non_zero_with_a_readable_reason(
    fake_transport: FakeTransport, tmp_path: Path
) -> None:
    result = runner.invoke(
        cli.app,
        [
            "fetch-her",
            "--record-id",
            "9999999",
            "--landing-root",
            str(tmp_path),
            "--file",
            "alpha_tabel.csv",
        ],
    )

    assert result.exit_code == 1
    assert "alpha_tabel.csv" in result.output
    assert fake_transport.stream_urls == []


def test_a_transport_failure_is_reported_as_a_classified_error_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 404 must reach the operator as `source_unavailable`, not as an httpx stack trace."""

    class FailingTransport:
        def get_json(self, url: str) -> dict[str, object]:
            raise SourceUnavailableError(url=url, detail="NOT FOUND", status=404)

        def stream_to(self, url: str, sink: object) -> None:  # pragma: no cover - never reached
            raise AssertionError("stream_to must not be called")

    monkeypatch.setattr(cli, "_build_transport", FailingTransport)

    result = runner.invoke(
        cli.app,
        ["fetch-her", "--record-id", "999999999999", "--landing-root", str(tmp_path), "--dry-run"],
    )

    assert result.exit_code == 1
    assert "source_unavailable" in result.output
    assert "Traceback" not in result.output

"""Every committed `REPRODUCE.txt` command must be accepted by the CLI it names.

`labbridge validate-artifacts build/<name>` was published in two artifacts for several releases.
`validate-artifacts` takes no positional argument, so the documented reproduction exited 2. Parsing
each line against the real Typer application makes that class of drift a test failure.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import click
import pytest
import typer.main

from labbridge import cli

ROOT = Path(__file__).resolve().parents[2]
REPRODUCE_FILES = sorted(ROOT.glob("artifacts/*/REPRODUCE.txt"))


def _parse(argv: list[str]) -> None:
    """Resolve and bind a command line without running it.

    Appending `--help` would not work: Click prints help and exits 0 before reporting an extra
    argument, so a broken command line would look valid. `make_context` binds every parameter and
    raises `UsageError` instead.
    """
    command = typer.main.get_command(cli.app)
    context = click.Context(command, info_name="labbridge")
    args = list(argv)
    # `TyperGroup` does not subclass `click.Group`, so descend on the command mapping instead.
    while args and not args[0].startswith("-") and hasattr(command, "commands"):
        name = args[0]
        subcommand = command.commands.get(name)
        if subcommand is None:
            raise click.UsageError(f"no such command: {name}")
        command = subcommand
        args = args[1:]
        context = click.Context(command, parent=context, info_name=name)
    command.make_context(context.info_name, args, parent=context)


def _commands() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for path in REPRODUCE_FILES:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                found.append((path.parent.name, stripped))
    return found


def test_every_artifact_publishes_a_reproduction_command() -> None:
    assert REPRODUCE_FILES, "no committed artifact publishes a reproduction command"
    assert _commands()


@pytest.mark.parametrize(("artifact", "command"), _commands(), ids=lambda value: str(value)[:60])
def test_documented_command_is_accepted_by_its_cli(artifact: str, command: str) -> None:
    argv = shlex.split(command)
    if argv[0] != "labbridge":
        # `python scripts/reproduce_*.py` lines: assert the script exists rather than running it.
        assert argv[0] == "python", f"{artifact}: unexpected program {argv[0]}"
        assert (ROOT / argv[1]).is_file(), f"{artifact}: {argv[1]} does not exist"
        return

    try:
        _parse(argv[1:])
    except click.UsageError as error:
        pytest.fail(
            f"{artifact}: `{command}` is not accepted by the CLI — {error.format_message()}"
        )

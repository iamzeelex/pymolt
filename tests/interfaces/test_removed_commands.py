"""Removed commands keep their name for one release: they say what replaces them.

Hidden from --help, they accept any arguments and exit 2 (usage), so an old
script fails loudly with the new command instead of "No such command".
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from pymolt.interfaces.cli.commands import app

runner = CliRunner()


@pytest.mark.parametrize("argv, replacement", [
    (["doctor", "."], "pymolt status"),
    (["migrate", ".", "--write"], "pymolt plan"),
    (["codemods", ".", "-p", "flask", "--from", "1", "--to", "2"], "pymolt plan"),
    (["env", "hint", "."], "pymolt assess"),
    (["contract", "capture", "--when", "baseline"], "pymolt capture"),
    (["contract", "report", "."], "pymolt verify"),
    (["contract", "map", "."], "pymolt scan"),
])
def test_a_removed_command_names_its_replacement(argv, replacement):
    result = runner.invoke(app, argv)

    assert result.exit_code == 2
    assert "was removed" in result.output
    assert replacement in result.output


@pytest.mark.parametrize("name", ["doctor", "migrate", "codemods", "env", "contract"])
def test_removed_commands_are_not_in_help(name):
    listed = runner.invoke(app, ["--help"]).output
    assert f"│ {name} " not in listed

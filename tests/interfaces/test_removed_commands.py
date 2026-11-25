"""Removed commands keep their name for one release: they say what replaces them.

Hidden from --help, they accept any arguments and exit 2 (usage), so an old
script fails loudly with the new command instead of "No such command".
"""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from pymolt.interfaces.cli.commands import app

runner = CliRunner()

# Typer forces a terminal under GITHUB_ACTIONS, FORCE_COLOR or PY_COLORS: help then carries
# ANSI codes, which would make every "not listed" check pass whatever help lists.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@pytest.mark.parametrize("argv, replacement", [
    (["doctor", "."], "pymolt status"),
    (["migrate", ".", "--write"], "pymolt plan"),
    (["codemods", ".", "-p", "flask", "--from", "1", "--to", "2"], "pymolt plan"),
    (["env", "hint", "."], "pymolt assess"),
])
def test_a_removed_command_names_its_replacement(argv, replacement):
    result = runner.invoke(app, argv)

    assert result.exit_code == 2
    assert "was removed" in result.output
    assert replacement in result.output


@pytest.mark.parametrize("old, new", [
    ("capture", "pymolt capture"), ("trace", "pymolt capture"),
    ("export-watcher", "pymolt capture"), ("boundary", "pymolt capture"),
    ("report", "pymolt verify"), ("diff", "pymolt verify --baseline"), ("map", "pymolt scan"),
])
def test_a_removed_contract_subcommand_names_its_replacement(old, new):
    result = runner.invoke(app, ["contract", old, ".", "--when", "baseline"])

    assert result.exit_code == 2
    assert f"pymolt contract {old}` was removed" in result.output
    assert new in result.output


@pytest.mark.parametrize("name", ["doctor", "migrate", "codemods", "env", "contract"])
def test_removed_commands_are_not_in_help(name):
    listed = _ANSI.sub("", runner.invoke(app, ["--help"]).output)
    assert "│ status " in listed  # the check below reads the command list, not an empty page
    assert f"│ {name} " not in listed

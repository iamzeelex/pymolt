"""`pymolt setup` must not accept an answer it cannot act on.

Reported from a real session: typing `q` at every prompt produced a saved config
with ``base_python: "q"`` and ``target_python: "q"``. Nothing complained — the
unrecognized answers were quietly swapped for defaults, and the two free-text
prompts took the string verbatim. Every later phase resolves against that file,
so a typo at setup silently steers the whole migration.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from pymolt.ingestion.config import EnvConfig
from pymolt.interfaces.cli.commands import app

runner = CliRunner()


@pytest.fixture
def project(tmp_path, monkeypatch):
    import sys

    import typer.testing

    (tmp_path / "requirements.txt").write_text("flask==2.0.3\n", encoding="utf-8")
    (tmp_path / "requirements-dev.txt").write_text("pytest\n", encoding="utf-8")
    # setup only prompts on a tty. CliRunner swaps sys.stdout for its own capture
    # object, so the class has to be patched, not the instance we can see here.
    monkeypatch.setattr(typer.testing._NamedTextIOWrapper, "isatty", lambda self: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    return tmp_path


def _saved(project):
    return json.loads((project / ".pymolt" / "env_config.json").read_text(encoding="utf-8"))


class TestGarbageIsRejected:
    def test_q_at_every_prompt_never_reaches_the_config(self, project):
        """The reported reproduction, start to finish."""
        # q at manifest, tool, base and target; then real answers.
        result = runner.invoke(
            app, ["setup", str(project)],
            input="q\n1\nq\n1\nq\n3.11\nq\n1\nn\n",
        )
        assert result.exit_code == 0, result.output

        saved = _saved(project)
        assert saved["base_python"] == "3.11"
        assert saved["target_python"] != "q"
        assert saved["selected_manifest"] in ("requirements.txt", "requirements-dev.txt")
        # and the user was told, rather than having the answer swapped in silence
        assert "not a Python version" in result.stderr

    def test_unknown_manifest_name_re_asks_instead_of_defaulting(self, project):
        result = runner.invoke(
            app, ["setup", str(project)],
            input="nope.txt\nrequirements.txt\n1\n3.11\n1\nn\n",
        )
        assert result.exit_code == 0, result.output
        assert _saved(project)["selected_manifest"] == "requirements.txt"
        assert "Enter a number" in result.stderr

    def test_unknown_tool_re_asks(self, project):
        result = runner.invoke(
            app, ["setup", str(project)],
            input="1\nrustup\nuv\n3.11\n1\nn\n",
        )
        assert result.exit_code == 0, result.output
        assert _saved(project)["selected_tool"] == "uv"

    def test_a_valid_run_still_takes_names_and_numbers(self, project):
        result = runner.invoke(
            app, ["setup", str(project)],
            input="requirements.txt\n1\n3.9\n1\nn\n",
        )
        assert result.exit_code == 0, result.output
        saved = _saved(project)
        assert saved["selected_manifest"] == "requirements.txt"
        assert saved["base_python"] == "3.9"


class TestConfigModelIsTheLastLineOfDefence:
    """Even if an interface (a hand-edited file, a future command) tries,
    the persisted schema refuses a version that isn't one."""

    def test_rejects_a_non_version(self):
        with pytest.raises(ValueError):
            EnvConfig(base_python="q")
        with pytest.raises(ValueError):
            EnvConfig(target_python="quit")

    def test_accepts_the_real_shapes(self):
        for value in ("3", "3.12", "3.6.1", "3.13.0rc1"):
            assert EnvConfig(base_python=value).base_python == value

    def test_none_and_empty_stay_allowed(self):
        assert EnvConfig(base_python=None).base_python is None
        assert EnvConfig(target_python="").target_python == ""

    def test_a_corrupt_file_on_disk_does_not_load(self, tmp_path):
        path = tmp_path / "env_config.json"
        path.write_text(json.dumps({"base_python": "q"}), encoding="utf-8")
        assert EnvConfig.load(path) is None

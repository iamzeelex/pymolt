"""The CLI's output contract — the promise the README makes to agents and CI.

Three invariants, each of which was broken in a way no existing test could see
(they all asserted on `result.output`, which mixes both streams):

* stdout carries only the answer; diagnostics go to stderr,
* `--json` produces parseable JSON on *failure* too, not a rich-rendered error,
* exit codes distinguish "wrong invocation" from "the world is broken" from
  "ran fine, answer is negative".
"""

from __future__ import annotations

import json
import re
import sys

import pytest
from typer.testing import CliRunner

from pymolt.interfaces.cli.commands import app
from pymolt.interfaces.cli.output import (
    EXIT_ENVIRONMENT,
    EXIT_FINDING,
    EXIT_INCONCLUSIVE,
    EXIT_OK,
    EXIT_USAGE,
)

runner = CliRunner()

# Typer forces a terminal under GITHUB_ACTIONS, FORCE_COLOR or PY_COLORS, so rich help
# carries ANSI codes there; the panel checks read the text without them.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _help_text() -> str:
    return _ANSI.sub("", runner.invoke(app, ["--help"]).output)


@pytest.fixture
def project(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask==2.0.3\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("import flask\nflask.Flask(__name__)\n", encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def _mock_assess_resolution(monkeypatch):
    from pymolt.core.enums import Mode, Provenance, ResolutionQuality, SourceFixation
    from pymolt.core.graph import DependencyGraph, Node
    from pymolt.core.layers import IngestionReport

    node = Node(
        name="flask", version="2.0.3", mode=Mode.PYPI, provenance=Provenance.PYPI, direct=True,
    )
    fake_graph = DependencyGraph(
        nodes={"flask": node},
        resolution_quality=ResolutionQuality.RESOLVED,
        source_fixation=SourceFixation.PINNED,
    )
    fake_report = IngestionReport(
        resolution_quality="resolved",
        source_fixation="pinned",
        manual_zone=[],
        warnings=[],
        detected_python="3.12",
    )
    monkeypatch.setattr(
        "pymolt.interfaces.cli.commands._resolve_baseline_graph",
        lambda **k: (fake_graph, fake_report, None),
    )
    monkeypatch.setattr(
        "pymolt.interfaces.cli.commands._resolve_target_graph",
        lambda *a, **k: (fake_graph, None),
    )


# ── stdout purity ─────────────────────────────────────────────────────────────

class TestStdoutCarriesOnlyTheAnswer:
    def test_scan_json_stdout_is_exactly_the_payload(self, project):
        result = runner.invoke(app, ["scan", str(project), "--json"])
        assert result.exit_code == EXIT_OK
        payload = json.loads(result.stdout)  # would raise if anything else leaked
        assert payload["surfaces"]["project_roots"]

    def test_assess_warning_goes_to_stderr_not_stdout(self, project, monkeypatch):
        """No env_config -> a warning. It must not sit in the data channel."""
        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        result = runner.invoke(
            app, ["assess", str(project), "--target-python", "3.12", "--json", "--no-write"]
        )
        json.loads(result.stdout)  # stdout still pure JSON despite the warning
        assert "No environment configuration found" in result.stderr


# ── failures are JSON under --json ────────────────────────────────────────────

class TestJsonFailureEnvelope:
    def _envelope(self, result):
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["error"]
        return payload

    def test_missing_directory(self, tmp_path):
        result = runner.invoke(app, ["scan", str(tmp_path / "nope"), "--json"])
        assert result.exit_code == EXIT_USAGE
        assert "no such directory" in self._envelope(result)["error"]

    def test_no_manifest_for_assess(self, tmp_path):
        result = runner.invoke(app, ["assess", str(tmp_path), "--target-python", "3.12", "--json"])
        assert result.exit_code == EXIT_USAGE
        assert "no Python dependency sources" in self._envelope(result)["error"]

    def test_succession_without_a_manifest_is_an_empty_answer_not_prose(self, tmp_path):
        """Nothing declared is a real (empty) answer — valid JSON, exit 0."""
        result = runner.invoke(app, ["succession", str(tmp_path), "--json"])
        assert result.exit_code == EXIT_OK
        assert json.loads(result.stdout) == {
            "frameworks": [], "edges": [], "transplant_plans": [],
            "in_place_files_changed": 0, "dry_run": True,
        }

    def test_human_mode_failure_stays_off_stdout(self, tmp_path):
        result = runner.invoke(app, ["scan", str(tmp_path / "nope")])
        assert result.exit_code == EXIT_USAGE
        assert result.stdout.strip() == ""
        assert "no such directory" in result.stderr


# ── exit-code contract ────────────────────────────────────────────────────────

class TestExitCodes:
    def test_missing_dir_is_usage_everywhere(self, tmp_path):
        missing = str(tmp_path / "nope")
        for argv in (
            ["scan", missing],
            ["assess", missing, "--target-python", "3.12"],
            ["setup", missing],
            ["plan", missing],
            ["succession", missing],
            ["verify", missing],
        ):
            result = runner.invoke(app, argv)
            assert result.exit_code == EXIT_USAGE, f"{argv} -> {result.exit_code}"

    def test_missing_recording_never_reports_a_clean_diff(self, tmp_path):
        """The dangerous one: a typo'd path used to fold into an empty diff and
        exit 0 — a false 'no behavior change'."""
        real = tmp_path / "old.jsonl"
        real.write_text('{"t": "return", "q": "dep.f", "in": {"bound": {}}, "result": 1}\n')
        result = runner.invoke(app, [
            "verify", str(tmp_path), "--baseline", str(real),
            "--post-migration", str(tmp_path / "typo.jsonl"),
        ])
        assert result.exit_code == EXIT_USAGE
        assert "no such --post-migration recording" in result.stderr

    def test_changed_recordings_are_a_finding_and_equal_ones_need_a_static_surface(
        self, tmp_path,
    ):
        """Equal recordings are not a PASS on their own: with no code calling the
        dependency there is no static surface to vouch for (`contract diff` said OK)."""
        def rec(path, result_value):
            path.write_text(
                json.dumps({"t": "return", "q": "dep.f", "in": {"bound": {}},
                            "result": result_value}) + "\n"
            )
            # the watcher's sidecar: without its event-loss counters a recording is never
            # compared to a verdict (QA V2-1)
            (path.parent / (path.name + ".meta.json")).write_text(json.dumps({"counters": {
                name: 0 for name in ("events_seen", "events_written", "sampling_dropped",
                                     "backpressure_dropped", "write_failures",
                                     "sink_failures", "shutdown_dropped")}}))
            return str(path)

        old = rec(tmp_path / "old.jsonl", 1)
        same = rec(tmp_path / "same.jsonl", 1)
        changed = rec(tmp_path / "new.jsonl", 2)

        def verify(new):
            return runner.invoke(app, [
                "verify", str(tmp_path), "--baseline", old, "--post-migration", new, "--json",
            ])

        unchanged = verify(same)
        assert unchanged.exit_code == EXIT_INCONCLUSIVE
        assert "static contract surface is empty" in unchanged.stdout
        assert verify(changed).exit_code == EXIT_FINDING

    def test_a_failing_delta_engine_is_environment_not_usage(self, project, monkeypatch):
        from pymolt.codemods import service as codemods_service
        from pymolt.codemods.models import DependencyMigration
        from pymolt.ingestion.config import EnvConfig, ToolChoice

        (project / "requirements-target.txt").write_text("flask==3.0.0\n", encoding="utf-8")
        EnvConfig(
            selected_manifest="requirements.txt", selected_tool=ToolChoice.UV,
            base_python="3.11", target_python="3.12",
        ).save(project / ".pymolt" / "env_config.json")

        def boom(*a, **k):
            raise RuntimeError("uv could not build the old environment")

        monkeypatch.setattr(
            codemods_service, "resolve_codemod_migrations",
            lambda *a, **k: ([DependencyMigration("flask", "2.0.3", "3.0.0")], "target"),
        )
        monkeypatch.setattr(codemods_service, "preview_codemods", boom)
        result = runner.invoke(app, ["plan", str(project)])
        assert result.exit_code == EXIT_ENVIRONMENT


# ── what the reader actually gets ─────────────────────────────────────────────

class TestRendering:
    def test_scan_names_the_dependencies_not_just_counts(self, project):
        """`Edges: 2 (pypi:2)` cannot answer "am I on flask?" — the first question
        this command is asked."""
        result = runner.invoke(app, ["scan", str(project)])
        assert result.exit_code == EXIT_OK
        # the declared requirement itself: "flask" alone also matches the call map's flask.Flask
        assert "flask==2.0.3" in _ANSI.sub("", result.stdout)

    def test_scan_shows_where_the_code_calls_dependencies(self, project):
        result = runner.invoke(app, ["scan", str(project)])
        assert "Calls into dependencies" in result.stdout
        assert "flask.Flask" in result.stdout

        payload = json.loads(runner.invoke(app, ["scan", str(project), "--json"]).stdout)
        assert any(c["target"] == "flask.Flask" for c in payload["contact_map"]["contacts"])

    def test_assess_says_how_to_build_the_target_environment(self, project):
        argv = ["assess", str(project), "--base-python", "3.11", "--target-python", "3.12"]
        result = runner.invoke(app, argv)
        assert result.exit_code == EXIT_OK, result.output
        text = _ANSI.sub("", result.stdout)   # FORCE_COLOR/CI colour the console
        assert "Target environment" in text
        assert "uv venv .venv-target --python 3.12" in text

        payload = json.loads(runner.invoke(app, [*argv, "--json", "--no-write"]).stdout)
        assert payload["target_hint"]["target_python"] == "3.12"

    def test_next_step_is_guidance_on_stderr_not_part_of_the_answer(self, project):
        result = runner.invoke(app, ["scan", str(project)])
        assert "next →" in result.stderr
        assert "next →" not in result.stdout

    def test_help_lists_the_preparation_phases_in_funnel_order(self):
        output = _help_text()
        funnel = output.split("╭─ Migration ")[1].split("╰")[0]
        order = [name for name in ("scan", "setup", "assess", "plan", "apply") if name in funnel]
        positions = [funnel.index(name) for name in order]
        assert positions == sorted(positions), f"funnel out of order: {order}"
        # scan is Phase 1 and must not sit below the phase that consumes it
        assert funnel.index("scan") < funnel.index("assess")

    def test_contract_is_its_own_panel_not_a_funnel_step(self):
        """The behavioural contract is the product, not the last chore in a list.

        It brackets the migration (baseline before, post-migration after), so it
        gets its own help panel rather than trailing the preparation phases.
        """
        output = _help_text()
        # Match the panel border, not the phrase — the help text names the panel too.
        assert "╭─ Behavioural proof" in output
        proof = output.split("╭─ Behavioural proof")[1].split("╰")[0]
        assert "capture" in proof and "verify" in proof
        prep = output.split("╭─ Migration ")[1].split("╰")[0]
        assert "capture" not in prep and "verify" not in prep

    def test_help_leads_with_capturing_a_baseline_first(self):
        """The one instruction a user cannot act on retroactively goes up top."""
        result = runner.invoke(app, ["--help"])
        lead = result.output.split("╭─ Options")[0]
        assert "pymolt capture baseline" in lead
        assert "before any code change" in lead

    def test_version_diff_is_prose_not_a_python_dict(self, tmp_path):
        from pymolt.interfaces.cli.verify_cmd import _render_contract_report
        from pymolt.verify.report import ContractReport

        rep = ContractReport(
            root=str(tmp_path), static_targets=1, confirmed=1,
            diff={"disappeared": 235, "result_changed": 0, "appeared": 5},
            diff_clean=False,
        )
        with runner.isolation() as (out, _err, _):
            _render_contract_report(rep)
            rendered = out.getvalue().decode()
        assert "{'disappeared'" not in rendered
        assert "disappeared 235" in rendered
        assert "result_changed" not in rendered   # zero counts are not news


# ── --no-write: the read-only path an agent can trust ─────────────────────────

class TestNoWrite:
    def _assess(self, project, *extra):
        return runner.invoke(
            app, ["assess", str(project), "--target-python", "3.12", "--json", *extra]
        )

    def test_no_write_leaves_the_project_clean(self, project, monkeypatch):
        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        before = {p.name for p in project.iterdir()}

        result = self._assess(project, "--no-write")

        assert result.exit_code == EXIT_OK
        payload = json.loads(result.stdout)
        assert payload["target_manifest_path"] is None
        new_files = {p.name for p in project.iterdir()} - before
        assert "requirements-target.txt" not in new_files

    def test_default_still_writes_and_discloses_the_path(self, project, monkeypatch):
        monkeypatch.setattr("sys.stdout.isatty", lambda: False)

        result = self._assess(project)

        assert result.exit_code == EXIT_OK
        payload = json.loads(result.stdout)
        # The write is a documented artifact — and the JSON says where it went.
        assert payload["target_manifest_path"] is not None
        assert (project / "requirements-target.txt").is_file()



# ── one name per option; old names still parse, hidden ───────────────────────

class TestOptionNames:
    def test_assess_takes_the_setup_names(self, project):
        result = runner.invoke(app, [
            "assess", str(project), "--manifest", "requirements.txt",
            "--base-python", "3.11", "--target-python", "3.12", "--json", "--no-write",
        ])
        assert result.exit_code == EXIT_OK, result.output
        assert json.loads(result.stdout)["source_manifest"] == "requirements.txt"

    def test_old_names_still_parse_but_are_not_listed(self, project):
        constraint = project / "constraints.txt"
        constraint.write_text("flask<3\n", encoding="utf-8")
        result = runner.invoke(app, [
            "assess", str(project), "--source-manifest", "requirements.txt",
            "--current-python", "3.11", "--target-python", "3.12", "-c", str(constraint),
            "--json", "--no-write",
        ])
        assert result.exit_code == EXIT_OK, result.output

        listed = _ANSI.sub("", runner.invoke(app, ["assess", "--help"]).output)
        assert "--manifest" in listed and "--base-python" in listed  # the new names are listed
        for old in ("--source-manifest", "--current-python", "-c "):
            assert old not in listed
        plan_help = _ANSI.sub("", runner.invoke(app, ["plan", "--help"]).output)
        assert "--patch" in plan_help
        assert "--target-file" not in plan_help



def test_parsing_project_source_prints_no_syntax_warning(tmp_path):
    """QA H6: `plan` printed `<unknown>:78: SyntaxWarning: invalid escape sequence` from parsing
    the project's files; the compile check already reports each one as a finding."""
    import ast
    import warnings

    (tmp_path / "requirements.txt").write_text("flask==2.0.3\n", encoding="utf-8")
    runner.invoke(app, ["status", str(tmp_path)])
    source = "pattern = '" + "\\" + "s'\n"
    with warnings.catch_warnings(record=True) as caught:
        ast.parse(source)
        compile(source, "app.py", "exec")
    shown = [w.filename for w in caught if issubclass(w.category, SyntaxWarning)]
    # The parse in pymolt's process is quiet; a compile that names a file keeps its warning.
    assert shown == ["app.py"]


# ── bracketed data survives rich markup ───────────────────────────────────────


def _plain(output: str) -> str:
    return _ANSI.sub("", output)


def test_scan_shows_extras_in_the_edges_table(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "requests[socks]==2.31.0\nuvicorn[standard]>=0.20\n", encoding="utf-8")
    out = _plain(runner.invoke(app, ["scan", str(tmp_path)]).output)
    assert "requests[socks]==2.31.0" in out
    assert "uvicorn[standard]>=0.20" in out


def test_mcp_without_the_extra_names_the_extra(monkeypatch):
    # None in sys.modules makes `from pymolt.interfaces.mcp_server import run` raise
    # ModuleNotFoundError, as it does when the extra is not installed.
    monkeypatch.setitem(sys.modules, "pymolt.interfaces.mcp_server", None)
    result = runner.invoke(app, ["mcp"])
    assert result.exit_code == 1
    out = " ".join(_plain(result.output).split())
    assert "pymolt[mcp]" in out


def test_mcp_help_keeps_the_codex_table_name():
    help_text = _plain(runner.invoke(app, ["mcp", "--help"]).output)
    assert "[mcp_servers.pymolt]" in help_text


def test_error_messages_keep_bracketed_text():
    out = " ".join(_plain(runner.invoke(app, ["scan", "no[such]dir"]).output).split())
    assert "no[such]dir" in out

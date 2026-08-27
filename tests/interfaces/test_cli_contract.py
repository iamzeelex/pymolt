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

import pytest
from typer.testing import CliRunner

from pymolt.interfaces.cli.commands import app
from pymolt.interfaces.cli.output import (
    EXIT_ENVIRONMENT,
    EXIT_FINDING,
    EXIT_OK,
    EXIT_USAGE,
)

runner = CliRunner()


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

    def test_env_hint_without_a_target_python(self, project):
        result = runner.invoke(app, ["env", "hint", str(project), "--json"])
        assert result.exit_code == EXIT_USAGE
        assert "target Python" in self._envelope(result)["error"]

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
            ["codemods", missing, "-p", "flask", "--from", "1", "--to", "2"],
            ["succession", missing],
            ["env", "hint", missing],
            ["contract", "map", missing],
            ["contract", "report", missing],
        ):
            result = runner.invoke(app, argv)
            assert result.exit_code == EXIT_USAGE, f"{argv} -> {result.exit_code}"

    def test_missing_recording_never_reports_a_clean_diff(self, tmp_path):
        """The dangerous one: a typo'd path used to fold into an empty diff and
        exit 0 — a false 'no behavior change'."""
        real = tmp_path / "old.jsonl"
        real.write_text('{"t": "return", "q": "dep.f", "in": {"bound": {}}, "result": 1}\n')
        result = runner.invoke(app, ["contract", "diff", str(real), str(tmp_path / "typo.jsonl")])
        assert result.exit_code == EXIT_USAGE
        assert "no such NEW recording" in result.stderr

    def test_clean_diff_is_ok_and_changed_diff_is_a_finding(self, tmp_path):
        def rec(path, result_value):
            path.write_text(
                json.dumps({"t": "return", "q": "dep.f", "in": {"bound": {}},
                            "result": result_value}) + "\n"
            )
            return str(path)

        old = rec(tmp_path / "old.jsonl", 1)
        same = rec(tmp_path / "same.jsonl", 1)
        changed = rec(tmp_path / "new.jsonl", 2)

        assert runner.invoke(app, ["contract", "diff", old, same]).exit_code == EXIT_OK
        assert runner.invoke(app, ["contract", "diff", old, changed]).exit_code == EXIT_FINDING

    def test_dead_service_is_environment_not_usage(self, project, monkeypatch):
        from pymolt.codemods import service as codemods_service
        from pymolt.codemods.client import AxiomGraphError

        def boom(*a, **k):
            raise AxiomGraphError("connection refused")

        monkeypatch.setattr(codemods_service, "run_codemods", boom)
        result = runner.invoke(
            app, ["codemods", str(project), "-p", "flask", "--from", "2.0.3", "--to", "3.0.0"]
        )
        assert result.exit_code == EXIT_ENVIRONMENT


# ── what the reader actually gets ─────────────────────────────────────────────

class TestRendering:
    def test_scan_names_the_dependencies_not_just_counts(self, project):
        """`Edges: 2 (pypi:2)` cannot answer "am I on flask?" — the first question
        this command is asked."""
        result = runner.invoke(app, ["scan", str(project)])
        assert result.exit_code == EXIT_OK
        assert "flask" in result.stdout

    def test_next_step_is_guidance_on_stderr_not_part_of_the_answer(self, project):
        result = runner.invoke(app, ["scan", str(project)])
        assert "next →" in result.stderr
        assert "next →" not in result.stdout

    def test_help_lists_the_funnel_in_funnel_order(self):
        result = runner.invoke(app, ["--help"])
        funnel = result.output.split("Migration Workflow")[1].split("╰")[0]
        order = [name for name in ("scan", "setup", "assess", "codemods", "contract")
                 if name in funnel]
        positions = [funnel.index(name) for name in order]
        assert positions == sorted(positions), f"funnel out of order: {order}"
        # scan is Phase 1 and must not sit below the phase that consumes it
        assert funnel.index("scan") < funnel.index("assess")

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


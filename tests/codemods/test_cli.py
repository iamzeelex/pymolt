"""Tests for the `pymolt codemods` CLI command."""

from __future__ import annotations

from typer.testing import CliRunner

from pymolt.codemods import service as codemods_service
from pymolt.codemods.client import AxiomGraphError
from pymolt.codemods.models import CodemodPattern, CodemodRunResult, FileChange
from pymolt.codemods.rules import CodemodRule, RuleAdvisory
from pymolt.interfaces.cli import commands

runner = CliRunner()


def test_requires_migration_args(tmp_path):
    result = runner.invoke(commands.app, ["codemods", str(tmp_path)])
    assert result.exit_code == 2
    # Nothing to derive a migration from — and the fix is named: run assess first.
    assert "assess" in result.output.lower()


def test_bare_invocation_picks_up_the_manifest_assess_wrote(tmp_path, monkeypatch):
    """`pymolt codemods .` is the documented flow; it used to exit 2 unless you
    repeated the target file that assess had already written."""
    (tmp_path / "requirements-target.txt").write_text("flask==3.0.0\n", encoding="utf-8")
    seen = {}

    def fake_resolve(project_dir, *, target_dependency_file=None, **kw):
        seen["target_file"] = target_dependency_file
        return [], "target dependency file requirements-target.txt"

    monkeypatch.setattr(codemods_service, "resolve_codemod_migrations", fake_resolve)

    result = runner.invoke(commands.app, ["codemods", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert seen["target_file"] == "requirements-target.txt"


def test_dry_run_renders_patterns(tmp_path, monkeypatch):
    pattern = CodemodPattern(
        old_qualname="flask.helpers.safe_join",
        new_qualname="werkzeug.utils.safe_join",
        kind="rewrite-import",
        confidence="high",
    )
    run_result = CodemodRunResult(
        root=str(tmp_path), dry_run=True, files_scanned=3,
        changes=[FileChange(path=str(tmp_path / "app.py"), sites=1,
                            patterns=[pattern.summary()])],
        patterns_applied=1,
    )
    monkeypatch.setattr(
        codemods_service,
        "run_codemods",
        lambda *a, **k: ({"flask": [pattern]}, run_result),
    )

    result = runner.invoke(
        commands.app,
        ["codemods", str(tmp_path), "-p", "flask", "--from", "2.0.3", "--to", "3.0.0"],
    )
    assert result.exit_code == 0
    assert "werkzeug.utils.safe_join" in result.output
    assert "Would rewrite" in result.output
    assert "--write" in result.output  # hint to apply


def test_dry_run_renders_rules_and_advisories(tmp_path, monkeypatch):
    rule = CodemodRule(
        library="pandas", from_version="1.0.0", to_version="1.2.0",
        match="$DF.lookup($ROWS, $COLS)",
        rewrite=["$RESULT = $DF.to_numpy()"],
        confidence="verified",
    )
    advisory = RuleAdvisory(
        line=5, column=0, site_kind="return", severity="not-applied",
        reason="call is the return value of a function; not auto-rewritable",
    )
    run_result = CodemodRunResult(
        root=str(tmp_path), dry_run=True, files_scanned=2,
        advisories_by_file={str(tmp_path / "app.py"): [advisory]},
        downgraded=["pandas.DataFrame.lookup → pandas.DataFrame.to_numpy"],
    )
    monkeypatch.setattr(
        codemods_service,
        "run_codemods",
        lambda *a, **k: ({"pandas": [rule]}, run_result),
    )

    result = runner.invoke(
        commands.app,
        ["codemods", str(tmp_path), "-p", "pandas", "--from", "1.0.0", "--to", "1.2.0"],
    )
    assert result.exit_code == 0
    assert "$DF.lookup($ROWS, $COLS)" in result.output
    assert "Downgraded to heuristic" in result.output
    assert "Manual review needed" in result.output
    assert "app.py" in result.output
    assert "not auto-rewritable" in result.output


def test_service_unavailable_is_an_environment_failure(tmp_path, monkeypatch):
    """A dead Axiom Graph is the environment's fault, not a finding about the
    code — exit 3, so CI can tell "service down" from "rewrites needed"."""
    from pymolt.interfaces.cli.output import EXIT_ENVIRONMENT

    def boom(*a, **k):
        raise AxiomGraphError("connection refused")

    monkeypatch.setattr(codemods_service, "run_codemods", boom)

    result = runner.invoke(
        commands.app,
        ["codemods", str(tmp_path), "-p", "flask", "--from", "2.0.3", "--to", "3.0.0"],
    )
    assert result.exit_code == EXIT_ENVIRONMENT
    assert "unavailable" in result.output.lower()

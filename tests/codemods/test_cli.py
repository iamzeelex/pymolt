"""Tests for the `pymolt codemods` CLI command."""

from __future__ import annotations

from typer.testing import CliRunner

from pymolt.codemods import service as codemods_service
from pymolt.codemods.client import AxiomGraphError
from pymolt.codemods.models import CodemodPattern, FilePreview
from pymolt.codemods.rules import CodemodRule, RuleAdvisory
from pymolt.ingestion.config import EnvConfig, ToolChoice
from pymolt.interfaces.cli import commands
from pymolt.migration_state import MigrationReceipt
from pymolt.verify import service as verify_service
from pymolt.verify.models import (
    CaptureMode,
    CaptureValidity,
    ContractSlot,
    ContractState,
)

runner = CliRunner()


def test_requires_migration_args(tmp_path):
    result = runner.invoke(commands.app, ["codemods", str(tmp_path)])
    assert result.exit_code == 2
    # Nothing to derive a migration from — and the fix is named: run assess first.
    assert "assess" in result.output.lower()


def test_write_requires_a_valid_baseline_before_contacting_service(tmp_path, monkeypatch):
    called = False

    def unexpected(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("service must not run before the baseline gate")

    monkeypatch.setattr(codemods_service, "run_codemods", unexpected)

    result = runner.invoke(commands.app, [
        "codemods", str(tmp_path), "--write",
        "-p", "flask", "--from", "2.0.3", "--to", "3.0.0",
    ])

    assert result.exit_code == 2
    assert "without a valid baseline" in result.output
    assert called is False


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
    preview = FilePreview(
        path=str(tmp_path / "app.py"),
        old_source="from flask.helpers import safe_join\n",
        new_source="from werkzeug.utils import safe_join\n",
        sites=1,
        patterns=[pattern],
    )
    monkeypatch.setattr(
        codemods_service,
        "preview_codemods",
        lambda *a, **k: ({"flask": [pattern]}, [preview]),
    )

    result = runner.invoke(
        commands.app,
        ["codemods", str(tmp_path), "-p", "flask", "--from", "2.0.3", "--to", "3.0.0"],
    )
    assert result.exit_code == 0
    assert "werkzeug.utils.safe_join" in result.output
    assert "Would rewrite" in result.output
    assert "--write" in result.output  # hint to apply


def test_dry_run_writes_relative_unified_patch(tmp_path, monkeypatch):
    pattern = CodemodPattern(
        old_qualname="flask.helpers.safe_join",
        new_qualname="werkzeug.utils.safe_join",
        kind="rewrite-import",
        confidence="high",
    )
    source = "from flask.helpers import safe_join\n"
    preview = FilePreview(
        path=str(tmp_path / "app.py"),
        old_source=source,
        new_source="from werkzeug.utils import safe_join\n",
        sites=1,
        patterns=[pattern],
    )
    monkeypatch.setattr(
        codemods_service,
        "preview_codemods",
        lambda *a, **k: ({"flask": [pattern]}, [preview]),
    )
    patch_path = tmp_path / "migration.patch"

    result = runner.invoke(
        commands.app,
        [
            "codemods", str(tmp_path), "-p", "flask", "--from", "2.0.3",
            "--to", "3.0.0", "--patch", str(patch_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "app.py").exists() is False
    patch = patch_path.read_text(encoding="utf-8")
    assert "--- a/app.py" in patch
    assert "+++ b/app.py" in patch
    assert str(tmp_path) not in patch


def test_patch_cannot_be_combined_with_write(tmp_path):
    result = runner.invoke(
        commands.app,
        [
            "codemods", str(tmp_path), "--write", "--patch", "migration.patch",
            "-p", "flask", "--from", "2.0.3", "--to", "3.0.0",
        ],
    )

    assert result.exit_code == 2
    assert "cannot be combined" in result.output


def test_write_uses_a_durable_exact_plan_and_can_rollback(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("flask==2.0.3\n", encoding="utf-8")
    EnvConfig(
        selected_manifest="requirements.txt",
        selected_tool=ToolChoice.UV,
        base_python="3.11",
        target_python="3.12",
    ).save(tmp_path / ".pymolt" / "env_config.json")
    trace = tmp_path / ".pymolt" / "baseline.jsonl"
    trace.write_text('{"q":"flask.safe_join"}\n', encoding="utf-8")
    ContractState(baseline=ContractSlot(
        trace_path=str(trace),
        captured_at="2026-09-10T12:00:00+00:00",
        mode=CaptureMode.TEST_SUITE,
        command=["pytest"],
        target="flask",
        events=1,
        validity=CaptureValidity.VALID,
        env_fingerprint=verify_service.environment_fingerprint(tmp_path),
    )).save(tmp_path / ".pymolt" / "contract_state.json")
    source_path = tmp_path / "app.py"
    old_source = "from flask.helpers import safe_join\n"
    source_path.write_text(old_source, encoding="utf-8")
    pattern = CodemodPattern(
        old_qualname="flask.helpers.safe_join",
        new_qualname="werkzeug.utils.safe_join",
        kind="rewrite-import",
    )
    preview = FilePreview(
        path=str(source_path),
        old_source=old_source,
        new_source="from werkzeug.utils import safe_join\n",
        sites=1,
        patterns=[pattern],
    )

    def fake_preview(*args, **kwargs):
        assert kwargs["auto_apply_only"] is True
        return {"flask": [pattern]}, [preview]

    monkeypatch.setattr(codemods_service, "preview_codemods", fake_preview)

    result = runner.invoke(commands.app, [
        "codemods", str(tmp_path), "--write",
        "-p", "flask", "--from", "2.0.3", "--to", "3.0.0",
    ])

    assert result.exit_code == 0, result.output
    assert "werkzeug.utils" in source_path.read_text(encoding="utf-8")
    receipt = MigrationReceipt.load(tmp_path)
    assert receipt is not None and receipt.plan_path
    assert (tmp_path / ".pymolt" / "migrations" / receipt.run_id / "plan.json").is_file()

    rolled_back = runner.invoke(commands.app, ["rollback", str(tmp_path)])
    assert rolled_back.exit_code == 0, rolled_back.output
    assert source_path.read_text(encoding="utf-8") == old_source


def test_dry_run_renders_rules_and_advisories(tmp_path, monkeypatch):
    rule = CodemodRule(
        library="pandas", from_version="1.0.0", to_version="1.2.0",
        match="$DF.lookup($ROWS, $COLS)",
        rewrite=["$RESULT = $DF.to_numpy()"],
        confidence="heuristic",
    )
    advisory = RuleAdvisory(
        line=5, column=0, site_kind="return", severity="not-applied",
        reason="call is the return value of a function; not auto-rewritable",
    )
    preview = FilePreview(
        path=str(tmp_path / "app.py"), old_source="return df.lookup(rows, cols)\n",
        new_source="return df.lookup(rows, cols)\n", sites=0,
        rules=[rule], advisories=[advisory],
    )
    monkeypatch.setattr(
        codemods_service,
        "preview_codemods",
        lambda *a, **k: ({"pandas": [rule]}, [preview]),
    )

    result = runner.invoke(
        commands.app,
        ["codemods", str(tmp_path), "-p", "pandas", "--from", "1.0.0", "--to", "1.2.0"],
    )
    assert result.exit_code == 0
    assert "$DF.lookup($ROWS, $COLS)" in result.output
    assert "heuristic" in result.output
    assert "Manual review needed" in result.output
    assert "app.py" in result.output
    assert "not auto-rewritable" in result.output


def test_service_unavailable_is_an_environment_failure(tmp_path, monkeypatch):
    """A dead Axiom Graph is the environment's fault, not a finding about the
    code — exit 3, so CI can tell "service down" from "rewrites needed"."""
    from pymolt.interfaces.cli.output import EXIT_ENVIRONMENT

    def boom(*a, **k):
        raise AxiomGraphError("connection refused")

    monkeypatch.setattr(codemods_service, "preview_codemods", boom)

    result = runner.invoke(
        commands.app,
        ["codemods", str(tmp_path), "-p", "flask", "--from", "2.0.3", "--to", "3.0.0"],
    )
    assert result.exit_code == EXIT_ENVIRONMENT
    assert "unavailable" in result.output.lower()

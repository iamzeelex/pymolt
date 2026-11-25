from __future__ import annotations

import json
import sys

from typer.testing import CliRunner

from pymolt.codemods import service as codemod_service
from pymolt.codemods.models import DependencyMigration
from pymolt.codemods.models import CodemodPattern, FilePreview, PreviewProgress
from pymolt.ingestion.config import EnvConfig, ToolChoice
from pymolt.interfaces.cli.commands import app
from pymolt.migration_plan import MigrationPlan
from pymolt.migration_state import MigrationReceipt
from pymolt.status import build_status
from pymolt.verify import service as verify_service
from pymolt.verify.models import CaptureMode

runner = CliRunner()


def _configured_project(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask==2.0.3\n", encoding="utf-8")
    (tmp_path / "requirements-target.txt").write_text("flask==3.0.0\n", encoding="utf-8")
    EnvConfig(
        selected_manifest="requirements.txt",
        selected_tool=ToolChoice.UV,
        base_python="3.11",
        target_python="3.12",
    ).save(tmp_path / ".pymolt" / "env_config.json")
    script = tmp_path / "baseline.py"
    script.write_text("import json\njson.loads('{}')\n", encoding="utf-8")
    verify_service.capture_named_trace(
        tmp_path,
        "baseline",
        CaptureMode.LIVE_COMMAND,
        command=[sys.executable, str(script)],
        target="json",
    )


def _mock_plan_services(tmp_path, monkeypatch):
    source = "from flask.helpers import safe_join\n"
    path = tmp_path / "app.py"
    path.write_text(source, encoding="utf-8")
    pattern = CodemodPattern(
        old_qualname="flask.helpers.safe_join",
        new_qualname="werkzeug.utils.safe_join",
        kind="rewrite-import",
    )
    preview = FilePreview(
        path=str(path),
        old_source=source,
        new_source="from werkzeug.utils import safe_join\n",
        sites=1,
        patterns=[pattern],
    )
    migration = DependencyMigration("flask", "2.0.3", "3.0.0")
    monkeypatch.setattr(
        codemod_service,
        "resolve_codemod_migrations",
        lambda *a, **k: ([migration], "requirements-target.txt"),
    )

    def fake_preview(*args, **kwargs):
        if callback := kwargs.get("on_file"):
            callback(PreviewProgress(path=str(path), sites=1))
        assert kwargs["auto_apply_only"] is True
        return {"flask": [pattern]}, [preview]

    monkeypatch.setattr(codemod_service, "preview_codemods", fake_preview)
    return path, source


def test_plan_apply_rollback_cli_lifecycle(tmp_path, monkeypatch):
    _configured_project(tmp_path)
    source_path, original = _mock_plan_services(tmp_path, monkeypatch)
    patch_path = tmp_path / "migration.patch"

    planned = runner.invoke(app, [
        "plan", str(tmp_path), "--patch", str(patch_path),
    ])

    assert planned.exit_code == 0, planned.output
    assert "Migration plan" in planned.output
    assert "--- a/app.py" in planned.output
    assert source_path.read_text(encoding="utf-8") == original
    assert patch_path.is_file()
    plan = MigrationPlan.load(tmp_path)
    assert plan is not None

    applied = runner.invoke(app, ["apply", str(tmp_path)])

    assert applied.exit_code == 0, applied.output
    assert "Migration applied" in applied.output
    assert "werkzeug.utils" in source_path.read_text(encoding="utf-8")
    receipt = MigrationReceipt.load(tmp_path)
    assert receipt is not None and receipt.status == "applied"

    rolled_back = runner.invoke(app, ["rollback", str(tmp_path)])

    assert rolled_back.exit_code == 0, rolled_back.output
    assert "Migration rolled back" in rolled_back.output
    assert source_path.read_text(encoding="utf-8") == original
    receipt = MigrationReceipt.load(tmp_path)
    assert receipt is not None and receipt.status == "rolled_back"
    status = build_status(tmp_path)
    # The reviewed plan still describes the restored files: apply runs it again, no new plan.
    assert status.phase("plan").state == "done"
    assert "apply runs it again" in status.phase("plan").detail
    assert status.next_command == "pymolt apply ."

    reapplied = runner.invoke(app, ["apply", str(tmp_path)])

    assert reapplied.exit_code == 0, reapplied.output
    assert "Migration applied" in reapplied.output and " as run " not in reapplied.output
    assert "werkzeug.utils" in source_path.read_text(encoding="utf-8")


def test_apply_again_says_the_plan_is_already_applied(tmp_path, monkeypatch):
    _configured_project(tmp_path)
    source_path, _original = _mock_plan_services(tmp_path, monkeypatch)
    assert runner.invoke(app, ["plan", str(tmp_path)]).exit_code == 0
    assert runner.invoke(app, ["apply", str(tmp_path)]).exit_code == 0
    applied = source_path.read_text(encoding="utf-8")

    again = runner.invoke(app, ["apply", str(tmp_path)])
    as_json = runner.invoke(app, ["apply", str(tmp_path), "--json"])

    assert again.exit_code == 0, again.output
    assert "already applied" in again.output and "Nothing was written" in again.output
    assert as_json.exit_code == 0, as_json.output
    assert json.loads(as_json.stdout)["outcome"] == "already_applied"
    assert source_path.read_text(encoding="utf-8") == applied


def test_rollback_json_says_what_became_of_the_post_migration_capture(tmp_path, monkeypatch):
    _configured_project(tmp_path)
    _mock_plan_services(tmp_path, monkeypatch)
    assert runner.invoke(app, ["plan", str(tmp_path)]).exit_code == 0
    assert runner.invoke(app, ["apply", str(tmp_path)]).exit_code == 0
    script = tmp_path / "post.py"
    script.write_text("import json\njson.loads('{}')\n", encoding="utf-8")
    verify_service.capture_named_trace(
        tmp_path, "post_migration", CaptureMode.LIVE_COMMAND,
        command=[sys.executable, str(script)], target="json",
    )
    kept = runner.invoke(app, ["rollback", str(tmp_path), "--step", "env-1", "--json"])
    assert kept.exit_code == 0, kept.output
    # The pin is not code: the capture still describes every file it recorded.
    assert json.loads(kept.stdout)["post_migration_capture"] == "kept"
    _record_in_post(tmp_path, "app.py")

    result = runner.invoke(app, ["rollback", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["files_restored"] == 1
    assert payload["post_migration_capture"] == "set_aside"
    assert "/archive/" in payload["post_migration_trace"]


def _record_in_post(root, *names):
    """The post-migration capture as one that had facts in ``names``: its source snapshot
    records their text (a real capture records the files whose calls it saw)."""
    import gzip
    import hashlib
    import json as _json

    slot = verify_service.load_contract_state(root).post_migration
    with gzip.open(slot.sources_path, "rt", encoding="utf-8") as fh:
        data = _json.load(fh)
    for name in names:
        text = (root / name).read_text(encoding="utf-8")
        data["files"][name] = {"sha256": hashlib.sha256(text.encode()).hexdigest(),
                               "text": text}
    with gzip.open(slot.sources_path, "wt", encoding="utf-8") as fh:
        _json.dump(data, fh)


def test_plan_json_is_machine_readable(tmp_path, monkeypatch):
    _configured_project(tmp_path)
    _mock_plan_services(tmp_path, monkeypatch)

    result = runner.invoke(app, ["plan", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["files_to_change"] == 1
    assert payload["rewrite_sites"] == 1
    assert payload["plan_path"].endswith(".pymolt/migration_plan.json")


def test_apply_rejects_source_changed_after_plan(tmp_path, monkeypatch):
    _configured_project(tmp_path)
    source_path, original = _mock_plan_services(tmp_path, monkeypatch)
    assert runner.invoke(app, ["plan", str(tmp_path)]).exit_code == 0
    source_path.write_text(original + "# user edit\n", encoding="utf-8")

    result = runner.invoke(app, ["apply", str(tmp_path)])

    assert result.exit_code == 2
    assert "stale migration plan" in result.output
    assert source_path.read_text(encoding="utf-8").endswith("# user edit\n")


def test_rollback_refuses_to_discard_later_edits(tmp_path, monkeypatch):
    _configured_project(tmp_path)
    source_path, _original = _mock_plan_services(tmp_path, monkeypatch)
    assert runner.invoke(app, ["plan", str(tmp_path)]).exit_code == 0
    assert runner.invoke(app, ["apply", str(tmp_path)]).exit_code == 0
    source_path.write_text(
        source_path.read_text(encoding="utf-8") + "# post migration\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["rollback", str(tmp_path)])

    assert result.exit_code == 2
    assert "explicitly pass --force" in result.output
    assert source_path.read_text(encoding="utf-8").endswith("# post migration\n")


# ── what `pymolt codemods` showed, now shown by plan ──────────────────────────

def _configured_without_baseline(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask==2.0.3\n", encoding="utf-8")
    (tmp_path / "requirements-target.txt").write_text("flask==3.0.0\n", encoding="utf-8")
    EnvConfig(
        selected_manifest="requirements.txt", selected_tool=ToolChoice.UV,
        base_python="3.11", target_python="3.12",
    ).save(tmp_path / ".pymolt" / "env_config.json")


def test_plan_picks_up_the_manifest_assess_wrote(tmp_path, monkeypatch):
    _configured_without_baseline(tmp_path)
    seen = {}

    def fake_resolve(project_dir, *, target_dependency_file=None, **kw):
        seen["target_file"] = target_dependency_file
        return [], "target dependency file requirements-target.txt"

    monkeypatch.setattr(codemod_service, "resolve_codemod_migrations", fake_resolve)

    result = runner.invoke(app, ["plan", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert seen["target_file"].name == "requirements-target.txt"


def test_plan_shows_recipes_diff_and_writes_a_relative_patch(tmp_path, monkeypatch):
    _configured_without_baseline(tmp_path)
    path, source = _mock_plan_services(tmp_path, monkeypatch)
    patch_path = tmp_path / "migration.patch"

    result = runner.invoke(app, ["plan", str(tmp_path), "--patch", str(patch_path)])

    assert result.exit_code == 0, result.output
    assert "flask.helpers.safe_join → werkzeug.utils.safe_join" in result.output
    assert "+from werkzeug.utils import safe_join" in result.output
    patch = patch_path.read_text(encoding="utf-8")
    assert "--- a/app.py" in patch and "+++ b/app.py" in patch
    assert str(tmp_path) not in patch
    assert path.read_text(encoding="utf-8") == source


def test_plan_names_what_needs_manual_review(tmp_path, monkeypatch):
    from pymolt.codemods.rules import CodemodRule, RuleAdvisory

    _configured_without_baseline(tmp_path)
    rule = CodemodRule(
        library="pandas", from_version="1.0.0", to_version="1.2.0",
        match="$DF.lookup($ROWS, $COLS)", rewrite=["$RESULT = $DF.to_numpy()"],
        confidence="heuristic",
    )
    advisory = RuleAdvisory(
        line=5, column=0, site_kind="return", severity="not-applied",
        reason="call is the return value of a function; not auto-rewritable",
    )
    preview = FilePreview(
        path=str(tmp_path / "app.py"), old_source="return df.lookup(rows, cols)\n",
        new_source="return df.lookup(rows, cols)\n", sites=0, rules=[rule],
        advisories=[advisory],
    )
    monkeypatch.setattr(
        codemod_service, "resolve_codemod_migrations",
        lambda *a, **k: ([DependencyMigration("pandas", "1.0.0", "1.2.0")], "target"),
    )
    monkeypatch.setattr(
        codemod_service, "preview_codemods", lambda *a, **k: ({"pandas": [rule]}, [preview]),
    )

    result = runner.invoke(app, ["plan", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "$DF.lookup($ROWS, $COLS)" in result.output and "heuristic" in result.output
    assert "Manual review needed" in result.output
    assert "not auto-rewritable" in result.output
    assert "No call sites matched" in result.output


def test_plan_lists_a_blocked_rule_as_manual_work(tmp_path, monkeypatch):
    from pymolt.codemods.models import BlockedRewrite
    from pymolt.codemods.rules import CodemodRule

    _configured_without_baseline(tmp_path)
    rule = CodemodRule(
        library="pandas", from_version="1.0.0", to_version="1.2.0",
        match="$DF.lookup($ROWS, $COLS)", rewrite=["$RESULT = $DF.to_numpy()"],
        confidence="heuristic",
    )
    source = "vals = df.lookup(rows, cols)\n"
    (tmp_path / "app.py").write_text(source, encoding="utf-8")
    preview = FilePreview(path=str(tmp_path / "app.py"), old_source=source, new_source=source,
                          sites=0, rules=[rule],
                          blocked=[BlockedRewrite(rule=rule, sites=1, lines=[1])])
    monkeypatch.setattr(
        codemod_service, "resolve_codemod_migrations",
        lambda *a, **k: ([DependencyMigration("pandas", "1.0.0", "1.2.0")], "target"),
    )
    monkeypatch.setattr(
        codemod_service, "preview_codemods", lambda *a, **k: ({"pandas": [rule]}, [preview]),
    )

    text = runner.invoke(app, ["plan", str(tmp_path)])
    as_json = runner.invoke(app, ["plan", str(tmp_path), "--json"])
    agent = runner.invoke(app, ["plan", str(tmp_path), "--agent"])

    assert text.exit_code == 0, text.output
    assert "app.py:1" in text.output and "[blocked rule]" in text.output
    [item] = json.loads(as_json.stdout)["manual"]
    assert (item["path"], item["lines"], item["sites"]) == ("app.py", [1], 1)
    assert "not applied" in item["reason"]
    assert json.loads(agent.stdout)["data"]["manual_total"] == 1


def test_apply_of_a_plan_with_no_files_says_it_changed_nothing(tmp_path, monkeypatch):
    _configured_project(tmp_path)
    migration = DependencyMigration("flask", "2.0.3", "3.0.0")
    monkeypatch.setattr(codemod_service, "resolve_codemod_migrations",
                        lambda *a, **k: ([migration], "requirements-target.txt"))
    monkeypatch.setattr(codemod_service, "preview_codemods", lambda *a, **k: ({}, []))
    planned = runner.invoke(app, ["plan", str(tmp_path)])
    assert planned.exit_code == 0, planned.output

    applied = runner.invoke(app, ["apply", str(tmp_path)])

    assert applied.exit_code == 0, applied.output
    assert "it changed no source file" in applied.output
    assert "env-1 pinned the target versions in requirements.txt" in applied.output
    assert "Migration applied" not in applied.output
    assert (tmp_path / "requirements.txt").read_text() == "flask==3.0.0\n"


def test_plan_agent_prints_a_json_failure_when_the_resolver_fails(tmp_path, monkeypatch):
    _configured_project(tmp_path)

    def failing(*args, **kwargs):
        raise codemod_service.PlanInputError(
            "the target manifest requirements-target.txt does not resolve: Failed to build `x`",
            "Culprit(s): x. Fix or loosen them.")

    monkeypatch.setattr(codemod_service, "resolve_codemod_migrations", failing)
    result = runner.invoke(app, ["plan", str(tmp_path), "--agent"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False and "does not resolve" in payload["error"]
    assert "Culprit(s): x" in payload["hint"]


def test_apply_lists_what_the_plan_leaves_to_a_person(tmp_path, monkeypatch):
    from pymolt.codemods.rules import RuleAdvisory

    _configured_project(tmp_path)
    source_path, original = _mock_plan_services(tmp_path, monkeypatch)
    caveat = RuleAdvisory(line=1, column=0, site_kind="precondition", severity="precondition",
                          reason="safe_join now raises on absolute paths")
    skipped = RuleAdvisory(line=7, column=4, site_kind="return", severity="not-applied",
                           reason="call is the return value; not auto-rewritable")
    preview = FilePreview(path=str(source_path), old_source=original,
                          new_source="from werkzeug.utils import safe_join\n", sites=1,
                          advisories=[caveat, skipped])
    monkeypatch.setattr(codemod_service, "preview_codemods",
                        lambda *a, **k: ({"flask": []}, [preview]))
    assert runner.invoke(app, ["plan", str(tmp_path)]).exit_code == 0

    text = runner.invoke(app, ["apply", str(tmp_path)])
    as_json = runner.invoke(app, ["apply", str(tmp_path), "--json"])

    assert text.exit_code == 0, text.output
    assert "Left to you" in text.output
    assert "app.py:1 — [caveat] safe_join now raises" in text.output
    assert "app.py:7 — [not applied] call is the return value" in " ".join(text.output.split())
    payload = json.loads(as_json.stdout)
    assert [(a["path"], a["line"], a["severity"]) for a in payload["advisories"]] == [
        ("app.py", 1, "precondition"), ("app.py", 7, "not-applied")]
    assert payload["manual"] == []


def test_plan_without_a_baseline_points_at_the_baseline_capture(tmp_path, monkeypatch):
    """QA H3: with no baseline, `apply` refuses the plan, so plan's next is the baseline capture
    (human, --agent and PLAN.md alike), and the human output says why and claims no risk check."""
    _configured_project(tmp_path)
    (tmp_path / ".pymolt" / "contract_state.json").unlink()
    _mock_plan_services(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)

    human = runner.invoke(app, ["plan", "."])
    assert human.exit_code == 0, human.output
    assert "Next: pymolt capture baseline --mode tests -- " in human.stdout
    assert "pymolt apply" not in human.stdout.split("Next:")[1]
    assert "No baseline capture is bound to this plan" in human.stderr
    assert "the call sites the baseline exercised" not in human.stdout
    plan_md = (tmp_path / ".pymolt" / "PLAN.md").read_text(encoding="utf-8")
    assert "Next: `pymolt capture baseline --mode tests -- " in plan_md

    agent = json.loads(runner.invoke(app, ["plan", ".", "--agent"]).stdout)
    assert agent["data"]["next"]["command"].startswith(
        "pymolt capture baseline --mode tests -- ")
    assert agent["data"]["warnings"][0].startswith("No baseline capture is bound")
    assert "Record the calls your code makes" in agent["hint"]


def test_a_plan_saved_elsewhere_is_applied_with_its_file(tmp_path, monkeypatch):
    _configured_project(tmp_path)
    _mock_plan_services(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    out = runner.invoke(app, ["plan", ".", "--output", "other-plan.json"])
    assert out.exit_code == 0, out.output
    assert "Next: pymolt apply . --plan other-plan.json" in out.stdout


def test_no_risks_without_a_baseline_is_not_a_clean_bill(capsys):
    """Deltas but no baseline: no site was exercised to rate, so the plan must not say the
    baseline's call sites carry no risk (QA H3)."""
    from types import SimpleNamespace

    from pymolt.interfaces.cli import commands

    report = SimpleNamespace(risks=[], needs_attention=[], fixed=[], notes=[],
                             deltas=[SimpleNamespace(subject="dependency", quality="complete")])
    commands._render_risks(report, baseline=False)
    assert "the baseline exercised" not in capsys.readouterr().out
    commands._render_risks(report, baseline=True)
    assert "the baseline exercised" in capsys.readouterr().out

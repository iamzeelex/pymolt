from __future__ import annotations

import sys

import pytest

from pymolt.codemods.client import DependencyMigration
from pymolt.codemods.models import ApiImpact, CodemodPattern, FilePreview
from pymolt.ingestion.config import EnvConfig, ToolChoice
from pymolt.migration_plan import (
    MigrationConflictError,
    MigrationPlan,
    MigrationPlanError,
    MigrationRunState,
    _migration_lock,
    _save_run_state,
    apply_migration_plan,
    create_migration_plan,
    inspect_migration_plan,
    rollback_migration,
)
from pymolt.migration_state import MigrationReceipt
from pymolt.verify import service
from pymolt.verify.models import CaptureMode


def _project(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask==2.0.3\n", encoding="utf-8")
    target = tmp_path / "requirements-target.txt"
    target.write_text("flask==3.0.0\n", encoding="utf-8")
    EnvConfig(
        selected_manifest="requirements.txt",
        selected_tool=ToolChoice.UV,
        base_python="3.11",
        target_python="3.12",
    ).save(tmp_path / ".pymolt" / "env_config.json")
    script = tmp_path / "baseline.py"
    script.write_text("import json\njson.loads('{}')\n", encoding="utf-8")
    service.capture_named_trace(
        tmp_path,
        "baseline",
        CaptureMode.LIVE_COMMAND,
        command=[sys.executable, str(script)],
        target="json",
    )
    return target


def _plan(tmp_path, *, files=1):
    target = _project(tmp_path)
    previews = []
    for index in range(files):
        path = tmp_path / f"app{index}.py"
        old = "from flask.helpers import safe_join\n"
        new = "from werkzeug.utils import safe_join\n"
        path.write_text(old, encoding="utf-8")
        previews.append(FilePreview(
            path=str(path),
            old_source=old,
            new_source=new,
            sites=1,
            patterns=[CodemodPattern(
                old_qualname="flask.helpers.safe_join",
                new_qualname="werkzeug.utils.safe_join",
                kind="rewrite-import",
            )],
        ))
    plan = create_migration_plan(
        tmp_path,
        previews,
        [DependencyMigration("flask", "2.0.3", "3.0.0")],
        target_python="3.12",
        source_manifest="requirements.txt",
        target_manifest_path=target,
        files_scanned=files,
        predict=False,  # plan/apply integrity only: prediction is tests/verify/test_predict.py
    )
    return plan


def test_plan_round_trip_is_relative_and_inspectable(tmp_path):
    plan = _plan(tmp_path)

    saved = plan.save(tmp_path)
    loaded = MigrationPlan.load(tmp_path)

    assert saved == tmp_path / ".pymolt" / "migration_plan.json"
    assert loaded == plan
    assert plan.changes[0].path == "app0.py"
    assert plan.target_manifest_path == "requirements-target.txt"
    assert inspect_migration_plan(tmp_path, plan).valid is True


def test_plan_freezes_axiom_api_impacts(tmp_path):
    target = _project(tmp_path)
    plan = create_migration_plan(
        tmp_path,
        [],
        [DependencyMigration("flask", "2.0.3", "3.0.0")],
        target_python="3.12",
        source_manifest="requirements.txt",
        target_manifest_path=target,
        files_scanned=0,
        impacts={"flask": [ApiImpact(
            path="flask.helpers.safe_join",
            replacement_path="werkzeug.utils.safe_join",
            kind="object-removed",
            state="removed",
            risk="high",
            explanation="public function moved",
        )]},
        predict=False,
    )

    assert plan.impacts[0].package == "flask"
    assert plan.impacts[0].path == "flask.helpers.safe_join"
    assert MigrationPlan.model_validate(plan.model_dump()).impacts == plan.impacts


def test_apply_uses_exact_plan_and_rollback_restores_it(tmp_path):
    plan = _plan(tmp_path, files=2)
    plan.save(tmp_path)

    receipt = apply_migration_plan(tmp_path, plan)

    assert receipt.run_id == plan.plan_id
    assert receipt.status == "applied"
    assert receipt.plan_path
    assert "werkzeug.utils" in (tmp_path / "app0.py").read_text(encoding="utf-8")
    assert "werkzeug.utils" in (tmp_path / "app1.py").read_text(encoding="utf-8")
    frozen = tmp_path / ".pymolt" / "migrations" / plan.plan_id / "plan.json"
    assert frozen.is_file()

    rolled_plan, rolled_at, rolled_run = rollback_migration(tmp_path)

    assert rolled_plan.plan_id == plan.plan_id
    assert rolled_run == plan.plan_id      # a plan's first run carries the plan's id
    assert rolled_at
    assert "flask.helpers" in (tmp_path / "app0.py").read_text(encoding="utf-8")
    restored_receipt = MigrationReceipt.load(tmp_path)
    assert restored_receipt is not None
    assert restored_receipt.status == "rolled_back"


def test_apply_records_the_environment_the_migration_started_from(tmp_path):
    """The baseline is later judged against this, not against the upgraded manifest."""
    plan = _plan(tmp_path)
    started_from = service.environment_fingerprint(tmp_path)

    receipt = apply_migration_plan(tmp_path, plan)

    assert started_from is not None
    assert receipt.environment_fingerprint == started_from
    (tmp_path / "requirements.txt").write_text("flask==3.0.0\n", encoding="utf-8")
    saved = MigrationReceipt.load(tmp_path)
    assert saved is not None
    assert saved.starting_fingerprint(tmp_path) == started_from


def test_rollback_invalidates_post_migration_capture(tmp_path):
    plan = _plan(tmp_path)
    apply_migration_plan(tmp_path, plan)
    script = tmp_path / "post.py"
    script.write_text("import json\njson.loads('{}')\n", encoding="utf-8")
    service.capture_named_trace(
        tmp_path,
        "post_migration",
        CaptureMode.LIVE_COMMAND,
        command=[sys.executable, str(script)],
        target="json",
    )

    rollback_migration(tmp_path)

    state = service.load_contract_state(tmp_path)
    assert state.post_migration is None
    aside = state.diagnostic_captures[-1]
    assert "invalidated by rollback" in (aside.validity_reason or "")
    assert aside.set_aside_by == plan.plan_id
    # Its recording moved to the archive: the next post-migration capture cannot overwrite it.
    assert "/archive/" in aside.trace_path
    recorded = (tmp_path / aside.trace_path).read_bytes()
    assert MigrationReceipt.load(tmp_path).files_restored == ["app0.py"]

    service.capture_named_trace(
        tmp_path, "post_migration", CaptureMode.LIVE_COMMAND,
        command=[sys.executable, str(script)], target="json",
    )
    assert (tmp_path / aside.trace_path).read_bytes() == recorded


def _post_capture(tmp_path):
    script = tmp_path / "post.py"
    script.write_text("import json\njson.loads('{}')\n", encoding="utf-8")
    return service.capture_named_trace(
        tmp_path, "post_migration", CaptureMode.LIVE_COMMAND,
        command=[sys.executable, str(script)], target="json",
    )


def test_rollback_of_a_run_that_changed_no_file_keeps_the_post_migration_capture(tmp_path):
    """A no-op apply's rollback restores nothing, so the post-migration capture still records
    the code on disk: it stays in its slot, and verify still compares it against the baseline
    and reads that run's plan (the legacy harness rolls back after every run)."""
    target = _project(tmp_path)
    plan = create_migration_plan(
        tmp_path, [], [DependencyMigration("flask", "2.0.3", "3.0.0")],
        target_python="3.12", source_manifest="requirements.txt",
        target_manifest_path=target, files_scanned=0, predict=False,
        impacts={"flask": [ApiImpact(
            path="json.loads", kind="signature-changed", state="changed", risk="low",
            explanation="test impact",
        )]},
    )
    apply_migration_plan(tmp_path, plan)
    post = _post_capture(tmp_path)

    rollback_migration(tmp_path)

    state = service.load_contract_state(tmp_path)
    assert state.post_migration == post
    receipt = MigrationReceipt.load(tmp_path)
    assert receipt.status == "rolled_back"
    assert receipt.files_restored == []
    assert receipt.in_effect
    report = service.build_contract_report_from_state(tmp_path)
    assert report.diff is not None or report.fact_diff  # compared, not "no capture yet"
    assert not any("no post-migration capture" in reason for reason in report.verdict_reasons)
    assert any("had changed no file" in note for note in report.notes)
    assert service._applied_plan_impact_targets(tmp_path) == ["json.loads"]


def test_verify_names_the_post_migration_capture_a_rollback_set_aside(tmp_path):
    plan = _plan(tmp_path)
    apply_migration_plan(tmp_path, plan)
    _post_capture(tmp_path)

    rollback_migration(tmp_path)

    aside = service.set_aside_capture(service.load_contract_state(tmp_path))
    assert aside is not None
    report = service.build_contract_report_from_state(tmp_path)
    reason = next(r for r in report.verdict_reasons if "post-migration capture" in r)
    assert "set aside by the rollback of migration " + plan.plan_id[:8] in reason
    assert aside.trace_path in reason
    assert "yet" not in reason
    assert not MigrationReceipt.load(tmp_path).in_effect


def test_apply_rejects_a_stale_plan_before_any_write(tmp_path):
    plan = _plan(tmp_path, files=2)
    second = tmp_path / "app1.py"
    second.write_text("# user edit\n" + second.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(MigrationPlanError, match="stale migration plan"):
        apply_migration_plan(tmp_path, plan)

    assert "flask.helpers" in (tmp_path / "app0.py").read_text(encoding="utf-8")
    assert second.read_text(encoding="utf-8").startswith("# user edit")


def test_apply_recovers_a_partial_prepared_run_before_retry(tmp_path):
    plan = _plan(tmp_path, files=2)
    run_dir = tmp_path / ".pymolt" / "migrations" / plan.plan_id
    run_dir.mkdir(parents=True)
    plan.save(tmp_path, run_dir / "plan.json")
    _save_run_state(run_dir, MigrationRunState(
        run_id=plan.plan_id,
        status="prepared",
        prepared_at="2026-01-01T00:00:00+00:00",
    ))
    (tmp_path / "app0.py").write_text(
        plan.changes[0].new_source, encoding="utf-8"
    )

    receipt = apply_migration_plan(tmp_path, plan)

    assert receipt.status == "applied"
    for index, change in enumerate(plan.changes):
        assert (tmp_path / f"app{index}.py").read_text(encoding="utf-8") == (
            change.new_source
        )


def test_project_lock_rejects_concurrent_source_mutation(tmp_path):
    with _migration_lock(tmp_path, "first"):
        with pytest.raises(MigrationConflictError, match="another migration"):
            with _migration_lock(tmp_path, "second"):
                pass


def test_apply_rejects_plan_that_was_not_bound_to_baseline(tmp_path):
    plan = _plan(tmp_path).model_copy(update={
        "baseline_trace_path": None,
        "baseline_captured_at": None,
        "baseline_trace_sha256": None,
    })

    with pytest.raises(MigrationPlanError, match="not bound"):
        apply_migration_plan(tmp_path, plan)


def test_apply_rejects_baseline_trace_tampering(tmp_path):
    plan = _plan(tmp_path)
    state = service.load_contract_state(tmp_path)
    with open(state.baseline.trace_path, "a", encoding="utf-8") as stream:
        stream.write('{"q":"json.loads"}\n')

    with pytest.raises(MigrationPlanError, match="trace content changed"):
        apply_migration_plan(tmp_path, plan)


def test_apply_rejects_tampered_plan_contents(tmp_path):
    plan = _plan(tmp_path)
    plan.changes[0].new_source = "raise SystemExit('tampered')\n"

    with pytest.raises(MigrationPlanError, match="replacement-source hash"):
        apply_migration_plan(tmp_path, plan)

    assert (tmp_path / "app0.py").read_text(encoding="utf-8") == (
        "from flask.helpers import safe_join\n"
    )


def test_apply_rejects_plan_without_target_manifest_hash(tmp_path):
    plan = _plan(tmp_path).model_copy(update={"target_manifest_sha256": None})

    with pytest.raises(MigrationPlanError, match="target manifest hash is missing"):
        apply_migration_plan(tmp_path, plan)

    assert (tmp_path / "app0.py").read_text(encoding="utf-8") == (
        "from flask.helpers import safe_join\n"
    )


def test_run_id_cannot_escape_the_migration_directory(tmp_path):
    plan = _plan(tmp_path).model_copy(update={"plan_id": "../../outside"})

    with pytest.raises(MigrationPlanError, match="invalid migration run id"):
        apply_migration_plan(tmp_path, plan)

    assert not (tmp_path / ".pymolt" / "outside").exists()


def test_apply_requires_a_valid_baseline(tmp_path):
    plan = _plan(tmp_path)
    state = service.load_contract_state(tmp_path)
    state.baseline = None
    state.save(tmp_path / ".pymolt" / "contract_state.json")

    with pytest.raises(MigrationPlanError, match="baseline"):
        apply_migration_plan(tmp_path, plan)


def test_apply_rolls_sources_back_when_receipt_cannot_be_saved(tmp_path, monkeypatch):
    plan = _plan(tmp_path)

    def fail_save(self, project_dir):
        raise OSError("disk full")

    monkeypatch.setattr(MigrationReceipt, "save", fail_save)

    with pytest.raises(OSError, match="disk full"):
        apply_migration_plan(tmp_path, plan)

    assert (tmp_path / "app0.py").read_text(encoding="utf-8") == (
        "from flask.helpers import safe_join\n"
    )


def test_rollback_preserves_post_migration_edits_without_force(tmp_path):
    plan = _plan(tmp_path)
    apply_migration_plan(tmp_path, plan)
    path = tmp_path / "app0.py"
    path.write_text(path.read_text(encoding="utf-8") + "# later edit\n", encoding="utf-8")

    with pytest.raises(MigrationPlanError, match="post-migration edits"):
        rollback_migration(tmp_path)

    assert path.read_text(encoding="utf-8").endswith("# later edit\n")

    rollback_migration(tmp_path, force=True)
    assert path.read_text(encoding="utf-8") == "from flask.helpers import safe_join\n"


def test_rollback_restores_applied_sources_when_metadata_fails(tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    apply_migration_plan(tmp_path, plan)
    path = tmp_path / "app0.py"

    def fail_save(self, project_dir):
        raise OSError("disk full")

    monkeypatch.setattr(MigrationReceipt, "save", fail_save)

    with pytest.raises(OSError, match="disk full"):
        rollback_migration(tmp_path)

    assert path.read_text(encoding="utf-8") == (
        "from werkzeug.utils import safe_join\n"
    )


def test_apply_writes_the_ledger_from_the_written_files(tmp_path):
    import json

    plan = _plan(tmp_path, files=2)
    apply_migration_plan(tmp_path, plan)

    ledger = json.loads((tmp_path / ".pymolt" / "apply_ledger.json").read_text())
    assert ledger["schema"] == "pymolt.apply_ledger/2"
    [event] = ledger["events"]
    # Previews with no recorded recipe chain: the builder keeps the code change in one step
    # (the plan's manifest pin, env-1, is not written by this apply).
    [step] = [s for s in plan.steps if s.type in ("codemod", "python-fix")]
    assert step.do.recipe.kind == "whole-file"
    assert (event["seq"], event["event"], event["run_id"], event["step_id"], event["wrote"]) == (
        1, "apply", plan.plan_id, step.id, True)
    assert [f["file"] for f in event["files"]] == ["app0.py", "app1.py"]
    assert event["files"][0]["after"] == plan.changes[0].new_sha256
    entries = ledger["entries"]
    assert {e["file"] for e in entries} == {"app0.py", "app1.py"}
    entry = next(e for e in entries if e["file"] == "app0.py")
    written = (tmp_path / "app0.py").read_text().splitlines()
    a, b = entry["new_lines"]
    assert "werkzeug.utils" in "\n".join(written[a - 1:b])
    assert (entry["epoch"], entry["run_id"], entry["step_id"]) == (1, plan.plan_id, step.id)
    assert entry["anchor_new"]["stmt"] == "from werkzeug.utils import safe_join"
    assert entry["anchor_old"]["stmt"] == "from flask.helpers import safe_join"
    assert (entry["kind"], entry["rule"], entry["old_symbol"], entry["new_symbol"]) == (
        "rewrite-import", "rewrite-import:flask.helpers.safe_join→werkzeug.utils.safe_join",
        "flask.helpers.safe_join", "werkzeug.utils.safe_join")
    assert entry["claim"] is None and entry["basis"] is None   # a bare pattern makes no claim


def test_apply_again_on_an_applied_plan_is_a_no_op_that_says_so(tmp_path):
    plan = _plan(tmp_path, files=2)
    first = apply_migration_plan(tmp_path, plan)
    ledger = tmp_path / ".pymolt" / "apply_ledger.json"
    ledger_before = ledger.read_text(encoding="utf-8")
    sources = [(tmp_path / f"app{i}.py").read_text(encoding="utf-8") for i in range(2)]

    again = apply_migration_plan(tmp_path, plan)

    assert first.outcome == "applied"
    assert again.outcome == "already_applied"
    assert again.run_id == first.run_id and again.applied_at == first.applied_at
    assert [(tmp_path / f"app{i}.py").read_text(encoding="utf-8") for i in range(2)] == sources
    assert ledger.read_text(encoding="utf-8") == ledger_before    # no second ledger
    assert not (tmp_path / ".pymolt" / "archive").exists()
    assert MigrationReceipt.load(tmp_path) == first


def test_apply_again_after_a_later_edit_refuses_and_points_at_rollback(tmp_path):
    """The old message called the plan stale and its hint led to a second, stacked plan."""
    plan = _plan(tmp_path)
    apply_migration_plan(tmp_path, plan)
    path = tmp_path / "app0.py"
    path.write_text(path.read_text(encoding="utf-8") + "# later edit\n", encoding="utf-8")

    with pytest.raises(MigrationConflictError, match="already applied") as caught:
        apply_migration_plan(tmp_path, plan)

    assert "stale" not in str(caught.value)
    assert "rollback" in caught.value.hint and "stack" in caught.value.hint
    assert path.read_text(encoding="utf-8").endswith("# later edit\n")


def test_a_rolled_back_plan_applies_again_as_a_new_run(tmp_path):
    import json

    plan = _plan(tmp_path, files=2)
    first = apply_migration_plan(tmp_path, plan)
    rollback_migration(tmp_path)

    again = apply_migration_plan(tmp_path, plan)

    assert again.outcome == "applied" and again.status == "applied"
    assert again.run_id != first.run_id and again.plan_id == plan.plan_id
    for index, change in enumerate(plan.changes):
        assert (tmp_path / f"app{index}.py").read_text(encoding="utf-8") == change.new_source
    run_dir = tmp_path / ".pymolt" / "migrations" / again.run_id
    state = json.loads((run_dir / "state.json").read_text())
    assert (state["status"], state["plan_id"], state["attempt"]) == ("applied", plan.plan_id, 2)
    assert MigrationPlan.load(tmp_path, run_dir / "plan.json") == plan
    ledger = json.loads((tmp_path / ".pymolt" / "apply_ledger.json").read_text())
    assert [(e["event"], e["run_id"], e.get("restored")) for e in ledger["events"]] == [
        ("apply", first.run_id, None), ("rollback", first.run_id, True),
        ("apply", again.run_id, None)]
    # The first run stays rolled back; the default rollback undoes the run in effect.
    with pytest.raises(MigrationConflictError, match="not rollback-ready"):
        rollback_migration(tmp_path, run_id=first.run_id)
    _plan_again, _at, rolled = rollback_migration(tmp_path)
    assert rolled == again.run_id
    assert (tmp_path / "app0.py").read_text(encoding="utf-8") == plan.changes[0].old_source
    third = apply_migration_plan(tmp_path, plan)
    assert third.run_id not in (first.run_id, again.run_id)
    assert apply_migration_plan(tmp_path, plan).outcome == "already_applied"


def test_re_apply_after_rollback_refuses_a_file_changed_since(tmp_path):
    plan = _plan(tmp_path)
    apply_migration_plan(tmp_path, plan)
    rollback_migration(tmp_path)
    path = tmp_path / "app0.py"
    path.write_text("# user edit\n" + path.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(MigrationConflictError, match="source changed after planning"):
        apply_migration_plan(tmp_path, plan)

    assert path.read_text(encoding="utf-8").startswith("# user edit")


def test_re_apply_after_rollback_still_needs_the_plan_s_baseline(tmp_path):
    plan = _plan(tmp_path)
    apply_migration_plan(tmp_path, plan)
    rollback_migration(tmp_path)
    script = tmp_path / "baseline.py"
    service.capture_named_trace(
        tmp_path, "baseline", CaptureMode.LIVE_COMMAND,
        command=[sys.executable, str(script)], target="json",
    )

    with pytest.raises(MigrationPlanError, match="baseline"):
        apply_migration_plan(tmp_path, plan)

    assert (tmp_path / "app0.py").read_text(encoding="utf-8") == plan.changes[0].old_source


def test_an_interrupted_re_apply_is_recovered_under_its_own_run(tmp_path):
    plan = _plan(tmp_path, files=2)
    apply_migration_plan(tmp_path, plan)
    rollback_migration(tmp_path)
    run_id = "ab" * 16
    run_dir = tmp_path / ".pymolt" / "migrations" / run_id
    run_dir.mkdir(parents=True)
    plan.save(tmp_path, run_dir / "plan.json")
    _save_run_state(run_dir, MigrationRunState(
        run_id=run_id, status="prepared", prepared_at="2026-01-01T00:00:00+00:00",
        plan_id=plan.plan_id, attempt=2,
    ))
    (tmp_path / "app0.py").write_text(plan.changes[0].new_source, encoding="utf-8")

    receipt = apply_migration_plan(tmp_path, plan)

    assert receipt.run_id == run_id
    for index, change in enumerate(plan.changes):
        assert (tmp_path / f"app{index}.py").read_text(encoding="utf-8") == change.new_source


def _post_capture_and_report(tmp_path):
    _post_capture(tmp_path)
    return service.build_contract_report_from_state(tmp_path)


def test_pins_bumped_before_apply_leave_the_plan_and_baseline_valid(tmp_path):
    """Bumping the source manifest's pins is the migration: before apply it makes neither the
    plan nor the baseline stale, and verify takes the baseline as the starting point."""
    from pymolt.status import build_status

    plan = _plan(tmp_path)
    (tmp_path / "requirements.txt").write_text("flask==3.0.0\n", encoding="utf-8")

    assert inspect_migration_plan(tmp_path, plan).valid
    receipt = apply_migration_plan(tmp_path, plan)

    assert receipt.environment_setup_fingerprint == plan.environment_setup_fingerprint
    assert receipt.environment_fingerprint != plan.environment_fingerprint
    report = _post_capture_and_report(tmp_path)
    assert report.baseline_stale is False
    assert not any("STALE EVIDENCE" in note for note in report.notes)
    assert any("contents of the same manifest" in note for note in report.notes)
    status = build_status(tmp_path)
    assert status.phase("baseline").state == "done"
    assert "manifest's contents changed" in status.phase("baseline").detail


def test_a_setup_change_after_planning_still_makes_the_plan_stale(tmp_path):
    plan = _plan(tmp_path)
    EnvConfig(
        selected_manifest="requirements.txt", selected_tool=ToolChoice.UV,
        base_python="3.10", target_python="3.12",
    ).save(tmp_path / ".pymolt" / "env_config.json")

    with pytest.raises(MigrationConflictError, match="configured environment changed"):
        apply_migration_plan(tmp_path, plan)


def test_a_target_manifest_change_after_planning_still_makes_the_plan_stale(tmp_path):
    plan = _plan(tmp_path)
    (tmp_path / "requirements-target.txt").write_text("flask==3.1.0\n", encoding="utf-8")

    with pytest.raises(MigrationConflictError, match="target manifest changed"):
        apply_migration_plan(tmp_path, plan)


def test_a_plan_without_a_setup_fingerprint_is_checked_on_the_full_one(tmp_path):
    plan = _plan(tmp_path).model_copy(update={"environment_setup_fingerprint": None})
    (tmp_path / "requirements.txt").write_text("flask==3.0.0\n", encoding="utf-8")

    reasons = inspect_migration_plan(tmp_path, plan).reasons

    assert reasons == ["configured environment or source manifest changed after planning"]


def test_a_baseline_from_another_setup_is_still_refused(tmp_path):
    """Only a move of the same manifest's contents is forgiven: a baseline taken under another
    setup is not this migration's starting point, even when the plan was made after it."""
    from pymolt.migration_plan import MigrationEvidenceError

    _project(tmp_path)
    EnvConfig(
        selected_manifest="requirements.txt", selected_tool=ToolChoice.UV,
        base_python="3.10", target_python="3.12",
    ).save(tmp_path / ".pymolt" / "env_config.json")
    (tmp_path / "app0.py").write_text("from flask.helpers import safe_join\n", encoding="utf-8")
    plan = create_migration_plan(
        tmp_path,
        [FilePreview(path=str(tmp_path / "app0.py"),
                     old_source="from flask.helpers import safe_join\n",
                     new_source="from werkzeug.utils import safe_join\n", sites=1)],
        [DependencyMigration("flask", "2.0.3", "3.0.0")],
        target_python="3.12", source_manifest="requirements.txt",
        target_manifest_path=tmp_path / "requirements-target.txt", files_scanned=1,
        predict=False,
    )

    with pytest.raises(MigrationEvidenceError, match="invalid or stale"):
        apply_migration_plan(tmp_path, plan)


def _blocked_preview(tmp_path, *, with_change=False):
    from pymolt.codemods.models import BlockedRewrite
    from pymolt.codemods.rules import CodemodRule

    # An unverified LEGACY rule: its evidence says "same object", its golden pair failed.
    blocked = CodemodRule(
        library="flask", from_version="2.0.3", to_version="3.0.0",
        match="flask.helpers.escape", rewrite=["markupsafe.escape"], kind="rewrite-import",
        old_qualname="flask.helpers.escape", new_qualname="markupsafe.escape",
        confidence="heuristic",
        evidence=["pymolt.delta MOVED: flask.helpers.escape -> markupsafe.escape",
                  "pymolt knowledge flask-escape (curated, 2026-01-01)"])
    old = "from flask.helpers import escape\nfrom flask.helpers import safe_join\n"
    new = old.replace("from flask.helpers import safe_join",
                      "from werkzeug.utils import safe_join") if with_change else old
    path = tmp_path / "app0.py"
    path.write_text(old, encoding="utf-8")
    applied = CodemodPattern(old_qualname="flask.helpers.safe_join",
                             new_qualname="werkzeug.utils.safe_join", kind="rewrite-import",
                             evidence=["pymolt.delta MOVED: flask.helpers.safe_join"])
    return FilePreview(
        path=str(path), old_source=old, new_source=new, sites=1 if with_change else 0,
        rules=[blocked], patterns=[applied] if with_change else [],
        blocked=[BlockedRewrite(rule=blocked, sites=1, lines=[1])])


def _plan_from(tmp_path, previews, downgraded=()):
    target = _project(tmp_path)
    return create_migration_plan(
        tmp_path, previews, [DependencyMigration("flask", "2.0.3", "3.0.0")],
        target_python="3.12", source_manifest="requirements.txt",
        target_manifest_path=target, files_scanned=1, predict=False,
        downgraded=list(downgraded))


def test_a_file_whose_only_hit_was_a_blocked_rule_becomes_a_manual_item(tmp_path):
    plan = _plan_from(tmp_path, [_blocked_preview(tmp_path)], downgraded=[
        "flask-escape: flask.helpers.escape → markupsafe.escape (its golden pair did not "
        "verify)",
        "flask-other: flask.x → flask.y (needs `import flask` in app.py)"])

    assert plan.changes == []
    blocked, unmatched = plan.manual
    assert (blocked.path, blocked.lines, blocked.sites) == ("app0.py", [1], 1)
    assert blocked.rule == "rewrite-import:flask.helpers.escape→markupsafe.escape"
    assert "its golden pair did not verify" in blocked.reason
    assert unmatched.path is None and unmatched.rule.startswith("flask-other:")
    assert MigrationPlan.model_validate(plan.model_dump()).manual == plan.manual


def test_a_blocked_rule_is_never_a_recipe_claim_or_ledger_entry(tmp_path):
    import json

    from pymolt.apply_ledger import claim_for

    preview = _blocked_preview(tmp_path, with_change=True)
    plan = _plan_from(tmp_path, [preview])

    [change] = plan.changes
    assert change.recipes == ["rewrite-import:flask.helpers.safe_join→werkzeug.utils.safe_join"]
    assert set(change.claims) == set(change.recipes)
    assert claim_for(preview.blocked[0].rule) is None    # unverified: no claim, any evidence
    apply_migration_plan(tmp_path, plan)
    ledger = json.loads((tmp_path / ".pymolt" / "apply_ledger.json").read_text())
    assert {e["rule"] for e in ledger["entries"]} == set(change.recipes)
    assert [m.path for m in plan.manual] == ["app0.py"]


def test_a_rule_blocked_by_the_preview_never_reaches_the_ledger(tmp_path):
    """End to end through the real blocking path: an unverified LEGACY rule whose evidence
    says "same object" matches in a file a verified rule rewrites. It is listed as manual
    work, and neither the plan's recipes and claims nor the ledger mention it."""
    import json

    from pymolt.codemods.apply import preview_rules_repo
    from pymolt.codemods.rules import CodemodRule

    def legacy(old, new, confidence):
        return CodemodRule(
            library="flask", from_version="2.0.3", to_version="3.0.0", match=old,
            rewrite=[new], kind="rewrite-import", old_qualname=old, new_qualname=new,
            confidence=confidence, evidence=[f"pymolt.delta MOVED: {old} -> {new}"])

    target = _project(tmp_path)
    source = "from flask.helpers import escape\nfrom flask.helpers import safe_join\n"
    (tmp_path / "app0.py").write_text(source, encoding="utf-8")
    blocked = legacy("flask.helpers.escape", "markupsafe.escape", "heuristic")
    verified = legacy("flask.helpers.safe_join", "werkzeug.utils.safe_join", "verified")
    previews = [p for p in preview_rules_repo(tmp_path, [blocked, verified], verified_only=True)
                if p.path.endswith("app0.py")]
    plan = create_migration_plan(
        tmp_path, previews, [DependencyMigration("flask", "2.0.3", "3.0.0")],
        target_python="3.12", source_manifest="requirements.txt",
        target_manifest_path=target, files_scanned=1, predict=False)

    [change] = plan.changes
    key = "rewrite-import:flask.helpers.safe_join→werkzeug.utils.safe_join"
    assert change.recipes == [key] and change.claims == {key: "same-object"}
    assert "from flask.helpers import escape" in change.new_source
    [manual] = plan.manual
    assert manual.rule == "rewrite-import:flask.helpers.escape→markupsafe.escape"
    assert (manual.path, manual.lines) == ("app0.py", [1])
    apply_migration_plan(tmp_path, plan)
    ledger = json.loads((tmp_path / ".pymolt" / "apply_ledger.json").read_text())
    assert {(e["rule"], e["claim"], e["basis"]) for e in ledger["entries"]} == {
        (key, "preserve", "same-object")}

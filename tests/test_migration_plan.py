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

    rolled_plan, rolled_at = rollback_migration(tmp_path)

    assert rolled_plan.plan_id == plan.plan_id
    assert rolled_at
    assert "flask.helpers" in (tmp_path / "app0.py").read_text(encoding="utf-8")
    restored_receipt = MigrationReceipt.load(tmp_path)
    assert restored_receipt is not None
    assert restored_receipt.status == "rolled_back"


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
    assert "invalidated by rollback" in (
        state.diagnostic_captures[-1].validity_reason or ""
    )


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

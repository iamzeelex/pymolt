from __future__ import annotations

import json

from pymolt.migration_plan import MigrationPlan, PlannedApiImpact
from pymolt.migration_state import MigrationReceipt
from pymolt.verify import service
from pymolt.verify.models import CaptureMode
from pymolt.verify.report import ContractReport
from pymolt.verify.service import TraceCaptureResult


def _applied_impact_plan(tmp_path):
    run_id = "b" * 32
    run_dir = tmp_path / ".pymolt" / "migrations" / run_id
    plan = MigrationPlan(
        plan_id=run_id,
        target_python="3.13",
        source_manifest="requirements.txt",
        impacts=[PlannedApiImpact(
            package="flask",
            path="flask.helpers.safe_join",
            replacement_path="werkzeug.utils.safe_join",
            kind="object-removed",
            state="removed",
            risk="high",
            explanation="moved",
        )],
    )
    plan_path = plan.save(tmp_path, run_dir / "plan.json")
    MigrationReceipt(
        run_id=run_id,
        target_python="3.13",
        source_manifest="requirements.txt",
        plan_path=str(plan_path),
    ).save(tmp_path)
    return plan


def test_post_capture_automatically_targets_applied_axiom_impacts(tmp_path, monkeypatch):
    _applied_impact_plan(tmp_path)
    captured = {}

    def fake_capture(target, command, out_path, backend, **kwargs):
        captured.update(kwargs)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps({
            "q": "werkzeug.utils.safe_join",
            "t": "return",
            "in": {"bound": {}},
            "result": None,
        }) + "\n")
        return TraceCaptureResult(
            out_path=str(out_path), events=1, processes=1, where="local",
            impact_targets=kwargs.get("impact_targets", []),
        )

    monkeypatch.setattr(service, "capture_trace", fake_capture)

    slot = service.capture_named_trace(
        tmp_path,
        "post_migration",
        CaptureMode.LIVE_COMMAND,
        command=["python", "app.py"],
    )

    assert captured["impact_targets"] == [
        "flask.helpers.safe_join", "werkzeug.utils.safe_join"
    ]
    assert slot.impact_targets == captured["impact_targets"]


def test_report_automatically_loads_impacts_from_applied_plan(tmp_path, monkeypatch):
    plan = _applied_impact_plan(tmp_path)
    observed = {}

    def fake_report(project_dir, **kwargs):
        observed.update(kwargs)
        return ContractReport(root=str(project_dir))

    monkeypatch.setattr(service, "build_contract_report", fake_report)

    service.build_contract_report_from_state(tmp_path)

    assert observed["changed_api_paths"] == plan.impacts


def test_empty_applied_impact_set_falls_back_to_full_contract(tmp_path, monkeypatch):
    run_id = "c" * 32
    run_dir = tmp_path / ".pymolt" / "migrations" / run_id
    plan = MigrationPlan(
        plan_id=run_id,
        target_python="3.13",
        source_manifest="requirements.txt",
    )
    plan_path = plan.save(tmp_path, run_dir / "plan.json")
    MigrationReceipt(
        run_id=run_id,
        target_python="3.13",
        source_manifest="requirements.txt",
        plan_path=str(plan_path),
    ).save(tmp_path)
    observed = {}

    def fake_report(project_dir, **kwargs):
        observed.update(kwargs)
        return ContractReport(root=str(project_dir))

    monkeypatch.setattr(service, "build_contract_report", fake_report)

    service.build_contract_report_from_state(tmp_path)

    assert observed["changed_api_paths"] is None

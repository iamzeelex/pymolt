from __future__ import annotations

import json

from typer.testing import CliRunner

from pymolt.interfaces.cli.commands import app
from pymolt.interfaces.cli.output import EXIT_FINDING, EXIT_INCONCLUSIVE, EXIT_USAGE
from pymolt.migration_state import MigrationReceipt
from pymolt.verify.models import VerificationVerdict
from pymolt.verify.report import ContractReport
from pymolt.verify.unified import UnifiedVerificationReport

runner = CliRunner()


def _passing_report(path):
    return ContractReport(root=str(path), verdict=VerificationVerdict.PASS)


def _gate(threshold):
    return {"threshold": threshold}


def test_root_verify_persists_one_machine_readable_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "pymolt.verify.service.build_contract_report_from_state",
        lambda *args, **kwargs: _passing_report(tmp_path),
    )

    result = runner.invoke(app, ["verify", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["verdict"] == "pass"
    assert UnifiedVerificationReport.load(tmp_path).verdict is VerificationVerdict.PASS


def test_root_verify_rejects_invalid_custom_comparator_as_usage(tmp_path):
    result = runner.invoke(app, [
        "verify", str(tmp_path), "--profile", "custom", "--json",
    ])

    assert result.exit_code == EXIT_USAGE
    assert "requires --comparator" in json.loads(result.stdout)["error"]


def test_skipped_golden_keeps_root_verdict_inconclusive(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "pymolt.verify.service.build_contract_report_from_state",
        lambda *args, **kwargs: _passing_report(tmp_path),
    )
    old = tmp_path / "old.json"
    new = tmp_path / "new.json"
    old.write_text(json.dumps({
        "version": "1", "values": {"point": {"__opaque__": "Thing"}}
    }))
    new.write_text(json.dumps({
        "version": "2", "values": {"point": {"__opaque__": "Thing"}}
    }))

    result = runner.invoke(app, [
        "verify", str(tmp_path),
        "--golden-before", str(old), "--golden-after", str(new), "--json",
    ])

    assert result.exit_code == EXIT_INCONCLUSIVE
    assert json.loads(result.stdout)["verdict"] == "inconclusive"


def test_root_verify_folds_persisted_cascade_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "pymolt.verify.service.build_contract_report_from_state",
        lambda *args, **kwargs: _passing_report(tmp_path),
    )
    cascade = tmp_path / "cascade.json"
    cascade.write_text(json.dumps({
        "scope": "full",
        "results": [{
            "node": {
                "name": "dep.api",
                "old_version": "1",
                "new_version": "2",
                "trace_prefix": "dep",
            },
            "verdict": "behavior-changed",
            "evidence_level": "trace",
        }],
    }))

    result = runner.invoke(app, [
        "verify", str(tmp_path), "--cascade", str(cascade), "--json",
    ])

    assert result.exit_code == EXIT_FINDING, result.output
    assert json.loads(result.stdout)["verdict"] == "fail"


def test_failed_canary_can_execute_explicit_auto_rollback(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "pymolt.verify.service.build_contract_report_from_state",
        lambda *args, **kwargs: _passing_report(tmp_path),
    )
    MigrationReceipt(
        run_id="a" * 32,
        target_python="3.13",
        source_manifest="requirements.txt",
    ).save(tmp_path)
    rolled_back = []
    monkeypatch.setattr(
        "pymolt.migration_plan.rollback_migration",
        lambda project_dir, run_id=None: rolled_back.append(run_id),
    )
    policy = tmp_path / "policy.json"
    metrics = tmp_path / "metrics.json"
    policy.write_text(json.dumps({
        "error_rate": _gate(0.01),
        "p95_latency_ms": _gate(100),
        "p99_latency_ms": _gate(200),
        "output_shape_mismatch_rate": _gate(0.01),
        "dependency_call_count_drift": _gate(0.1),
        "dropped_event_rate": _gate(0.01),
    }))
    metrics.write_text(json.dumps({
        "error_rate": 0.02,
        "p95_latency_ms": 90,
        "p99_latency_ms": 190,
        "output_shape_mismatch_rate": 0,
        "dependency_call_count_drift": 0,
        "dropped_event_rate": 0,
    }))

    result = runner.invoke(app, [
        "verify", str(tmp_path), "--policy", str(policy),
        "--metrics", str(metrics), "--auto-rollback", "--json",
    ])

    assert result.exit_code == EXIT_FINDING, result.output
    assert rolled_back == ["a" * 32]
    assert json.loads(result.stdout)["rollback_executed"] is True

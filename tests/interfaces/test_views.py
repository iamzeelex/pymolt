"""The agent view layer (pymolt/interfaces/views.py).

These views are what `pymolt <cmd> --agent` prints and what the MCP tools return,
so the honesty rules live here once: the service's own verdict wording, true counts,
completeness flags, and no swallowed warnings.
"""
from __future__ import annotations

import json
import re

import pytest

from pymolt.interfaces import views

_ENVELOPE = {"ok", "summary", "data", "full_report_path", "hint"}
_COMMON = {"warnings", "complete", "truncated"}


def _assert_envelope(view: dict, keys: set[str]) -> dict:
    assert set(view) == _ENVELOPE
    assert view["ok"] is True
    assert set(view["data"]) == keys | _COMMON, set(view["data"]) ^ (keys | _COMMON)
    json.dumps(view)  # plain data, nothing that needs a custom encoder
    return view["data"]


# ── budgets and notes ────────────────────────────────────────────────────────


def test_fit_answer_keeps_everything_that_fits():
    rows = [f"pkg{i} 1.0>2.0 upgrade" for i in range(40)]
    assert views.fit_answer(rows) == (rows, 0)


def test_fit_answer_budgets_by_size_not_item_count():
    rows = ["x" * 500 for _ in range(40)]
    fitted, dropped = views.fit_answer(rows, budget=2000)
    assert 0 < len(fitted) < 40
    assert dropped == 40 - len(fitted)
    assert sum(len(r) + 1 for r in fitted) <= 2000


def test_fit_answer_always_returns_at_least_one_row():
    assert views.fit_answer(["y" * 9000], budget=100) == (["y" * 9000], 0)


def test_cap_list_reports_the_remainder():
    assert views.cap_list(list(range(views.LIST_CAP))) == (list(range(views.LIST_CAP)), 0)
    capped, dropped = views.cap_list(list(range(views.LIST_CAP + 5)))
    assert len(capped) == views.LIST_CAP and dropped == 5


def test_truncation_note_states_completeness():
    assert views.truncation_note(31, 0, None) == "All 31 listed."
    note = views.truncation_note(20, 11, "/tmp/full.json")
    assert "20 of 31" in note and "11 omitted" in note and "/tmp/full.json" in note


def test_failure_envelope_matches_the_cli_json_failure():
    assert views.failure("boom") == {"ok": False, "error": "boom"}
    assert views.failure("boom", "fix it") == {"ok": False, "error": "boom", "hint": "fix it"}


# ── status ───────────────────────────────────────────────────────────────────


def test_status_surfaces_environment_problems():
    from pymolt.status import EnvironmentCheck, FunnelStatus, PhaseState

    report = FunnelStatus(
        project_dir="/work/proj",
        phases=[PhaseState(phase="scan", state="done"),
                PhaseState(phase="baseline", state="stale", detail="manifest changed")],
        checks=[EnvironmentCheck(name="base environment", state="error",
                                 detail="container abc is not running", hint="start it")],
        next_command="pymolt capture baseline",
        next_reason="The baseline no longer describes the project.",
    )
    view = views.status_view(report)
    data = _assert_envelope(view, {"phases", "checks", "next_command", "next_reason", "notes"})
    assert view["full_report_path"] is None, "status fits whole: nothing to point at"
    assert data["warnings"] == ["base environment: container abc is not running (start it)"]
    assert "ERROR base environment" in view["summary"]
    assert "Stale, redo: baseline." in view["summary"]
    assert view["summary"].endswith("Next: pymolt capture baseline")


def test_status_without_a_next_command_does_not_claim_completion():
    from pymolt.status import FunnelStatus

    view = views.status_view(FunnelStatus(project_dir="/p", next_reason="verify said FAIL."))
    assert "complete" not in view["summary"].lower()
    assert "verify said FAIL." in view["summary"]


# ── scan ─────────────────────────────────────────────────────────────────────


def test_scan_keeps_excluded_roots_and_notes(tmp_path):
    from pymolt.discovery.models import ProjectRoot, SurfaceMap
    from pymolt.scan.report import ScanReport
    from pymolt.verify.contact_map import Contact, ContactMap

    report = ScanReport(
        root_dir=str(tmp_path),
        surfaces=SurfaceMap(root_dir=str(tmp_path), project_roots=[ProjectRoot(path=".")],
                            excluded_roots=["vendor/lib: vendored tree"],
                            notes=["Dockerfile ARG PY_VERSION has no default"]),
        contact_map=ContactMap(root=str(tmp_path), files_scanned=2,
                               contacts=[Contact(caller="app.f", target="requests.get",
                                                 dep="requests")],
                               by_dep={"requests": ["requests.get"]},
                               notes=["could not parse broken.py"]),
    )
    view = views.scan_view(report, tmp_path)
    data = _assert_envelope(view, {"root_count", "roots", "excluded_roots", "edge_totals",
                                   "divergences", "contacts"})
    assert data["excluded_roots"] == ["vendor/lib: vendored tree"]
    assert "could not parse broken.py" in data["warnings"]
    assert "Dockerfile ARG PY_VERSION has no default" in data["warnings"]
    assert data["contacts"]["per_dependency"] == ["requests 1"]
    assert "feasib" not in json.dumps(view).lower()
    assert view["full_report_path"].endswith(".pymolt/agent/scan.json")


# ── setup ────────────────────────────────────────────────────────────────────


def _options(**overrides):
    from pymolt.setup.service import ManifestOption, SetupOptions, ToolOption

    fields = dict(
        project_dir="/p",
        manifests=[ManifestOption(name="requirements.txt", mode="pip", fixation="pinned",
                                  is_lock=False, is_default=True)],
        tools=[ToolOption(name="uv", available=True, detail="found", is_default=True)],
        base_python_default="3.12.1", base_python_source="assumed",
        notes=["No Python version is declared"],
    )
    fields.update(overrides)
    return SetupOptions(**fields)


def test_setup_choices_lists_when_nothing_is_given():
    assert views.setup_choices(_options(), manifest=None, tool=None, base_python=None,
                               target_python=None) is None


def test_setup_choices_names_what_is_missing():
    with pytest.raises(views.AgentInputError, match="tool, base_python, target_python"):
        views.setup_choices(_options(), manifest="requirements.txt", tool=None,
                            base_python=None, target_python=None)


def test_setup_choices_keeps_an_assumed_base_python_assumed():
    """Echoing the detected default back must not launder a guess into a fact."""
    choices = views.setup_choices(_options(), manifest="requirements.txt", tool="uv",
                                  base_python="3.12.1", target_python="3.13")
    assert choices.base_python_source == "assumed"
    stated = views.setup_choices(_options(), manifest="requirements.txt", tool="uv",
                                 base_python="3.8", target_python="3.13")
    assert stated.base_python_source == "declared"


def test_setup_view_has_one_key_set_listing_or_saved(tmp_path):
    from pymolt.ingestion.config import EnvConfig

    keys = {"saved", "config_path", "config", "options"}
    listing = _assert_envelope(views.setup_view(_options(), tmp_path), keys)
    assert listing["saved"] is False and listing["options"]["manifests"] == ["requirements.txt"]
    assert listing["warnings"] == ["No Python version is declared"]

    config = EnvConfig(selected_manifest="requirements.txt", base_python="3.8",
                       target_python="3.13")
    saved = _assert_envelope(views.setup_view(_options(), tmp_path, config=config), keys)
    assert saved["saved"] is True and saved["options"] is None
    assert saved["config"]["target_python"] == "3.13"


# ── assess ───────────────────────────────────────────────────────────────────

_ASSESS_KEYS = {
    "target_python", "verdict", "baseline_tier", "baseline_unresolved", "source_manifest",
    "source_fixation", "resolution_quality", "target_resolved", "target_error",
    "changes_format", "changes", "changes_total", "unchanged", "status_counts",
    "manual_zone", "manual_zone_count", "risk", "target_manifest_path", "target_env",
    "baseline_env",
}


def _row(name, base, target, status):
    return {"name": name, "baseline_version": base, "target_version": target, "status": status,
            "declared_constraint": None, "origin": "pypi", "direct": True,
            "manual_bridge": False}


def _assess_result(**overrides):
    from pymolt.assess.service import AssessResult

    fields = dict(
        project_dir="/p", source_manifest="requirements.txt", source_fixation="ranged",
        resolution_quality="resolved", detected_python="3.8", target_python="3.12",
        target_resolved=True, baseline_tier="bronze",
        warnings=["configured container abc not running"],
        rows=[_row("flask", "1.1.4", "3.0.3", "upgrade"),
              _row("click", "8.1.7", "8.1.7", "unchanged"),
              _row("itsdangerous", "1.1.0", "2.2.0", "upgrade")],
    )
    fields.update(overrides)
    return AssessResult(**fields)


def test_assess_uses_the_service_verdict_and_never_says_feasible(tmp_path):
    from pymolt.assess.service import BaselineTier, compatibility_verdict

    view = views.assess_view(_assess_result(), tmp_path)
    data = _assert_envelope(view, _ASSESS_KEYS)
    title = compatibility_verdict("3.12", None, BaselineTier.BRONZE)[0]
    assert view["summary"].splitlines()[0] == re.sub(r"^\W+", "", title)
    assert data["verdict"]["level"] == "unproven"
    assert not re.search(r"\bfeasible\b", json.dumps(view), re.IGNORECASE)
    assert "capture and verify decide" in view["summary"]


def test_assess_counts_only_what_differs(tmp_path):
    """`unchanged` rows are not changes: the old summary said '3 packages differ' here."""
    data = views.assess_view(_assess_result(), tmp_path)["data"]
    assert data["changes"] == ["flask 1.1.4>3.0.3 upgrade", "itsdangerous 1.1.0>2.2.0 upgrade"]
    assert data["changes_total"] == 2 and data["unchanged"] == 1
    assert data["complete"] is True and data["truncated"] == {}


def test_assess_does_not_drop_warnings(tmp_path):
    view = views.assess_view(_assess_result(), tmp_path, risk_requested=True)
    warnings = view["data"]["warnings"]
    assert "configured container abc not running" in warnings
    assert any("risk was requested but not assessed" in w for w in warnings)
    assert "2 warning(s)" in view["summary"]


def test_assess_blocked_target_says_so(tmp_path):
    result = _assess_result(target_resolved=False, target_error="no solution for flask>=9",
                            rows=[_row("flask", "1.1.4", None, "conflict")])
    view = views.assess_view(result, tmp_path)
    assert view["data"]["verdict"]["level"] == "blocked"
    assert "do not resolve" in view["summary"].splitlines()[0]
    assert "conflict" in view["hint"]


def test_assess_unresolved_base_is_not_read_as_additions(tmp_path):
    result = _assess_result(baseline_unresolved=True, baseline_tier="none",
                            rows=[_row("flask", None, "3.0.3", "added")])
    view = views.assess_view(result, tmp_path, **views.assess_hints(tmp_path, result))
    assert "No base was resolved" in view["summary"]
    assert view["data"]["baseline_env"]["base_python"] == "3.8"
    assert "Build the base environment" in view["hint"]


def test_assess_full_report_is_the_json_payload(tmp_path):
    result = _assess_result()
    view = views.assess_view(result, tmp_path)
    with open(view["full_report_path"], encoding="utf-8") as stream:
        assert json.load(stream) == json.loads(json.dumps(views.assess_payload(result)))


# ── capture ──────────────────────────────────────────────────────────────────

_CAPTURE_KEYS = {
    "when", "accepted", "validity", "validity_reason", "events", "processes", "returncode",
    "trace_path", "command_log", "filled_slots", "env_note", "duration_seconds",
    "dropped_events", "coverage_pct", "archived_previous", "impact_targets",
    "deployment_id", "request_id", "correlation_id", "metadata_path", "red",
}


def test_capture_not_accepted_is_said_first_and_untraced_processes_are_warned():
    from pymolt.verify.models import CaptureMode, CaptureValidity, ContractSlot, ContractState

    slot = ContractSlot(
        trace_path="/p/.pymolt/diag.jsonl", captured_at="2026-11-14T00:00:00",
        mode=CaptureMode.TEST_SUITE, validity=CaptureValidity.COMMAND_FAILED,
        validity_reason="the command exited 2", returncode=2,
        activation_failures=[{"pid": 7, "python": "3.6", "error": "SyntaxError"}],
    )
    view = views.capture_view(slot, when="baseline", state=ContractState())
    data = _assert_envelope(view, _CAPTURE_KEYS)
    assert data["accepted"] is False and data["validity"] == "command-failed"
    assert view["summary"].startswith("NOT accepted as 'baseline' evidence: the command exited 2")
    assert any("UNTRACED" in w for w in data["warnings"])


def test_red_capture_is_ok_and_says_so():
    from pymolt.verify.models import CaptureMode, CaptureValidity, ContractSlot, ContractState

    slot = ContractSlot(
        trace_path="/p/t.jsonl", captured_at="now", mode=CaptureMode.TEST_SUITE,
        validity=CaptureValidity.VALID, events=12, processes=1, returncode=1,
        red={"returncode": 1, "failed": 2, "summary": "2 failed"})
    view = views.capture_view(slot, when="baseline", state=ContractState(baseline=slot))
    assert view["ok"] is True and view["data"]["red"]["failed"] == 2
    assert "RED" in view["summary"]


def test_capture_accepted_points_at_the_other_side():
    from pymolt.verify.models import CaptureMode, CaptureValidity, ContractSlot, ContractState

    slot = ContractSlot(trace_path="/p/t.jsonl", captured_at="now", mode=CaptureMode.TEST_SUITE,
                        validity=CaptureValidity.VALID, events=12, processes=1)
    view = views.capture_view(slot, when="baseline", state=ContractState(baseline=slot))
    assert view["data"]["filled_slots"] == ["baseline"]
    assert "post-migration" in view["hint"]


# ── plan ─────────────────────────────────────────────────────────────────────

_PLAN_KEYS = {
    "plan_id", "plan_path", "source", "dependencies", "per_package", "files_to_change",
    "files_total", "rewrite_sites", "diffs", "advisories", "advisories_total", "api_impacts",
    "api_impacts_total", "baseline_bound", "risks_status", "risks_attention",
    "risks_attention_total", "risks_fixed", "risks_fixed_total",
}


def test_plan_shows_manual_review_and_an_unbound_baseline():
    from pymolt.migration_plan import (
        MigrationPlan,
        PlannedAdvisory,
        PlannedDependency,
        PlannedFileChange,
    )

    plan = MigrationPlan(
        target_python="3.12", source_manifest="requirements.txt",
        dependencies=[PlannedDependency(name="pandas", from_version="1.5.3", to_version="2.2.3")],
        changes=[PlannedFileChange(path="core.py", old_sha256="a", new_sha256="b",
                                   old_source="s.append(t)\n", new_source="pd.concat([s, t])\n",
                                   sites=1)],
        advisories=[PlannedAdvisory(path="core.py", line=24, column=0, severity="manual",
                                    reason="DataFrame.mean default changed")],
    )
    view = views.plan_view(plan, {"pandas": []}, "requirements-target.txt",
                           plan_path="/p/.pymolt/migration_plan.json")
    data = _assert_envelope(view, _PLAN_KEYS)
    assert view["full_report_path"] == "/p/.pymolt/migration_plan.json"
    assert data["files_to_change"] == ["core.py"]
    assert "pd.concat" in data["diffs"][0]["diff"]
    assert data["advisories"] == ["core.py:24 [not applied] DataFrame.mean default changed"]
    assert data["baseline_bound"] is False
    assert any("No baseline capture is bound" in w for w in data["warnings"])
    assert data["dependencies"] == ["pandas 1.5.3>2.2.3"]


# ── verify ───────────────────────────────────────────────────────────────────

_VERIFY_KEYS = {
    "verdict", "reasons", "saved_path", "baseline_stale", "static_targets", "confirmed",
    "blind", "dynamic_only", "trust_pct", "compared", "changes", "change_counts",
    "calibration",
}


def _unified(**fields):
    from pymolt.verify.report import ContractReport
    from pymolt.verify.unified import build_unified_verification

    return build_unified_verification(ContractReport(root="/p", **fields))


def test_verify_pass_with_blind_calls_says_what_it_did_not_cover(tmp_path):
    from pymolt.verify.models import VerificationVerdict

    unified = _unified(verdict=VerificationVerdict.PASS, static_targets=10, confirmed=7,
                       blind=3, trust=0.7, diff={"result_changed": 0},
                       notes=["trust basis: executed files"])
    view = views.verify_view(unified, tmp_path, saved_path=tmp_path / "verification.json")
    data = _assert_envelope(view, _VERIFY_KEYS)
    assert view["summary"].startswith("VERDICT: PASS")
    assert "3 BLIND symbol(s) were never run" in view["summary"]
    assert data["warnings"] == ["trust basis: executed files"]
    assert data["compared"] is True and view["hint"] is None


def test_verify_stale_evidence_and_unsaved_verdict_are_stated(tmp_path):
    from pymolt.verify.models import VerificationVerdict

    unified = _unified(verdict=VerificationVerdict.INCONCLUSIVE, baseline_stale=True,
                       verdict_reasons=["baseline is stale"])
    view = views.verify_view(unified, tmp_path, saved_path=None)
    assert "STALE EVIDENCE" in view["summary"]
    assert "Not saved" in view["summary"]
    assert view["full_report_path"].endswith(".pymolt/agent/verify.json")
    assert view["data"]["compared"] is False
    assert "post-migration" in view["hint"]


def test_verify_lists_the_changed_calls(tmp_path):
    from pymolt.verify.models import VerificationVerdict

    fact_diff = {"entries": [{"symbol": "pandas.DataFrame.mean", "category": "changed"},
                             {"symbol": "requests.get", "category": "equal"}],
                 "counts": {"changed": 1, "equal": 1, "disappeared": 0}}
    unified = _unified(verdict=VerificationVerdict.FAIL, fact_diff=fact_diff,
                       diff={"result_changed": 1}, verdict_reasons=["1 call changed"])
    data = views.verify_view(unified, tmp_path, saved_path=None)["data"]
    assert data["changes"] == {"changed": ["pandas.DataFrame.mean"]}
    assert data["change_counts"] == {"changed": 1, "equal": 1}


def test_plan_view_splits_predicted_risks_by_whether_the_plan_fixes_them():
    from pymolt.interfaces.views import _Lists, _risk_data
    from pymolt.verify.predict import PredictedRisk, PredictionReport, RiskSite, RiskTier

    def risk(tier, line, symbol, fixed):
        return PredictedRisk(id=f"{symbol}@{line}", tier=tier, symbol=symbol,
                             site=RiskSite(file="toyapp/core.py", func="f", line=line),
                             reason="r", fixed_by_plan=fixed)

    report = PredictionReport(risks=[
        risk(RiskTier.HIGH, 24, "pandas.core.frame.DataFrame.mean", False),
        risk(RiskTier.CERTAIN, 13, "pandas.core.series.Series.append", True),
    ])
    data = _risk_data(_Lists(), report)
    assert data["risks_status"] == "complete"
    assert data["risks_attention_total"] == 1 and data["risks_fixed_total"] == 1
    assert data["risks_attention"][0].startswith("high toyapp/core.py:24")
    assert "[fixed by the plan]" in data["risks_fixed"][0]
    assert _risk_data(_Lists(), None)["risks_status"] is None

"""assess honesty: baseline trust as a medallion tier (Bronze→Gold), a verdict that
never overclaims, and a missing legacy interpreter that degrades to a hint.
"""

from __future__ import annotations

import json

from pymolt.assess.service import (
    BaselineTier,
    _is_missing_interpreter_error,
    baseline_tests_passed,
    build_comparison_rows,
    classify_baseline_tier,
    compatibility_verdict,
)
from pymolt.setup.baseline_hint import build_baseline_hint

# ── medallion classifier ──────────────────────────────────────────────────────

def test_tier_none_when_baseline_unresolved():
    assert classify_baseline_tier(
        baseline_resolved=False, source_fixation="pinned", tests_passed=True
    ) is BaselineTier.NONE


def test_tier_gold_when_pinned():
    assert classify_baseline_tier(
        baseline_resolved=True, source_fixation="pinned", tests_passed=False
    ) is BaselineTier.GOLD


def test_tier_silver_when_unpinned_but_tests_pass():
    assert classify_baseline_tier(
        baseline_resolved=True, source_fixation="intent", tests_passed=True
    ) is BaselineTier.SILVER


def test_tier_bronze_when_unpinned_and_unvalidated():
    assert classify_baseline_tier(
        baseline_resolved=True, source_fixation="intent", tests_passed=False
    ) is BaselineTier.BRONZE


# ── silver evidence comes from the contract baseline slot ─────────────────────

def _write_state(tmp_path, slot):
    d = tmp_path / ".pymolt"
    d.mkdir(parents=True, exist_ok=True)
    (d / "contract_state.json").write_text(json.dumps({"baseline": slot}), encoding="utf-8")


def test_tests_passed_true_on_returncode_zero(tmp_path):
    _write_state(tmp_path, {"returncode": 0})
    assert baseline_tests_passed(tmp_path) is True


def test_tests_passed_false_on_nonzero_or_missing(tmp_path):
    _write_state(tmp_path, {"returncode": 1})
    assert baseline_tests_passed(tmp_path) is False
    assert baseline_tests_passed(tmp_path / "nope") is False  # no state file at all


# ── the verdict, keyed on the tier ────────────────────────────────────────────

def test_verdict_none_without_target():
    assert compatibility_verdict(None, None, BaselineTier.GOLD) is None


def test_verdict_blocked_on_target_error():
    title, _body, style = compatibility_verdict("3.12", "resolution conflict", BaselineTier.GOLD)
    assert style == "red" and "do not resolve" in title


def test_verdict_none_tier_is_not_feasible():
    title, body, style = compatibility_verdict("3.12", None, BaselineTier.NONE)
    assert style == "yellow" and "no baseline" in title and "nothing to compare" in body


def test_verdict_bronze_is_amber_with_upgrade_path():
    title, body, style = compatibility_verdict("3.12", None, BaselineTier.BRONZE)
    assert style == "yellow" and "BRONZE" in title
    assert "SILVER" in body and "GOLD" in body  # names the explicit upgrade path


def test_verdict_silver_is_green_but_scoped():
    title, body, style = compatibility_verdict("3.12", None, BaselineTier.SILVER)
    assert style == "green" and "SILVER" in title and "pymolt contract" in body


def test_verdict_gold_is_green():
    title, body, style = compatibility_verdict("3.12", None, BaselineTier.GOLD)
    assert style == "green" and "GOLD" in title and "pymolt contract" in body


# ── degrade + hint ────────────────────────────────────────────────────────────

def test_missing_interpreter_error_detected():
    assert _is_missing_interpreter_error(ValueError("Python 3.6 interpreter was not found in PATH"))
    assert not _is_missing_interpreter_error(ValueError("some unrelated resolution conflict"))


def test_missing_container_error_detected():
    from pymolt.assess.service import _is_missing_container_error

    # the exact wording compile_legacy raises for a dead configured container
    assert _is_missing_container_error(ValueError(
        "Specified Docker container 'deadbeef0000' "
        "was not found running or is not matching."
    ))
    assert _is_missing_container_error(ValueError("Container 'x' is not matching"))
    # a real resolution conflict must never be classified as a dead container
    conflict = ValueError("resolution conflict: flask==3 vs werkzeug<2")
    assert not _is_missing_container_error(conflict)
    # the phrase without the word "container" is somebody else's error
    assert not _is_missing_container_error(ValueError("was not found running"))


def test_container_classifier_round_trips_the_real_error(monkeypatch, tmp_path):
    """The classifier and compile_legacy's actual message must stay in lockstep —
    a rewording on either side silently breaks the degrade path otherwise."""
    import pytest

    import pymolt.ingestion.fallback_compiler as fc
    from pymolt.assess.service import _is_missing_container_error

    monkeypatch.setattr(fc.shutil, "which", lambda c: None)  # no docker -> no containers
    (tmp_path / "requirements.txt").write_text("flask\n", encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        fc.compile_legacy(tmp_path / "requirements.txt", "3.6.1", container_id="deadbeef0000")
    assert _is_missing_container_error(exc_info.value)


def test_comparison_rows_tolerate_missing_baseline():
    assert build_comparison_rows(None, None, "3.12", target_error=None) == []


def test_baseline_hint_carries_recipe_and_agent_brief(tmp_path):
    hint = build_baseline_hint(tmp_path, "3.6")
    assert "python:3.6-slim" in hint.suggested_dockerfile
    assert hint.rerun_command.startswith("pymolt assess") and "--container" in hint.rerun_command
    assert "Python 3.6" in hint.agent_brief and "do NOT" in hint.agent_brief

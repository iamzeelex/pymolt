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
    assert style == "yellow" and "no base" in title and "nothing to compare" in body


def test_verdict_bronze_is_amber_with_upgrade_path():
    title, body, style = compatibility_verdict("3.12", None, BaselineTier.BRONZE)
    assert style == "yellow" and "BRONZE" in title
    assert "SILVER" in body and "GOLD" in body  # names the explicit upgrade path


def test_verdict_bronze_example_is_the_projects_own():
    """QA V2-6: the example was always `tensorflow>=1.3` → 2.x, whatever the project."""
    from pymolt.assess.service import unpinned_example

    _title, body, _style = compatibility_verdict("3.12", None, BaselineTier.BRONZE)
    assert "tensorflow" not in body and "e.g." not in body
    rows = [
        {"name": "numpy", "declared_constraint": ">=1.10", "baseline_version": "1.16.6",
         "target_version": "1.26.4", "direct": True, "status": "upgrade"},
        {"name": "pandas", "declared_constraint": ">=0.18", "baseline_version": "0.22.0",
         "target_version": "3.1.1", "direct": True, "status": "upgrade"},
        {"name": "pytz", "declared_constraint": None, "baseline_version": "2017.2",
         "target_version": "2025.2", "direct": False, "status": "upgrade"},
        {"name": "six", "declared_constraint": "==1.10.0", "baseline_version": "1.10.0",
         "target_version": "1.17.0", "direct": True, "status": "upgrade"},
        {"name": "scipy", "declared_constraint": ">=0.14", "baseline_version": "1.2.0",
         "target_version": "1.2.0", "direct": True, "status": "unchanged"},
    ]
    example = unpinned_example(rows)
    assert example == "`pandas>=0.18` → 3.1.1"      # direct, and a new major version
    _title, body, _style = compatibility_verdict("3.12", None, BaselineTier.BRONZE, example)
    assert "(e.g. `pandas>=0.18` → 3.1.1)." in body
    assert unpinned_example([r for r in rows if r["name"] in ("six", "scipy")]) is None


def test_verdict_silver_is_green_but_scoped():
    title, body, style = compatibility_verdict("3.12", None, BaselineTier.SILVER)
    assert style == "green" and "SILVER" in title and "capture and verify" in body


def test_verdict_gold_is_green():
    title, body, style = compatibility_verdict("3.12", None, BaselineTier.GOLD)
    assert style == "green" and "GOLD" in title and "pymolt verify" in body


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


def test_an_echoed_assumed_base_python_stays_assumed():
    """The MCP adapter hands the config's base_python back to run_assess; that must not
    turn setup's guess into a fact that lets the tier reach GOLD."""
    from pymolt.assess.service import base_python_assumed

    guessed = {"base_python": "3.12.12", "base_python_source": "assumed"}
    assert base_python_assumed(guessed, None) is True
    assert base_python_assumed(guessed, "3.12.12") is True     # echoed back: still a guess
    assert base_python_assumed(guessed, "3.9") is False        # a real override is a fact
    assert base_python_assumed({"base_python": "3.9", "base_python_source": "detected"},
                               None) is False


# ── resolver failures: culprits and the stderr tail (real uv shapes) ──────────

_UV_BUILD_FAILURE = """\
  \x1b[31m×\x1b[0m Failed to build `bcolz==1.2.1`
  ├─▶ The build backend returned an error
  ╰─▶ Call to
      `setuptools.build_meta:__legacy__.prepare_metadata_for_build_wheel`
      failed (exit status: 1)

      [stderr]
      ModuleNotFoundError: No module named 'setuptools'

      hint: This error likely indicates that `bcolz@1.2.1` depends on
      `setuptools`, but doesn't declare it as a build dependency.
"""

_UV_NO_SOLUTION = """\
  × No solution found when resolving dependencies:
  ╰─▶ Because only numpy<=1.19.1 is available and pandas==0.22.0 depends on
      numpy>=1.9, we can conclude that pandas==0.22.0 cannot be used.
      And because you require pandas==0.22.0, we can conclude that your
      requirements are unsatisfiable.
"""


def test_culprits_from_a_failed_build():
    from pymolt.assess.service import parse_resolver_culprits

    assert parse_resolver_culprits(_UV_BUILD_FAILURE) == ["bcolz"]


def test_culprits_from_a_no_solution_chain():
    from pymolt.assess.service import parse_resolver_culprits

    assert parse_resolver_culprits(_UV_NO_SOLUTION) == ["numpy", "pandas"]


def test_culprits_from_registry_misses_and_git_urls():
    from pymolt.assess.service import parse_resolver_culprits

    assert parse_resolver_culprits(
        "  × No solution found when resolving dependencies:\n"
        "  ╰─▶ Because `Foo_Bar` was not found in the package registry and you require "
        "Foo_Bar, we can conclude that your requirements are unsatisfiable.\n"
    ) == ["foo-bar"]
    assert parse_resolver_culprits(
        "error: Failed to download and build `quantopian-thing @ "
        "git+git://github.com/quantopian/thing.git`\n"
    ) == ["quantopian-thing", "thing"]
    assert parse_resolver_culprits("error: there are no versions of nose-timer that satisfy") == [
        "nose-timer"]
    assert parse_resolver_culprits("  × No solution found when resolving dependencies:\n") == []


def test_describe_failure_keeps_stderr_tail_summary_and_culprits():
    import subprocess

    from pymolt.assess.service import describe_resolver_failure

    long = "\n".join(f"line {i}" for i in range(80)) + "\n" + _UV_BUILD_FAILURE
    exc = subprocess.CalledProcessError(2, ["uv", "pip", "compile"], output="", stderr=long)
    found = describe_resolver_failure(exc)

    assert found == "Failed to build `bcolz==1.2.1`"
    assert found.culprits == ["bcolz"]
    assert len(found.detail.splitlines()) == 40 and "\x1b" not in found.detail
    assert "No module named 'setuptools'" in found.detail


def test_describe_failure_without_stderr_falls_back_to_the_message():
    from pymolt.assess.service import describe_resolver_failure

    found = describe_resolver_failure(ValueError("boom"))
    assert found == "boom" and found.detail is None and found.culprits == []


def test_rows_mark_only_culprits_as_conflict():
    class _N:
        def __init__(self, name):
            self.version, self.declared_requirement = "1.0", None
            self.direct, self.manual_bridge = True, False
            self.provenance = type('P', (), {'value': 'pypi'})()
            self.name = name

    class _G:
        nodes = {"bcolz": _N("bcolz"), "six": _N("six")}

    from pymolt.assess.service import TargetResolveError

    rows = {r["name"]: r["status"] for r in build_comparison_rows(
        _G, None, "3.12", TargetResolveError("x", "tail", ["BColz"]))}
    assert rows == {"bcolz": "conflict", "six": "unresolved"}


def test_assess_reports_the_manifest_fixation_not_the_resolve_fixation():
    from pymolt.assess.service import manifest_fixation
    from pymolt.core.enums import SourceFixation

    class _Src:
        fixation = SourceFixation.INTENT

    assert manifest_fixation(_Src) == "intent"

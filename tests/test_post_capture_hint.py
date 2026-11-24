"""The post-migration capture pymolt prints runs as printed and is one verify can compare (QA H2).

The hint used to be `pymolt capture post-migration --mode tests -- python -m pytest`, whatever
the baseline ran: verify then found the two commands different and could only be INCONCLUSIVE.
It is now the baseline's mode, target, privacy and sample rate, and its workload (the command
after the interpreter) run by the target environment's interpreter.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from pymolt.ingestion.config import EnvConfig, ToolChoice
from pymolt.status import TARGET_PYTHON, post_capture_command
from pymolt.verify import service
from pymolt.verify.models import CaptureMode, CaptureValidity, ContractSlot
from pymolt.verify.report import ContractReport

BASELINE = ["/home/user/envs/3.11/bin/python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]


def _slot(command, mode=CaptureMode.TEST_SUITE, **fields) -> ContractSlot:
    return ContractSlot(trace_path="/x.jsonl", captured_at="2026-11-14T00:00:00+00:00",
                        mode=mode, command=list(command), validity=CaptureValidity.VALID,
                        dropped_events=0, **fields)


def _config(project: Path, target_env: str | None = None) -> EnvConfig:
    return EnvConfig(selected_manifest="requirements.txt", selected_tool=ToolChoice.UV,
                     base_python="3.11", target_python="3.12", target_env_path=target_env)


def _target_env(project: Path) -> str:
    python = project / ".venv-target" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    return ".venv-target"


def _argv(hint: str) -> list[str]:
    return shlex.split(hint.split(" -- ", 1)[1])


def test_the_hint_reuses_the_baseline_workload_with_the_target_interpreter(tmp_path):
    config = _config(tmp_path, _target_env(tmp_path))
    hint = post_capture_command(tmp_path, config, _slot(BASELINE))
    assert hint == ("pymolt capture post-migration --mode tests -- "
                    ".venv-target/bin/python -m pytest -q -p no:cacheprovider")
    assert post_capture_command(tmp_path, config, _slot(BASELINE), "proj").startswith(
        "pymolt capture post-migration --project-dir proj --mode tests -- ")


def test_without_a_target_environment_the_interpreter_is_marked(tmp_path):
    hint = post_capture_command(tmp_path, _config(tmp_path), _slot(BASELINE))
    assert hint == (f"pymolt capture post-migration --mode tests -- {TARGET_PYTHON} "
                    "-m pytest -q -p no:cacheprovider")
    # A configured environment that does not exist yet is not used either.
    missing = post_capture_command(tmp_path, _config(tmp_path, ".nope"), _slot(BASELINE))
    assert TARGET_PYTHON in missing


def test_a_tool_runs_by_name_and_the_capture_options_follow_the_baseline(tmp_path):
    slot = _slot(["/home/user/envs/3.11/bin/pytest", "tests/test a.py"],
                 mode=CaptureMode.LIVE_COMMAND, target="flask,werkzeug", privacy_profile="values",
                 sample_rate=0.5)
    assert post_capture_command(tmp_path, _config(tmp_path), slot) == (
        "pymolt capture post-migration --mode command --target flask,werkzeug "
        "--privacy values --sample-rate 0.5 -- pytest 'tests/test a.py'")
    attach = _slot([], mode=CaptureMode.LIVE_ATTACH, privacy_profile="shape", sample_rate=0.1)
    assert post_capture_command(tmp_path, _config(tmp_path), attach) == \
        "pymolt capture post-migration --mode attach"
    assert post_capture_command(tmp_path, _config(tmp_path), None) is None


def _reasons(baseline: list[str], post: list[str], tmp_path) -> ContractReport:
    trace = tmp_path / "t.jsonl"
    trace.write_text('{"t": "return", "q": "m.f", "result": 1}\n')
    report = ContractReport(root=".", diff={"disappeared": 0}, diff_clean=True,
                            static_targets=1, confirmed=1, baseline_stale=False,
                            fact_diff={"verdict": "pass", "counts": {"equal": 1}})
    service._stamp_verdict(report, str(trace), str(trace),
                           [("baseline", _slot(baseline)), ("post-migration", _slot(post))])
    return report


def test_verify_accepts_the_command_the_hint_prints(tmp_path):
    config = _config(tmp_path, _target_env(tmp_path))
    hinted = _argv(post_capture_command(tmp_path, config, _slot(BASELINE)))
    # What capture records: the target interpreter, absolute.
    recorded = [str(tmp_path / hinted[0]), *hinted[1:]]
    report = _reasons(BASELINE, recorded, tmp_path)
    assert "baseline and post-migration used different commands" not in report.verdict_reasons
    # Another interpreter name runs the same script: still the same workload.
    script = _reasons(["/home/user/envs/3.11/bin/python3.11", "app.py"],
                      [str(tmp_path / ".venv-target/bin/python"), "app.py"], tmp_path)
    assert "baseline and post-migration used different commands" not in script.verdict_reasons


def test_different_commands_name_the_baseline_command(tmp_path):
    report = _reasons(BASELINE, ["python", "-m", "pytest"], tmp_path)
    assert "baseline and post-migration used different commands" in report.verdict_reasons
    (cause,) = [c for c in report.inconclusive_causes if "different commands" in c.reason]
    assert shlex.join(BASELINE) in cause.settle

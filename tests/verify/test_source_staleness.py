"""The current-side capture's source freshness: a verdict never stands on code that is gone.

A post-migration capture describes the code it ran. When a file it recorded no longer has the
recorded text (a hand edit, a later step, a revert), PASS would describe code that is no longer
on disk: the verdict is INCONCLUSIVE, needs evidence, and names the files and the capture to
take again (``docs/commands.md``: "stale when a file it recorded no longer has the recorded
text")."""
from __future__ import annotations

import json
from pathlib import Path

from pymolt.ingestion.config import EnvConfig, ToolChoice
from pymolt.verify import service
from pymolt.verify.inconclusive import NEEDS_EVIDENCE
from pymolt.verify.models import (
    CaptureMode,
    CaptureValidity,
    ContractSlot,
    ContractState,
    VerificationVerdict,
)

VIEWS = "import dep\n\n\ndef handler():\n    result = dep.compute()\n    return result.size()\n"


def _event(root: Path, sym: str, result, line: int) -> dict:
    return {"v": 2, "t": "return", "q": sym, "sym": sym, "in": {"bound": {}},
            "where": {"file": str(root / "myapp" / "views.py"), "line": line,
                      "func": "handler"},
            "result": result}


def _slot(root: Path, name: str, *, snapshot: bool = True) -> ContractSlot:
    traces = root / ".pymolt" / "contract_traces"
    traces.mkdir(parents=True, exist_ok=True)
    trace = traces / f"{name}.jsonl"
    events = [_event(root, "dep.compute", {"__opaque__": "dep.Result"}, 5),
              _event(root, "dep.Result.size", 3, 6)]
    trace.write_text("".join(json.dumps(e) + "\n" for e in events))
    sources = service.record_sources(trace, root) if snapshot else None
    return ContractSlot(
        trace_path=str(trace), mode=CaptureMode.TEST_SUITE,
        captured_at=("2026-11-14T00:00:00+00:00" if name == "baseline"
                     else "2026-11-14T00:00:01+00:00"),
        command=["python", "-m", "pytest"], validity=CaptureValidity.VALID, dropped_events=0,
        events=len(events), env_fingerprint=service.environment_fingerprint(root),
        env_setup_fingerprint=service.environment_setup_fingerprint(root),
        sources_path=sources)


def _project(root: Path, *, post: bool = True, post_snapshot: bool = True) -> Path:
    """A configured project whose two captures compare equal on every call: PASS."""
    (root / "requirements.txt").write_text("dep\n")
    EnvConfig(selected_manifest="requirements.txt", selected_tool=ToolChoice.UV,
              base_python="3.11", target_python="3.12").save(
        root / ".pymolt" / "env_config.json")
    (root / "myapp").mkdir()
    (root / "myapp" / "__init__.py").write_text("")
    (root / "myapp" / "views.py").write_text(VIEWS)
    slots = {"baseline": _slot(root, "baseline")}
    if post:
        slots["post_migration"] = _slot(root, "post_migration", snapshot=post_snapshot)
    ContractState(**slots).save(service._state_path(root))
    return root


def _edit(root: Path, text: str = VIEWS.replace("result.size()", "result.size() + 0")) -> None:
    (root / "myapp" / "views.py").write_text(text)


def test_a_post_migration_capture_whose_code_changed_since_does_not_pass(tmp_path):
    root = _project(tmp_path)
    fresh = service.build_contract_report_from_state(root)
    assert fresh.verdict is VerificationVerdict.PASS, fresh.verdict_reasons
    assert fresh.stale_sources == {}

    _edit(root)                                  # e.g. a fix reverted by hand, no re-capture
    stale = service.build_contract_report_from_state(root)

    assert stale.verdict is VerificationVerdict.INCONCLUSIVE
    assert stale.stale_sources == {"post-migration": ["myapp/views.py"]}
    (why,) = stale.inconclusive_causes
    assert why.kind is NEEDS_EVIDENCE and why.cause == "capture"
    assert why.reason == ("the post-migration capture is stale: myapp/views.py changed since "
                          "it was captured")
    assert why.settle.startswith("capture post-migration again")
    assert any(n.startswith("STALE EVIDENCE: the post-migration capture recorded code")
               for n in stale.notes)

    _edit(root, VIEWS)                           # the recorded text is back: fresh again
    assert service.build_contract_report_from_state(root).verdict is VerificationVerdict.PASS


def test_the_post_migration_file_passed_by_path_is_judged_the_same(tmp_path):
    """`verify --post-migration .pymolt/contract_traces/post_migration.jsonl` is the capture."""
    root = _project(tmp_path)
    _edit(root)
    state = service.load_contract_state(root)
    report = service.build_contract_report_from_state(
        root, trace_override=state.post_migration.trace_path,
        against_override=state.baseline.trace_path)
    assert report.verdict is VerificationVerdict.INCONCLUSIVE
    assert report.stale_sources == {"post-migration": ["myapp/views.py"]}


def test_a_post_migration_capture_without_a_source_snapshot_is_not_read_as_fresh(tmp_path):
    root = _project(tmp_path, post_snapshot=False)
    report = service.build_contract_report_from_state(root)
    assert report.verdict is VerificationVerdict.INCONCLUSIVE
    assert report.sources_unchecked == ["post-migration"]
    (why,) = report.inconclusive_causes
    assert why.kind is NEEDS_EVIDENCE and "keeps no source snapshot" in why.reason
    assert why.settle.startswith("capture post-migration again")


def test_a_lone_baseline_whose_code_changed_names_the_capture_to_take(tmp_path):
    root = _project(tmp_path, post=False)
    _edit(root)
    report = service.build_contract_report_from_state(root)
    assert report.verdict is VerificationVerdict.INCONCLUSIVE
    assert report.stale_sources == {"baseline": ["myapp/views.py"]}
    moved = next(c for c in report.inconclusive_causes if "changed since" in c.reason)
    assert moved.kind is NEEDS_EVIDENCE
    assert moved.settle.startswith("capture post-migration to record the code on disk now")


def test_the_baseline_on_the_before_side_is_not_judged_by_its_source(tmp_path):
    """The migration moves the code away from the baseline by design: only the current side's
    recorded code must still be on disk."""
    root = _project(tmp_path)
    state = service.load_contract_state(root)
    baseline_sources = Path(state.baseline.sources_path)
    _edit(root)
    service.record_sources(Path(state.post_migration.trace_path), root)  # post is current
    assert baseline_sources.is_file()
    report = service.build_contract_report_from_state(root)
    assert report.stale_sources == {}
    assert report.verdict is VerificationVerdict.PASS, report.verdict_reasons

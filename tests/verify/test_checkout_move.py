"""A capture compares across checkout paths.

The same project captured at one path and verified, or captured again, at another (a copy, a
second worktree, a CI workspace whose captures travel between jobs as artifacts) is the same
project. Call sites are made project-relative against the root each capture was taken in (its
source snapshot's ``root``), not only against the directory verify runs in; a capture that
records no root cannot place its call sites, and the calls it cannot place are not comparable,
never a FAIL."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from pymolt.ingestion.config import EnvConfig, ToolChoice
from pymolt.verify import service
from pymolt.verify.facts import load_facts
from pymolt.verify.inconclusive import NEEDS_EVIDENCE
from pymolt.verify.models import (
    CaptureMode,
    CaptureValidity,
    ContractSlot,
    ContractState,
    VerificationVerdict,
)

VIEWS = ("import dep\n\n\ndef handler():\n    result = dep.compute()\n    return result.size()\n"
         "\n\ndef where():\n    return dep.locate()\n")
STAMPS = {"baseline": "2026-11-14T00:00:00+00:00", "post_migration": "2026-11-14T00:00:01+00:00"}


def _event(root: Path, sym: str, result, line: int, func: str = "handler") -> dict:
    return {"v": 2, "t": "return", "q": sym, "sym": sym, "in": {"bound": {}},
            "where": {"file": str(root / "myapp" / "views.py"), "line": line, "func": func},
            "result": result}


def _configure(root: Path) -> Path:
    (root / "myapp").mkdir(parents=True)
    (root / "requirements.txt").write_text("dep\n")
    EnvConfig(selected_manifest="requirements.txt", selected_tool=ToolChoice.UV,
              base_python="3.11", target_python="3.12").save(
        root / ".pymolt" / "env_config.json")
    (root / "myapp" / "__init__.py").write_text("")
    (root / "myapp" / "views.py").write_text(VIEWS)
    return root


def _views_events(root: Path) -> list[dict]:
    return [_event(root, "dep.compute", {"__opaque__": "dep.Result"}, 5),
            _event(root, "dep.Result.size", 3, 6),
            _event(root, "dep.locate", str(root / "myapp" / "data.txt"), 10, "where")]


def _capture(root: Path, name: str, events=_views_events, *,
             snapshot: bool = True) -> ContractSlot:
    """A capture taken with the project at ``root``: its call sites and the values that name a
    project file carry that path, as a real capture's do."""
    traces = root / ".pymolt" / "contract_traces"
    traces.mkdir(parents=True, exist_ok=True)
    trace = traces / f"{name}.jsonl"
    recorded = events(root)
    trace.write_text("".join(json.dumps(e) + "\n" for e in recorded))
    return ContractSlot(
        trace_path=str(trace), mode=CaptureMode.TEST_SUITE, captured_at=STAMPS[name],
        command=["python", "-m", "pytest"], validity=CaptureValidity.VALID, dropped_events=0,
        events=len(recorded), env_fingerprint=service.environment_fingerprint(root),
        env_setup_fingerprint=service.environment_setup_fingerprint(root),
        sources_path=service.record_sources(trace, root) if snapshot else None)


def _save(root: Path, **slots: ContractSlot | None) -> None:
    ContractState(**slots).save(service._state_path(root))


def _failing(report) -> list[dict]:
    return (report.fact_diff or {}).get("summary", {}).get("failing", [])


def _sites(root: Path) -> set[str]:
    """The call-site files of both captures, as verify run in ``root`` folds them, plus those
    of the report's diff entries."""
    state = service.load_contract_state(root)
    return {f.file for slot in (state.baseline, state.post_migration) if slot is not None
            for f in load_facts(Path(slot.trace_path), root=root).facts}


def _entry_files(report) -> set[str]:
    entries = (report.all_fact_diff or {}).get("entries", [])
    return {site["file"] for e in entries for site in (e.get("old_site"), e.get("new_site"))
            if site}


def test_a_baseline_taken_in_another_checkout_compares_with_a_post_taken_here(tmp_path):
    a = _configure(tmp_path / "a" / "proj")
    _save(a, baseline=_capture(a, "baseline"))
    b = tmp_path / "ci" / "workspace" / "proj"
    shutil.copytree(a, b)                        # a copy, a second worktree, a CI workspace
    _save(b, baseline=service.load_contract_state(b).baseline,
          post_migration=_capture(b, "post_migration"))

    report = service.build_contract_report_from_state(b)

    assert report.verdict is VerificationVerdict.PASS, (report.verdict_reasons, _failing(report))
    assert _sites(b) == {"myapp/views.py"}
    assert _entry_files(report) <= {"myapp/views.py"}


def test_a_post_taken_in_another_checkout_compares_from_this_one(tmp_path):
    """The reverse: the post-migration capture is taken in B and its recording is brought back
    to A's checkout (a CI artifact), where verify runs."""
    a = _configure(tmp_path / "a" / "proj")
    _save(a, baseline=_capture(a, "baseline"))
    b = tmp_path / "b" / "proj"
    shutil.copytree(a, b)
    _save(b, baseline=service.load_contract_state(b).baseline,
          post_migration=_capture(b, "post_migration"))
    for path in (b / ".pymolt" / "contract_traces").glob("post_migration*"):
        shutil.copy2(path, a / ".pymolt" / "contract_traces" / path.name)
    shutil.copy2(b / ".pymolt" / "contract_state.json", a / ".pymolt" / "contract_state.json")

    report = service.build_contract_report_from_state(a)

    assert report.verdict is VerificationVerdict.PASS, (report.verdict_reasons, _failing(report))
    assert _sites(a) == {"myapp/views.py"}
    assert _entry_files(report) <= {"myapp/views.py"}


def test_a_capture_from_a_checkout_nested_in_this_one_is_placed_by_its_own_root(tmp_path):
    """A second worktree inside the first (``<repo>/.claude/worktrees/<name>``): its files lie
    under this checkout too, so relative to this one they would read
    ``.worktrees/w/myapp/views.py``. The root the capture was taken in comes first."""
    a = _configure(tmp_path / "repo")
    _save(a, baseline=_capture(a, "baseline"))
    staging = tmp_path / "staging"
    shutil.copytree(a, staging)
    b = a / ".worktrees" / "w"
    shutil.move(str(staging), str(b))
    _save(b, baseline=service.load_contract_state(b).baseline,
          post_migration=_capture(b, "post_migration"))
    for path in (b / ".pymolt" / "contract_traces").glob("post_migration*"):
        shutil.copy2(path, a / ".pymolt" / "contract_traces" / path.name)
    shutil.copy2(b / ".pymolt" / "contract_state.json", a / ".pymolt" / "contract_state.json")
    shutil.rmtree(a / ".worktrees")             # its own files would be scanned as the project's

    report = service.build_contract_report_from_state(a)

    assert report.verdict is VerificationVerdict.PASS, (report.verdict_reasons, _failing(report))
    assert _sites(a) == {"myapp/views.py"}
    assert report.executed_files == ["myapp/views.py"]


def test_both_captures_taken_before_the_project_moved(tmp_path):
    """The QA case: both captures taken at A, the project then moved to B and verified there."""
    a = _configure(tmp_path / "a" / "proj")
    _save(a, baseline=_capture(a, "baseline"), post_migration=_capture(a, "post_migration"))
    b = tmp_path / "b" / "moved"
    shutil.move(str(a), str(b))

    report = service.build_contract_report_from_state(b)

    assert report.verdict is VerificationVerdict.PASS, (report.verdict_reasons, _failing(report))
    assert _sites(b) == {"myapp/views.py"}
    facts = load_facts(Path(service.load_contract_state(b).baseline.trace_path), root=b)
    assert not any("outside the project root" in n for n in facts.notes)


GRAPH = "import dep\n\n\ndef edges(g):\n    return sorted(g.edges(data=True))\n"
GRAPH_EDITED = GRAPH.replace("\n\n\ndef", "\n\n# sorted edges\n\ndef")


def _graph_events(line: int, *, getter: bool):
    """``g.edges(data=True)``: 3.8's profiler records the ``edges`` property read as a call,
    3.12's monitoring does not (the networkx case of the QA)."""
    def events(root: Path) -> list[dict]:
        where = {"file": str(root / "myapp" / "views.py"), "line": line, "func": "edges"}
        read = {"v": 2, "t": "return", "q": "dep.Graph.edges", "sym": "dep.Graph.edges",
                "in": {"bound": {"self": {"__opaque__": "dep.Graph"}}}, "where": where,
                "result": {"__opaque__": "dep.EdgeView"}}
        call = {"v": 2, "t": "return", "q": "dep.EdgeView.__call__",
                "sym": "dep.EdgeView.__call__", "where": where,
                "in": {"bound": {"self": {"__opaque__": "dep.EdgeView"}, "data": True}},
                "result": {"__opaque__": "dep.EdgeDataView"}}
        return [read, call] if getter else [call]
    return events


def test_edit_provenance_places_the_sites_of_a_moved_project(tmp_path):
    """Both captures at A with lines moved in between, verified at B: the property read that
    the new backend no longer records stands on an unedited, line-moved statement only when
    the source snapshots (project-relative) place it. Unplaced, it was a false FAIL."""
    a = _configure(tmp_path / "a" / "proj")
    (a / "myapp" / "views.py").write_text(GRAPH)
    baseline = _capture(a, "baseline", _graph_events(5, getter=True))
    (a / "myapp" / "views.py").write_text(GRAPH_EDITED)
    _save(a, baseline=baseline, post_migration=_capture(a, "post_migration",
                                                        _graph_events(6, getter=False)))
    there = service.build_contract_report_from_state(a)
    assert there.verdict is not VerificationVerdict.FAIL, (there.verdict_reasons,
                                                          _failing(there))
    b = tmp_path / "b" / "moved"
    shutil.move(str(a), str(b))

    here = service.build_contract_report_from_state(b)

    assert here.verdict is there.verdict, (here.verdict_reasons, _failing(here))
    assert here.fact_diff["summary"]["ledger"] == there.fact_diff["summary"]["ledger"]


def test_a_capture_that_records_no_root_is_not_comparable_never_failing(tmp_path):
    """An older capture keeps no source snapshot, so nothing says where the project was: the
    calls it recorded in another checkout cannot be placed in this one. Not comparable (a cause
    that says to capture again), never a FAIL."""
    a = _configure(tmp_path / "a" / "proj")
    _save(a, baseline=_capture(a, "baseline", snapshot=False))
    b = tmp_path / "b" / "proj"
    shutil.copytree(a, b)
    _save(b, baseline=service.load_contract_state(b).baseline,
          post_migration=_capture(b, "post_migration"))

    report = service.build_contract_report_from_state(b)

    assert report.verdict is VerificationVerdict.INCONCLUSIVE, (report.verdict_reasons,
                                                                 _failing(report))
    assert not _failing(report)
    assert any(c.kind is NEEDS_EVIDENCE and c.cause == "uncomparable"
               and c.settle.startswith("capture again in this checkout the side taken in "
                                       "another one") for c in report.inconclusive_causes), \
        report.inconclusive_causes


def test_a_capture_without_a_root_still_compares_where_it_was_taken(tmp_path):
    """The degrade is for call sites a capture cannot place, not for older captures as such."""
    root = _configure(tmp_path / "proj")
    _save(root, baseline=_capture(root, "baseline", snapshot=False),
          post_migration=_capture(root, "post_migration"))
    report = service.build_contract_report_from_state(root)
    assert report.verdict is VerificationVerdict.PASS, (report.verdict_reasons, _failing(report))


def _real_project(root: Path) -> Path:
    (root / "app").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text("")
    (root / "app" / "codec.py").write_text(
        "import json\n\n\ndef encode(obj):\n    return json.dumps(obj, sort_keys=True)\n\n\n"
        "def decode(text):\n    return json.loads(text)\n")
    (root / "run.py").write_text(
        "from app.codec import decode, encode\n\nassert decode(encode({'b': 1, 'a': [2]})) "
        "== {'a': [2], 'b': 1}\n")
    return root


def _real_capture(root: Path, when: str):
    return service.capture_named_trace(root, when, CaptureMode.LIVE_COMMAND,
                                       command=[sys.executable, str(root / "run.py")],
                                       target="json")


def test_real_captures_at_two_checkout_paths_compare_as_one_project(tmp_path):
    """End to end with the watcher: the baseline at A, the post-migration at B, then both ways
    round. Nothing changed but the path, so nothing differs."""
    a = _real_project(tmp_path / "a" / "proj")
    _real_capture(a, "baseline")
    b = tmp_path / "elsewhere" / "proj"
    shutil.copytree(a, b)
    _real_capture(b, "post_migration")

    here = service.build_contract_report_from_state(b)
    assert here.verdict is not VerificationVerdict.FAIL, (here.verdict_reasons, _failing(here))
    assert here.fact_diff["summary"]["ledger"]["failing"] == 0
    assert here.fact_diff["summary"]["ledger"]["equal"] > 0
    assert _sites(b) == {"app/codec.py"}

    for path in (b / ".pymolt" / "contract_traces").glob("post_migration*"):
        shutil.copy2(path, a / ".pymolt" / "contract_traces" / path.name)
    shutil.copy2(b / ".pymolt" / "contract_state.json", a / ".pymolt" / "contract_state.json")
    there = service.build_contract_report_from_state(a)
    assert there.verdict is here.verdict, (there.verdict_reasons, _failing(there))
    assert there.fact_diff["summary"]["ledger"] == here.fact_diff["summary"]["ledger"]
    assert _sites(a) == {"app/codec.py"}

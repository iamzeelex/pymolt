"""Coverage and recall over static contacts (``report._judge_contacts``).

Each contact is recorded, ran-not-recorded (a tracer gap), not-run (a test gap) or
unobservable; coverage = (recorded + ran-not-recorded) / observable, recall = recorded /
(recorded + ran-not-recorded). "Ran" comes from the executed record in the capture's sidecar
(line or function granularity), else from the files the trace recorded call sites in (file),
else from the symbol alone (a trace without call sites).
"""
import json

import pytest

from pymolt.verify import service
from pymolt.verify.inconclusive import NEEDS_EVIDENCE, NEEDS_REVIEW
from pymolt.verify.report import build_contract_report

CORE = '''import fakedep


def tested(x):
    a = fakedep.load(x)  # @load
    b = fakedep.dump(a)  # @dump
    if x is None:
        fakedep.load(None)  # @branch
    loader = fakedep.Loader()  # @loader-call
    return fakedep.render(  # @render
        b, loader)  # @render-site


def untested(x):
    return fakedep.load(x)  # @untested


def reads_ran(x):
    return isinstance(x, fakedep.Loader)  # @reads-ran


def reads_cold(x):
    return isinstance(x, fakedep.Loader)  # @reads-cold


def setting():
    return fakedep.SETTING  # @setting
'''

COLD = '''import fakedep


def cold():
    return fakedep.load(1)  # @cold
'''


def _line(src, marker):
    for number, text in enumerate(src.splitlines(), 1):
        if text.endswith("# @" + marker):
            return number
    raise AssertionError(marker)


def _lines(src, markers):
    return {_line(src, m) for m in markers}


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "app").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text("")
    (root / "app" / "core.py").write_text(CORE)
    (root / "app" / "cold.py").write_text(COLD)
    return root


def _trace(root, tmp_path, executed=None, processes=None, prefix=None):
    """A trace with calls of load, render (at its continuation line) and Loader in core.py:
    dump ran at its line but the tracer recorded nothing there (a simulated miss)."""
    core = (prefix or str(root)) + "/app/core.py"
    calls = [("fakedep.load", "load"), ("fakedep.render", "render-site"),
             ("fakedep.Loader", "loader-call")]
    records = [{"q": sym, "sym": sym, "t": "return", "in": {"bound": {}}, "result": None,
                "where": {"file": core, "line": _line(CORE, marker), "func": "tested"}}
               for sym, marker in calls]
    path = tmp_path / "trace.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records))
    if executed is not None or processes is not None:
        procs = processes if processes is not None else [{"executed": executed}]
        (tmp_path / "trace.jsonl.meta.json").write_text(json.dumps({"processes": procs}))
    return path


def _record(granularity, files, complete=True):
    return {"v": 1, "granularity": granularity, "complete": complete, "until": None,
            "files": files}


def _report(root, trace):
    # exact-string identity: the trace names the static paths themselves
    return build_contract_report(root, trace=trace, resolve_identities=False)


def _status(rep):
    return {(c.file, c.line): (c.status, c.granularity) for c in rep.contacts}


def _core(marker):
    return ("app/core.py", _line(CORE, marker))


MODULE_LINES = {1, 4, 14, 18, 22, 26}           # import, the defs
TESTED_RAN = _lines(CORE, ["load", "dump", "loader-call", "render", "render-site"]) | {7}


def test_line_granularity_tells_a_tracer_gap_from_a_test_gap(project, tmp_path):
    core = str(project / "app" / "core.py")
    ran = sorted(MODULE_LINES | TESTED_RAN | {_line(CORE, "reads-ran")})
    trace = _trace(project, tmp_path, executed=_record(
        "line", {core: {"lines": ",".join(map(str, ran))}}))
    rep = _report(project, trace)
    got = _status(rep)
    # recorded at its line; a multi-line statement counts by its span (site on line 2)
    assert got[_core("load")] == ("recorded", None)
    assert got[_core("render")] == ("recorded", None)
    # its line ran, the trace has nothing there: a tracer gap
    assert got[_core("dump")] == ("ran-not-recorded", "line")
    # a branch not taken and a function never called, in a touched file: test gaps
    assert got[_core("branch")] == ("not-run", "line")
    assert got[_core("untested")] == ("not-run", "line")
    # a file that never ran
    assert got[("app/cold.py", _line(COLD, "cold"))] == ("not-run", "line")
    # an existence contact of a symbol observed elsewhere: recorded by symbol when its line
    # ran, not run when it did not; of a symbol never observed: unobservable
    by = {(c.file, c.line): c for c in rep.contacts}
    assert (by[_core("reads-ran")].status, by[_core("reads-ran")].matched_by) == (
        "recorded", "symbol")
    assert by[_core("reads-cold")].status == "not-run"
    assert (by[_core("setting")].status, by[_core("setting")].why) == (
        "unobservable", "existence")

    # recorded: load, render, Loader(), reads_ran; ran: + dump; observable: + branch,
    # untested, cold, reads_cold
    assert (rep.contacts_recorded, rep.contacts_ran_not_recorded, rep.contacts_not_run,
            rep.contacts_unobservable) == (4, 1, 4, 1)
    assert rep.coverage == pytest.approx(5 / 9)
    assert rep.recall == pytest.approx(4 / 5)
    assert rep.coverage_granularity == "line" and rep.executed_complete is True
    assert rep.unobservable_by_reason == {"existence": 1}
    # dump is BLIND because it ran unrecorded; SETTING is unobservable
    blind = {s.target: s.blind_reason for s in rep.symbols if s.status == "blind"}
    assert blind == {"fakedep.dump": "ran-not-recorded", "fakedep.SETTING": "unobservable"}
    assert (rep.blind_not_run, rep.blind_ran_not_recorded, rep.blind_unobservable) == (0, 1, 1)
    assert any("executed-code record: granularity line, from 1 process" in n
               for n in rep.notes)


def test_an_untested_function_lowers_coverage_not_recall(project, tmp_path):
    core = str(project / "app" / "core.py")
    ran = MODULE_LINES | TESTED_RAN | {_line(CORE, "reads-ran")}
    rep = _report(project, _trace(project, tmp_path, executed=_record(
        "line", {core: {"lines": ",".join(map(str, sorted(ran)))}})))
    with_untested_run = _report(project, _trace(project, tmp_path, executed=_record(
        "line", {core: {"lines": ",".join(map(str, sorted(ran | {_line(CORE, "untested")})))}})))
    assert with_untested_run.coverage > rep.coverage
    # the newly run contact is not recorded either: recall falls when it RAN unrecorded,
    # never because it did not run
    assert rep.recall == pytest.approx(4 / 5)
    assert with_untested_run.recall == pytest.approx(4 / 6)


def test_function_granularity_counts_a_contact_in_a_function_that_started(project, tmp_path):
    core = str(project / "app" / "core.py")
    # setprofile: the line tables of the module body, tested() and reads_ran() (started)
    tables = MODULE_LINES | TESTED_RAN | _lines(CORE, ["branch", "reads-ran"])
    trace = _trace(project, tmp_path, executed=_record(
        "function", {core: {"functions": ",".join(map(str, sorted(tables)))}}))
    rep = _report(project, trace)
    got = _status(rep)
    assert got[_core("dump")] == ("ran-not-recorded", "function")
    # the branch's function started: it counts as ran (the function granularity's limit)
    assert got[_core("branch")] == ("ran-not-recorded", "function")
    assert got[_core("untested")] == ("not-run", "function")
    assert got[("app/cold.py", _line(COLD, "cold"))] == ("not-run", "function")
    assert rep.coverage_granularity == "function"


def test_file_granularity_without_an_executed_record(project, tmp_path):
    rep = _report(project, _trace(project, tmp_path))
    got = _status(rep)
    # core.py has call sites, so every contact in it counts as ran: the old rule
    assert got[_core("dump")] == ("ran-not-recorded", "file")
    assert got[_core("untested")] == ("ran-not-recorded", "file")
    assert got[("app/cold.py", _line(COLD, "cold"))] == ("not-run", "file")
    assert rep.coverage_granularity == "file" and rep.executed_complete is None
    assert any("has no executed-code record" in n for n in rep.notes)


def test_symbol_granularity_when_the_trace_has_no_call_sites(project, tmp_path):
    path = tmp_path / "nosites.jsonl"
    path.write_text(json.dumps({"q": "fakedep.load", "sym": "fakedep.load", "t": "return"}))
    rep = _report(project, path)
    by = {(c.file, c.line): c for c in rep.contacts}
    assert (by[_core("load")].status, by[_core("load")].matched_by) == ("recorded", "symbol")
    assert (by[_core("dump")].status, by[_core("dump")].granularity) == ("not-run", "symbol")
    assert rep.coverage_granularity == "symbol" and rep.recall == 1.0


def test_a_process_without_a_record_falls_back_to_the_file_rule(project, tmp_path):
    cold = str(project / "app" / "cold.py")
    trace = _trace(project, tmp_path, processes=[
        {"executed": _record("line", {cold: {"lines": "1,4"}})},
        {"pid": 2},                          # an older runtime: no record
    ])
    rep = _report(project, trace)
    got = _status(rep)
    # core.py: no process recorded its lines, but the trace has call sites there
    assert got[_core("dump")] == ("ran-not-recorded", "file")
    assert got[_core("load")] == ("recorded", None)
    assert rep.executed_complete is False
    assert rep.granularity_counts["file"] >= 1
    assert any("1 process(es) have none" in n for n in rep.notes)


def test_an_incomplete_record_is_stated(project, tmp_path):
    core = str(project / "app" / "core.py")
    trace = _trace(project, tmp_path, executed=_record(
        "line", {core: {"lines": "1,4,5"}}, complete=False))
    rep = _report(project, trace)
    assert rep.executed_complete is False
    assert any("stops before the end of the run" in n for n in rep.notes)
    # render and Loader() were recorded on lines the record does not list
    assert any("recorded contact(s) lie on lines the executed record does not list" in n
               for n in rep.notes)


def test_container_paths_map_by_suffix_for_sites_and_the_executed_record(project, tmp_path):
    prefix = "/work/proj"
    ran = sorted(MODULE_LINES | TESTED_RAN)
    trace = _trace(project, tmp_path, prefix=prefix, executed=_record(
        "line", {prefix + "/app/core.py": {"lines": ",".join(map(str, ran))}}))
    rep = _report(project, trace)
    got = _status(rep)
    assert got[_core("load")] == ("recorded", None)
    assert got[_core("dump")] == ("ran-not-recorded", "line")
    assert got[_core("untested")] == ("not-run", "line")


def test_an_unobserved_symbols_existence_contact_stays_out_even_when_its_line_ran(
        project, tmp_path):
    core = str(project / "app" / "core.py")
    ran = sorted(MODULE_LINES | TESTED_RAN | _lines(CORE, ["reads-ran", "setting"]))
    rep = _report(project, _trace(project, tmp_path, executed=_record(
        "line", {core: {"lines": ",".join(map(str, ran))}})))
    unobservable = [c for c in rep.contacts if c.status == "unobservable"]
    assert [(c.target, c.why) for c in unobservable] == [("fakedep.SETTING", "existence")]
    # the same counts as when SETTING's line did not run: it is in neither denominator
    assert (rep.contacts_recorded, rep.contacts_ran_not_recorded, rep.contacts_not_run,
            rep.contacts_unobservable) == (4, 1, 4, 1)
    assert rep.coverage == pytest.approx(5 / 9)
    assert rep.recall == pytest.approx(4 / 5)


def test_blind_symbols_no_capture_can_record_need_a_person(project, tmp_path):
    """QA V2b: dump ran at its line unrecorded and SETTING is unobservable: capturing again
    records the same, so no evidence settles them. They need a person's review, and the cause
    lists them; they still keep the verdict from PASS (the owner's open decision)."""
    core = str(project / "app" / "core.py")
    ran = sorted(MODULE_LINES | TESTED_RAN)
    rep = _report(project, _trace(project, tmp_path, executed=_record(
        "line", {core: {"lines": ",".join(map(str, ran))}})))
    (why,) = service._blind_causes(rep)
    assert why.reason == ("2 static dependency symbol(s) no capture can record "
                          "(1 ran but not recorded; 1 unobservable): fakedep.SETTING, "
                          "fakedep.dump")
    assert (why.kind, why.cause) == (NEEDS_REVIEW, "unobservable")
    assert why.settle == ("no capture can record them: a person reviews each use of "
                          "fakedep.SETTING, fakedep.dump")


def test_ran_only_by_its_function_is_still_a_test_gap(project, tmp_path):
    """At function granularity "ran" means its function started: the statement may not have
    run (a branch not taken), so a test that reaches it is evidence that settles it."""
    core = str(project / "app" / "core.py")
    tables = MODULE_LINES | TESTED_RAN | _lines(CORE, ["branch"])   # tested() started
    rep = _report(project, _trace(project, tmp_path, executed=_record(
        "function", {core: {"functions": ",".join(map(str, sorted(tables)))}})))
    assert {(c.target, c.status, c.granularity) for c in rep.contacts
            if c.target == "fakedep.dump"} == {("fakedep.dump", "ran-not-recorded", "function")}
    evidence, review = service._blind_causes(rep)
    assert evidence.reason == ("1 static dependency symbol(s) were not exercised "
                               "(1 ran but not recorded at function granularity)")
    assert evidence.kind is NEEDS_EVIDENCE
    assert evidence.settle.startswith("add a test that reaches those statements")
    assert "only their function is known to have run" in evidence.settle
    assert review.kind is NEEDS_REVIEW and review.reason.endswith(": fakedep.SETTING")


def test_whether_unobservable_blind_blocks_pass_is_one_switch(project, tmp_path, monkeypatch):
    """The owner's open decision lives in one constant: off, an unobservable BLIND symbol is
    noted, never a reason."""
    from pymolt.verify import inconclusive

    core = str(project / "app" / "core.py")
    ran = sorted(MODULE_LINES | TESTED_RAN)
    rep = _report(project, _trace(project, tmp_path, executed=_record(
        "line", {core: {"lines": ",".join(map(str, ran))}})))
    assert inconclusive.UNOBSERVABLE_BLIND_BLOCKS_PASS is True
    monkeypatch.setattr(inconclusive, "UNOBSERVABLE_BLIND_BLOCKS_PASS", False)
    (why,) = service._blind_causes(rep)
    assert why.reason.endswith("(1 ran but not recorded): fakedep.dump")


def test_no_trace_every_observable_contact_is_not_run(project):
    rep = build_contract_report(project, resolve_identities=False)
    assert rep.contacts_recorded == rep.contacts_ran_not_recorded == 0
    assert rep.coverage == 0.0 and rep.recall is None
    # existence contacts can only be judged against an observation
    assert rep.unobservable_by_reason == {"existence": 3}

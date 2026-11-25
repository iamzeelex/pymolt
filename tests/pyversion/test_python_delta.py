"""``python_delta``: the stdlib as a pseudo-migration, probed on the target, base from data."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from pymolt.delta.engine import ProbeRun
from pymolt.pyversion import facts as pyfacts
from pymolt.pyversion.facts import classify_origin, python_delta

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12),
                                reason="the target interpreter under test is this one (3.12+)")


def _site(spelling, file="app.py", line=1, kind="from", reached="import", guard=None):
    return SimpleNamespace(spelling=spelling, module=spelling.split(".")[0], file=file,
                           line=line, kind=kind, reached_at=reached, guard=guard)


def _delta(spellings, tmp_path, **kwargs):
    return python_delta(tmp_path, [_site(s) for s in spellings], "3.6", "3.12",
                        target_executable=sys.executable, **kwargs)


def _by_symbol(report):
    return {f.symbol: f for f in report.facts}


def test_a_dropped_alias_is_moved_with_its_successor(tmp_path):
    report = _delta(["collections.Iterable"], tmp_path)
    fact = _by_symbol(report)["collections.Iterable"]
    assert report.subject == "python" and report.package == "python"
    assert fact.kind == "MOVED" and fact.successor == "collections.abc.Iterable"
    assert fact.basis == {"old": "knowledge", "new": "probe"}
    assert fact.removed_in == "3.10" and fact.deprecated_in == "3.3"
    assert fact.warns_since == "3.7"


def test_a_removed_module_is_removed_not_moved(tmp_path):
    facts = _by_symbol(_delta(["imp", "imp.reload"], tmp_path))
    assert facts["imp"].kind == "REMOVED" and facts["imp.reload"].kind == "REMOVED"
    assert facts["imp"].removed_in == "3.12"
    assert any(e.startswith("hint:") for e in facts["imp"].evidence)


def test_a_removed_name_carries_the_curated_hint_and_fix(tmp_path):
    fact = _by_symbol(_delta(["ssl.wrap_socket"], tmp_path))["ssl.wrap_socket"]
    assert fact.kind == "REMOVED"
    assert "fix: advisory" in fact.evidence
    assert any(e.startswith("hint:") for e in fact.evidence)


def test_what_still_exists_has_no_fact(tmp_path):
    assert _delta(["os.path.join", "collections.abc.Iterable"], tmp_path).facts == []


def test_absent_on_the_base_too_is_listed_as_pre_existing(tmp_path):
    fact = _by_symbol(_delta(["urllib2"], tmp_path))["urllib2"]
    assert fact.kind == "UNCHANGED"
    assert any("pre-existing" in e for e in fact.evidence)


def test_the_base_is_not_probed_so_the_report_is_partial_and_says_so(tmp_path):
    report = _delta(["imp"], tmp_path)
    assert report.quality == "partial"
    assert report.sides["old"].startswith("knowledge:")
    assert report.sides["new"].startswith("probe:")
    assert any("base" in n and "not probed" in n for n in report.notes)


def test_no_target_interpreter_is_data_only_and_says_why(tmp_path, monkeypatch):
    monkeypatch.setattr(pyfacts, "find_target_interpreter", lambda *a, **k: None)
    report = python_delta(tmp_path, [_site("collections.Iterable")], "3.6", "3.12")
    fact = report.facts[0]
    assert fact.kind == "MOVED" and fact.basis == {"old": "knowledge", "new": "knowledge"}
    assert report.quality == "partial"
    assert any("no interpreter found" in n for n in report.notes)
    assert report.sides["new"].startswith("knowledge:")


def test_present_only_through_a_shim_is_deprecated_with_its_origin(tmp_path, monkeypatch):
    def probe(exe, root, paths, roots, *, runner=None):
        return ProbeRun(meta={"python": "3.12.1"}, results={
            p: {"exists": True, "module": p.split(".")[0], "steps": []} for p in paths})

    monkeypatch.setattr(pyfacts, "_probe", probe)
    monkeypatch.setattr(pyfacts, "_origins",
                        lambda *a, **k: {"distutils": "site-packages:setuptools"})
    fact = _by_symbol(_delta(["distutils"], tmp_path))["distutils"]
    assert fact.kind == "DEPRECATED" and fact.origin == "site-packages:setuptools"
    assert fact.removed_in == "3.12"


def test_a_module_that_raises_on_import_is_never_removed(tmp_path, monkeypatch):
    def probe(exe, root, paths, roots, *, runner=None):
        return ProbeRun(meta={"python": "3.12.1"}, results={
            p: {"exists": False, "module": None, "error": "ImportError: boom", "steps": []}
            for p in paths})

    monkeypatch.setattr(pyfacts, "_probe", probe)
    report = _delta(["imp"], tmp_path)
    assert report.facts == [] and report.import_errors


def test_a_failed_probe_degrades_to_data(tmp_path, monkeypatch):
    monkeypatch.setattr(pyfacts, "_probe",
                        lambda *a, **k: ProbeRun(failure="probe timed out after 1s"))
    report = _delta(["collections.Iterable"], tmp_path)
    assert report.facts[0].basis["new"] == "knowledge"
    assert any("probe failed" in n for n in report.notes)


def test_a_dependency_report_still_dumps_without_the_python_keys():
    """The python additions are absent from a dependency report's dump, so it dumps as before."""
    from pymolt.delta.models import DeltaReport

    dump = DeltaReport(package="pandas", from_version="1.5.3", to_version="2.2.0",
                       quality="complete").model_dump(mode="json")
    assert dump["package"] == "pandas" and dump["from"] == "1.5.3" and dump["to"] == "2.2.0"
    assert not {"subject", "sides", "import_errors", "type_relocations"} & set(dump)


def test_a_python_report_dumps_its_subject_and_sides(tmp_path):
    dump = _delta(["imp"], tmp_path).model_dump(mode="json")
    assert dump["subject"] == "python"
    assert dump["sides"]["old"].startswith("knowledge:")
    assert dump["sides"]["new"].startswith("probe:")


def test_classify_origin():
    assert classify_origin("/x/lib/python3.12/collections/__init__.py") == "stdlib"
    assert classify_origin("/x/site-packages/setuptools/_distutils/__init__.py") == \
        "site-packages:setuptools"
    assert classify_origin(None) is None


def _base_probe_with_loop(real_probe):
    """The target is probed for real; the base answers with asyncio.sleep's 3.8 shape."""
    def fake(exe, root, paths, roots, *, runner=None):
        if exe != "base-python":
            return real_probe(exe, root, paths, roots, runner=runner)
        shape = [["delay", "POSITIONAL_OR_KEYWORD", None], ["result", "POSITIONAL_OR_KEYWORD",
                                                             "None"],
                 ["loop", "KEYWORD_ONLY", "None"]]
        results = {p: {"exists": True, "module": p.rsplit(".", 1)[0],
                       "steps": [{"attr": p.rsplit(".", 1)[1], "exists": True, "shape": shape}]}
                   for p in paths if p == "asyncio.tasks.sleep"}
        return ProbeRun(meta={"python": "3.8.20"}, results=results)
    return fake


def test_runtime_symbols_get_signature_facts_when_both_sides_are_probed(tmp_path, monkeypatch):
    monkeypatch.setattr(pyfacts, "_probe", _base_probe_with_loop(pyfacts._probe))
    report = python_delta(tmp_path, [], "3.8", "3.12", target_executable=sys.executable,
                          base_executable="base-python",
                          runtime_symbols={"asyncio.tasks.sleep",
                                           "unittest.case.TestCase.assertEquals"})
    facts = _by_symbol(report)
    sig = facts["asyncio.tasks.sleep"]
    assert (sig.kind, sig.param, sig.new) == ("SIGNATURE", "loop", None)
    assert sig.basis == {"old": "probe", "new": "probe"}
    # absent on the target; the baseline ran it, so its presence on the base is observed
    gone = facts["unittest.case.TestCase.assertEquals"]
    assert gone.kind == "REMOVED" and gone.basis["old"] == "baseline-trace"


def test_no_signature_facts_without_a_probed_base(tmp_path):
    report = python_delta(tmp_path, [], "3.8", "3.12", target_executable=sys.executable,
                          runtime_symbols={"asyncio.tasks.sleep"})
    assert not [f for f in report.facts if f.kind == "SIGNATURE"]


def test_an_observed_call_passing_the_removed_param_is_certain(tmp_path):
    from pymolt.verify.predict import RiskTier, predict_python_risks

    def call(passed, line):
        return SimpleNamespace(symbol="asyncio.tasks.sleep", file="app/aio.py", func="p",
                               line=line, text=None, count=1, input={"bound": passed},
                               defaulted=None, warnings=None, outcomes=[])
    delta = {"package": "python", "quality": "complete", "notes": [], "facts": [{
        "symbol": "asyncio.tasks.sleep", "kind": "SIGNATURE", "param": "loop",
        "old": "KEYWORD_ONLY",
        "new": None, "basis": {"old": "probe", "new": "probe"}, "evidence": []}]}
    out = predict_python_risks(delta, [], base="3.8", target="3.12", root=tmp_path,
                               runtime_facts=[call({"delay": 1, "loop": "x"}, 7),
                                              call({"delay": 1}, 9)])
    assert [(r.tier, r.site.line, r.basis) for r in out.risks] == [
        (RiskTier.CERTAIN, 7, "trace")]            # the call that never passed loop is not at risk
    delta["facts"][0]["basis"] = {"old": "knowledge", "new": "probe"}
    out = predict_python_risks(delta, [], base="3.8", target="3.12", root=tmp_path,
                               runtime_facts=[call({"loop": "x"}, 7)])
    assert out.risks[0].tier is RiskTier.HIGH


# ── a site's parameters are its callee's own (legacy QA P2) ─────────────────────────────


def _runtime(symbol, line, text, bound, defaulted=None, file="tests.py"):
    return SimpleNamespace(symbol=symbol, file=file, func="t", line=line, text=text, count=1,
                           input={"bound": bound}, defaulted=defaulted, warnings=None,
                           outcomes=[])


def _python(*facts):
    return {"package": "python", "quality": "complete", "notes": [],
            "facts": [{"basis": {"old": "probe", "new": "probe"}, "evidence": [], **f}
                      for f in facts]}


def _rated(tmp_path, delta, *calls):
    from pymolt.verify.predict import predict_python_risks

    return predict_python_risks(delta, [], base="3.6", target="3.12", root=tmp_path,
                                runtime_facts=list(calls)).risks


SLEEP_LOOP = {"symbol": "asyncio.tasks.sleep", "kind": "SIGNATURE", "param": "loop",
              "old": "KEYWORD_ONLY", "new": None}


def test_another_callees_arguments_are_never_the_written_spellings(tmp_path):
    """vibora: uvloop's compiled ``run_until_complete`` calls ``ensure_future(coro, loop=self)``,
    recorded at the user's ``await asyncio.sleep(0)``; ``sleep`` itself never got ``loop``."""
    text = "await asyncio.sleep(0)"
    sleep = _runtime("asyncio.tasks.sleep", 10, text, {"delay": 0}, [["loop", "result"]])
    ensure = _runtime("asyncio.tasks.ensure_future", 10, text, {"coro_or_future": "c",
                                                               "loop": "uvloop"})
    assert _rated(tmp_path, _python(SLEEP_LOOP), sleep, ensure) == []
    # Without sleep's own record the other call is still no alias for a parameter fact.
    assert _rated(tmp_path, _python(SLEEP_LOOP), ensure) == []


def test_a_call_the_statement_does_not_name_is_not_its_call(tmp_path):
    """vibora: a ``for`` resuming the stdlib's own generator (``dis``), and uvloop calling
    ``_set_running_loop`` under ``loop.run_until_complete``."""
    cells = _runtime("dis._get_instructions_bytes", 5,
                     "for instr in dis.get_instructions(route_handler):", {"cells": ["x"]})
    running = _runtime("asyncio.events._set_running_loop", 8,
                       "loop.run_until_complete(main())", {"running_loop": "loop"})
    delta = _python({"symbol": "dis._get_instructions_bytes", "kind": "SIGNATURE",
                     "param": "cells", "old": "POSITIONAL_OR_KEYWORD", "new": None},
                    {"symbol": "asyncio.events._set_running_loop", "kind": "SIGNATURE",
                     "param": "running_loop", "old": "POSITIONAL_OR_KEYWORD",
                     "new": "POSITIONAL_ONLY"})
    assert _rated(tmp_path, delta, cells, running) == []
    # The same calls written on the statement are rated.
    cells.text = "dis._get_instructions_bytes(code, cells=cells)"
    (risk,) = _rated(tmp_path, delta, cells)
    assert risk.symbol == "dis._get_instructions_bytes"


def test_self_is_never_a_parameter_a_bound_method_call_misses(tmp_path):
    """hyper: ``MutableMapping.update(*args, **kwds)`` became ``update(self, other=(), /,
    **kwds)``; ``headers.update(...)`` binds ``self``."""
    update = _runtime("collections.abc.MutableMapping.update", 242, "headers.update(extra)",
                      {"args": ["headers", {"a": 1}]})
    delta = _python({"symbol": "collections.abc.MutableMapping.update", "kind": "SIGNATURE",
                     "param": "self", "old": None, "new": "POSITIONAL_ONLY (required)"})
    assert _rated(tmp_path, delta, update) == []


def test_an_alias_the_statement_writes_still_carries_a_removal(tmp_path):
    """``self.assertEquals(...)`` is recorded as ``assertEqual``: the removal of the name the
    statement writes is the site's."""
    from pymolt.verify.predict import RiskTier

    call = _runtime("unittest.case.TestCase.assertEqual", 3, "self.assertEquals(1, 1)",
                    {"first": 1, "second": 1})
    gone = {"symbol": "unittest.case.TestCase.assertEquals", "kind": "REMOVED"}
    (risk,) = _rated(tmp_path, _python(gone), call)
    assert risk.symbol == "unittest.case.TestCase.assertEquals"
    assert risk.tier is RiskTier.CERTAIN


def test_a_private_class_the_statement_reaches_through_a_public_method_is_not_its_call(tmp_path):
    """locust: ``with self.assertLogs(...)`` runs ``unittest.case._AssertLogsContext``, which
    Python 3.9 moved to ``unittest._log``; ``TestCase.assertLogs`` is still there."""
    text = "with self.assertLogs('locust', level='INFO') as logs:"
    calls = [_runtime(f"unittest.case._AssertLogsContext.{m}", 12, text, {})
             for m in ("__init__", "__enter__", "__exit__")]
    calls.append(_runtime("unittest.case.TestCase.assertLogs", 12, text, {}))
    gone = {"symbol": "unittest.case._AssertLogsContext", "kind": "REMOVED"}
    kept = {"symbol": "unittest.case.TestCase.assertLogs", "kind": "UNCHANGED"}
    assert _rated(tmp_path, _python(gone, kept), *calls) == []


def test_a_protocol_method_of_a_public_class_is_the_statements_own_operation(tmp_path):
    """``LooseVersion(a) >= LooseVersion(b)``: the comparison calls ``Version.__ge__``;
    distutils is gone on 3.12, so the statement fails."""
    from pymolt.verify.predict import RiskTier

    ge = _runtime("distutils.version.Version.__ge__", 14,
                  "return LooseVersion(version) >= LooseVersion(minimum)", {})
    gone = {"symbol": "distutils", "kind": "REMOVED"}
    (risk,) = _rated(tmp_path, _python(gone), ge)
    assert risk.symbol == "distutils.version.Version.__ge__"
    assert risk.tier is RiskTier.CERTAIN

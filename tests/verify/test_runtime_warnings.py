"""Runtime warnings in the injected runtime: deprecation-family warnings raised through
``warnings.warn`` while a boundary call is recorded land on that call (sym, site) as
``warnings: [{category, message, count}]`` — on every backend, sampled or tallied — and the
program's own warning behaviour (filters, registries, reported file/line) is unchanged."""
import json
import sys
import types
import warnings

import pytest

from pymolt.verify._backend import (
    HAS_MONITORING,
    WARN_MESSAGE_MAX,
    SetProfileBackend,
    install_warning_capture,
    remove_warning_capture,
    warning_key,
)
from pymolt.verify._sinks import (
    MAX_WARNINGS,
    WARNING_MORE,
    FactSink,
    JsonlSink,
    MemorySink,
    NoveltySampler,
    merge_warnings,
)
from pymolt.verify._wrap import WrapBackend

MOD = "vwarndep"

_SRC = '''
import warnings


def mean(x, numeric_only=None):
    if numeric_only is None:
        warnings.warn("The default value of numeric_only is deprecated", FutureWarning,
                      stacklevel=2)
    return x


def _deep():
    warnings.warn("old path", DeprecationWarning, stacklevel=3)


def via_helper(x):
    _deep()
    return x


def user_warning(x):
    warnings.warn("just a user warning")
    return x


def pending(x):
    warnings.warn(PendingDeprecationWarning("soon gone"))
    return x


def chatty(n):
    for i in range(n):
        warnings.warn("message %d" % i, DeprecationWarning, stacklevel=2)
    return n


def long_message(x):
    warnings.warn("x" * 1000, DeprecationWarning)
    return x


def calls_back(fn):
    return fn()


def prefixed(x):
    import os
    warnings.warn("in dir", DeprecationWarning, skip_file_prefixes=(os.path.dirname(__file__),))
    warnings.warn("own file", DeprecationWarning, skip_file_prefixes=(__file__,))
    warnings.warn("level 3", DeprecationWarning, stacklevel=3,
                  skip_file_prefixes=(os.path.dirname(__file__),))
    return x
'''


@pytest.fixture
def dep(tmp_path):
    path = tmp_path / "site-packages" / (MOD + ".py")
    path.parent.mkdir()
    path.write_text(_SRC)
    mod = types.ModuleType(MOD)
    mod.__file__ = str(path)
    exec(compile(_SRC, str(path), "exec"), mod.__dict__)
    sys.modules[MOD] = mod
    yield mod
    sys.modules.pop(MOD, None)


SYMBOLS = [MOD + "." + n for n in ("mean", "via_helper", "user_warning", "pending", "chatty",
                                   "long_message", "calls_back", "prefixed")]


def _backends():
    names = ["setprofile", "wrap"]
    if HAS_MONITORING:
        names += ["monitoring", "incremental"]
    return names


def _make(name, sink):
    if name == "setprofile":
        return SetProfileBackend(MOD, sink, source=__name__)
    if name == "wrap":
        return WrapBackend(SYMBOLS, sink, source=__name__)
    from pymolt.verify._monitoring import MonitoringBackend
    mode = "incremental" if name == "incremental" else "discover"
    return MonitoringBackend(MOD, sink, source=__name__, mode=mode)


def _run(name, sink, body):
    be = _make(name, sink)
    be.start()
    try:
        return body()
    finally:
        be.stop()


def _by_sym(sink):
    return {rec.get("sym") or rec["qualname"]: rec for rec in sink.records.values()}


@pytest.fixture(autouse=True)
def _always_show():
    # Every warning is issued (never deduplicated away) and kept out of pytest's summary.
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        yield


# ── attribution ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", _backends())
def test_deprecation_family_warnings_land_on_the_recorded_call(dep, backend):
    sink = MemorySink()

    def body():
        dep.mean(1)
        dep.mean(2, numeric_only=True)
        dep.via_helper(3)
        dep.pending(4)

    _run(backend, sink, body)
    recs = _by_sym(sink)
    defaulted = [r for r in sink.records.values()
                 if r["qualname"].endswith("mean") and "numeric_only" not in r["inputs"]["bound"]]
    assert defaulted[0]["warnings"] == [{
        "category": "FutureWarning",
        "message": "The default value of numeric_only is deprecated", "count": 1}]
    explicit = [r for r in sink.records.values()
                if r["qualname"].endswith("mean") and "numeric_only" in r["inputs"]["bound"]]
    assert "warnings" not in explicit[0]
    assert recs[MOD + ".via_helper"]["warnings"] == [
        {"category": "DeprecationWarning", "message": "old path", "count": 1}]
    assert recs[MOD + ".pending"]["warnings"] == [
        {"category": "PendingDeprecationWarning", "message": "soon gone", "count": 1}]


@pytest.mark.parametrize("backend", _backends())
def test_other_categories_and_warnings_outside_a_call_are_not_captured(dep, backend):
    sink = MemorySink()

    def body():
        dep.user_warning(1)
        warnings.warn("our own code", DeprecationWarning)
        dep.calls_back(lambda: warnings.warn("in our callback", DeprecationWarning))

    _run(backend, sink, body)
    assert all("warnings" not in rec for rec in sink.records.values())


@pytest.mark.parametrize("backend", _backends())
def test_repeats_are_counted_and_distinct_messages_capped(dep, backend):
    sink = MemorySink()
    _run(backend, sink, lambda: (dep.chatty(MAX_WARNINGS + 3), dep.long_message(0)))
    recs = _by_sym(sink)
    chatty = recs[MOD + ".chatty"]["warnings"]
    assert sum(w["count"] for w in chatty) == MAX_WARNINGS + 3
    assert {"category": "DeprecationWarning", "message": WARNING_MORE, "count": 3} in chatty
    (long_w,) = recs[MOD + ".long_message"]["warnings"]
    assert len(long_w["message"]) == WARN_MESSAGE_MAX


@pytest.mark.parametrize("backend", _backends())
def test_unsampled_calls_keep_their_warnings_on_the_tally(dep, backend, tmp_path):
    sink = FactSink(str(tmp_path / "trace-{pid}.jsonl"), flush_seconds=0)
    sink._sampler = NoveltySampler(rate=0.0, first=1)    # the 1st call per site, then none
    _run(backend, sink, lambda: [dep.mean(i) for i in range(3)])
    sink.close()
    with open(sink.path) as fh:
        snap = json.load(fh)
    (fact,) = [f for f in snap["facts"] if f["sym"].endswith("mean")]
    assert fact["warnings"][0]["count"] == 1                 # the sampled call
    (tally,) = [t for t in snap["tallies"] if t["sym"].endswith("mean")]
    assert tally["count"] == 3
    assert tally["warnings"][0]["count"] == 2                # the two tallied ones


def test_jsonl_events_and_sidecar_carry_warnings(dep, tmp_path):
    sink = JsonlSink(str(tmp_path / "trace.jsonl"))
    sink._sampler = NoveltySampler(rate=0.0, first=1)
    _run("setprofile", sink, lambda: [dep.mean(i) for i in range(3)])
    sink.close()
    events = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    (ev,) = [e for e in events if e["sym"].endswith("mean")]
    assert ev["warnings"][0]["category"] == "FutureWarning"
    meta = json.loads((tmp_path / "trace.jsonl.meta.json").read_text())
    (tw,) = meta["tally_warnings"]
    assert tw["sym"].endswith("mean") and tw["where"]["func"]
    assert tw["warnings"][0]["count"] == 2


# ── zero behaviour change ────────────────────────────────────────────────────


def _shown(body):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        body()
    return [(type(w.message).__name__, str(w.message), w.filename, w.lineno) for w in caught]


@pytest.mark.parametrize("backend", [b for b in _backends() if b != "wrap"])
def test_reported_file_and_line_are_unchanged(dep, backend):
    def body():
        dep.mean(1)
        dep.via_helper(2)
        dep.prefixed(3)          # skip_file_prefixes (3.12+, the host minimum)

    untraced = _shown(body)
    traced = _run(backend, MemorySink(), lambda: _shown(body))
    assert traced == untraced
    assert all(f == __file__ for _c, _m, f, _l in untraced)


@pytest.mark.parametrize("backend", _backends())
def test_a_warning_the_app_filters_is_still_not_shown_but_still_captured(dep, backend):
    sink = MemorySink()

    def body():
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("ignore", FutureWarning)
            dep.mean(1)
        return caught

    assert _run(backend, sink, body) == []
    (rec,) = [r for r in sink.records.values() if r["qualname"].endswith("mean")]
    assert rec["warnings"][0]["category"] == "FutureWarning"


@pytest.mark.parametrize("backend", _backends())
def test_a_warning_the_app_shows_is_still_shown_and_error_still_raises(dep, backend):
    sink = MemorySink()

    def body():
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("default")
            for _ in range(3):
                dep.mean(1)                  # one location: "default" shows it once
        with warnings.catch_warnings():
            warnings.simplefilter("error", FutureWarning)
            with pytest.raises(FutureWarning):
                dep.mean(2)
        return caught

    caught = _run(backend, sink, body)
    assert [str(w.message) for w in caught] == [
        "The default value of numeric_only is deprecated"]
    raised = [r for r in sink.records.values() if r["raised"]]
    assert raised and raised[0]["warnings"][0]["category"] == "FutureWarning"


@pytest.mark.parametrize("backend", _backends())
def test_warn_is_restored_after_stop(dep, backend):
    original = warnings.warn
    be = _make(backend, MemorySink())
    be.start()
    assert warnings.warn is not original
    assert warnings.warn.__wrapped__ is original
    be.stop()
    assert warnings.warn is original


def test_two_owners_share_one_wrapper_and_the_last_restores(dep):
    original = warnings.warn
    a, b = SetProfileBackend(MOD, MemorySink()), WrapBackend([], MemorySink())
    install_warning_capture(a)
    hook = warnings.warn
    install_warning_capture(b)
    assert warnings.warn is hook
    remove_warning_capture(a)
    assert warnings.warn is hook
    remove_warning_capture(b)
    assert warnings.warn is original


def test_switch_off_leaves_warn_alone(dep, monkeypatch):
    monkeypatch.setenv("PYMOLT_TRACE_WARNINGS", "0")
    original = warnings.warn
    sink = MemorySink()
    be = SetProfileBackend(MOD, sink, source=__name__)
    be.start()
    try:
        assert warnings.warn is original
        dep.mean(1)
    finally:
        be.stop()
    assert all("warnings" not in r for r in sink.records.values())


def test_bad_arguments_fail_exactly_as_the_original(dep):
    be = SetProfileBackend(MOD, MemorySink(), source=__name__)
    errors = []
    for args in (("m", int), ("m", DeprecationWarning, "two")):
        try:
            warnings.warn(*args)
        except Exception as exc:  # noqa: BLE001 - the error type is what is compared
            errors.append(type(exc))
    be.start()
    try:
        for i, args in enumerate((("m", int), ("m", DeprecationWarning, "two"))):
            with pytest.raises(errors[i]):
                warnings.warn(*args)
    finally:
        be.stop()


def test_warning_key_reads_without_running_user_code():
    class Loud(DeprecationWarning):
        def __str__(self):  # pragma: no cover - must never be called
            raise AssertionError("user __str__ ran")

    assert warning_key(Loud("why"))[1] == "why"
    assert warning_key("m", UserWarning) is None
    assert warning_key("m") is None                          # no category = UserWarning
    assert warning_key(12, FutureWarning) == ("FutureWarning", "<int>")
    assert warning_key("m", Loud)[0].endswith("test_warning_key_reads_without_running_user_code"
                                              ".<locals>.Loud")


def test_merge_warnings_keeps_counts_whole():
    acc = merge_warnings({}, [{"category": "C", "message": str(i), "count": 2}
                              for i in range(MAX_WARNINGS + 2)])
    assert sum(acc.values()) == 2 * (MAX_WARNINGS + 2)
    assert acc[("C", WARNING_MORE)] == 4
    assert merge_warnings({}, [None, {"category": 1}, "x"]) == {}


# ── host: warnings on facts are evidence, never outcome ─────────────────────


def _event(result, warnings=None, line=7):
    ev = {"v": 2, "t": "return", "q": "lib.f", "sym": "lib.f",
          "in": {"bound": {"x": 1}, "defaulted": []}, "result": result,
          "where": {"file": "/app/main.py", "line": line, "func": "main"}}
    if warnings:
        ev["warnings"] = warnings
    return ev


W = [{"category": "FutureWarning", "message": "soon", "count": 1}]


def test_fact_warnings_merge_across_occurrences():
    from pymolt.verify.facts import fold_facts

    fs = fold_facts([_event(1, W), _event(1, W), _event(1)], root="/app")
    (fact,) = fs.facts
    assert [(w.category, w.message, w.count) for w in fact.warnings] == [
        ("FutureWarning", "soon", 2)]


def test_diff_ignores_warnings_for_categories_but_shows_them():
    from pymolt.verify.facts import FactCategory, diff_facts, fold_facts

    old = fold_facts([_event(1, W)], root="/app")
    new = fold_facts([_event(1)], root="/app")
    (entry,) = diff_facts(old, new).entries
    assert entry.category is FactCategory.EQUAL
    assert entry.old_warnings[0].message == "soon" and entry.new_warnings is None
    changed = diff_facts(old, fold_facts([_event(2, W)], root="/app")).entries[0]
    assert changed.category is FactCategory.CHANGED


def test_snapshot_warnings_survive_merge_and_projection_without_doubling(tmp_path):
    from pymolt.verify.facts import (
        load_facts,
        merge_fact_snapshots,
        project_snapshot,
        snapshot_path,
    )

    sinks = []
    for i in range(2):
        sink = FactSink(str(tmp_path / f"p{i}-{{pid}}.jsonl"), flush_seconds=0)
        sink._sampler = NoveltySampler(rate=0.0, first=1)
        site = {"file": "/app/main.py", "line": 7, "func": "main"}
        sink.on_return("lib.f", {"bound": {"x": 1}}, 1, site=site, sym="lib.f", warnings=W,
                       admitted=True)
        sink.on_raise("lib.f", {"bound": {"x": 1}}, "TypeError", site=site, sym="lib.f",
                      warnings=W, admitted=True)
        sink.tally("lib.f", "lib.f", site, "return", warnings=W)   # unsampled
        sink.close()
        sinks.append(sink.path)
    trace = tmp_path / "trace.jsonl"
    merge_fact_snapshots(sinks, snapshot_path(trace))
    project_snapshot(snapshot_path(trace), trace)
    for path in (trace, snapshot_path(trace)):
        fs = load_facts(path, root="/app")
        (fact,) = fs.facts
        assert fact.warnings[0].count == 4                 # 2 sampled calls x 2 processes
    fs = load_facts(snapshot_path(trace), root="/app")
    (unsampled,) = fs.unsampled_warnings
    assert (unsampled.symbol, unsampled.file, unsampled.line) == ("lib.f", "main.py", 7)
    assert unsampled.warnings[0].count == 2


def test_jsonl_sidecar_tally_warnings_and_capture_flag_reach_the_factset(tmp_path):
    from pymolt.verify.facts import load_facts

    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps(_event(1, W)) + "\n")
    meta = {"processes": [{"warnings_capture": "on", "tally_warnings": [
        {"q": "lib.f", "sym": "lib.f", "where": {"file": "/app/main.py", "line": 7,
                                                  "func": "main"}, "warnings": W}]}]}
    (tmp_path / "trace.jsonl.meta.json").write_text(json.dumps(meta))
    fs = load_facts(trace, root="/app")
    assert fs.warnings_capture == "on"
    assert fs.unsampled_warnings[0].warnings[0].message == "soon"
    assert load_facts(trace, root="/app").facts[0].warnings[0].count == 1

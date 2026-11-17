"""FactSink: the in-process aggregating sink (phase 2, PYMOLT_TRACE_SINK=facts).

Bounds (outcome cap, per-site widening, global eviction, tally overflow, oversize values),
completeness (every call tallied, sampled or not), the snapshot format (one JSON document,
streamable line by line), atomic periodic flush, thread safety and the sidecar the host's
counter accounting reads.
"""
import ast
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from pymolt.verify import _sinks
from pymolt.verify._sinks import (
    MAX_OUTCOMES,
    FactSink,
    facts_path_for,
    widen_value,
)
from pymolt.verify.service import _runtime_counters_complete

SITE = {"file": "/app/main.py", "line": 7, "func": "main"}


def _sink(tmp_path, **kw):
    kw.setdefault("flush_seconds", 0)
    return FactSink(str(tmp_path / "trace-{pid}.jsonl"), **kw)


def _snapshot(sink):
    with open(sink.path) as fh:
        return json.load(fh)


def _facts(sink, sym):
    return [f for f in _snapshot(sink)["facts"] if f["sym"] == sym]


def test_paths_follow_the_jsonl_template(tmp_path):
    sink = _sink(tmp_path)
    pid = str(os.getpid())
    assert sink.path == str(tmp_path / f"trace-{pid}.facts.json")
    assert sink.metadata_path == str(tmp_path / f"trace-{pid}.jsonl.meta.json")
    # The tracer may hand over the already-replaced template: same result.
    assert FactSink(str(tmp_path / "trace-{pid}.facts.json"), flush_seconds=0).path == sink.path
    assert facts_path_for("x.facts.json") == "x.facts.json"


def test_identical_calls_aggregate_into_one_fact_with_counts_and_durations(tmp_path):
    sink = _sink(tmp_path)
    for dur in (0.001, 0.003, 0.002):
        sink.on_return("m.f", {"bound": {"x": 1}}, 2, site=SITE, sym="m.f", dur=dur, tid=1)
    sink.on_raise("m.f", {"bound": {"x": 1}}, "TypeError", site=SITE, sym="m.f")
    sink.close()
    (fact,) = _facts(sink, "m.f")
    assert fact["in"] == {"bound": {"x": 1}}
    assert fact["where"] == SITE
    assert fact["count"] == 4
    assert sorted((o["t"], o.get("result", o.get("raised")), o["count"])
                  for o in fact["outcomes"]) == [("raise", "TypeError", 1), ("return", 2, 3)]
    assert fact["dur"] == {"n": 3, "sum": pytest.approx(0.006), "min": 0.001, "max": 0.003}
    (tally,) = _snapshot(sink)["tallies"]
    assert (tally["count"], tally["returns"], tally["raises"]) == (4, 3, {"TypeError": 1})
    assert sum(tally["dur"]["hist"]) == 3


def test_outcomes_past_the_cap_fold_into_other_with_their_types(tmp_path):
    sink = _sink(tmp_path)
    for i in range(MAX_OUTCOMES + 5):
        sink.on_return("m.now", {"bound": {}}, i, site=SITE, sym="m.now")
    sink.on_return("m.now", {"bound": {}}, "late", site=SITE, sym="m.now")
    sink.close()
    (fact,) = _facts(sink, "m.now")
    exact = [o for o in fact["outcomes"] if o["t"] == "return"]
    (other,) = [o for o in fact["outcomes"] if o["t"] == "other"]
    assert len(exact) == MAX_OUTCOMES
    assert other["count"] == 6
    assert other["types"] == [{"t": "return", "type": "int", "count": 5},
                              {"t": "return", "type": "str", "count": 1}]
    assert _snapshot(sink)["counters"]["outcomes_other"] == 6


def test_inputs_past_the_per_site_cap_widen_to_type_level(tmp_path):
    sink = _sink(tmp_path, facts_per_site=3)
    for i in range(10):
        sink.on_return("m.get", {"bound": {"id": i}}, {"id": i}, site=SITE, sym="m.get")
    sink.on_raise("m.get", {"bound": {"id": 99}}, "KeyError", site=SITE, sym="m.get")
    sink.close()
    facts = _facts(sink, "m.get")
    exact = [f for f in facts if not f.get("widened")]
    (wide,) = [f for f in facts if f.get("widened")]
    assert len(exact) == 3
    assert wide["in"] == {"__widened__": True}
    assert sorted(json.dumps(o, sort_keys=True) for o in wide["outcomes"]) == sorted([
        json.dumps({"t": "return", "result": {"__opaque__": "dict"}, "count": 7},
                   sort_keys=True),
        json.dumps({"t": "raise", "raised": "KeyError", "count": 1}, sort_keys=True),
    ])
    counters = _snapshot(sink)["counters"]
    assert (counters["widened"], counters["widened_facts"]) == (8, 1)


def test_global_cap_evicts_the_lowest_counts_and_accounts_for_them(tmp_path):
    sink = _sink(tmp_path, max_facts=64)
    for _ in range(50):  # one heavy hitter
        sink.on_return("m.hot", {"bound": {}}, 1, site=SITE, sym="m.hot")
    for i in range(500):
        site = {"file": "/app/main.py", "line": 100 + i, "func": "main"}
        sink.on_return("m.cold", {"bound": {}}, 1, site=site, sym="m.cold")
    sink.close()
    snap = _snapshot(sink)
    counters = snap["counters"]
    assert len(snap["facts"]) <= 64
    assert counters["evicted"] > 0 and counters["evicted_facts"] > 0
    assert any(f["sym"] == "m.hot" for f in snap["facts"])  # heavy hitter survives
    kept = sum(f["count"] for f in snap["facts"])
    assert kept + counters["evicted"] == 550 == counters["sampled"]
    # Values were evicted, the calls were not: tallies still count all of them.
    assert sum(t["count"] for t in snap["tallies"]) == 550


def test_every_call_is_tallied_sampled_or_not(tmp_path, monkeypatch):
    monkeypatch.setenv("PYMOLT_TRACE_SAMPLE_RATE", "0.5")
    draws = iter([0.1, 0.9] * 50)
    monkeypatch.setattr(_sinks.random, "random", lambda: next(draws))
    sink = _sink(tmp_path)
    for _ in range(100):  # the backend's protocol: admit first, tally the declined
        if sink.admit():
            sink.on_return("m.f", {"bound": {}}, 1, site=SITE, admitted=True, sym="m.f")
        else:
            sink.tally("m.f", "m.f", SITE, "return", dur=0.001)
    sink.close()
    counters = sink.metrics
    assert (counters["events_seen"], counters["sampled"], counters["tallied"]) == (100, 50, 50)
    assert counters["calls"] == 100
    assert counters["sampling_dropped"] == 0 and counters["dropped_events"] == 0
    (tally,) = _snapshot(sink)["tallies"]
    assert tally["count"] == 100


def test_unsampled_call_without_a_tally_is_reported_as_dropped(tmp_path, monkeypatch):
    monkeypatch.setenv("PYMOLT_TRACE_SAMPLE_RATE", "0.5")
    monkeypatch.setattr(_sinks.random, "random", lambda: 0.9)
    sink = _sink(tmp_path)
    assert sink.admit() is False  # declined, and the backend never tallies it
    sink.close()
    assert sink.metrics["sampling_dropped"] == 1


def test_non_admitted_call_is_admitted_or_tallied_by_the_sink(tmp_path, monkeypatch):
    monkeypatch.setenv("PYMOLT_TRACE_SAMPLE_RATE", "0.5")
    monkeypatch.setattr(_sinks.random, "random", lambda: 0.9)
    sink = _sink(tmp_path)
    sink.on_return("m.f", {"bound": {}}, 1, site=SITE, sym="m.f")      # declined -> tallied
    sink.on_raise("m.f", {"bound": {}}, "ValueError", site=SITE, sym="m.f")  # always kept
    sink.close()
    m = sink.metrics
    assert (m["calls"], m["sampled"], m["tallied"], m["dropped_events"]) == (2, 1, 1, 0)


def test_oversize_values_are_kept_as_type_and_digest(tmp_path):
    sink = _sink(tmp_path, value_bytes=128)
    big = "x" * 1000
    sink.on_return("m.f", {"bound": {"s": big}}, big, site=SITE, sym="m.f")
    sink.on_return("m.f", {"bound": {"s": big}}, big, site=SITE, sym="m.f")
    sink.close()
    (fact,) = _facts(sink, "m.f")
    assert set(fact["in"]) == {"__oversize__", "__digest__"}
    (out,) = fact["outcomes"]
    assert out["result"]["__opaque__"] == "str" and out["count"] == 2
    assert sink.metrics["oversize_values"] == 4




def test_periodic_flush_replaces_the_snapshot_atomically(tmp_path, monkeypatch):
    sink = _sink(tmp_path, flush_seconds=0.05)
    seen = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr(_sinks.os, "replace", spy)
    sink.on_return("m.f", {"bound": {}}, 1, site=SITE, sym="m.f")
    deadline = time.time() + 5
    while not os.path.exists(sink.path) and time.time() < deadline:
        time.sleep(0.01)
    assert os.path.exists(sink.path), "the flusher never wrote a snapshot"
    assert json.load(open(sink.path))["facts"][0]["count"] == 1
    assert all(dst == sink.path and src.startswith(sink.path + ".tmp-")
               for src, dst in seen if dst == sink.path)
    sink.close()
    assert not [p for p in os.listdir(tmp_path) if ".tmp-" in p]


def test_a_failed_flush_leaves_the_previous_snapshot_intact(tmp_path, monkeypatch):
    sink = _sink(tmp_path)
    sink.on_return("m.f", {"bound": {}}, 1, site=SITE, sym="m.f")
    assert sink.flush()
    before = open(sink.path).read()

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(_sinks.os, "replace", boom)
    sink.on_return("m.f", {"bound": {}}, 1, site=SITE, sym="m.f")
    assert sink.flush() is False
    assert open(sink.path).read() == before
    assert sink.metrics["flush_failures"] == 1
    assert not [p for p in os.listdir(tmp_path) if ".tmp-" in p]


def test_threads_lose_no_call(tmp_path):
    sink = _sink(tmp_path, flush_seconds=0.01, facts_per_site=4)
    threads, per = 8, 5000
    barrier = threading.Barrier(threads)

    def work(n):
        barrier.wait()
        for i in range(per):
            site = {"file": "/app/main.py", "line": i % 13, "func": "w"}
            if i % 7 == 0:
                sink.tally("m.t", "m.t", site, "raise", raised="E")
            elif sink.admit():
                sink.on_return("m.f", {"bound": {"i": i % 9}}, i % 11, site=site,
                               admitted=True, sym="m.f", dur=1e-6, tid=n)

    ts = [threading.Thread(target=work, args=(n,)) for n in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    sink.close()
    snap = _snapshot(sink)
    total = threads * per
    tallied = threads * len(range(0, per, 7))
    assert snap["counters"]["calls"] == total
    assert snap["counters"]["tallied"] == tallied
    assert sum(t["count"] for t in snap["tallies"]) == total
    assert sum(f["count"] for f in snap["facts"]) == total - tallied
    assert sum(o["count"] for f in snap["facts"] for o in f["outcomes"]) == total - tallied
    assert snap["counters"]["dropped_events"] == 0


def test_close_is_idempotent_and_later_calls_are_counted_not_lost(tmp_path):
    sink = _sink(tmp_path)
    sink.on_return("m.f", {"bound": {}}, 1, site=SITE, sym="m.f")
    sink.close()
    first = open(sink.path).read()
    sink.close()
    sink.on_return("m.f", {"bound": {}}, 1, site=SITE, sym="m.f")
    sink.tally("m.f", "m.f", SITE, "return")
    assert open(sink.path).read() == first
    assert sink.metrics["shutdown_dropped"] == 2


def test_sidecar_carries_the_counters_the_host_requires(tmp_path):
    sink = _sink(tmp_path, metadata={"target": "m", "python": "3.12.1"})
    sink.set_metadata(backend="monitoring", mode="auto", switched_at=12.5, discovered=3)
    sink.on_return("m.f", {"bound": {}}, 1, site=SITE, sym="m.f")
    sink.close()
    meta = json.load(open(sink.metadata_path))
    assert _runtime_counters_complete(meta["counters"])
    assert (meta["sink"], meta["facts_path"], meta["backend"]) == (
        "facts", sink.path, "monitoring")
    snap = _snapshot(sink)
    assert (snap["mode"], snap["switched_at"], snap["discovered"]) == ("auto", 12.5, 3)


def test_tally_overflow_keeps_counting(tmp_path):
    sink = _sink(tmp_path, max_tallies=2)
    for line in range(5):
        sink.tally("m.f", "m.f", {"file": "/a.py", "line": line + 1, "func": "f"}, "return")
    sink.close()
    snap = _snapshot(sink)
    assert len(snap["tallies"]) == 3  # two sites + the overflow slot
    assert sum(t["count"] for t in snap["tallies"]) == 5
    assert snap["counters"]["tally_overflow"] == 3


def test_memory_is_bounded_by_the_caps_not_the_run(tmp_path):
    sink = _sink(tmp_path, max_facts=200, facts_per_site=4)
    for i in range(20000):
        site = {"file": "/app/main.py", "line": i % 50, "func": "f"}
        sink.on_return("m.f", {"bound": {"k": i}}, i, site=site, sym="m.f")
    sink.flush()
    m = sink.metrics
    assert m["facts"] <= 200 and m["pending"] == 0
    assert m["tallies"] == 50
    sink.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
def test_a_forked_child_starts_empty_under_its_own_pid(tmp_path):
    sink = FactSink(str(tmp_path / "trace-{pid}.jsonl"), flush_seconds=0)
    sink.on_return("m.parent", {"bound": {}}, 1, site=SITE, sym="m.parent")
    pid = os.fork()
    if pid == 0:  # child
        code = 1
        try:
            sink.on_return("m.child", {"bound": {}}, 2, site=SITE, sym="m.child")
            sink.close()
            doc = json.load(open(sink.path))
            code = 0 if [f["sym"] for f in doc["facts"]] == ["m.child"] else 2
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    assert os.WEXITSTATUS(status) == 0
    sink.close()
    assert [f["sym"] for f in _snapshot(sink)["facts"]] == ["m.parent"]
    assert (tmp_path / f"trace-{pid}.facts.json").is_file()




def test_widen_keeps_type_only_renderings():
    ambiguous = {"__opaque__": "ambiguous:return-or-raise"}
    assert widen_value(ambiguous) is ambiguous
    assert widen_value({"__shape__": "str"}) == {"__shape__": "str"}
    assert widen_value(5) == {"__opaque__": "int"}


@pytest.mark.parametrize("version", [(3, 6), (3, 4)])
def test_sinks_module_parses_on_old_pythons(version):
    source = Path(_sinks.__file__).read_text(encoding="utf-8")
    ast.parse(source, feature_version=version)
    assert sys.version_info >= (3, 8)  # the check itself needs a modern host


# ── novelty-first sampling (all sinks) ───────────────────────────────────────────────────

def test_novelty_first_admits_each_keys_first_calls_then_the_rate(monkeypatch):
    monkeypatch.setattr(_sinks.random, "random", lambda: 0.99)  # the rate never admits
    sampler = _sinks.NoveltySampler(rate=0.1, first=3, max_keys=10)
    assert [sampler.sampled(("m.f", "/a.py", 1)) for _ in range(5)] == [True] * 3 + [False] * 2
    assert sampler.sampled(("m.f", "/a.py", 2)) is True  # another site is new again
    assert sampler.sampled(None) is False                 # no key: the rate alone


def test_novelty_table_is_bounded_and_falls_back_to_the_rate(monkeypatch):
    monkeypatch.setattr(_sinks.random, "random", lambda: 0.99)
    sampler = _sinks.NoveltySampler(rate=0.1, first=5, max_keys=2)
    assert sampler.sampled("a") and sampler.sampled("b")
    assert sampler.sampled("c") is False  # past the cap: never remembered, the rate decides
    assert len(sampler._seen) == 2


def test_novelty_defaults(monkeypatch):
    monkeypatch.delenv("PYMOLT_TRACE_SAMPLE_FIRST", raising=False)
    assert _sinks.NoveltySampler(rate=0.1).first == 32
    assert _sinks.NoveltySampler(rate=1.0).first == 0  # unused when everything is sampled
    monkeypatch.setenv("PYMOLT_TRACE_SAMPLE_FIRST", "4")
    assert _sinks.NoveltySampler(rate=0.5).first == 4


@pytest.mark.parametrize("make", [
    lambda p: FactSink(str(p / "f-{pid}.jsonl"), flush_seconds=0),
    lambda p: _sinks.JsonlSink(str(p / "j-{pid}.jsonl")),
])
def test_sinks_admit_by_key(tmp_path, monkeypatch, make):
    monkeypatch.setenv("PYMOLT_TRACE_SAMPLE_RATE", "0.1")
    monkeypatch.setenv("PYMOLT_TRACE_SAMPLE_FIRST", "2")
    monkeypatch.setattr(_sinks.random, "random", lambda: 0.99)
    sink = make(tmp_path)
    key = ("m.f", "/a.py", 3)
    assert [sink.admit(key=key) for _ in range(4)] == [True, True, False, False]
    assert sink.admit(key=key, force=True) is True
    assert sink.admit() is False
    sink.close()
    assert sink.metrics["events_seen"] == 6
    assert _sinks.MemorySink().admit(key=key) is True


def test_a_symbol_with_huge_object_results_goes_type_level(tmp_path):
    class Big:
        pass

    def make():
        b = Big()
        for i in range(400):
            setattr(b, f"attr{i:03d}", "x" * 20)
        return b

    sink = _sink(tmp_path)
    first = make()
    sink.on_return("m.f", {}, first, SITE, sym="m.f")
    sink.on_return("m.f", {}, make(), SITE, sym="m.f")
    sink.close()
    outcomes = [o["result"] for f in _facts(sink, "m.f") for o in f["outcomes"]]
    assert any("__object__" in r for r in outcomes)       # the witness that proved it heavy
    assert any("__opaque__" in r for r in outcomes)       # type-level from then on

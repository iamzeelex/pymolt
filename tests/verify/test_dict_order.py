"""Dict key insertion order (value format ``dict_order``): recorded by the runtime beside the
sorted pairs, stripped by the host at load, compared like a value where both captures record
it. Every rule: the noise case it absorbs and the adversarial case it must not hide.

Values are produced by the real runtime normalizer (``_normalize.normalize``), so the format
the host reads is the one the tracer writes.
"""
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from pymolt.verify import _normalize as nz
from pymolt.verify import _sinks
from pymolt.verify.fact_report import summarize
from pymolt.verify.facts import (
    Comparison,
    ComparisonLevel,
    FactCategory,
    OrderCause,
    diff_facts,
    fold_facts,
    load_facts,
)
from pymolt.verify.models import VerificationVerdict
from pymolt.verify.replicate import build_noise_profile

ROOT = "/proj"
STATS = ["last_request_timestamp", "start_time", "num_requests", "num_none_requests",
         "num_failures", "total_response_time", "max_response_time", "min_response_time",
         "total_content_length", "response_times", "num_reqs_per_sec", "num_fail_per_sec",
         "name", "method"]
STATS_39 = ["name", "method"] + STATS[:12]


def ordered(keys, value=lambda k: 1):
    return {k: value(k) for k in keys}


def rec(symbol, inp, result=None, *, line=10, kind="return", raised=None):
    out = {"t": kind, "v": 2, "q": symbol, "sym": symbol, "in": inp,
           "where": {"file": f"{ROOT}/app.py", "func": "main", "line": line}}
    if kind == "return":
        out["result"] = result
    else:
        out["raised"] = raised
    return out


def call(symbol, kwargs=None, result=None, **kw):
    """One call: ``kwargs`` / ``result`` are Python values the runtime normalizes."""
    inp = {"bound": {}, "kwargs": nz.normalize(kwargs)} if kwargs is not None else {"bound": {}}
    return rec(symbol, inp, nz.normalize(result), **kw)


def side(*records, order=True, env=None):
    fs = fold_facts(records, root=ROOT)
    fs.dict_order = order
    fs.environment = env
    return fs


def only(d):
    (e,) = d.entries
    return e


# ── load: strip, never key ────────────────────────────────────────────────────────────────


def test_order_is_stripped_at_load_and_never_splits_a_fact():
    a = call("lib.f", ordered(["b", "a"]), 1)
    b = call("lib.f", ordered(["a", "b"]), 1)
    fs = side(a, b)
    (f,) = fs.facts                       # one input content: one fact
    assert "__order__" not in json.dumps(f.input)
    assert f.count == 2
    assert f.input_order == {'["kwargs"]': "*"}   # two orders within one run: volatile


def test_the_same_order_twice_is_kept():
    fs = side(call("lib.f", ordered(["b", "a"]), 1), call("lib.f", ordered(["b", "a"]), 1))
    assert fs.facts[0].input_order == {'["kwargs"]': [1, 0]}


def test_old_captures_diff_exactly_as_before_when_order_is_not_recorded_on_one_side():
    # A new capture (with order) against an old one (without): order is ignored, and the diff
    # is byte-identical to the one of two captures that never recorded it.
    old = [call("lib.f", ordered(["b", "a"]), ordered(["y", "x"]))]
    new = [call("lib.f", ordered(["a", "b"]), ordered(["x", "y"]))]
    plain_old = json.loads(json.dumps(old).replace(', "__order__": [1, 0]', ""))
    gated = diff_facts(side(*old, order=False), side(*new, order=True))
    plain = diff_facts(side(*plain_old, order=False), side(*new, order=False))
    jg, jp = (x.to_json_dict(include_equal=True) for x in (gated, plain))
    # only the stamp says which side did not record the order
    assert jg["summary"]["quality"].pop("dict_order") == "not recorded (old)"
    assert jp["summary"]["quality"].pop("dict_order") == "not recorded (old, new)"
    assert json.dumps(jg, sort_keys=True, default=str) == json.dumps(jp, sort_keys=True,
                                                                     default=str)
    assert only(gated).category is FactCategory.EQUAL and gated.dict_order == \
        "not recorded (old)"


def test_the_locust_fixture_diffs_byte_identically_with_its_orders_unrecorded():
    # golden: the pre-order fixture, loaded as old captures are (no value_format), gives the
    # same entries whatever the other side declares
    from test_fact_noise import LOCUST, _locust_side  # noqa: PLC0415

    data = json.loads(LOCUST.read_text())
    a = diff_facts(_locust_side(data["old"]), _locust_side(data["new"]))
    new = _locust_side(data["new"])
    new.dict_order = True
    b = diff_facts(_locust_side(data["old"]), new)
    ja, jb = a.to_json_dict(include_equal=True), b.to_json_dict(include_equal=True)
    for key in ("entries", "counts", "verdict", "warnings"):
        assert json.dumps(ja[key], sort_keys=True, default=str) == \
            json.dumps(jb[key], sort_keys=True, default=str)


# ── a: output order only ──────────────────────────────────────────────────────────────────


def test_a_returned_dict_whose_order_changed_is_a_review_not_a_failure():
    old = side(call("lib.f", None, ordered(["a", "b", "c"])))
    new = side(call("lib.f", None, ordered(["c", "a", "b"])))
    d = diff_facts(old, new)
    e = only(d)
    assert e.category is FactCategory.CHANGED and not e.fails
    assert e.review.startswith("order changed: same keys and values")
    assert not e.review.startswith("input changed")
    assert e.order == {"side": "output", "path": "<value>", "keys": 3, "old": ["a", "b", "c"],
                       "new": ["c", "a", "b"]}
    assert d.verdict is VerificationVerdict.INCONCLUSIVE


def test_an_order_change_with_int_keys_is_a_review_too():
    # decision 1: an unmeasured order difference never fails, whatever the key type
    old = side(call("lib.f", None, {3: "x", 1: "y"}))
    new = side(call("lib.f", None, {1: "y", 3: "x"}))
    e = only(diff_facts(old, new))
    assert e.review.startswith("order changed") and not e.fails


def test_an_order_change_under_the_same_pinned_hash_seed_fails():
    pinned = [{"hash": {"seed": 0, "randomized": False, "algorithm": "siphash13"}}]
    old = side(call("lib.f", None, ordered(["a", "b"])), env=pinned)
    new = side(call("lib.f", None, ordered(["b", "a"])), env=pinned)
    old.python_versions = new.python_versions = ["3.12.1"]
    e = only(diff_facts(old, new))
    assert e.fails and e.review is None and "order-changed" in e.notes


def test_a_pinned_seed_never_controls_order_across_a_python_move():
    pinned = [{"hash": {"seed": 0, "randomized": False, "algorithm": "siphash13"}}]
    old = side(call("lib.f", None, ordered(["a", "b"])), env=pinned)
    new = side(call("lib.f", None, ordered(["b", "a"])), env=pinned)
    old.python_versions, new.python_versions = ["3.11.9"], ["3.12.1"]
    assert only(diff_facts(old, new)).review.startswith("order changed")


def test_equal_orders_stay_equal_and_record_their_agreement():
    old = side(call("lib.f", None, ordered(["b", "a", "c"])))
    new = side(call("lib.f", None, ordered(["b", "a", "c"])))
    e = only(diff_facts(old, new))
    assert e.category is FactCategory.EQUAL and e.order is None
    assert e.order_agreed == [('"a"', '"b"', '"c"')]


# ── b / c: input order only ───────────────────────────────────────────────────────────────


def test_b_an_input_reordered_with_the_same_output_is_equal_and_explained():
    old = side(call("json.dumps", ordered(["a", "b"]), '{"a": 1, "b": 1}'))
    new = side(call("json.dumps", ordered(["b", "a"]), '{"a": 1, "b": 1}'))
    e = only(diff_facts(old, new))
    assert e.category is FactCategory.EQUAL and not e.review
    assert e.explained.startswith("order: input key order differs")
    assert "input-order-differs" in e.notes


def test_c_an_output_that_follows_the_inputs_order_is_equal_at_masked_level():
    old = side(call("copy.copy", ordered(["a", "b", "c"]), ordered(["a", "b", "c"])))
    new = side(call("copy.copy", ordered(["c", "b", "a"]), ordered(["c", "b", "a"])))
    e = only(diff_facts(old, new))
    assert e.category is FactCategory.EQUAL and e.level is ComparisonLevel.MASKED
    assert e.explained.startswith("order: the output's key order follows the input's")


def test_c_adversarial_an_output_order_that_does_not_follow_the_input_is_a_review():
    old = side(call("lib.f", ordered(["a", "b", "c"]), ordered(["a", "b", "c"])))
    new = side(call("lib.f", ordered(["c", "b", "a"]), ordered(["b", "a", "c"])))
    assert only(diff_facts(old, new)).review.startswith("order changed")


# ── d: input order only, the output differs ───────────────────────────────────────────────


def _packb(keys):
    """A msgpack-like encoding: the key order reaches the bytes."""
    return "".join(f"{k}={len(k)};" for k in keys).encode()


def test_d_a_byte_permutation_after_an_input_reorder_is_a_review_never_same_complete_input():
    old = side(call("msgpack.packb", ordered(STATS), _packb(STATS)))
    new = side(call("msgpack.packb", ordered(STATS_39), _packb(STATS_39)))
    d = diff_facts(old, new)
    e = only(d)
    assert e.category is FactCategory.CHANGED and e.level is ComparisonLevel.EXACT
    assert e.review.startswith("input order changed: the dict key order reaching this call")
    assert not e.review.startswith("input changed")
    assert "byte-permutation (len" in e.review and not e.fails
    assert e.order["side"] == "input" and e.order["keys"] == 14
    assert e.order["new"][:2] == ["name", "method"]
    finding = summarize(d)["review"][0]
    assert finding["reason"] == "input order changed"
    assert finding["findings"][0]["sample"]["order"]["keys"] == 14


def test_d_adversarial_an_output_that_differs_beyond_a_reordering_still_fails():
    # a real encoder change must not hide behind an order change
    old = side(call("msgpack.packb", ordered(STATS), _packb(STATS)))
    new = side(call("msgpack.packb", ordered(STATS_39), _packb(STATS_39) + b"!"))
    d = diff_facts(old, new)
    e = only(d)
    assert e.fails and e.review is None and "input-order-differs" in e.notes
    (f,) = summarize(d)["failing"]
    assert "same complete input" not in f["why"]
    assert f["why"].startswith("value changed; input equal except dict key order at kwargs")


def test_d_a_kind_change_after_an_input_reorder_fails_as_before():
    old = side(call("msgpack.packb", ordered(STATS), _packb(STATS)))
    new = side(rec("msgpack.packb", {"bound": {}, "kwargs": nz.normalize(ordered(STATS_39))},
                   kind="raise", raised="TypeError"))
    e = only(diff_facts(old, new))
    assert e.fails and e.review is None


def test_e_an_input_that_differs_in_content_too_is_judged_by_the_existing_rules():
    old = side(call("lib.f", ordered(["a", "b"], lambda k: 1), "x"))
    new = side(call("lib.f", ordered(["b", "a"], lambda k: 2), "y"))
    e = only(diff_facts(old, new))
    assert e.review.startswith("input changed")      # the existing rule, not an order rule
    assert "input-order-differs" in e.notes


# ── knowledge causes ──────────────────────────────────────────────────────────────────────

TD_CAUSE = OrderCause(keys=frozenset(STATS), old=tuple(STATS), new=tuple(STATS_39),
                      cause="typing-typeddict-annotations-order",
                      detail="Python 3.9 TypedDict annotation order, dict built at stats.py:546")


def test_an_exactly_matching_cause_of_five_or_more_keys_makes_it_a_failure_with_the_cause():
    old = side(call("msgpack.packb", ordered(STATS), _packb(STATS)))
    new = side(call("msgpack.packb", ordered(STATS_39), _packb(STATS_39)))
    d = diff_facts(old, new, order_causes=[TD_CAUSE])
    e = only(d)
    assert e.fails and e.review is None and "order-cause-cited" in e.notes
    assert "cause: Python 3.9 TypedDict annotation order" in e.explained
    assert e.order["cause"].endswith("(typing-typeddict-annotations-order)")
    (f,) = summarize(d)["failing"]
    assert "cause: Python 3.9 TypedDict" in f["why"] and f["sample"]["order"]["cause"]


def test_a_partial_match_gets_no_citation():
    other = OrderCause(keys=frozenset(STATS), old=tuple(STATS), new=tuple(reversed(STATS)),
                       cause="x", detail="something else")
    old = side(call("msgpack.packb", ordered(STATS), _packb(STATS)))
    new = side(call("msgpack.packb", ordered(STATS_39), _packb(STATS_39)))
    e = only(diff_facts(old, new, order_causes=[other]))
    assert e.review.startswith("input order changed") and "cause" not in e.order


def test_a_cause_under_five_keys_is_cited_but_proves_nothing():
    small = OrderCause(keys=frozenset("abc"), old=("a", "b", "c"), new=("c", "a", "b"),
                       cause="k", detail="known")
    old = side(call("lib.f", None, ordered(["a", "b", "c"])))
    new = side(call("lib.f", None, ordered(["c", "a", "b"])))
    e = only(diff_facts(old, new, order_causes=[small]))
    assert e.review.startswith("order changed") and e.order["cause"] == "known (k)"


# ── where order is not compared ───────────────────────────────────────────────────────────


def test_order_that_varies_within_one_run_is_not_compared():
    old = side(call("lib.f", None, ordered(["a", "b"])), call("lib.f", None, ordered(["b", "a"])))
    new = side(call("lib.f", None, ordered(["b", "a"])))
    e = only(diff_facts(old, new))
    assert not e.review and "order-varies-in-run" in e.notes


def test_masked_keys_that_collide_are_not_comparable():
    import uuid
    u = [str(uuid.uuid4()) for _ in range(4)]
    src = rec("uuid.uuid4", {"bound": {}}, {"__uuid__": u[0]}, line=1)
    src2 = rec("uuid.uuid4", {"bound": {}}, {"__uuid__": u[1]}, line=1)
    old = side(src, src2, call("lib.f", None, ordered([u[0], u[1]]), line=2))
    src3 = rec("uuid.uuid4", {"bound": {}}, {"__uuid__": u[2]}, line=1)
    src4 = rec("uuid.uuid4", {"bound": {}}, {"__uuid__": u[3]}, line=1)
    new = side(src3, src4, call("lib.f", None, ordered([u[3], u[2]]), line=2))
    d = diff_facts(old, new)
    (e,) = [x for x in d.entries if x.symbol == "lib.f"]
    assert not (e.review or "").startswith("order") and e.order is None


def test_order_is_never_compared_at_type_level():
    old = side(call("lib.f", None, ordered(["a", "b"], lambda k: object())))
    new = side(call("lib.f", None, ordered(["b", "a"], lambda k: object())))
    e = only(diff_facts(old, new))
    assert e.level is ComparisonLevel.TYPE and e.order is None and not e.review


# ── report ────────────────────────────────────────────────────────────────────────────────


def test_the_report_says_whether_order_was_compared():
    d = diff_facts(side(call("lib.f", None, 1)), side(call("lib.f", None, 1)))
    assert summarize(d)["quality"]["dict_order"] == "compared"
    d = diff_facts(side(call("lib.f", None, 1), order=False), side(call("lib.f", None, 1)))
    assert summarize(d)["quality"]["dict_order"] == "not recorded (old)"


def test_unrecorded_order_never_claims_the_same_complete_input_for_a_dict_input():
    old = side(call("msgpack.packb", ordered(STATS), b"\x01\x02"), order=False)
    new = side(call("msgpack.packb", ordered(STATS_39), b"\x02\x01"), order=False)
    (f,) = summarize(diff_facts(old, new))["failing"]
    assert f["why"] == ("value changed; same input content (dict key order not recorded by "
                        "these captures)")


def test_a_scalar_input_keeps_the_same_complete_input_wording():
    old = side(rec("lib.f", {"bound": {"x": 1}}, 1), order=False)
    new = side(rec("lib.f", {"bound": {"x": 1}}, 2), order=False)
    (f,) = summarize(diff_facts(old, new))["failing"]
    assert f["why"] == "value changed; same complete input on both sides"


# ── load from files ───────────────────────────────────────────────────────────────────────


def test_a_trace_and_its_metadata_declare_the_format(tmp_path: Path):
    trace = tmp_path / "t.jsonl"
    lines = [call("lib.f", ordered(["b", "a"]), ordered(["y", "x"]))]
    trace.write_text("".join(json.dumps(x) + "\n" for x in lines))
    (tmp_path / "t.jsonl.meta.json").write_text(json.dumps(
        {"processes": [{"value_format": {"dict_order": 1}, "environment": {"schema": 1}}]}))
    fs = load_facts(trace, root=ROOT, runner=None)
    assert fs.dict_order and fs.environment == [{"schema": 1}]
    (f,) = fs.facts
    assert f.input_order == {'["kwargs"]': [1, 0]} and f.outcomes[0].order == {"[]": [1, 0]}
    (tmp_path / "t.jsonl.meta.json").write_text(json.dumps({"processes": [{"python": "3.8"}]}))
    fs = load_facts(trace, root=ROOT, runner=None)
    assert not fs.dict_order and fs.environment is None


def test_the_line_fast_path_skips_lines_without_order(tmp_path: Path):
    trace = tmp_path / "t.jsonl"
    trace.write_text(json.dumps(call("lib.f", {"a": 1, "b": 2}, 1)) + "\n")
    fs = load_facts(trace, root=ROOT, runner=None)
    assert fs.facts[0].input_order is None
    assert Comparison  # imported for readers of these tests


# ── the baseline replicate ────────────────────────────────────────────────────────────────

RANDOM = [{"hash": {"seed": None, "randomized": True, "algorithm": "siphash13"}}]
PINNED = [{"hash": {"seed": 0, "randomized": False, "algorithm": "siphash13"}}]


def _judge(baseline, replicate, post):
    from pymolt.verify.replicate import apply_noise_profile, build_noise_profile

    d = diff_facts(baseline, post)
    profile = build_noise_profile(baseline, replicate)
    apply_noise_profile(d, profile, "the replicate")
    return d, profile


def test_an_order_that_varies_between_baseline_runs_is_noise():
    base = side(call("lib.f", None, ordered(["a", "b", "c"])), env=RANDOM)
    rep = side(call("lib.f", None, ordered(["b", "c", "a"])), env=RANDOM)
    post = side(call("lib.f", None, ordered(["c", "a", "b"])), env=RANDOM)
    d, profile = _judge(base, rep, post)
    e = only(d)
    assert e.noise and "(order)" in e.noise and not e.fails and not e.inconclusive
    assert list(profile["varying"].values()) == [["order"]]


def test_a_large_order_stable_across_random_seeded_runs_is_confirmed_deterministic():
    base = side(call("msgpack.packb", ordered(STATS), _packb(STATS)), env=RANDOM)
    rep = side(call("msgpack.packb", ordered(STATS), _packb(STATS)), env=RANDOM)
    post = side(call("msgpack.packb", ordered(STATS_39), _packb(STATS_39)), env=RANDOM)
    d, profile = _judge(base, rep, post)
    e = only(d)
    assert "equal@order" in next(iter(profile["equal"].values()))
    assert e.fails and e.review is None
    (f,) = summarize(d)["failing"]
    assert "stable across baseline runs (confirmed deterministic)" in f["why"]
    assert f["why"].startswith("value changed as the input's dict key order changed")


def test_a_small_order_that_agreed_by_chance_proves_nothing():
    # 3 keys agree by luck with chance 1/6: above 1/100, the review stays and says why
    base = side(call("lib.f", None, ordered(["a", "b", "c"])), env=RANDOM)
    rep = side(call("lib.f", None, ordered(["a", "b", "c"])), env=RANDOM)
    post = side(call("lib.f", None, ordered(["c", "a", "b"])), env=RANDOM)
    d, profile = _judge(base, rep, post)
    e = only(d)
    assert "equal@order" not in next(iter(profile["equal"].values()))
    assert e.review.startswith("order changed") and "cannot tell" in e.review and not e.fails


def test_a_replicate_with_a_pinned_hash_seed_cannot_measure_order():
    base = side(call("msgpack.packb", ordered(STATS), _packb(STATS)), env=PINNED)
    rep = side(call("msgpack.packb", ordered(STATS), _packb(STATS)), env=PINNED)
    post = side(call("msgpack.packb", ordered(STATS_39), _packb(STATS_39)), env=RANDOM)
    d, profile = _judge(base, rep, post)
    e = only(d)
    assert profile["order_evidence"] is False and "pinned seed" in profile["order_note"]
    assert e.review.startswith("input order changed") and "pinned seed" in e.review


def test_a_replicate_of_unknown_hashing_cannot_measure_order():
    base = side(call("msgpack.packb", ordered(STATS), _packb(STATS)))
    rep = side(call("msgpack.packb", ordered(STATS), _packb(STATS)))
    post = side(call("msgpack.packb", ordered(STATS_39), _packb(STATS_39)))
    d, profile = _judge(base, rep, post)
    assert profile["order_evidence"] is False and only(d).review.startswith("input order")


def test_an_entry_is_noise_only_when_every_class_it_carries_varies():
    # the bytes vary between baseline runs at the same input order; post: the input's order
    # changed and the bytes are a permutation (value@complete + order). The value noise alone
    # does not explain an order change, and the replicate's two runs agreed on the 14-key
    # input order: the order change is confirmed deterministic.
    base = side(call("msgpack.packb", ordered(STATS), b"\x01\x02"), env=RANDOM)
    rep = side(call("msgpack.packb", ordered(STATS), b"\x02\x01"), env=RANDOM)
    post = side(call("msgpack.packb", ordered(STATS_39), b"\x02\x01"), env=RANDOM)
    d, profile = _judge(base, rep, post)
    e = only(d)
    assert next(iter(profile["varying"].values())) == ["value@complete"]
    assert not e.noise and e.fails
    assert any(n.startswith("confirmed deterministic") for n in e.notes)


def test_a_strict_input_order_failure_keeps_todays_value_class():
    # beyond a reordering: "a strict FAIL as today", so value noise at the site explains it
    # as it always did
    base = side(call("msgpack.packb", ordered(STATS), b"\x01"), env=RANDOM)
    rep = side(call("msgpack.packb", ordered(STATS), b"\x02"), env=RANDOM)
    post = side(call("msgpack.packb", ordered(STATS_39), b"\x03\x04"), env=RANDOM)
    d, _profile = _judge(base, rep, post)
    assert only(d).noise


# ── review regressions ────────────────────────────────────────────────────────────────────

SITE = {"file": "/p/app.py", "func": "main", "line": 3}


def _sink_capture(tmp_path, order_on, calls, name="t"):
    """``calls``: (kwargs, result) Python values, recorded by a real FactSink, loaded back."""
    saved = nz.DICT_ORDER[0]
    try:
        nz.DICT_ORDER[0] = order_on
        d = tmp_path / f"{name}-{order_on}"
        d.mkdir()
        sink = _sinks.FactSink(str(d / "t-{pid}.jsonl"), flush_seconds=0,
                               metadata={"value_format": nz.value_format()})
        for kwargs, result in calls:
            inp = {"bound": {}, "kwargs": nz.normalize(kwargs)}
            sink.on_return("lib.f", inp, result, site=SITE, admitted=True, sym="lib.f")
        sink.close()
        snap = next(p for p in d.iterdir() if p.name.endswith(".facts.json"))
        return sink, load_facts(snap, root="/p", runner=None)
    finally:
        nz.DICT_ORDER[0] = saved


def _shuffled(seed, n, value=lambda k, i: 1):
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        ks = list("abcdef")
        rnd.shuffle(ks)                  # stands in for the hash order of 6 str keys
        out.append(({k: value(k, i) for k in ks}, {k: value(k, i) for k in ks}))
    return out


def test_the_facts_sink_keys_on_the_order_stripped_value(tmp_path):
    # F1: 300 calls with one hash-ordered input and result are ONE fact with ONE outcome
    sink, fs = _sink_capture(tmp_path, True, _shuffled(1, 300))
    m = sink.metrics
    assert m["facts"] == 1 and m["widened"] == 0 and m["outcomes_other"] == 0
    (f,) = fs.facts
    assert fs.dict_order and len(f.outcomes) == 1 and f.count == 300
    # the order rides beside the value: seen in several orders, so "varies within the run"
    assert f.input_order == {'["kwargs"]': "*"} and f.outcomes[0].order == {"[]": "*"}
    assert "__order__" not in json.dumps(f.input) + json.dumps(f.outcomes[0].value)


def test_a_stable_order_reaches_the_host_through_the_snapshot(tmp_path):
    calls = [({"b": 1, "a": 2}, {"y": 1, "x": 2})] * 3
    _sink, fs = _sink_capture(tmp_path, True, calls)
    (f,) = fs.facts
    assert f.input_order == {'["kwargs"]': [1, 0]} and f.outcomes[0].order == {"[]": [1, 0]}


def test_a_real_change_at_a_site_with_hash_ordered_inputs_still_fails(tmp_path):
    # F1b: order must not widen the site (a widened site compares by type only)
    def calls(seed, delta):
        return [({k: i % 3 for k in ks}, (i % 3) + delta)
                for i, (ks, _r) in enumerate(_shuffled(seed, 300, lambda k, i: i % 3))]
    for on in (False, True):
        _s, old = _sink_capture(tmp_path, on, calls(1, 0), "old")
        _s, new = _sink_capture(tmp_path, on, calls(2, 100), "new")
        assert diff_facts(old, new).verdict is VerificationVerdict.FAIL


def test_equivalent_hash_ordered_results_keep_their_evidence(tmp_path):
    # F1c: one hash-ordered result is one outcome, not "other" past the outcome cap
    _s, old = _sink_capture(tmp_path, True, [({"x": 1}, r) for _k, r in _shuffled(1, 100)],
                            "old")
    _s, new = _sink_capture(tmp_path, True, [({"x": 1}, r) for _k, r in _shuffled(2, 100)],
                            "new")
    d = diff_facts(old, new)
    assert d.verdict is VerificationVerdict.PASS
    assert not any(w.startswith("outcomes-capped") for w in d.warnings)


def test_an_oversize_result_is_digested_without_its_order(tmp_path):
    # the digest of an oversize value is taken over the order-stripped JSON
    big = {f"key{i:03d}": "v" * 3000 for i in range(40)}
    rev = dict(reversed(list(big.items())))
    _s, old = _sink_capture(tmp_path, False, [({"x": 1}, big)], "old")
    _s, new = _sink_capture(tmp_path, True, [({"x": 1}, rev)], "new")
    assert old.facts[0].outcomes[0].value == new.facts[0].outcomes[0].value
    assert diff_facts(old, new).verdict is VerificationVerdict.PASS


def test_merged_process_snapshots_merge_their_orders(tmp_path):
    from pymolt.verify.facts import merge_fact_snapshots

    _s1, _ = _sink_capture(tmp_path, True, [({"b": 1, "a": 2}, 1)], "p1")
    _s2, _ = _sink_capture(tmp_path, True, [({"a": 2, "b": 1}, 1)], "p2")
    paths = [_s1.path, _s2.path]
    out = tmp_path / "merged.facts.json"
    merge_fact_snapshots(paths, out)
    fs = load_facts(out, root="/p", runner=None)
    (f,) = fs.facts
    assert f.input_order == {'["kwargs"]': "*"}   # two processes, two orders: varies


def test_replicate_order_evidence_is_per_key_set_not_per_site():
    # F2: a deterministic 5-key dict at one input never proves a hash-ordered 3-key dict at
    # another input of the same site deterministic ...
    from pymolt.verify.replicate import apply_noise_profile

    d5 = ["a", "b", "c", "d", "e"]

    def calls(five, three):
        return [call("lib.f", {"i": 1}, ordered(five)), call("lib.f", {"i": 2}, ordered(three))]
    base = side(*calls(d5, ["x", "y", "z"]), env=RANDOM)
    rep = side(*calls(d5, ["x", "y", "z"]), env=RANDOM)
    post = side(*calls(d5, ["z", "x", "y"]), env=RANDOM)
    d = diff_facts(base, post)
    apply_noise_profile(d, build_noise_profile(base, rep), "r")
    (e,) = [x for x in d.entries if x.category is not FactCategory.EQUAL]
    assert not e.fails and e.review.startswith("order changed") and "cannot tell" in e.review
    # ... and a hash-ordered 3-key dict varying there never explains a real 5-key change
    base = side(*calls(d5, ["x", "y", "z"]), env=RANDOM)
    rep = side(*calls(d5, ["z", "y", "x"]), env=RANDOM)
    post = side(*calls(["c", "a", "e", "b", "d"], ["x", "y", "z"]), env=RANDOM)
    d = diff_facts(base, post)
    apply_noise_profile(d, build_noise_profile(base, rep), "r")
    (e,) = [x for x in d.entries if x.category is not FactCategory.EQUAL]
    assert e.fails and not e.noise and e.order["keys"] == 5


def test_the_legacy_comparator_ignores_dict_key_order(tmp_path):
    # F6: an old capture against a new one that recorded order: no difference
    from pymolt.verify.diff import build_boundary_diff

    def trace(name, mapping, order_on):
        saved = nz.DICT_ORDER[0]
        try:
            nz.DICT_ORDER[0] = order_on
            p = tmp_path / f"{name}.jsonl"
            ev = {"t": "return", "q": "lib.f", "in": {"bound": {}, "kwargs": nz.normalize(mapping)},
                  "result": nz.normalize(mapping)}
            p.write_text(json.dumps(ev) + "\n")
            return str(p)
        finally:
            nz.DICT_ORDER[0] = saved
    a = dict.fromkeys("bca", 1)
    counts = build_boundary_diff(trace("old", a, False), trace("new", a, True)).counts()
    assert not any(counts.values()), counts


def test_case_d_residual_an_unrelated_permutation_is_a_review_never_a_pass():
    # stated residual (``_judge_order``): an output that changed into a permutation of itself
    # for another reason, at a call whose input dict was reordered, is a review
    old = side(call("lib.slug", ordered(["a", "b", "c"]), "abcdef"))
    new = side(call("lib.slug", ordered(["c", "a", "b"]), "fedcba"))
    d = diff_facts(old, new)
    assert d.verdict is VerificationVerdict.INCONCLUSIVE
    assert only(d).review.startswith("input order changed")


def test_the_unmeasured_noise_gate_never_softens_a_proved_order_failure():
    from pymolt.verify.facts import _gate_unmeasured

    old = side(call("msgpack.packb", ordered(STATS), _packb(STATS)))
    new = side(call("msgpack.packb", ordered(STATS_39), _packb(STATS_39)))
    e = only(diff_facts(old, new, order_causes=[TD_CAUSE]))
    e.input = {"bound": {"self": {"__opaque__": "msgpack.Packer"}}}   # a hidden input
    assert _gate_unmeasured([e], "1 volatile site(s)") == 0 and e.fails and e.review is None

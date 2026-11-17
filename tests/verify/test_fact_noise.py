"""Verify noise rules: what makes two runs differ besides the migration, and what must still fail.

Every rule has two kinds of tests: the noise case it explains and the regression it must not hide.
Facts are built directly (no trace files): ``fact`` is one dependency call from one site.
"""
import hashlib
import json
import uuid
from pathlib import Path

from pymolt.verify.facts import (
    Comparison,
    ComparisonLevel,
    DiffContext,
    Fact,
    FactCategory,
    FactSet,
    Outcome,
    compare_outcomes,
    diff_facts,
    types_match,
    value_type,
)
from pymolt.verify.models import VerificationVerdict

ROOT = "/proj"


def out(*pairs, ts=None):
    """``out(("return", 1), ("raise", "ValueError"))`` -> outcomes (count 1 each); ``ts``: when
    the calls producing them ran."""
    return [Outcome(kind=k, value=v, count=1, ts_min=ts, ts_max=ts) for k, v in pairs]


def fact(symbol, bound=None, outcomes=(("return", None),), *, file="app.py", func="main",
         line=10, text=None, count=1, first=0, inp=None, ts=None):
    """``ts``: when the call ran (epoch seconds), as the JSONL events' ``ts`` would say."""
    leaf = symbol.rsplit(".", 1)[-1]
    return Fact(
        symbol=symbol, file=file, func=func, line=line, lines=[line],
        text=text if text is not None else f"x = obj.{leaf}(a)",
        input=inp if inp is not None else {"bound": dict(bound or {})},
        outcomes=out(*outcomes, ts=ts), count=count, first_seen=first, ts_min=ts, ts_max=ts,
    )


def fs(*facts, backends=("monitoring",), **kw):
    return FactSet(facts=list(facts), root=ROOT, backends=list(backends), **kw)


def failing(d):
    return [e for e in d.entries if e.fails]


# ── step 1: an opaque rendering meets a structural rendering of the same type ─────────────

PATTERN_OBJ = {"__object__": "re.Pattern", "attrs": {"flags": 34, "groups": 0,
                                                     "pattern": "^https?://"}}


def test_opaque_and_object_of_the_same_type_match_at_type_level():
    assert types_match({"opaque": "re.Pattern"}, value_type(PATTERN_OBJ))
    assert types_match(value_type(PATTERN_OBJ), {"opaque": "re.Pattern"})
    cmp = compare_outcomes(out(("return", {"__opaque__": "re.Pattern"})),
                           out(("return", PATTERN_OBJ)))
    assert cmp.comparison is Comparison.EQUAL and cmp.level is ComparisonLevel.TYPE


def test_opaque_and_object_of_different_types_still_differ():
    other = {"__object__": "re.Match", "attrs": {"pos": 0}}
    assert not types_match({"opaque": "re.Pattern"}, value_type(other))
    cmp = compare_outcomes(out(("return", {"__opaque__": "re.Pattern"})), out(("return", other)))
    assert cmp.comparison is Comparison.CHANGED


def test_renderer_asymmetry_is_not_a_failure_in_the_diff():
    # 3.8 renders re.Pattern opaque (a static type), 3.12 as an __object__ (a heap type).
    old = fs(fact("re.compile", {"pattern": "^https?://", "flags": 2},
                  (("return", {"__opaque__": "re.Pattern"}),)), backends=("setprofile",))
    new = fs(fact("re.compile", {"pattern": "^https?://", "flags": 2},
                  (("return", PATTERN_OBJ),)))
    d = diff_facts(old, new)
    assert not failing(d) and d.verdict is VerificationVerdict.PASS
    assert d.entries[0].category is FactCategory.EQUAL
    assert d.entries[0].level is ComparisonLevel.TYPE


def test_renderer_asymmetry_does_not_hide_a_new_raise():
    old = fs(fact("re.compile", {"pattern": "("}, (("return", {"__opaque__": "re.Pattern"}),)),
             backends=("setprofile",))
    new = fs(fact("re.compile", {"pattern": "("}, (("raise", "re.error"),)))
    d = diff_facts(old, new)
    assert [e.category for e in failing(d)] == [FactCategory.CHANGED]


# ── step 2: volatile sites (every call a fresh input) compare as a group ──────────────────

PACK = "msgpack.packb"
PACK_LINE = "return msgpack.dumps((self.type, self.data, self.node_id))"


def node_uuid(i):
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"node-{i}"))


def packs(ids, *, result=None, first=0, raise_at=None, evidenced=True):
    """One fact per call: each carries its own client id, so no input repeats across runs.
    ``evidenced``: the id embeds a uuid4 value the trace recorded (a visible source of the
    variation, like locust's ``hostname_uuid4hex`` node ids), and the packed bytes carry it
    too; once masked, every call is the same ``ack`` on both sides. Otherwise the ids are just
    different."""
    facts = []
    if evidenced:
        facts.append(fact("uuid.uuid4", {}, tuple(("return", {"__uuid__": node_uuid(i)})
                                                  for i in ids),
                          file="locust/runners.py", func="__init__", line=5, text="uuid4()",
                          first=first))
    for k, i in enumerate(ids):
        hexid = node_uuid(i).replace("-", "")
        res = result(i) if result else {"__bytes__": ("ack_" + hexid).encode().hex()} \
            if evidenced else {"__bytes__": f"93a3{i}"}
        outcome = (("raise", raise_at),) if raise_at is not None and k == 0 else \
            (("return", res),)
        # host part: the container's on one side, the laptop's on the other (as locust's
        # node ids), plus a per-call worker number, so no input repeats or merges
        host = "container" if str(i).startswith("a") else "laptop"
        node = f"{host}{k}_{hexid}" if evidenced else f"node_{i}"
        facts.append(fact(PACK, {"o": {"__tuple__": ["ack", node]}}, outcome,
                          file="locust/rpc/protocol.py", func="serialize", line=14,
                          text=PACK_LINE, first=first + k + 1))
    return facts


def group_of(d, symbol=PACK):
    (e,) = [e for e in d.entries if e.symbol == symbol]
    return e


def test_fresh_inputs_at_one_site_compare_as_one_group():
    old = fs(*packs(["a1", "a2", "a3"]))
    new = fs(*packs(["b1", "b2"]))
    d = diff_facts(old, new)
    assert not failing(d) and d.verdict is VerificationVerdict.PASS
    e = group_of(d)
    assert e.category is FactCategory.EQUAL and e.level is ComparisonLevel.MASKED
    assert (e.old_count, e.new_count) == (3, 2)
    assert e.explained.startswith("volatile-site") and "volatile-site:3/2" in e.notes
    assert any(w.startswith("volatile-sites: 1 fully") for w in d.warnings)


def test_a_new_raise_type_in_a_volatile_group_fails():
    old = fs(*packs(["a1", "a2", "a3"]))
    new = fs(*packs(["b1", "b2"], raise_at="TypeError"))
    d = diff_facts(old, new)
    (e,) = failing(d)
    assert e.category is FactCategory.CHANGED and e.explained.startswith("volatile-site")


def test_a_lost_raise_in_a_volatile_group_fails():
    old = fs(*packs(["a1", "a2", "a3"], raise_at="ValueError"))
    new = fs(*packs(["b1", "b2"]))
    assert [e.category for e in failing(diff_facts(old, new))] == [FactCategory.CHANGED]


def test_a_new_return_type_in_a_volatile_group_fails():
    old = fs(*packs(["a1", "a2"]))
    new = fs(*packs(["b1", "b2", "b3"], result=lambda i: f"93a3{i}"))  # bytes -> str
    assert [e.category for e in failing(diff_facts(old, new))] == [FactCategory.CHANGED]


def test_exact_repeats_at_a_volatile_site_still_compare_exactly():
    # msgpack: the same message (same input) packs to different bytes (dict order changed):
    # the repeat pairs exactly and fails; the fresh-input calls around it form the group.
    stats = {"o": {"__tuple__": ["stats", {"__dict__": [["name", "/1"], ["method", "GET"]]}]}}
    repeat_old = fact(PACK, stats, (("return", {"__bytes__": "93a5aa"}),),
                      file="locust/rpc/protocol.py", func="serialize", line=14, text=PACK_LINE)
    repeat_new = fact(PACK, stats, (("return", {"__bytes__": "93a5bb"}),),
                      file="locust/rpc/protocol.py", func="serialize", line=14, text=PACK_LINE)
    d = diff_facts(fs(repeat_old, *packs(["a1", "a2", "a3"], first=1)),
                   fs(repeat_new, *packs(["b1", "b2"], first=1)))
    (e,) = failing(d)
    assert e.category is FactCategory.CHANGED and e.level is ComparisonLevel.EXACT
    assert e.input == {"bound": stats}
    rest = [x for x in d.entries if x.symbol == PACK and x is not e]
    assert [x.category for x in rest] == [FactCategory.EQUAL]  # the group


def test_one_sided_site_is_not_a_group():
    d = diff_facts(fs(*packs(["a1", "a2"], evidenced=False)), fs())
    assert d.counts()["disappeared"] == 2 and d.verdict is VerificationVerdict.FAIL


def test_a_changed_input_signature_is_not_absorbed_into_a_group():
    # Same symbol and site, but the bound parameter was renamed: structure, not noise.
    old = fs(*[fact("lib.f", {"a": i}, first=i) for i in range(2)])
    new = fs(fact("lib.f", {"b": 9}))
    d = diff_facts(old, new)
    assert d.counts()["disappeared"] == 2 and d.counts()["appeared"] == 1


def tax(amounts, rate):
    return [fact("tax.compute", {"amount": a}, (("return", a * rate),), first=k)
            for k, a in enumerate(amounts)]


def test_a_group_without_a_visible_source_of_variation_is_a_review_item():
    """Deterministic project, inputs changed by an untraced upstream: the rate moved 1.2 ->
    1.3. The inputs reaching the site changed with no visible cause: evidence that something
    changed, not that the dependency did, so INCONCLUSIVE (was FAIL), never a PASS."""
    d = diff_facts(fs(*tax([100, 200], 1.2)), fs(*tax([300, 400, 500], 1.3)))
    (e,) = d.entries
    assert not e.fails and e.review.startswith("unpaired site")
    assert e.explained.startswith("unpaired-site") and (e.old_count, e.new_count) == (2, 3)
    assert e.input == {"bound": {"amount": 100}} and e.new_input == {"bound": {"amount": 300}}
    assert d.verdict is VerificationVerdict.INCONCLUSIVE
    assert any(w.startswith("volatile-sites: 0 fully") and "1 not at all" in w
               for w in d.warnings)


def test_an_unpaired_site_with_a_type_change_fails():
    d = diff_facts(fs(*tax([100, 200], 1.2)),
                   fs(*[fact("tax.compute", {"amount": a}, (("return", str(a)),), first=k)
                        for k, a in enumerate([300, 400, 500])]))
    assert [e.symbol for e in failing(d)] == ["tax.compute"]


def test_an_unpaired_site_is_a_review_item_whatever_the_gate():
    d = diff_facts(fs(*tax([100, 200], 1.2), *packs(["a1", "a2", "a3"], first=10)),
                   fs(*tax([300, 400, 500], 1.3), *packs(["b1", "b2"], first=10)))
    e = group_of(d, "tax.compute")
    assert not e.fails and e.review.startswith("unpaired site")
    assert d.verdict is VerificationVerdict.INCONCLUSIVE


def sends(ids, first, data):
    send = "zmq.green.core._Socket.send"
    return [fact(send, {"data": data(i)}, file="locust/rpc/zmqrpc.py", func="send", line=20,
                 text="self.socket.send(msg.serialize())", first=first + k)
            for k, i in enumerate(ids)]


SEND = "zmq.green.core._Socket.send"


def packed_bytes(i):
    """Bytes that do not carry the id itself (a digest of the message): no mask reaches
    them, only propagation from the call that produced them."""
    return {"__bytes__": hashlib.sha1(f"msg-{i}".encode()).hexdigest()}


def test_volatility_propagates_from_a_fully_paired_group_to_the_next_call():
    # msgpack (a fully paired site: node ids carry uuid4 values) produces zmq send's input.
    d = diff_facts(fs(*packs(["a1", "a2", "a3"], result=packed_bytes),
                      *sends(["a1", "a2", "a3"], 50, packed_bytes)),
                   fs(*packs(["b1", "b2"], result=packed_bytes),
                      *sends(["b1", "b2"], 50, packed_bytes)))
    e = group_of(d, SEND)
    assert e.category is FactCategory.EQUAL and e.explained.startswith("volatile-site")
    # msgpack's own outputs differ (they digest the id): a review item, not a failure
    (p,) = [x for x in d.entries if x.symbol == PACK]
    assert p.review and not p.fails and not failing(d)
    # ... and without the group upstream, the same sends are an unpaired site (a review item).
    d = diff_facts(fs(*sends(["a1", "a2", "a3"], 50, packed_bytes)),
                   fs(*sends(["b1", "b2"], 50, packed_bytes)))
    e = group_of(d, SEND)
    assert e.explained.startswith("unpaired-site") and e.review and not failing(d)


# ── step 3: environment, clock, entropy sources, input substitution ───────────────────────

T_OLD = 1_794_824_765.0   # 2026-11-16 10:26:05 UTC: when the old side's call ran
T_NEW = 1_794_825_101.0   # 2026-11-16 10:31:41 UTC


def env_fs(*facts, roots=(), python=None, ts=None, **kw):
    """A side with its environment identity; ``ts``: every fact's call time."""
    for f in facts:
        if ts is not None:
            f.ts_min = f.ts_max = ts
            for o in f.outcomes:
                o.ts_min = o.ts_max = ts
    s = fs(*facts, **kw)
    s.env_roots = list(roots)
    s.python_versions = [python] if python else []
    s.host_capture = False
    return s


def test_project_root_as_each_process_saw_it_is_environment():
    tb_old = '  File "/work/locust/test/test_web.py", line 198, in test_exceptions\n'
    tb_new = ('  File "/Users/me/src/locust/locust/test/test_web.py", line 198, in '
              'test_exceptions\n')
    old = env_fs(fact("html.escape", {"s": tb_old}, (("return", tb_old),)), roots=["/work"])
    new = env_fs(fact("html.escape", {"s": tb_new}, (("return", tb_new),)),
                 roots=["/Users/me/src/locust"])
    d = diff_facts(old, new)
    (e,) = d.entries
    assert not failing(d) and e.category is FactCategory.EQUAL
    assert e.level is ComparisonLevel.MASKED and e.explained.startswith("environment")


def test_a_path_that_lost_its_root_still_differs():
    old = env_fs(fact("lib.where", {}, (("return", "/work/locust/data.csv"),)), roots=["/work"])
    new = env_fs(fact("lib.where", {}, (("return", "locust/data.csv"),)),
                 roots=["/Users/me/src/locust"])
    assert [e.category for e in failing(diff_facts(old, new))] == [FactCategory.CHANGED]


def test_the_python_version_is_environment_and_stays_visible():
    old = env_fs(fact("platform.python_version", {}, (("return", "3.8.20"),)), python="3.8.20")
    new = env_fs(fact("platform.python_version", {}, (("return", "3.12.11"),)),
                 python="3.12.11")
    d = diff_facts(old, new)
    (e,) = d.entries
    assert not failing(d) and e.explained.startswith("environment")
    assert e.old[0].value == "⟨python⟩" and "3.8.20" in e.explained


def test_clock_readings_at_the_call_time_are_masked():
    old = env_fs(fact("lib.render", {"t": "x"},
                      (("return", "During: 2026-11-16 10:26:05 - report_1794824765.0062904"),)),
                 ts=T_OLD)
    new = env_fs(fact("lib.render", {"t": "x"},
                      (("return", "During: 2026-11-16 10:31:41 - report_1794825101.045572"),)),
                 ts=T_NEW)
    d = diff_facts(old, new)
    (e,) = d.entries
    assert not failing(d) and e.explained.startswith("clock")
    assert e.level is ComparisonLevel.MASKED


def test_a_timezone_shift_is_outside_the_window_and_still_differs():
    # UTC -> local time (UTC-6): the new value is six hours away from the new run's window.
    old = env_fs(fact("lib.stamp", {"fmt": "iso"}, (("return", "2026-11-16T10:26:04"),)),
                 ts=T_OLD)
    new = env_fs(fact("lib.stamp", {"fmt": "iso"}, (("return", "2026-11-16T04:31:41"),)),
                 ts=T_NEW)
    assert [e.category for e in failing(diff_facts(old, new))] == [FactCategory.CHANGED]


def test_a_clock_value_away_from_the_call_time_is_not_masked():
    # auth.expiry returns now + TTL: the TTL shrank from 120 s to 30 s. Both values are inside
    # their runs' windows, but not within seconds of the call: not a reading of the clock.
    old = env_fs(fact("auth.expiry", {"user": "u"}, (("return", int(T_OLD) + 120),)), ts=T_OLD)
    new = env_fs(fact("auth.expiry", {"user": "u"}, (("return", int(T_NEW) + 30),)), ts=T_NEW)
    assert [e.symbol for e in failing(diff_facts(old, new))] == ["auth.expiry"]
    # A fact without a call time (a facts-snapshot capture) is never clock-masked, and says so.
    old = env_fs(fact("lib.stamp", {"x": 1}, (("return", int(T_OLD)),)))
    new = env_fs(fact("lib.stamp", {"x": 1}, (("return", int(T_NEW)),)))
    d = diff_facts(old, new)
    assert failing(d) and any(w.startswith("clock-unmasked:old:1:") for w in d.warnings)


def test_a_huge_integer_is_no_clock_reading_and_does_not_crash():
    big = 10 ** 400
    old = env_fs(fact("m.f", {"n": big}, (("return", big),)), ts=T_OLD)
    new = env_fs(fact("m.f", {"n": big}, (("return", big),)), ts=T_NEW)
    assert not failing(diff_facts(old, new))


def test_a_clock_format_change_still_differs():
    # Seconds precision lost (a float epoch became an int), and an offset appeared.
    old = env_fs(fact("lib.ts", {"x": 1}, (("return", 1_794_824_765.006),)), ts=T_OLD)
    new = env_fs(fact("lib.ts", {"x": 1}, (("return", 1_794_825_101),)), ts=T_NEW)
    assert failing(diff_facts(old, new))
    old = env_fs(fact("lib.iso", {"x": 1}, (("return", "2026-11-16T10:26:04"),)),
                 ts=T_OLD)
    new = env_fs(fact("lib.iso", {"x": 1}, (("return", "2026-11-16T10:31:41+00:00"),)),
                 ts=T_NEW)
    assert failing(diff_facts(old, new))


UUID_A = "03c09253-4e55-4474-913d-3ac4c0d94453"
UUID_B = "d2b0d279-25f4-4c89-b5f0-02153bbccd83"


def uuid_side(u, rendered, line_out="node"):
    return [fact("uuid.uuid4", {}, (("return", {"__uuid__": u}),), line=5, text="uuid4()"),
            fact("lib.register", {"name": "w"}, (("return", f"{line_out}_{rendered}"),),
                 line=6, first=1)]


def test_values_from_an_entropy_source_are_masked_everywhere_on_that_side():
    old = fs(*uuid_side(UUID_A, UUID_A.replace("-", "")))
    new = fs(*uuid_side(UUID_B, UUID_B.replace("-", "")))
    d = diff_facts(old, new)
    assert not failing(d), [(e.symbol, e.old, e.new) for e in failing(d)]
    reg = next(e for e in d.entries if e.symbol == "lib.register")
    assert reg.explained.startswith("source") and reg.level is ComparisonLevel.MASKED


def test_a_uuid_format_change_still_differs():
    # The dependency used to render the id with hyphens; now it renders the hex form.
    old = fs(*uuid_side(UUID_A, UUID_A))
    new = fs(*uuid_side(UUID_B, UUID_B.replace("-", "")))
    d = diff_facts(old, new)
    assert [e.symbol for e in failing(d)] == ["lib.register"]


def test_a_random_pick_is_compared_by_structure():
    choice = "random.Random.choice"
    seq = {"self": {"__opaque__": "random.Random"}, "seq": {"__list__": [0.5, 1, 1.5]}}
    d = diff_facts(fs(fact(choice, seq, (("return", 1),))),
                   fs(fact(choice, seq, (("return", 1.5),))))
    (e,) = d.entries
    assert not failing(d) and e.explained.startswith("source")
    # ... unless the program seeded its generator: then the values are deterministic.
    seed = fact("random.seed", {"a": 42}, line=3, text="random.seed(42)")
    d = diff_facts(fs(seed, fact(choice, seq, (("return", 1),))),
                   fs(seed, fact(choice, seq, (("return", 1.5),))))
    assert [e.symbol for e in failing(d)] == [choice]


def test_a_new_raise_from_an_entropy_source_still_fails():
    old = fs(fact("random.Random.choice", {"seq": {"__list__": []}}, (("return", 1),)))
    new = fs(fact("random.Random.choice", {"seq": {"__list__": []}}, (("raise", "IndexError"),)))
    assert failing(diff_facts(old, new))


def url_pair(port_old, port_new, *, scheme_new="http"):
    old = fact("urllib.parse.urlparse", {"url": f"http://127.0.0.1:{port_old}/x"},
               (("return", {"__object__": "urllib.parse.ParseResult", "attrs": {
                   "netloc": f"127.0.0.1:{port_old}", "scheme": "http", "path": "/x"}}),))
    new = fact("urllib.parse.urlparse", {"url": f"http://127.0.0.1:{port_new}/x"},
               (("return", {"__object__": "urllib.parse.ParseResult", "attrs": {
                   "netloc": f"127.0.0.1:{port_new}", "scheme": scheme_new, "path": "/x"}}),))
    return fs(old), fs(new)


def test_output_that_differs_only_as_the_input_does_is_a_review_item_with_its_evidence():
    # The port is no visible run value (ports are untraced), so the input change is
    # unexplained: a review item, not the PASS it was; the substitution that explains the
    # output is kept as evidence.
    d = diff_facts(*url_pair(32925, 57864))
    (e,) = d.entries
    assert not e.fails and e.review.startswith("input changed")
    assert "input-substitution" in e.explained and "32925 -> 57864" in e.explained
    assert d.verdict is VerificationVerdict.INCONCLUSIVE


def test_an_input_difference_at_volatile_leaves_is_a_masked_input_pair():
    # The changed leaf holds a uuid4 value the trace recorded: explained by the run.
    def side(u, out):
        return fs(fact("uuid.uuid4", {}, (("return", {"__uuid__": u}),), line=1, text="u()"),
                  fact("lib.tag", {"id": f"job-{u}", "kind": "a"}, (("return", out),), line=2))
    old, new = side(UUID_A, "ok"), side(UUID_B, "ok")
    (e,) = [x for x in diff_facts(old, new).entries if x.symbol == "lib.tag"]
    assert e.category is FactCategory.EQUAL
    old, new = side(UUID_A, "ok"), side(UUID_B, "no")
    (e,) = [x for x in diff_facts(old, new).entries if x.symbol == "lib.tag"]
    assert e.review.startswith("masked input") and not e.fails


def test_a_residual_difference_beyond_the_input_is_never_a_pass():
    # The port changed (no visible cause) AND the scheme now differs: the pair's input changed,
    # so this is a review item (was FAIL); kinds and types agree, so not a failure either.
    d = diff_facts(*url_pair(32925, 57864, scheme_new="https"))
    (e,) = d.entries
    assert e.review.startswith("input changed") and d.verdict is VerificationVerdict.INCONCLUSIVE
    assert "input-substitution" not in e.explained  # the residual is not explained


def test_an_input_change_with_a_new_raise_fails():
    old = fs(fact("billing.charge", {"amount": 100}, (("return", "120"),)))
    new = fs(fact("billing.charge", {"amount": 999}, (("raise", "ValueError"),)))
    assert [e.symbol for e in failing(diff_facts(old, new))] == ["billing.charge"]


def test_a_changed_amount_with_the_same_output_is_not_a_pass():
    # n1 "P1 ctrl": amount 100 -> 999, output "ok" both times. The input reaching the call
    # changed for no visible reason (an untraced round() upstream looks the same).
    old = fs(fact("billing.charge", {"amount": 100}, (("return", "ok"),)))
    new = fs(fact("billing.charge", {"amount": 999}, (("return", "ok"),)))
    d = diff_facts(old, new)
    assert d.verdict is VerificationVerdict.INCONCLUSIVE
    assert d.entries[0].review.startswith("input changed")


def test_the_same_values_bound_differently_are_binding_drift():
    """click 8 moved ``prog_name`` from ``**kwargs`` to a parameter: the same values, bound
    differently. Residual: two arguments swapped (``f(a=1, b=2)`` -> ``f(a=2, b=1)``) look
    unchanged too."""
    old = fs(fact("click.version_option", inp={"bound": {"version": "1.2.0"}, "kwargs": {
        "__dict__": [["prog_name", "deployctl"]]}}, outcomes=(("return", "d"),)))
    new = fs(fact("click.version_option", inp={"bound": {"prog_name": "deployctl",
                                                         "version": "1.2.0"}},
                  outcomes=(("return", "d"),)))
    (e,) = diff_facts(old, new).entries
    assert e.category is FactCategory.EQUAL and e.explained.startswith("binding")
    # ... and the outcome is still compared strictly
    new = fs(fact("click.version_option", inp={"bound": {"prog_name": "deployctl",
                                                         "version": "1.2.0"}},
                  outcomes=(("return", "e"),)))
    assert failing(diff_facts(old, new))


def test_an_input_structure_change_is_not_substituted():
    # Another key and another value: no substitution; the input value changed, so a review.
    old = fs(fact("lib.f", {"url": "a:1111"}, (("return", "a:1111"),)))
    new = fs(fact("lib.f", {"uri": "a:2222"}, (("return", "a:2222"),)))
    d = diff_facts(old, new)
    assert not any("input-substitution" in (e.explained or "") for e in d.entries)
    assert d.entries[0].review.startswith("input changed")


def test_a_short_token_only_masks_whole_words():
    # 9095 -> 9096 is the run's port; the output also holds a constant containing 9095.
    old = fs(fact("lib.page", {"port": 9095}, (("return", "port 9095, e=2.5949095"),)))
    new = fs(fact("lib.page", {"port": 9096}, (("return", "port 9096, e=2.5949096"),)))
    (e,) = diff_facts(old, new).entries
    # the constant changed: the substitution does not explain the output (no partial mask)
    assert "input-substitution" not in e.explained and e.review.startswith("input changed")
    new = fs(fact("lib.page", {"port": 9096}, (("return", "port 9096, e=2.5949095"),)))
    (e,) = diff_facts(old, new).entries
    assert "input-substitution" in e.explained


def test_same_complete_input_value_change_fails():
    # msgpack: the same message packs to different bytes (dict insertion order changed).
    msg = {"o": {"__tuple__": ["stats", {"__dict__": [["name", "/1"], ["method", "GET"]]}]}}
    old = fs(fact("msgpack.packb", msg, (("return", {"__bytes__": "93a5aa"}),)))
    new = fs(fact("msgpack.packb", msg, (("return", {"__bytes__": "93a5bb"}),)))
    assert [e.category for e in failing(diff_facts(old, new))] == [FactCategory.CHANGED]


def test_opaque_receiver_value_change_fails_in_a_quiet_workload():
    # jinja2: the same template and context now render https instead of http.
    inp = {"self": {"__opaque__": "jinja2.environment.Template"}, "t": "www.example.org"}
    old = fs(fact("jinja2.environment.Template.render", inp,
                  (("return", '<a href="http://www.example.org">'),)))
    new = fs(fact("jinja2.environment.Template.render", inp,
                  (("return", '<a href="https://www.example.org">'),)))
    d = diff_facts(old, new)
    assert [e.category for e in failing(d)] == [FactCategory.CHANGED]
    assert d.verdict is VerificationVerdict.FAIL


# ── step 4: attribution and evidence asymmetries ──────────────────────────────────────────

DECORATED = ('import flask\napp = flask.Flask("x")\n\n\n@app.route("/fast")\n'
             'def fast():\n    return "ok"\n')


def ctx_for(src):
    return DiffContext(old_sources={"app.py": src}, new_sources={"app.py": src})


def decorator_call(symbol, line, text, fn="app.fast", result=None):
    return fact(symbol, {"f": {"__callable__": fn}},
                (("return", result or {"__callable__": fn}),), func="<module>", line=line,
                text=text)


def test_a_decorator_named_and_placed_differently_by_the_interpreter_pairs():
    # 3.8: co_name only, call attributed to the def line; 3.12: co_qualname, the @ line.
    old = fs(decorator_call("flask.sansio.scaffold.decorator", 6, "def fast():"),
             backends=("setprofile",))
    new = fs(decorator_call("flask.sansio.scaffold.Scaffold.route.<locals>.decorator", 5,
                            '@app.route("/fast")'))
    d = diff_facts(old, new, context=ctx_for(DECORATED))
    assert not failing(d), [(e.category, e.symbol) for e in d.entries]
    (e,) = d.entries
    assert e.category is FactCategory.EQUAL and e.explained.startswith("attribution")


def test_qualname_pairing_needs_the_same_leaf_and_input():
    old = fs(decorator_call("flask.sansio.scaffold.decorator", 6, "def fast():"),
             backends=("setprofile",))
    new = fs(decorator_call("flask.sansio.scaffold.Scaffold.route.<locals>.wrapper", 5,
                            '@app.route("/fast")'))
    d = diff_facts(old, new, context=ctx_for(DECORATED))
    assert not any((e.explained or "").startswith("attribution") for e in d.entries)
    new = fs(decorator_call("flask.sansio.scaffold.Scaffold.route.<locals>.decorator", 5,
                            '@app.route("/fast")', fn="app.slow"))
    d = diff_facts(old, new, context=ctx_for(DECORATED))
    assert not any((e.explained or "").startswith("attribution") for e in d.entries)


WITH_SRC = "def main(s):\n    with s.get('/') as r:\n        r.read()\n"


def test_a_with_exit_attributed_to_another_unedited_line_is_not_a_rewrite():
    old = fs(fact("lib.Response.__exit__", {"self": {"__opaque__": "lib.Response"}},
                  line=3, text="r.read()"), backends=("setprofile",))
    new = fs(fact("lib.Response.__exit__", {"self": {"__opaque__": "lib.Response"}},
                  line=2, text="with s.get('/') as r:"))
    d = diff_facts(old, new, context=ctx_for(WITH_SRC))
    (e,) = d.entries
    assert e.category is FactCategory.EQUAL and "attribution-moved" in e.notes


def test_a_call_moved_by_an_edit_is_still_a_rewrite():
    new_src = "def main(s):\n    with s.get('/', timeout=1) as r:\n        r.read()\n"
    ctx = DiffContext(old_sources={"app.py": WITH_SRC}, new_sources={"app.py": new_src})
    old = fs(fact("lib.Session.get", {"self": {"__opaque__": "lib.Session"}}, line=2,
                  text="with s.get('/') as r:"))
    new = fs(fact("lib.Session.get", {"self": {"__opaque__": "lib.Session"}}, line=2,
                  text="with s.get('/', timeout=1) as r:"))
    d = diff_facts(old, new, context=ctx)
    assert not any("attribution-moved" in e.notes for e in d.entries)


def c_call(symbol, line=10, outcomes=(("return", {"__opaque__": "unobserved:c-return"}),)):
    return fact(symbol, outcomes=outcomes, inp={"__c_call__": True}, line=line)


def test_a_compiled_call_only_the_wider_backend_sees_is_not_an_appearance():
    old = fs(backends=("setprofile",))
    new = fs(c_call("gevent._gevent_cgreenlet.Greenlet.join"))
    d = diff_facts(old, new)
    (e,) = d.entries
    assert e.category is FactCategory.DRIFT and e.explained.startswith("backend")
    assert d.verdict is VerificationVerdict.PASS
    assert any(w.startswith("backend-only:1:") for w in d.warnings)


def test_a_compiled_call_the_wider_backend_lost_still_disappears():
    old = fs(c_call("collections.defaultdict.items"), backends=("setprofile",))
    new = fs(backends=("monitoring",))
    assert [e.category for e in failing(diff_facts(old, new))] == [FactCategory.DISAPPEARED]


def test_compiled_calls_compare_strictly_under_one_backend_family():
    old = fs(backends=("monitoring",))
    new = fs(c_call("gevent._gevent_cgreenlet.Greenlet.join"))
    assert [e.category for e in failing(diff_facts(old, new))] == [FactCategory.APPEARED]


def test_a_fact_missing_on_a_side_that_dropped_events_is_not_comparable():
    old = fs(fact("json.dumps", {"obj": {"__dict__": [["a", 1]]}}, (("return", '{"a": 1}'),)))
    new = fs(dropped_events=1053468)
    d = diff_facts(old, new)
    (e,) = d.entries
    assert e.category is FactCategory.UNCOMPARABLE and "possibly-dropped:new" in e.notes
    assert d.verdict is VerificationVerdict.INCONCLUSIVE and not failing(d)


def test_drops_on_one_side_do_not_excuse_an_appearance_on_the_other():
    old = fs()
    new = fs(fact("lib.new_call", {"x": 1}), dropped_events=10)
    assert [e.category for e in failing(diff_facts(old, new))] == [FactCategory.APPEARED]


def test_a_volatile_group_missing_a_kind_on_the_dropping_side_is_not_comparable():
    old = fs(*packs(["a1", "a2", "a3"], raise_at="TimeoutError"))
    new = fs(*packs(["b1", "b2"]), dropped_events=5)
    e = group_of(diff_facts(old, new))
    assert e.category is FactCategory.UNCOMPARABLE and "possibly-dropped:new" in e.notes


def test_a_new_raise_type_on_the_dropping_side_still_fails():
    old = fs(*packs(["a1", "a2", "a3"]))
    new = fs(*packs(["b1", "b2"], raise_at="TypeError"), dropped_events=5)
    assert [e.category for e in failing(diff_facts(old, new))] == [FactCategory.CHANGED]


REQ_SRC = "def test(port):\n    response = requests.get(url)\n    return response\n"


def test_a_property_read_not_named_on_its_line_is_implicit():
    # io finalizers read HTTPResponse.closed; the call lands on the project's nearest line.
    closed = fact("urllib3.response.HTTPResponse.closed",
                  {"self": {"__opaque__": "urllib3.response.HTTPResponse"}},
                  (("return", True),), func="test", line=2,
                  text="response = requests.get(url)")
    other = fact("requests.api.get", {"url": "u"}, func="test", line=2,
                 text="response = requests.get(url)")
    d = diff_facts(fs(other), fs(other, closed), context=ctx_for(REQ_SRC))
    e = next(e for e in d.entries if e.symbol.endswith("closed"))
    assert e.category is FactCategory.DRIFT and e.explained.startswith("implicit")
    assert not failing(d)


def test_a_vanished_call_named_on_its_line_stays_strict():
    src = "def cols(row):\n    return list(row.keys())\n"
    keys = fact("sqlalchemy.engine.result.RowProxy.keys",
                {"self": {"__opaque__": "sqlalchemy.engine.result.RowProxy"}},
                (("return", {"__list__": ["id", "name"]}),), func="cols", line=2,
                text="return list(row.keys())")
    other = fact("lib.other", {"x": 1}, func="cols", line=2, text="return list(row.keys())")
    d = diff_facts(fs(keys, other), fs(other), context=ctx_for(src))
    assert [e.category for e in failing(d)] == [FactCategory.DISAPPEARED]


PATH_SRC = "import pathlib\n\nBASE = pathlib.Path(__file__)\n"


def path_ctor(symbol, outcomes=(("return", None),)):
    return fact(symbol, {"cls": {"__type__": "pathlib.Path"}}, outcomes, func="<module>",
                line=3, text="BASE = pathlib.Path(__file__)")


def test_the_other_half_of_a_paired_constructor_is_the_constructor_protocol():
    new_ = path_ctor("pathlib.Path.__new__", (("return", {"__opaque__": "pathlib.PosixPath"}),))
    init = path_ctor("pathlib.Path.__init__")
    d = diff_facts(fs(new_), fs(new_, init), context=ctx_for(PATH_SRC))
    e = next(e for e in d.entries if e.symbol.endswith("__init__"))
    assert e.category is FactCategory.DRIFT and "constructor protocol" in e.explained


def test_a_constructor_half_that_raises_still_fails():
    new_ = path_ctor("pathlib.Path.__new__", (("return", {"__opaque__": "pathlib.PosixPath"}),))
    init = path_ctor("pathlib.Path.__init__", (("raise", "TypeError"),))
    d = diff_facts(fs(new_), fs(new_, init), context=ctx_for(PATH_SRC))
    assert [e.symbol for e in failing(d)] == ["pathlib.Path.__init__"]


def test_the_tracer_observing_itself_is_not_compared():
    own = fact("collections.deque.popleft", outcomes=(("return", 1),),
               file="/tmp/pymolt-trace-x/bundle/pymolt_trace/_rawthreads.py")
    d = diff_facts(fs(), fs(own))
    assert not d.entries and any(w.startswith("tracer-self:1:") for w in d.warnings)


# ── step 5: the unmeasured-noise gate ─────────────────────────────────────────────────────

RENDER = "jinja2.environment.Template.render"
TEMPLATE = {"self": {"__opaque__": "jinja2.environment.Template"}, "t": "www.example.org"}


def noisy(*facts, **kw):
    """A side whose workload shows run variation: a volatile site (fresh input every call;
    three calls on the old side, two on the new, so no pass can pair them one to one)."""
    tag = kw.pop("tag", "a")
    n = 3 if tag == "a" else 2
    return fs(*facts, *packs([f"{tag}{i}" for i in range(n)], first=100), **kw)


def test_a_hidden_input_value_change_in_a_noisy_workload_is_a_review_item():
    old = noisy(fact(RENDER, TEMPLATE, (("return", "<td>23.2</td>"),)))
    new = noisy(fact(RENDER, TEMPLATE, (("return", "<td>41.9</td>"),)), tag="b")
    d = diff_facts(old, new)
    e = next(e for e in d.entries if e.symbol == RENDER)
    assert e.category is FactCategory.CHANGED and not e.fails and e.inconclusive
    assert e.review.startswith("unmeasured noise") and e.explained.startswith("unmeasured")
    assert d.verdict is VerificationVerdict.INCONCLUSIVE
    assert any(w.startswith("run-variation: 1 volatile site(s) fully paired") for w in d.warnings)
    assert "capture a baseline replicate to settle" in e.review


def test_the_same_hidden_input_change_fails_when_the_workload_is_quiet():
    old = fs(fact(RENDER, TEMPLATE, (("return", "<td>23.2</td>"),)))
    new = fs(fact(RENDER, TEMPLATE, (("return", "<td>41.9</td>"),)))
    d = diff_facts(old, new)
    assert [e.symbol for e in failing(d)] == [RENDER] and not d.entries[0].review
    assert not any(w.startswith("run-variation") for w in d.warnings)


def test_a_kind_change_fails_even_in_a_noisy_workload():
    old = noisy(fact(RENDER, TEMPLATE, (("return", "<a>"),)))
    new = noisy(fact(RENDER, TEMPLATE, (("raise", "jinja2.exceptions.UndefinedError"),)),
                tag="b")
    assert [e.symbol for e in failing(diff_facts(old, new))] == [RENDER]


def test_a_fully_observed_same_input_change_fails_even_in_a_noisy_workload():
    msg = {"o": {"__tuple__": ["stats", {"__dict__": [["name", "/1"], ["method", "GET"]]}]}}
    old = noisy(fact("msgpack.packb", msg, (("return", {"__bytes__": "93a5aa"}),), line=20))
    new = noisy(fact("msgpack.packb", msg, (("return", {"__bytes__": "93a5bb"}),), line=20),
                tag="b")
    assert [e.symbol for e in failing(diff_facts(old, new))] == ["msgpack.packb"]


def test_inputs_equal_only_once_masked_are_not_a_controlled_comparison():
    # The message carries a timestamp (masked as a clock reading); the packed bytes encode
    # it in binary, where no mask can follow it.
    def side(stamp, packed, when, tag):
        return env_fs(fact("lib.pack", {"o": {"__tuple__": ["dummy", stamp]}},
                           (("return", {"__bytes__": packed}),), line=30),
                      *noisy(tag=tag).facts, ts=when)
    d = diff_facts(side(T_OLD, "cb41dabf", T_OLD, "a"), side(T_NEW, "cb41dac0", T_NEW, "b"))
    (e,) = [e for e in d.entries if e.symbol == "lib.pack"]
    assert "matched-modulo-masked-input" in e.notes and e.review.startswith("masked input")
    assert "capture a baseline replicate to settle" in e.review
    assert d.verdict is VerificationVerdict.INCONCLUSIVE


def test_a_type_change_fails_even_in_a_noisy_workload():
    old = noisy(fact(RENDER, TEMPLATE, (("return", "<a>"),)))
    new = noisy(fact(RENDER, TEMPLATE, (("return", {"__bytes__": "3c613e"}),)), tag="b")
    assert [e.symbol for e in failing(diff_facts(old, new))] == [RENDER]


def test_a_changed_document_upstream_is_not_run_variation():
    # networkx: node_link_data changed its keys, so json.loads gets another document. The
    # whole-leaf substitution carries digits, but it is not a per-run token.
    doc_old = '{"directed": false, "links": [{"source": "ann", "weight": 2}]}'
    doc_new = '{"directed": false, "edges": [{"source": "ann", "weight": 2}]}'
    old = fs(fact("json.loads", {"s": doc_old}, (("return", {"__dict__": [["links", 1]]}),)))
    new = fs(fact("json.loads", {"s": doc_new}, (("return", {"__dict__": [["edges", 1]]}),)))
    d = diff_facts(old, new)
    assert not any(w.startswith("run-variation") for w in d.warnings)
    # json.loads behaved consistently (its output differs exactly as its input does), but its
    # input changed with no visible cause AND its result's keys (structure) differ: a failure,
    # with the substitution kept as evidence of why
    (e,) = d.entries
    assert "input-substitution" in e.explained and e.fails


# ── locust run 6, distilled: one or a few facts per mechanism, both sides ─────────────────
# Built from the real captures (3.8 container, setprofile -> 3.12 host, sys.monitoring, 39%
# of post events dropped, no code edits, no dependency moves) by the scratchpad script
# make_fixture.py; host paths and the host name are anonymised.

LOCUST = Path(__file__).parent / "data" / "locust_noise.json"


def _locust_side(d):
    s = FactSet(facts=[Fact(**f) for f in d["facts"]], root=d["root"], backends=d["backends"])
    s.python_versions = d["python_versions"]
    s.run_windows = [tuple(w) for w in d["run_windows"]]
    s.env_roots, s.host_capture = d["env_roots"], d["host_capture"]
    s.dropped_events = d["dropped_events"]
    return s


def test_locust_run6_fails_only_on_its_true_finding_and_platform():
    data = json.loads(LOCUST.read_text())
    d = diff_facts(_locust_side(data["old"]), _locust_side(data["new"]))
    by_symbol = {}
    for e in d.entries:
        by_symbol.setdefault(e.symbol, []).append(e)
    # the true finding: msgpack packs the same message to other bytes (dict order 3.8 vs 3.12)
    fails = {e.symbol for e in failing(d)}
    assert fails == {"msgpack.packb", "resource.setrlimit"}
    true = [e for e in by_symbol["msgpack.packb"] if e.fails]
    assert true and all(e.level is ComparisonLevel.EXACT and not e.new_input for e in true)
    # the setrlimit difference is the platform (raises in the container, returns on macOS)
    (rl,) = by_symbol["resource.setrlimit"]
    assert rl.fails and rl.level is ComparisonLevel.TYPE
    # hidden-state noise in a workload that varies between runs: review, not failure
    reviews = {e.symbol for e in d.entries if e.review and e.review.startswith("unmeasured")}
    assert {"gevent.baseserver.BaseServer.server_port",
            "requests.structures.CaseInsensitiveDict.__getitem__"} <= reviews
    # gevent.sleep's durations vary with no visible source: one unpaired-site review entry
    (sleep,) = by_symbol["gevent.hub.sleep"]
    assert sleep.explained.startswith("unpaired-site") and sleep.review
    # zmq send's bytes carry the uuid4 node ids (hex-encoded): every call pairs once they are
    # masked, on both sides, which is what turns the gate on
    (send,) = by_symbol["zmq.green.core._Socket.send"]
    assert send.category is FactCategory.EQUAL and send.explained.startswith("volatile-site")
    assert any(w.startswith("run-variation: 1 volatile site(s) fully paired") and
               "zmq.green.core._Socket.send" in w for w in d.warnings)
    # the explained mechanisms, each at least once
    mechs = {(e.explained or "").split(":")[0] for e in d.entries}
    assert {"volatile-site", "unpaired-site", "masked-input", "input-changed", "source",
            "environment", "clock", "attribution", "backend", "possibly-dropped"} <= mechs
    # the ephemeral ports: input changes with no visible cause, review items, never failures
    ports = [e for e in d.entries if "input-changed" in e.notes]
    assert ports and all(e.review and not e.fails for e in ports)
    (pv,) = by_symbol["platform.python_version"]
    assert pv.explained.startswith("environment") and not pv.fails
    assert all(not e.fails for e in by_symbol["re.compile"])
    assert d.verdict is VerificationVerdict.FAIL


def test_a_lone_clock_reading_does_not_switch_the_gate_on():
    # One logged timestamp is not evidence that this call's hidden state varies by run.
    stamp = "logger.stamp"
    js = "requests.Response.json"
    r = {"self": {"__opaque__": "requests.Response"}}
    old = env_fs(fact(stamp, {"t": int(T_OLD)}, line=1, text="log.stamp(t)"),
                 fact(js, r, (("return", {"__dict__": [["total", 100]]}),), line=2,
                      text="d = r.json()"), ts=T_OLD)
    new = env_fs(fact(stamp, {"t": int(T_NEW)}, line=1, text="log.stamp(t)"),
                 fact(js, r, (("return", {"__dict__": [["total", 90]]}),), line=2,
                      text="d = r.json()"), ts=T_NEW)
    d = diff_facts(old, new)
    assert [e.symbol for e in failing(d)] == [js]
    assert not any(w.startswith("run-variation") for w in d.warnings)


def test_a_lone_run_like_substitution_does_not_switch_the_gate_on():
    old = fs(fact("api.get", {"id": "id12345"}, (("return", "id12345"),), line=1),
             fact("lib.r", {"self": {"__opaque__": "lib.R"}}, (("return", 1),), line=2))
    new = fs(fact("api.get", {"id": "id99999"}, (("return", "id99999"),), line=1),
             fact("lib.r", {"self": {"__opaque__": "lib.R"}}, (("return", 2),), line=2))
    assert [e.symbol for e in failing(diff_facts(old, new))] == ["lib.r"]


def test_the_gate_warning_names_its_evidence():
    d = diff_facts(noisy(fact(RENDER, TEMPLATE, (("return", "1"),))),
                   noisy(fact(RENDER, TEMPLATE, (("return", "2"),)), tag="b"))
    (w,) = [w for w in d.warnings if w.startswith("run-variation")]
    assert "1 volatile site(s) fully paired on masked run values: msgpack.packb @ " \
        "locust/rpc/protocol.py:14" in w


def test_a_call_with_no_input_at_all_is_fully_observed():
    # cfg.default_timeout() binds nothing and has no receiver: 30 -> 60 is a regression even
    # when the workload varies between runs.
    t = "cfg.default_timeout"
    d = diff_facts(noisy(fact(t, {}, (("return", 30),), line=2)),
                   noisy(fact(t, {}, (("return", 60),), line=2), tag="b"))
    assert [e.symbol for e in failing(d)] == [t]


def test_a_vanished_self_only_call_not_named_on_its_line_stays_strict():
    old = fs(fact("db.Session.commit", {"self": {"__opaque__": "db.Session"}}, line=5,
                  text="done()"))
    d = diff_facts(old, fs(), context=DiffContext())
    assert [e.category for e in failing(d)] == [FactCategory.DISAPPEARED]


def test_a_vanished_constructor_half_stays_strict():
    new_ = fact("lib.T.__new__", {"cls": {"__type__": "lib.T"}},
                (("return", {"__opaque__": "lib.T"}),), line=3, text="t = lib.T()")
    init = fact("lib.T.__init__", {"self": {"__opaque__": "lib.T"}}, line=3, text="t = lib.T()")
    d = diff_facts(fs(new_, init), fs(new_), context=DiffContext())
    assert [e.symbol for e in failing(d)] == ["lib.T.__init__"]


def test_a_compiled_call_that_raises_on_the_wider_backend_cannot_be_compared():
    # setprofile could not record the call at all: its absence is no evidence, the raise is
    # neither a change nor noise. Never FAIL, never DRIFT.
    old = fs(backends=("setprofile",))
    new = fs(c_call("lib.native.decode", outcomes=(("raise", "ValueError"),)))
    d = diff_facts(old, new)
    (e,) = d.entries
    assert e.category is FactCategory.UNCOMPARABLE and not e.fails and e.inconclusive
    assert "backend-unobservable" in e.notes and e.explained.startswith("backend")
    assert d.verdict is VerificationVerdict.INCONCLUSIVE


def test_a_raising_compiled_call_under_one_backend_family_stays_strict():
    old = fs(backends=("monitoring",))
    new = fs(c_call("lib.native.decode", outcomes=(("raise", "ValueError"),)))
    assert [e.category for e in failing(diff_facts(old, new))] == [FactCategory.APPEARED]


def test_qualname_pairing_needs_definition_lines_of_one_statement():
    # A closure named like the old one, forty lines further down: another statement.
    src = "\n" * 9 + "def h():\n    pass\n" + "\n" * 38 + "@lib.A.m\ndef g():\n    pass\n"
    old = fs(fact("lib.decorator", {"f": {"__callable__": "app.h"}},
                  (("return", {"__callable__": "app.h"}),), line=10, text="def h():"))
    new = fs(fact("lib.A.m.<locals>.decorator", {"f": {"__callable__": "app.h"}},
                  (("return", {"__callable__": "app.h"}),), line=50, text="@lib.A.m"))
    d = diff_facts(old, new, context=ctx_for(src))
    assert not any("attribution-moved" in e.notes for e in d.entries)
    assert [e.category for e in failing(d)] == [FactCategory.DISAPPEARED]
    # Without source snapshots: forty lines apart is not one decorated statement either.
    d = diff_facts(old, new)
    assert not any("attribution-moved" in e.notes for e in d.entries)
    assert d.verdict is not VerificationVerdict.PASS
    # Not a definition line at all: no pairing either.
    new = fs(fact("lib.A.m.<locals>.decorator", {"f": {"__callable__": "app.h"}},
                  (("return", {"__callable__": "app.h"}),), line=9, text="x = lib.A.m(h)"))
    assert not any("attribution-moved" in e.notes for e in diff_facts(old, new).entries)


def test_sampling_drops_are_not_lost_events():
    from pymolt.verify.facts import annotate_capture_meta

    old = fs(fact("lib.a", {"x": 1}), fact("lib.b", {"x": 1}, line=11))
    new = fs(fact("lib.a", {"x": 1}))
    annotate_capture_meta(new, {"processes": [{"counters": {
        "dropped_events": 7, "sampling_dropped": 7, "backpressure_dropped": 0,
        "shutdown_dropped": 0, "write_failures": 0}}]})
    assert new.dropped_events == 0
    assert [e.category for e in failing(diff_facts(old, new))] == [FactCategory.DISAPPEARED]


def test_a_fact_complete_tallies_prove_gone_is_never_softened_by_drops():
    from pymolt.verify.facts import FactEntry, _soften_dropped

    e = FactEntry(category=FactCategory.DISAPPEARED, comparison=Comparison.MISSING,
                  symbol="lib.b", notes=["site-fully-sampled"])
    _soften_dropped([e], fs(), fs(dropped_events=5))
    assert e.category is FactCategory.DISAPPEARED


def test_pair_entry_outside_a_diff_leaves_no_shared_noise_state():
    from pymolt.verify import facts as F

    assert F._NOISE.get() is None
    url_old, url_new = url_pair(1111, 2222)
    diff_facts(url_old, url_new)
    assert F._NOISE.get() is None
    o = F._V(fact=url_old.facts[0], site_value="x", input_key="a")
    n = F._V(fact=url_new.facts[0], site_value="x", input_key="b")
    F._pair_entry(o, n, F.DiffProfile.EXACT, {}, rewritten=None, input_changed=True)
    assert F._NOISE.get() is None


def test_substitution_on_long_leaves_stays_fast():
    import time

    def doc(i):
        return '{"a": [' + ", ".join(f'{{"k": "{j if j != 7 else i}", "v": [1, 2, 3]}}'
                                     for j in range(120)) + "]}"
    old = fs(*[fact("m.render", {"doc": doc(i)}, (("return", "<p>" + doc(i) + "</p>"),),
                    first=i) for i in range(300)])
    new = fs(*[fact("m.render", {"doc": doc(i + 10000)},
                    (("return", "<p>" + doc(i + 10000) + "</p>"),), first=i)
               for i in range(300)])
    t = time.perf_counter()
    d = diff_facts(old, new)
    assert time.perf_counter() - t < 5.0
    assert not failing(d)


def test_a_member_whose_producer_may_be_lost_is_not_comparable():
    # The new side dropped events: one send's msgpack producer is gone, so its bytes are not
    # a known volatile value; they have the shape of the site's volatile values.
    old = fs(*packs(["a1", "a2", "a3"], result=packed_bytes),
             *sends(["a1", "a2", "a3"], 50, packed_bytes))
    new = fs(*packs(["b1", "b2"], result=packed_bytes),
             *sends(["b1", "b2", "bX", "bY"], 50, packed_bytes), dropped_events=100)
    d = diff_facts(old, new)
    lost = [e for e in d.entries if "possibly-dropped-producer:new" in e.notes]
    assert len(lost) == 1 and lost[0].category is FactCategory.UNCOMPARABLE
    assert lost[0].new_count == 2 and not failing(d)
    assert d.verdict is VerificationVerdict.INCONCLUSIVE
    # ... a send of another shape is not a lost producer: strict.
    new = fs(*packs(["b1", "b2"], result=packed_bytes),
             *sends(["b1", "b2"], 50, packed_bytes),
             *sends(["static", "other"], 60, lambda i: {"__bytes__": i.encode().hex()}),
             dropped_events=100)
    d = diff_facts(old, new)
    (u,) = [e for e in d.entries if (e.explained or "").startswith("unpaired-site")]
    assert u.new_count == 2 and u.category is FactCategory.CHANGED
    assert not any("possibly-dropped-producer" in " ".join(e.notes) for e in d.entries)
    # ... and drops never excuse the side that lost nothing.
    old = fs(*packs(["a1", "a2", "a3"], result=packed_bytes),
             *sends(["a1", "a2", "aX"], 50, packed_bytes))
    new = fs(*packs(["b1", "b2"], result=packed_bytes), *sends(["b1", "b2"], 50, packed_bytes),
             dropped_events=100)
    d = diff_facts(old, new)
    assert not any("possibly-dropped-producer" in " ".join(e.notes) for e in d.entries)
    assert any((e.explained or "").startswith("unpaired-site") for e in d.entries)


def test_drops_do_not_evidence_a_group_without_volatile_inputs():
    d = diff_facts(fs(*tax([100, 200], 1.2)), fs(*tax([300, 400, 500], 1.3), dropped_events=9))
    (e,) = d.entries
    assert e.explained.startswith("unpaired-site") and e.review.startswith("unpaired site")
    assert "possibly-dropped-producer" not in " ".join(e.notes)


# ── fix round 2: masked-key pairing inside volatile sites, seeds, call windows, gate ──────

def billing(tag, specs):
    """uuid4 -> lib.digest -> h -> billing.charge(session=h, amount): the session is derived
    from a run value, but no mask reaches the digest."""
    facts, k = [], 0
    for i, amount, total in specs:
        u = f"{i:08x}-1111-2222-3333-{tag}{i:011x}"
        h = hashlib.sha1(u.encode()).hexdigest()
        facts.append(fact("uuid.uuid4", {}, (("return", {"__uuid__": u}),), line=1,
                          text="u = uuid.uuid4()", first=k))
        facts.append(fact("lib.digest", {"data": u}, (("return", h),), line=2,
                          text="h = lib.digest(u)", first=k + 1))
        facts.append(fact("billing.charge", {"session": h, "amount": amount},
                          (("return", total),), line=3, text="billing.charge(h, a)",
                          first=k + 2))
        k += 3
    return facts


def test_a_total_regression_behind_a_derived_session_is_not_a_pass():
    old = fs(*billing("a", [(1, 100, "120"), (2, 100, "120"), (3, 100, "120")]))
    new = fs(*billing("b", [(1, 100, "120"), (2, 100, "150")]))
    d = diff_facts(old, new)
    assert d.verdict is not VerificationVerdict.PASS
    (e,) = [e for e in d.entries if e.symbol == "billing.charge"]
    assert e.fails or e.review


def test_a_new_amount_behind_a_derived_session_is_not_explained():
    old = fs(*billing("a", [(1, 100, "120"), (2, 100, "120"), (3, 100, "120")]))
    new = fs(*billing("b", [(1, 100, "120"), (2, 999, "1198")]))
    d = diff_facts(old, new)
    assert d.verdict is VerificationVerdict.INCONCLUSIVE  # never a PASS
    (e,) = [e for e in d.entries if e.symbol == "billing.charge"]
    assert e.explained.startswith("unpaired-site") and e.review


def test_a_uuid_keyed_call_with_a_new_amount_fails():
    def side(tag, amounts):
        out = []
        for k, amount in enumerate(amounts):
            u = f"{k:08x}-1111-2222-3333-{tag}{k:011x}"
            out += [fact("uuid.uuid4", {}, (("return", {"__uuid__": u}),), line=1,
                         text="u = uuid.uuid4()", first=2 * k),
                    fact("billing.charge", {"id": u, "amount": amount}, (("return", "ok"),),
                         line=2, text="billing.charge(u, a)", first=2 * k + 1)]
        return out
    d = diff_facts(fs(*side("a", [100, 100, 100])), fs(*side("b", [100, 999])))
    assert [e.symbol for e in failing(d)] == ["billing.charge"]


def test_a_static_value_on_the_lossy_side_is_no_lost_producer():
    old = fs(*billing("a", [(1, 100, "120"), (2, 100, "120"), (3, 100, "120")]))
    new = fs(*billing("b", [(1, 100, "120")]),
             fact("billing.charge", {"session": "static-session-id", "amount": 999},
                  (("return", "1198"),), line=3, text="billing.charge(h, a)", first=90),
             dropped_events=7)
    d = diff_facts(old, new)
    assert d.verdict is not VerificationVerdict.PASS
    assert not any("possibly-dropped-producer" in " ".join(e.notes) for e in d.entries)


def test_a_constant_in_a_masked_fact_output_never_becomes_volatile():
    # api.headers(id=uuid4) returns a dict holding "application/json": a constant, not a run
    # value. A later call taking that constant (and other amounts) is not explained by it.
    def side(tag, ns):
        u = f"00000001-1111-2222-3333-{tag}00000000001"
        out = [fact("uuid.uuid4", {}, (("return", {"__uuid__": u}),), line=1, text="u()"),
               fact("api.headers", {"id": u}, (("return", {"__dict__": [
                   ["ct", "application/json"], ["id", u]]}),), line=2, text="h = api.headers(u)")]
        out += [fact("lib.post", {"ct": f"application/json; worker={tag}", "n": n}, line=3,
                     text="lib.post(h, n)", first=10 + k) for k, n in enumerate(ns)]
        return out
    # n=1 occurs on both sides; the content type differs only in its worker suffix. Were the
    # constant a volatile value, every call would read as explained.
    d = diff_facts(fs(*side("a", [1, 2, 3])), fs(*side("b", [1, 9])))
    (e,) = [e for e in d.entries if e.symbol == "lib.post"]
    assert e.explained.startswith("unpaired-site") and e.review.startswith("unpaired site")


def test_a_clock_value_away_from_its_own_outcome_time_is_not_masked():
    # auth.expiry("u") is called across a 250 s suite (the fact's window), and returns its
    # call time + TTL; the TTL shrank from 120 s to 40 s. Within the fact's window, but far
    # from the call that produced this outcome: not a clock reading, so not masked.
    def side(ttl, t0):
        f = fact("auth.expiry", {"user": "u"}, line=3, text="e = auth.expiry(u)")
        f.outcomes = [Outcome(kind="return", value=int(t0) + ttl, count=1, ts_min=t0,
                              ts_max=t0)]
        f.ts_min, f.ts_max = t0, t0 + 250.0
        return env_fs(f)
    d = diff_facts(side(120, T_OLD), side(40, T_NEW))
    assert [e.symbol for e in failing(d)] == ["auth.expiry"]


def test_an_input_clock_value_of_a_fact_spanning_the_suite_is_not_masked():
    def side(t0, result):
        f = fact("lib.bucket", {"t": int(t0)}, (("return", result),), line=4,
                 text="b = lib.bucket(t)")
        f.ts_min, f.ts_max = t0, t0 + 250.0
        for o in f.outcomes:
            o.ts_min, o.ts_max = t0, t0 + 250.0
        return env_fs(f)
    d = diff_facts(side(T_OLD, "x"), side(T_NEW, "x"))
    (e,) = d.entries
    assert "matched-modulo-masked-input" not in e.notes
    assert "clock" not in (e.explained or "")


def test_masked_bytes_compare_after_masking():
    # msgpack bytes that carry the uuid (hex-encoded) compare equal once masked.
    d = diff_facts(fs(*packs(["a1", "a2", "a3"])), fs(*packs(["b1", "b2"])))
    e = group_of(d)
    assert e.category is FactCategory.EQUAL and e.level is ComparisonLevel.MASKED
    assert "⟨src:hex:" in str(e.old)


def json_change(total):
    return fact("requests.Response.json", {"self": {"__opaque__": "requests.Response"}},
                (("return", {"__dict__": [["total", total]]}),), line=9, text="d = r.json()")


def test_the_gate_opens_only_on_a_site_fully_paired_on_masked_values_on_both_sides():
    # A uuid-keyed site whose calls all pair once the ids are masked: the gate is on, and the
    # opaque-receiver change is a review item.
    d = diff_facts(fs(*packs(["a1", "a2", "a3"]), json_change(100)),
                   fs(*packs(["b1", "b2"]), json_change(90)))
    assert d.verdict is VerificationVerdict.INCONCLUSIVE
    (e,) = [e for e in d.entries if e.symbol == "requests.Response.json"]
    assert "capture a baseline replicate to settle" in e.review
    # Evidence by propagation only (a digest of the id, no mask on it): no gate, FAIL.
    d = diff_facts(fs(*billing("a", [(1, 100, "120"), (2, 100, "120"), (3, 100, "120")]),
                      json_change(100)),
                   fs(*billing("b", [(1, 100, "120"), (2, 100, "120")]), json_change(90)))
    assert "requests.Response.json" in [e.symbol for e in failing(d)]
    assert not any(w.startswith("run-variation") for w in d.warnings)
    # Evidence only through the drop allowance: no gate either.
    old = fs(*sends(["a1", "a2", "a3"], 50, packed_bytes), json_change(100))
    new = fs(*sends(["b1", "b2", "bX", "bY"], 50, packed_bytes), json_change(90),
             dropped_events=10)
    d = diff_facts(old, new)
    assert "requests.Response.json" in [e.symbol for e in failing(d)]


def test_a_broken_call_duration_does_not_widen_the_clock_window():
    # An event whose start was lost carries a duration as long as the epoch: its outcome's
    # window must not swallow every small number (cpu_percent 6.9 is no clock reading).
    f_old = fact("psutil.Process.cpu_percent", {"self": {"__opaque__": "psutil.Process"}},
                 line=7, text="p = proc.cpu_percent()")
    f_old.outcomes = [Outcome(kind="return", value=11.6, count=1, ts_min=2.0, ts_max=T_OLD)]
    f_old.ts_min, f_old.ts_max = 2.0, T_OLD
    f_new = fact("psutil.Process.cpu_percent", {"self": {"__opaque__": "psutil.Process"}},
                 line=7, text="p = proc.cpu_percent()")
    f_new.outcomes = [Outcome(kind="return", value=6.9, count=1, ts_min=2.0, ts_max=T_NEW)]
    f_new.ts_min, f_new.ts_max = 2.0, T_NEW
    d = diff_facts(env_fs(f_old), env_fs(f_new))
    assert "clock" not in (d.entries[0].explained or "")
    assert d.entries[0].old[0].value == 11.6


def test_a_call_with_an_implausible_duration_keeps_its_end_time():
    from pymolt.verify.facts import _call_time

    assert _call_time({"ts": T_OLD, "dur": T_OLD - 2.0}) == (T_OLD, T_OLD)
    assert _call_time({"ts": T_OLD, "dur": 0.5}) == (T_OLD - 0.5, T_OLD)



# ── round 3: a clock value is its offset from its own call time ───────────────────────────

def calls_returning(symbol, results_at, *, bound=None):
    """One fact whose calls (each in its own sub-second window) returned ``value(t)``."""
    f = fact(symbol, bound or {"user": "u"}, line=3, text="e = auth.expiry(u)")
    f.outcomes = [Outcome(kind="return", value=v, count=1, ts_min=t0, ts_max=t1)
                  for v, t0, t1 in results_at]
    f.ts_min = min(t0 for _, t0, _ in results_at)
    f.ts_max = max(t1 for _, _, t1 in results_at)
    return env_fs(f)


def six_calls(t0, value):
    return [(value(t0 + 40.0 * k + 0.01), t0 + 40.0 * k, t0 + 40.0 * k + 0.02) for k in range(6)]


def test_a_ttl_spread_over_six_calls_compares_by_value():
    # Each call returns its own time + TTL; the TTL went 120 -> 40. Masked as offsets, the six
    # values collapse to ONE outcome per side, compared exactly (no type-level fallback).
    d = diff_facts(calls_returning("auth.expiry", six_calls(T_OLD, lambda t: int(t) + 120)),
                   calls_returning("auth.expiry", six_calls(T_NEW, lambda t: int(t) + 40)))
    (e,) = d.entries
    assert e.fails and e.level is not ComparisonLevel.TYPE
    assert e.old[0].value.endswith("@+120s⟩") and e.new[0].value.endswith("@+40s⟩")


def test_six_readings_of_the_clock_stay_equal():
    d = diff_facts(calls_returning("time_api.now", six_calls(T_OLD, lambda t: t)),
                   calls_returning("time_api.now", six_calls(T_NEW, lambda t: t)))
    (e,) = d.entries
    assert not e.fails and e.category is FactCategory.EQUAL
    assert len(e.old) == 1 and e.old[0].value.endswith("@+0s⟩")


def test_a_constant_past_date_is_no_clock_value():
    past = "2020-01-01T00:00:00"
    d = diff_facts(calls_returning("cfg.epoch", [(past, T_OLD, T_OLD)]),
                   calls_returning("cfg.epoch", [(past, T_NEW, T_NEW)]))
    (e,) = d.entries
    assert e.category is FactCategory.EQUAL and e.level is ComparisonLevel.EXACT
    assert e.old[0].value == past
    d = diff_facts(calls_returning("cfg.epoch", [(past, T_OLD, T_OLD)]),
                   calls_returning("cfg.epoch", [("2020-01-02T00:00:00", T_NEW, T_NEW)]))
    assert failing(d)


def test_sub_second_jitter_does_not_flip_the_offset():
    # The same now()+120 in calls of different lengths and read at different moments.
    old = calls_returning("auth.expiry", [(T_OLD + 0.1 + 120, T_OLD, T_OLD + 0.3)])
    new = calls_returning("auth.expiry", [(T_NEW + 0.7 + 120, T_NEW, T_NEW + 0.8)])
    assert not failing(diff_facts(old, new))


def test_an_outcome_of_calls_spread_over_the_suite_is_no_clock_value():
    # One outcome produced by calls 40 s apart: its window is too wide to anchor an offset.
    old = calls_returning("lib.stamp", [(int(T_OLD) + 120, T_OLD, T_OLD + 40.0)])
    new = calls_returning("lib.stamp", [(int(T_NEW) + 120, T_NEW, T_NEW + 40.0)])
    (e,) = diff_facts(old, new).entries
    assert "clock" not in (e.explained or "") and e.fails



# ── round 3: a definition whose body was edited is an edited site ─────────────────────────

PYD_OLD = ("from pydantic import BaseModel\n\n\nclass User(BaseModel):\n    name: str\n\n"
           "    class Config:\n        anystr_strip_whitespace = True\n")
PYD_NEW = ("from pydantic import BaseModel, ConfigDict\n\n\nclass User(BaseModel):\n"
           "    name: str\n\n    model_config = ConfigDict(str_strip_whitespace=True)\n")


def metaclass_new(namespace, outcome=(("return", {"__type__": "app.User"}),)):
    return fact("pydantic.main.ModelMetaclass.__new__",
                {"mcs": {"__type__": "pydantic.main.ModelMetaclass"}, "name": "User",
                 "namespace": {"__dict__": namespace}}, outcome, func="<module>", line=4,
                text="class User(BaseModel):")


def test_a_class_whose_body_was_hand_edited_is_a_hand_edited_site():
    # The class line is unedited; its body is not: the metaclass call's namespace differs
    # because of the hand edit. A value difference there is a review item (hand tier).
    ctx = DiffContext(old_sources={"app.py": PYD_OLD}, new_sources={"app.py": PYD_NEW})
    old = fs(metaclass_new([["Config", {"__type__": "app.User.Config"}]]))
    new = fs(metaclass_new([["model_config", {"__dict__": [["str_strip_whitespace", True]]}]],
                           outcome=(("return", {"__type__": "app.User2"}),)))
    d = diff_facts(old, new, context=ctx)
    (e,) = d.entries
    assert e.provenance == "hand" and e.review and not e.fails
    # ... and a new raise there still fails
    new = fs(metaclass_new([["model_config", {"__dict__": [["str_strip_whitespace", True]]}]],
                           outcome=(("raise", "pydantic.errors.PydanticUserError"),)))
    assert [x.symbol for x in failing(diff_facts(old, new, context=ctx))] == [
        "pydantic.main.ModelMetaclass.__new__"]


def test_without_sources_a_definition_span_is_not_known_and_says_so():
    old = fs(metaclass_new([["Config", {"__type__": "app.User.Config"}]]))
    new = fs(metaclass_new([["model_config", {"__dict__": [["str_strip_whitespace", True]]}]]))
    d = diff_facts(old, new, context=DiffContext())
    assert any(w.startswith("definition-spans-unknown") for w in d.warnings)

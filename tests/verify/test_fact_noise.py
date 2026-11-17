"""Verify noise rules: what makes two runs differ besides the migration, and what must still fail.

Every rule has two kinds of tests: the noise case it explains and the regression it must not hide.
Facts are built directly (no trace files): ``fact`` is one dependency call from one site.
"""
from pymolt.verify.facts import (
    Comparison,
    ComparisonLevel,
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


def out(*pairs):
    """``out(("return", 1), ("raise", "ValueError"))`` -> outcomes (count 1 each)."""
    return [Outcome(kind=k, value=v, count=1) for k, v in pairs]


def fact(symbol, bound=None, outcomes=(("return", None),), *, file="app.py", func="main",
         line=10, text=None, count=1, first=0, inp=None):
    leaf = symbol.rsplit(".", 1)[-1]
    return Fact(
        symbol=symbol, file=file, func=func, line=line, lines=[line],
        text=text if text is not None else f"x = obj.{leaf}(a)",
        input=inp if inp is not None else {"bound": dict(bound or {})},
        outcomes=out(*outcomes), count=count, first_seen=first,
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


def packs(ids, *, result=lambda i: {"__bytes__": f"93a3{i}"}, first=0, raise_at=None):
    """One fact per call: each carries its own client id, so no input repeats across runs."""
    facts = []
    for k, i in enumerate(ids):
        outcome = (("raise", raise_at),) if raise_at is not None and k == 0 else \
            (("return", result(i)),)
        facts.append(fact(PACK, {"o": {"__tuple__": ["ack", f"node_{i}"]}}, outcome,
                          file="locust/rpc/protocol.py", func="serialize", line=14,
                          text=PACK_LINE, first=first + k))
    return facts


def test_fresh_inputs_at_one_site_compare_as_one_group():
    old = fs(*packs(["a1", "a2", "a3"]))
    new = fs(*packs(["b1", "b2"]))
    d = diff_facts(old, new)
    assert not failing(d) and d.verdict is VerificationVerdict.PASS
    (e,) = d.entries
    assert e.category is FactCategory.EQUAL and e.level is ComparisonLevel.TYPE
    assert (e.old_count, e.new_count) == (3, 2)
    assert e.explained.startswith("volatile-site") and "volatile-site:3/2" in e.notes
    assert any(w.startswith("volatile-sites:1:") for w in d.warnings)


def test_a_new_raise_type_in_a_volatile_group_fails():
    old = fs(*packs(["a1", "a2", "a3"]))
    new = fs(*packs(["b1", "b2"], raise_at="TypeError"))
    d = diff_facts(old, new)
    (e,) = failing(d)
    assert e.category is FactCategory.CHANGED and e.explained.startswith("volatile-site")


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
    assert d.counts()["equal"] == 1  # the group


def test_one_sided_site_is_not_a_group():
    d = diff_facts(fs(*packs(["a1", "a2"])), fs())
    assert d.counts()["disappeared"] == 2 and d.verdict is VerificationVerdict.FAIL


def test_a_changed_input_signature_is_not_absorbed_into_a_group():
    # Same symbol and site, but the bound parameter was renamed: structure, not noise.
    old = fs(*[fact("lib.f", {"a": i}, first=i) for i in range(2)])
    new = fs(fact("lib.f", {"b": 9}))
    d = diff_facts(old, new)
    assert d.counts()["disappeared"] == 2 and d.counts()["appeared"] == 1

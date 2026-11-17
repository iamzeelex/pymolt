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

"""Edited statements: a new raise or a call that fails to bind fails even unpaired; a call
count that changed at an edited statement with equal values is a review; a dependency's
returned callable compares as a handle on edited lines too."""

from _semantics import ev, one, run

from pymolt.verify.facts import Comparison, FactCategory
from pymolt.verify.models import VerificationVerdict

PASS, FAIL = VerificationVerdict.PASS, VerificationVerdict.FAIL
INCONCLUSIVE = VerificationVerdict.INCONCLUSIVE

OLD = "from dep import enc\ndef names(e):\n    return list(e.get_feature_names(COLS))\n"
NEW = "from dep import enc\ndef names(e):\n    return list(e.get_feature_names_out(COLS))\n"


def edit(claim="preserve", **kw):
    return {"file": "app.py", "located": {"old": [3], "new": [3]}, "claim": claim,
            "basis": "manual", "step_id": "sig-1", "old_symbol": "dep.Enc.get_feature_names",
            "new_symbol": "dep.Enc.get_feature_names_other", **kw}


def old_call():
    return ev("dep.Enc.get_feature_names", {"self": {"__opaque__": "dep.Enc"}},
              result={"__list__": ["a", "b"]}, func="names")


def new_call(**kw):
    return ev("dep.Enc.get_feature_names_out", {"self": {"__opaque__": "dep.Enc"}},
              func="names", **kw)


def test_an_unpaired_call_that_fails_to_bind_fails_whatever_the_claim(tmp_path):
    for claim in ("preserve", "intended", None):
        edits = [edit(claim)] if claim else []
        d = run(tmp_path, [old_call()], [new_call(raised="TypeError", binding=True)], OLD, NEW,
                edits=edits)
        assert d.verdict is FAIL, claim


def test_an_unpaired_new_raise_fails_when_the_old_statement_raised_nothing(tmp_path):
    d = run(tmp_path, [old_call()], [new_call(raised="ValueError")], OLD, NEW, edits=[edit()])
    e = one(d, "dep.Enc.get_feature_names_out")
    assert e.fails and not e.unverified and d.verdict is FAIL


def test_an_attested_expectation_of_that_raise_keeps_it_unverified(tmp_path):
    attested = edit("intended", expect="raise:ValueError", attested=True,
                    attestation={"id": "a1", "user": "ana", "at": "2026-11-25T10:00:00+00:00"})
    d = run(tmp_path, [old_call()], [new_call(raised="ValueError")], OLD, NEW, edits=[attested])
    assert not one(d, "dep.Enc.get_feature_names_out").fails


def test_unpaired_returns_stay_unverified_both_ways(tmp_path):
    d = run(tmp_path, [old_call()], [new_call(result={"__list__": ["a"]})], OLD, NEW,
            edits=[edit()])
    assert all(e.unverified and not e.fails for e in d.entries
               if e.category is FactCategory.REWRITTEN)
    raised_before = [ev("dep.Enc.get_feature_names", {"self": {"__opaque__": "dep.Enc"}},
                        raised="KeyError", func="names")]
    d = run(tmp_path, raised_before, [new_call(raised="ValueError")], OLD, NEW, edits=[edit()])
    assert not one(d, "dep.Enc.get_feature_names_out").fails      # raised before too: unverified


def test_a_paired_call_that_fails_to_bind_fails_even_when_a_raise_is_attested(tmp_path):
    new_src = OLD.replace("(COLS)", "(COLS, bogus=1)")
    attested = {**edit("intended", expect="raise:TypeError", attested=True,
                       attestation={"id": "a1", "user": "ana", "at": "2026-11-25T10:00:00+00:00"}),
                "new_symbol": "dep.Enc.get_feature_names"}
    bad = ev("dep.Enc.get_feature_names", {"self": {"__opaque__": "dep.Enc"}},
             raised="TypeError", binding=True, func="names")
    d = run(tmp_path, [old_call()], [bad], OLD, new_src, edits=[attested])
    assert d.verdict is FAIL


# ── a call count that changed at an edited statement ──────────────────────────────────────

MK_OLD = "import dep\ndef f(text):\n    return dep.Markup(dep.escape(text))\n"
MK_NEW = "import dep\ndef f(text):\n    return dep.Markup(dep.escape_new(text))\n"


def mk(n, value="X&lt;Y"):
    return [ev("dep.Markup.__new__", {"base": "X&lt;Y"}, result=value, func="f")
            for _ in range(n)]


def mk_edit(claim):
    return {"file": "app.py", "located": {"old": [3], "new": [3]}, "claim": claim,
            "basis": "manual", "step_id": "sig-5"}


def test_a_count_change_at_an_edited_statement_with_equal_values_is_a_review(tmp_path):
    for claim in ("preserve", "intended", None):
        d = run(tmp_path, mk(2), mk(1), MK_OLD, MK_NEW, edits=[mk_edit(claim)] if claim else [])
        e = one(d, "dep.Markup.__new__")
        assert not e.fails and e.review.startswith("call count changed at an edited statement")
        assert d.verdict is INCONCLUSIVE, claim


def test_mutation_a_count_change_with_another_value_at_an_edited_statement_fails(tmp_path):
    d = run(tmp_path, mk(2), mk(1, "other"), MK_OLD, MK_NEW, edits=[mk_edit("preserve")])
    assert d.verdict is FAIL


def test_an_unedited_statement_keeps_the_strict_count_rule(tmp_path):
    d = run(tmp_path, mk(2), mk(1), MK_OLD)
    assert one(d, "dep.Markup.__new__").fails


# ── a callable a dependency returned is a handle, on edited lines too ─────────────────────

VAL_OLD = ("from dep import validator\nclass M:\n    @validator('name')\n"
           "    def v(cls, x):\n        return x\n")
VAL_NEW = VAL_OLD.replace("validator", "field_validator")
VAL_EDIT = {"file": "app.py", "located": {"old": [1, 3], "new": [1, 3]}, "claim": "preserve",
            "basis": "manual", "step_id": "sig-2", "old_symbol": "dep.validator",
            "new_symbol": "dep.field_validator"}


def deco(symbol, result, **kw):
    return ev(symbol, {"field": "name"}, result=result, line=3, func="M", **kw)


def test_a_decorator_factory_returning_another_callable_on_an_edited_line_is_drift(tmp_path):
    edit = VAL_EDIT
    old = [deco("dep.validator", {"__opaque__": "_cython_3_3_0.cython_function_or_method"})]
    new = [deco("dep.field_validator", {"__callable__": "dep.field_validator.<locals>.dec"})]
    d = run(tmp_path, old, new, VAL_OLD, VAL_NEW, edits=[edit])
    e = one(d, "dep.validator")
    assert not e.fails and "type-drift:callable" in e.absorbed
    strict = run(tmp_path, old, new, VAL_OLD, VAL_NEW, edits=[edit], strict=True)
    assert one(strict, "dep.validator").fails


def test_mutation_a_callable_against_a_value_still_fails(tmp_path):
    old = [deco("dep.validator", {"__callable__": "dep.validator.<locals>.dec"})]
    new = [deco("dep.validator", None)]
    assert run(tmp_path, old, new, VAL_OLD).verdict is FAIL


def test_mutation_a_dependency_callable_whose_name_the_project_reads_fails(tmp_path):
    edit = VAL_EDIT
    reads = "print(M.v.__name__)\n"
    old = [deco("dep.validator", {"__opaque__": "_cython_3_3_0.cython_function_or_method"})]
    new = [deco("dep.field_validator", {"__callable__": "dep.field_validator.<locals>.dec"})]
    d = run(tmp_path, old, new, VAL_OLD + reads, VAL_NEW + reads, edits=[edit])
    e = one(d, "dep.validator")
    assert e.fails and e.comparison is Comparison.CHANGED


# ── the import system's dunder probe is not a new raise ─────────────────────────────────

IMP_OLD = "from dep.urls import url_parse\n"
IMP_NEW = "from urllib.parse import urlsplit\n"
IMP_EDIT = {"file": "app.py", "located": {"old": [1, 1], "new": [1, 1]}, "claim": "hand",
            "basis": "manual"}


def probe(name):
    return ev("urllib.parse.__getattr__", {"name": name}, raised="AttributeError", line=1,
              func="<module>")


def test_an_import_probe_of_a_dunder_on_an_edited_line_stays_unverified(tmp_path):
    d = run(tmp_path, [], [probe("__path__")], IMP_OLD, IMP_NEW, edits=[IMP_EDIT])
    e = one(d, "urllib.parse.__path__")
    assert not e.fails and e.unverified


def test_mutation_a_getattr_of_a_plain_name_raising_on_an_edited_line_fails(tmp_path):
    d = run(tmp_path, [], [probe("urljoinx")], IMP_OLD, IMP_NEW, edits=[IMP_EDIT])
    e = one(d, "urllib.parse.urljoinx")
    assert e.fails and d.verdict is FAIL

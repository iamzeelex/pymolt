"""Edited statements: a new raise or a call that fails to bind fails even unpaired; a call
count that changed at an edited statement with equal values is a review; a dependency's
returned callable compares as a handle on edited lines too."""

from _semantics import ev, one, run

from pymolt.verify.facts import FactCategory
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

"""Verify's comparison semantics for library objects (Stage 1, host only).

Each relaxation has a mutation test that must FAIL next to the test that it absorbs the
equivalent difference."""

from _semantics import ev, one, run

from pymolt.verify.facts import (
    AWAITED_UNRECORDED,
    Comparison,
    FactCategory,
)
from pymolt.verify.inconclusive import entry_cause, uncomparable_mechanism
from pymolt.verify.models import VerificationVerdict

COROUTINE = {"__opaque__": "coroutine"}


# ── 1. awaited results ────────────────────────────────────────────────────────────────────

AWAIT_SRC = "import dep\nasync def main():\n    r = await dep.fetch(1)\n"


def test_two_coroutine_markers_are_uncomparable_not_equal(tmp_path):
    old = [ev("dep.fetch", {"n": 1}, result=COROUTINE)]
    new = [ev("dep.fetch", {"n": 1}, result=COROUTINE)]
    d = run(tmp_path, old, new, AWAIT_SRC)
    e = one(d, "dep.fetch")
    assert e.category is FactCategory.UNCOMPARABLE and e.comparison is Comparison.UNCOMPARABLE
    assert AWAITED_UNRECORDED in e.notes
    assert d.verdict is VerificationVerdict.INCONCLUSIVE
    kind, why = entry_cause(e.model_dump(mode="json"), noise_measured=False)
    assert (kind.value, why) == ("needs_evidence", "uncomparable")
    assert uncomparable_mechanism(e.model_dump(mode="json")) == "awaited-unrecorded"


def test_coroutine_against_an_awaitable_handle_is_uncomparable(tmp_path):
    old = [ev("dep.fetch", {"n": 1}, result={"__opaque__": "dep.client._RequestContextManager"})]
    new = [ev("dep.fetch", {"n": 1}, result=COROUTINE)]
    e = one(run(tmp_path, old, new, AWAIT_SRC), "dep.fetch")
    assert e.comparison is Comparison.UNCOMPARABLE and AWAITED_UNRECORDED in e.notes


def test_mutation_plain_value_becoming_a_coroutine_fails(tmp_path):
    old = [ev("dep.fetch", {"n": 1}, result=None)]
    new = [ev("dep.fetch", {"n": 1}, result=COROUTINE)]
    d = run(tmp_path, old, new, AWAIT_SRC)
    assert one(d, "dep.fetch").fails and d.verdict is VerificationVerdict.FAIL


def test_mutation_coroutine_call_that_now_raises_fails(tmp_path):
    old = [ev("dep.fetch", {"n": 1}, result=COROUTINE)]
    new = [ev("dep.fetch", {"n": 1}, raised="TypeError")]
    d = run(tmp_path, old, new, AWAIT_SRC)
    assert one(d, "dep.fetch").fails and d.verdict is VerificationVerdict.FAIL

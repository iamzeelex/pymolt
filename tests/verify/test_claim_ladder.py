"""The claim ladder (design 5.1): verify judges an edited line by the claim of the entry that
decides it, not by who made it. For every outcome, ``preserve`` >= ``intended`` unattested >=
a hand edit in strictness; only an ``intended`` claim with a person-attested expectation can
judge more leniently than a hand edit, and a new raise stays FAIL unless one covers it."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from _traces import diff_traces

from pymolt.verify.facts import (
    OPAQUE_REPRESENTATION,
    Comparison,
    ComparisonLevel,
    DiffContext,
    FactCategory,
    Outcome,
    _content_unseen,
    expectation_met,
)
from pymolt.verify.models import VerificationVerdict

ROOT = "/proj"
APP = "/proj/app.py"
OLD_SRC = "def main():\n    a = lib.f(1)\n    return lib.g(a)\n"
NEW_SRC = "def main():\n    a = lib.f(1)\n    return lib.h(a)\n"


def _event(q, line, *, result=None, raised=None):
    e = {"t": "raise" if raised else "return", "q": q, "in": {"bound": {}}, "v": 2, "ts": 0.0,
         "sym": q, "where": {"file": APP, "line": line, "func": "main"}}
    if raised:
        e["raised"] = raised
    else:
        e["result"] = result
    return e


def _trace(tmp_path, name, events, texts):
    p = tmp_path / name
    p.write_text("".join(json.dumps(e) + "\n" for e in events))
    (tmp_path / f"{name}.meta.json").write_text(json.dumps({"site_text": texts}))
    return p


OUTCOMES = {
    "equal": (5, {"result": 5}),
    "representation": ({"__list__": [1, 2]}, {"result": {"__tuple__": [1, 2]}}),
    "value": (5, {"result": 6}),
    "raise": (5, {"raised": "TypeError"}),
    # Only the types are visible (an opaque value on each side): the content is unknown.
    "opaque": ({"__opaque__": "lib.RowProxy"}, {"result": {"__opaque__": "lib.Row"}}),
    "opaque raise": ({"__opaque__": "lib.RowProxy"}, {"raised": "TypeError"}),
}


def _diff(tmp_path, outcome: str, edit: dict | None):
    old_value, new = OUTCOMES[outcome]
    context = DiffContext(old_sources={"app.py": OLD_SRC}, new_sources={"app.py": NEW_SRC},
                          edits=[edit] if edit else [])
    o = _trace(tmp_path, "old.jsonl", [_event("lib.g", 3, result=old_value)],
               {APP + ":3": "return lib.g(a)"})
    n = _trace(tmp_path, "new.jsonl", [_event("lib.h", 3, **new)],
               {APP + ":3": "return lib.h(a)"})
    return diff_traces(o, n, root=ROOT, context=context)


def _edit(claim, *, expect=None, attested=False, basis="manual"):
    edit = {"file": "app.py", "located": {"old": [3], "new": [3]}, "claim": claim,
            "basis": basis, "expect": expect, "old_symbol": "lib.g", "new_symbol": "lib.h",
            "step_id": "beh-1", "attested": attested}
    if attested:
        edit["attestation"] = {"id": "att-1", "user": "ana", "at": "2026-11-17T10:00:00+00:00"}
    return edit


#: who -> the ledger entry deciding the line (None: a hand edit).
CLAIMS = {
    "hand": None,
    "preserve": _edit("preserve"),
    "intended": _edit("intended"),
    "intended+representation": _edit("intended", expect="representation"),
    "intended+raise (unattested)": _edit("intended", expect="raise:TypeError"),
    "attested any": _edit("intended", expect="any", attested=True),
    "attested representation": _edit("intended", expect="representation", attested=True),
    "attested raise:TypeError": _edit("intended", expect="raise:TypeError", attested=True),
    "attested return:int": _edit("intended", expect="return:int", attested=True),
}

# The ladder (design 5.1): (outcome, claim) -> pass | drift | review | fail | accepted.
EXPECTED = {
    "equal": dict.fromkeys(CLAIMS, "pass"),
    "representation": {"hand": "drift", "preserve": "fail", "intended": "drift",
                       "intended+representation": "drift",
                       "intended+raise (unattested)": "drift", "attested any": "drift",
                       "attested representation": "accepted",
                       "attested raise:TypeError": "fail", "attested return:int": "fail"},
    "value": {"hand": "review", "preserve": "fail", "intended": "review",
              "intended+representation": "fail", "intended+raise (unattested)": "review",
              "attested any": "review", "attested representation": "fail",
              "attested raise:TypeError": "fail", "attested return:int": "accepted"},
    "raise": {"hand": "fail", "preserve": "fail", "intended": "fail",
              "intended+representation": "fail", "intended+raise (unattested)": "fail",
              "attested any": "fail", "attested representation": "fail",
              "attested raise:TypeError": "accepted", "attested return:int": "fail"},
    # An unattested representation expectation cannot be checked against unseen values: a
    # review, as under the hand rule (never more lenient than it); preserve still fails.
    "opaque": {"hand": "review", "preserve": "fail", "intended": "review",
               "intended+representation": "review", "intended+raise (unattested)": "review",
               "attested any": "review", "attested representation": "fail",
               "attested raise:TypeError": "fail", "attested return:int": "fail"},
    "opaque raise": {"hand": "fail", "preserve": "fail", "intended": "fail",
                     "intended+representation": "fail", "intended+raise (unattested)": "fail",
                     "attested any": "fail", "attested representation": "fail",
                     "attested raise:TypeError": "accepted", "attested return:int": "fail"},
}
SEVERITY = {"accepted": 0, "pass": 1, "drift": 1, "review": 2, "fail": 3}


def _judgement(diff) -> str:
    entry = next(e for e in diff.entries if e.symbol in ("lib.g", "lib.h"))
    if entry.accepted:
        return "accepted"
    if entry.fails:
        return "fail"
    if entry.review:
        return "review"
    if entry.category is FactCategory.DRIFT:
        return "drift"
    assert entry.comparison is Comparison.EQUAL, entry
    return "pass"


@pytest.mark.parametrize("outcome", list(OUTCOMES))
@pytest.mark.parametrize("who", list(CLAIMS))
def test_the_ladder(tmp_path, outcome, who):
    diff = _diff(tmp_path, outcome, CLAIMS[who])
    assert _judgement(diff) == EXPECTED[outcome][who]


@pytest.mark.parametrize("outcome", list(OUTCOMES))
def test_the_ladder_is_monotone(tmp_path, outcome):
    """preserve >= intended unattested >= hand, for every outcome and every unattested
    expectation; only an attested expectation ranks below the hand judgement. Judged by the
    code, not read from ``EXPECTED``."""
    row = {who: SEVERITY[_judgement(_diff(tmp_path, outcome, edit))]
           for who, edit in CLAIMS.items()}
    unattested = [w for w in CLAIMS if w.startswith("intended")]
    for who in unattested:
        assert row["preserve"] >= row[who] >= row["hand"], (outcome, who)
    for who in CLAIMS:
        if row[who] < row["hand"]:
            assert who.startswith("attested"), (outcome, who)


def test_a_new_raise_never_passes_without_an_attested_raise_expectation(tmp_path):
    for who, edit in CLAIMS.items():
        verdict = _diff(tmp_path, "raise", edit).verdict
        if who == "attested raise:TypeError":
            assert verdict is VerificationVerdict.PASS
        else:
            assert verdict is VerificationVerdict.FAIL, who


def test_an_unseen_change_under_a_representation_claim_is_a_review_naming_what_settles_it(
        tmp_path):
    diff = _diff(tmp_path, "opaque", CLAIMS["intended+representation"])
    entry = next(e for e in diff.entries if e.symbol in ("lib.g", "lib.h"))
    assert entry.provenance == "intended" and not entry.fails
    assert entry.review.startswith(OPAQUE_REPRESENTATION)
    for settles in ("--privacy values", "baseline replicate", "pymolt verify . --accept"):
        assert settles in entry.review
    assert diff.verdict is VerificationVerdict.INCONCLUSIVE
    # Visible content, more than a representation change: still a failure.
    assert _diff(tmp_path, "value", CLAIMS["intended+representation"]).verdict \
        is VerificationVerdict.FAIL


@pytest.mark.parametrize(("level", "old", "new", "unseen"), [
    (None, [Outcome(kind="return", value={"__opaque__": "a.A"})],
     [Outcome(kind="return", value={"__opaque__": "a.B"})], True),
    (None, [Outcome(kind="return", value=[1, 2])],
     [Outcome(kind="return", value={"__truncated__": True})], True),       # unknown on one side
    (ComparisonLevel.TYPE, [Outcome(kind="return", value=1)],
     [Outcome(kind="return", value="1")], True),                          # compared by type only
    (None, [Outcome(kind="return", value=1)], [Outcome(kind="return", value=2)], False),
    (None, [Outcome(kind="return", value={"__opaque__": "a.A"})],
     [Outcome(kind="raise", value="TypeError")], False),                  # a new raise
    (None, [Outcome(kind="raise", value="KeyError")],
     [Outcome(kind="raise", value="TypeError")], False),                  # another raised type
    (None, [Outcome(kind="return", value={"__opaque__": "a.A"}),
            Outcome(kind="raise", value="KeyError")],
     [Outcome(kind="return", value={"__opaque__": "a.B"}),
      Outcome(kind="raise", value="KeyError")], True),                    # the same raises
])
def test_content_unseen(level, old, new, unseen):
    assert _content_unseen(SimpleNamespace(level=level), old, new) is unseen


def test_an_accepted_entry_counts_as_equal_and_is_reported(tmp_path):
    diff = _diff(tmp_path, "value", CLAIMS["attested return:int"])
    entry = next(e for e in diff.entries if e.accepted)
    assert entry.accepted["by"] == "ana" and entry.accepted["how"] == "expectation return:int"
    assert entry.step_id == "beh-1" and entry.provenance == "intended"
    assert diff.verdict is VerificationVerdict.PASS
    assert diff.edited_sites()["accepted"] == 1
    assert diff.edited_summary().endswith("1 accepted")
    summary = diff.summary()
    assert summary["accepted_total"] == 1 and summary["accepted"][0]["accepted"]["by"] == "ana"
    assert any("1 accepted change" in r for r in summary["reasons"])


def test_a_preserve_entry_of_a_recorded_edit_says_so(tmp_path):
    diff = _diff(tmp_path, "value", CLAIMS["preserve"])
    entry = next(e for e in diff.entries if e.fails)
    assert entry.provenance == "ledger" and entry.step_id == "beh-1"
    assert "the recorded edit claimed preserve and changed behaviour" in entry.notes
    pymolt = _diff(tmp_path, "value", _edit("preserve", basis="same-object"))
    assert "pymolt's own edit changed behaviour" in next(
        e for e in pymolt.entries if e.fails).notes


def test_a_schema_1_claim_reads_as_preserve(tmp_path):
    edit = {**_edit("same-signature", basis=None), "claim": "same-signature"}
    entry = next(e for e in _diff(tmp_path, "value", edit).entries if e.fails)
    assert entry.provenance == "ledger"


def test_the_strictest_claim_wins_where_entries_overlap(tmp_path):
    context_edits = [CLAIMS["attested return:int"], CLAIMS["preserve"]]
    old_value, new = OUTCOMES["value"]
    context = DiffContext(old_sources={"app.py": OLD_SRC}, new_sources={"app.py": NEW_SRC},
                          edits=context_edits)
    o = _trace(tmp_path, "old.jsonl", [_event("lib.g", 3, result=old_value)],
               {APP + ":3": "return lib.g(a)"})
    n = _trace(tmp_path, "new.jsonl", [_event("lib.h", 3, **new)],
               {APP + ":3": "return lib.h(a)"})
    diff = diff_traces(o, n, root=ROOT, context=context)
    assert diff.verdict is VerificationVerdict.FAIL


@pytest.mark.parametrize(("expect", "old", "new", "met"), [
    ("representation", [Outcome(kind="return", value={"__list__": [1]})],
     [Outcome(kind="return", value={"__tuple__": [1]})], True),
    ("raise:TypeError", [Outcome(kind="return", value=1)],
     [Outcome(kind="raise", value="builtins.TypeError")], True),
    ("raise:TypeError", [Outcome(kind="return", value=1)],
     [Outcome(kind="raise", value="TypeError"), Outcome(kind="return", value=1)], False),
    ("return:Series", [Outcome(kind="return", value=1)],
     [Outcome(kind="return", value={"__structure__": "pandas.core.series.Series"})], True),
    ("return:int", [Outcome(kind="return", value=1)],
     [Outcome(kind="return", value="1")], False),
    ("any", [Outcome(kind="return", value=1)], [Outcome(kind="return", value=2)], False),
])
def test_expectation_met(expect, old, new, met):
    assert expectation_met(expect, old, new) is met

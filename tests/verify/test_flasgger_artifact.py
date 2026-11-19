"""Regression over the real flasgger artifact (a git submodule).

The static contact map over the real flasgger checkout, and the report's arithmetic over a
capture built from it. Marked ``integration``: it runs only where the submodule is
initialised (``git submodule update --init tests/artifacts/flasgger``); CI checks out
without submodules, so there it is skipped.

Floors and invariants, not exact pins: the submodule may carry local
modifications during development, and exact counts would turn every artifact
touch into a red suite. The floors still catch the regressions that matter —
an empty map, a broken dependency filter, report arithmetic drift.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_FLASGGER = Path(__file__).resolve().parents[2] / "tests" / "artifacts" / "flasgger"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (_FLASGGER / "flasgger" / "__init__.py").is_file(),
        reason="flasgger submodule not initialized (git submodule update --init)",
    ),
]

# A call no static contact names: the report must count it dynamic-only.
_UNMAPPED = "flask.pymolt_test_unmapped_call"


def test_static_contact_map_over_real_flasgger():
    from pymolt.verify.contact_map import build_contact_map

    cmap = build_contact_map(_FLASGGER)

    assert len(cmap.contacts) >= 150          # currently ≈227
    assert len({c.file for c in cmap.contacts}) >= 40   # currently ≈60 files
    assert len(cmap.by_dep) >= 10             # currently 19 deps
    assert "flask" in cmap.by_dep             # a Flask extension must touch flask
    assert "pymolt_trace" not in cmap.by_dep  # our own watcher bundle stays filtered


def test_static_only_report_is_all_blind():
    from pymolt.verify.report import build_contract_report

    rep = build_contract_report(_FLASGGER)

    assert rep.static_targets >= 40
    assert rep.confirmed == 0                 # no trace given -> nothing confirmed
    assert rep.blind == rep.static_targets
    assert rep.coverage == 0.0 and rep.recall is None


@pytest.fixture
def built_capture(tmp_path):
    """A capture built from the static map: every other observable call contact recorded at
    its own line, plus one call no static contact names. The report is built with
    exact-string identities, so what each contact's status must be follows from the trace."""
    from pymolt.verify.report import build_contract_report

    static = build_contract_report(_FLASGGER, resolve_identities=False)
    calls = sorted({(c.file, c.line, c.target) for c in static.contacts
                    if c.kind == "call" and c.status != "unobservable" and c.line})
    picked = calls[::2]
    assert picked, "no observable call contact in the static map"
    records = [{"q": target, "sym": target, "t": "return", "in": {"bound": {}},
                "result": None, "where": {"file": str(_FLASGGER / file), "line": line,
                                          "func": "f"}}
               for file, line, target in picked]
    file, line, _target = picked[0]
    records.append({"q": _UNMAPPED, "sym": _UNMAPPED, "t": "return", "in": {"bound": {}},
                    "result": None, "where": {"file": str(_FLASGGER / file), "line": line,
                                              "func": "f"}})
    trace = tmp_path / "baseline.jsonl"
    trace.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return trace, set(picked)


def test_fold_invariants_over_a_capture_built_from_the_static_map(built_capture):
    """Fold the built capture and hold the report's arithmetic to its own definitions."""
    from pymolt.verify.report import build_contract_report

    trace, picked = built_capture
    rep = build_contract_report(_FLASGGER, trace=trace, resolve_identities=False)

    # every call the trace recorded at a contact's own line records that contact
    status = {(c.file, c.line, c.target): (c.status, c.matched_by)
              for c in rep.contacts if c.kind == "call"}
    assert {key: status[key] for key in picked} == {
        key: ("recorded", "site") for key in picked}
    # no executed record, but the trace has call sites: "ran" is known by file
    assert rep.coverage_granularity == "file"
    # the one call no static contact names is the only dynamic-only symbol
    dynamic = [s.target for s in rep.symbols if s.status == "dynamic-only"]
    assert dynamic == [_UNMAPPED] and rep.dynamic_only == 1

    # confirmed / BLIND partition the static denominator; dynamic-only sits outside it.
    assert rep.confirmed + rep.blind == rep.static_targets
    assert rep.confirmed >= 1
    # coverage and recall are their own definitions over the contacts' statuses
    reached = rep.contacts_recorded + rep.contacts_ran_not_recorded
    observable = reached + rep.contacts_not_run
    assert rep.contacts_total == observable + rep.contacts_unobservable
    assert rep.contacts_recorded >= len(picked)
    assert rep.coverage == pytest.approx(reached / observable)
    assert rep.recall == pytest.approx(rep.contacts_recorded / reached)
    for tier in rep.identity_tiers:
        if tier.static:
            assert tier.share == pytest.approx(tier.confirmed / tier.static)

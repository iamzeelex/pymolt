"""Always-on regression over the real flasgger artifact (git submodule).

The devcontainer scaffold (test_flasgger_integration.py) needs a Python 3.6
container and hand-made capture artifacts, so the default suite never runs it.
This file is the always-running half: the static contact map over the real
flasgger checkout, plus fold invariants over whatever local capture exists.

Floors and invariants, not exact pins: the submodule may carry local
modifications during development, and exact counts would turn every artifact
touch into a red suite. The floors still catch the regressions that matter —
an empty map, a broken dependency filter, report arithmetic drift.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_FLASGGER = Path(__file__).resolve().parents[2] / "tests" / "artifacts" / "flasgger"

pytestmark = pytest.mark.skipif(
    not (_FLASGGER / "flasgger" / "__init__.py").is_file(),
    reason="flasgger submodule not initialized (git submodule update --init)",
)


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


def test_fold_invariants_over_local_capture():
    """When a named capture exists (pymolt capture), fold it and hold
    the report's arithmetic to its own definitions."""
    trace = _FLASGGER / ".pymolt" / "contract_traces" / "baseline.jsonl"
    if not trace.is_file():
        pytest.skip("no local capture present (pymolt capture baseline …)")

    from pymolt.verify.report import build_contract_report

    rep = build_contract_report(_FLASGGER, trace=trace)

    # confirmed / BLIND partition the static denominator; dynamic-only sits outside it.
    assert rep.confirmed + rep.blind == rep.static_targets
    assert rep.dynamic_only >= 0
    # coverage and recall are their own definitions over the contacts' statuses
    reached = rep.contacts_recorded + rep.contacts_ran_not_recorded
    observable = reached + rep.contacts_not_run
    assert rep.contacts_total == observable + rep.contacts_unobservable
    if observable:
        assert rep.coverage == pytest.approx(reached / observable)
    if reached:
        assert rep.recall == pytest.approx(rep.contacts_recorded / reached)
    for tier in rep.identity_tiers:
        if tier.static:
            assert tier.share == pytest.approx(tier.confirmed / tier.static)

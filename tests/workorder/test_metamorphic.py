"""The metamorphic guard (design 8): ``apply .``, ``--step`` one at a time in plan order,
``--through`` the last step, and an order permuted within ``depends_on`` give identical files,
identical ledger coverage and identical step receipts."""
from __future__ import annotations

import pytest
from _stepwise import APP, RECIPES, code_steps, plan_for, rename

from pymolt.apply_ledger import in_effect_entries
from pymolt.codemods.models import CodemodPattern
from pymolt.migration_state import MigrationReceipt
from pymolt.workorder.apply import apply_steps
from pymolt.workorder.coverage import ledger_coverage

MIXED = {
    "app/m.py": (APP, RECIPES),
    "app/n.py": ("from a.old import thing, other\n\n\ndef use():\n    return thing(other())\n",
                 [CodemodPattern(old_qualname="a.old.thing", new_qualname="a.new.thing",
                                 kind="rewrite-import"), rename("a.h", "a.k")]),
    "app/o.py": ("import a\n\n\nclass C:\n    def m(self):\n        a.h(1)\n        a.h(1)\n"
                 "        return a.f(2)\n", [rename("a.f", "a.g"), rename("a.h", "a.k")]),
}


def _permuted(plan) -> list[str]:
    """The code steps latest first wherever ``depends_on`` allows it."""
    steps = code_steps(plan)
    out: list[str] = []
    pending = [s.id for s in steps]
    deps = {s.id: set(s.depends_on) for s in steps}
    while pending:
        ready = [sid for sid in pending if deps[sid] <= set(out)]
        out.append(ready[-1])
        pending.remove(ready[-1])
    return out


def _run(root, mode):
    plan = plan_for(root, MIXED)
    ids = [s.id for s in code_steps(plan)]
    if mode == "all":
        apply_steps(root, plan)
    elif mode == "one-by-one":
        for sid in ids:
            apply_steps(root, plan, step=sid)
    elif mode == "through":
        apply_steps(root, plan, through=ids[-1])
    else:
        order = _permuted(plan)
        assert order != ids
        for sid in order:
            apply_steps(root, plan, step=sid)
    files = {c.path: (root / c.path).read_text(encoding="utf-8") for c in plan.changes}
    entries, _note, _l = in_effect_entries(root, MigrationReceipt.load(root))
    coverage = ledger_coverage({c.path: c.old_source for c in plan.changes}, files, entries,
                               python_old="3.11", python_new="3.12")
    located = sorted((e["file"], e["step_id"], e["claim"], e["basis"], e["old_symbol"],
                      e["new_symbol"], tuple(e["located"]["old"]), tuple(e["located"]["new"]))
                     for e in coverage.entries)
    receipt = MigrationReceipt.load(root)
    steps = {k.split(":", 1)[1]: (s.status, s.claim, tuple(s.files), tuple(s.risk_ids))
             for k, s in receipt.steps.items()}
    return files, located, steps, (receipt.status, receipt.progress,
                                   sorted(receipt.files_changed and
                                          [p.rsplit("/", 2)[-1] for p in receipt.files_changed]))


@pytest.mark.parametrize("mode", ["one-by-one", "through", "permuted"])
def test_every_way_of_applying_the_steps_gives_the_same_result(tmp_path, mode):
    whole, other = tmp_path / "all", tmp_path / mode
    whole.mkdir()
    other.mkdir()
    expected = _run(whole, "all")

    got = _run(other, mode)

    assert got[0] == expected[0]                         # files
    assert got[1] == expected[1]                         # ledger coverage
    assert got[2] == expected[2]                         # step receipts
    assert got[3] == expected[3]                         # the run's summary


def test_the_files_equal_the_v1_view_of_the_plan(tmp_path):
    plan = plan_for(tmp_path, MIXED)
    apply_steps(tmp_path, plan, through=code_steps(plan)[-1].id)

    for change in plan.changes:
        assert (tmp_path / change.path).read_text(encoding="utf-8") == change.new_source

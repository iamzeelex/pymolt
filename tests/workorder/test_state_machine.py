"""The state-machine guard (design 8): seeded random sequences (stdlib ``random``) of apply,
``--step``, ``--through``, rollback (whole run, one step, ``--force``), skip, hand edits and a
re-plan, over two runs. After every event:

- a refused command changed nothing: files, ledger and receipt are byte for byte the same;
- the receipt agrees with the ledger: a step receipt is in effect exactly when the ledger gives
  it a current epoch, with the same seq;
- the in-effect entries are exactly the entries of the current epochs of the runs on disk;
- the disk agrees with the ledger: a file the last epoch in effect wrote, and nobody edited since,
  holds that epoch's after-text; a file-level rollback leaves the epoch's before-text;
- every status change is a row of the state table (4.1);
- ``project_steps`` is total and deterministic, and a step in effect is projected in effect.

Every ordered pair of event kinds occurs (checked). ``--record`` and ``--abort`` come with
stage 5.
"""
from __future__ import annotations

import hashlib
import random
import shutil
from collections import Counter
from pathlib import Path

import pytest
from _stepwise import APP, RECIPES, plan_for, plan_next, rename

from pymolt.apply_ledger import chain_runs, in_effect_entries, load_ledger
from pymolt.migration_plan import MigrationConflictError, MigrationPlan, MigrationPlanError
from pymolt.migration_state import MigrationReceipt
from pymolt.workorder.apply import apply_steps, skip_step
from pymolt.workorder.rollback import rollback_steps
from pymolt.workorder.state import RunView, project_steps

SEQUENCES = 500
LENGTH = 9
M, N = "app/m.py", "app/n.py"
KINDS = ("apply", "step", "through", "rollback", "rollback_step", "force", "skip", "edit",
         "replan")
# (status before, command) -> statuses allowed after it; None is "planned" (no ledger status).
ALLOWED = {
    (None, "apply"): {"applied"}, ("rolled_back", "apply"): {"applied"},     # rows 1-3, 7
    ("skipped", "apply"): {"applied"},          # --step on a skipped step (a departure)
    ("applied", "rollback"): {"rolled_back"},   # rows 5, 12, 14
    (None, "skip"): {"skipped"}, ("rolled_back", "skip"): {"skipped"},    # row 16
}


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _snapshot(root: Path) -> dict:
    files = [root / M, root / N, root / "requirements.txt", root / ".pymolt" / "apply_ledger.json",
             root / ".pymolt" / "migration_state.json"]
    return {str(p): _digest(p) for p in files}


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    """Two plans over a project: R1 (three steps in m.py, one in n.py) and R2, made after R1
    was applied (it rewrites what R1 wrote in m.py, and another call in n.py)."""
    root = tmp_path_factory.mktemp("template")
    plan1 = plan_for(root, {M: (APP, RECIPES), N: ("import a\ny = a.h()\nw = a.f()\n",
                                                   [rename("a.h", "a.k")])}, baseline=False)
    texts = {p: (root / p).read_text() for p in (M, N)}
    apply_steps(root, plan1, allow_no_baseline=True)
    plan2 = plan_next(root, {M: [rename("a.m", "a.n")], N: [rename("a.f", "a.q")]},
                      save=False)
    for p, text in texts.items():
        (root / p).write_text(text)
    shutil.rmtree(root / ".pymolt" / "migrations")
    for name in ("apply_ledger.json", "migration_state.json"):
        (root / ".pymolt" / name).unlink()
    shutil.rmtree(root / ".pymolt" / "blobs", ignore_errors=True)
    plan1.save(root)
    return root, plan1, plan2


def _check(root: Path, plans: dict[str, MigrationPlan], edited_after: dict[str, int]) -> None:
    receipt = MigrationReceipt.load(root)
    ledger = load_ledger(root, receipt=receipt)
    if ledger is None:
        return
    current = ledger.current_epochs()
    for key, step in (receipt.steps if receipt else {}).items():
        run, sid = key.split(":", 1)
        assert step.in_effect == ((run, sid) in current), key
        if step.in_effect:
            assert step.epoch == current[(run, sid)], key
    chain = chain_runs(receipt)
    entries, _note, _l = in_effect_entries(root, receipt)
    want = {(e.run_id, e.step_id, e.epoch, e.file, e.new_lines) for e in ledger.entries
            if current.get((e.run_id, e.step_id)) == e.epoch and e.run_id in chain}
    got = {(e.run_id, e.step_id, e.epoch, e.file, e.new_lines) for e in entries or []}
    assert got == want or (entries is None and not want)
    by_seq = {e.seq: e for e in ledger.events}
    last: dict[str, tuple[int, str]] = {}
    for (run, _sid), seq in current.items():
        if run not in chain:
            continue
        for f in by_seq[seq].files:
            if f.file not in last or last[f.file][0] < seq:
                last[f.file] = (seq, f.after)
    for rel, (seq, after) in last.items():
        if edited_after.get(rel, -1) < seq:
            assert _digest(root / rel) == after, (rel, seq)


def _statuses(root: Path, plans: dict[str, MigrationPlan]) -> dict[tuple[str, str], str | None]:
    ledger = load_ledger(root)
    out = {}
    for run, plan in plans.items():
        view = RunView.of(ledger, run)
        for step in plan.steps:
            out[(run, step.id)] = view.status(step.id)
    return out


def _one(template_root: Path, plan1, plan2, work: Path, seed: int, pairs: Counter) -> None:
    rng = random.Random(seed)
    root = work / f"s{seed}"
    shutil.copytree(template_root, root)
    plans = {plan1.plan_id: plan1, plan2.plan_id: plan2}
    plan = plan1
    edited_after: dict[str, int] = {}
    previous = None
    for _ in range(LENGTH):
        kind = rng.choice(KINDS)
        if previous is not None:
            pairs[(previous, kind)] += 1
        previous = kind
        before = _snapshot(root)
        statuses = _statuses(root, plans)
        code = [s.id for s in plan.steps if s.executor == "pymolt"]
        command = {"apply": "apply", "step": "apply", "through": "apply", "skip": "skip"}.get(
            kind, "rollback")
        rolled_step = None
        try:
            if kind == "apply":
                apply_steps(root, plan, allow_no_baseline=True)
            elif kind == "step":
                apply_steps(root, plan, step=rng.choice(code), allow_no_baseline=True)
            elif kind == "through":
                apply_steps(root, plan, through=rng.choice(code), allow_no_baseline=True)
            elif kind == "rollback":
                rollback_steps(root)
            elif kind == "rollback_step":
                rolled_step = rng.choice(code)
                rollback_steps(root, step=rolled_step)
            elif kind == "force":
                rollback_steps(root, force=True)
            elif kind == "skip":
                skip_step(root, plan, rng.choice(code + ["env-2"]), "declined")
            elif kind == "edit":
                rel = rng.choice((M, N))
                path = root / rel
                text = path.read_text()
                path.write_text(text.replace("z = 1", "z = 2") if "z = 1" in text
                                and rng.random() < 0.5 else text + "# edited\n")
                ledger = load_ledger(root)
                edited_after[rel] = ledger.next_seq() if ledger else 1
                continue
            elif kind == "replan":
                plan = plan2
                plan.save(root)
                continue
        except (MigrationConflictError, MigrationPlanError) as exc:
            assert _snapshot(root) == before, f"seed {seed}: {kind} refused but wrote: {exc}"
            # Nothing here interrupts an invocation, so nothing is ever left to recover.
            assert "recover" not in str(exc), f"seed {seed}: {kind}: {exc}"
            continue
        after = _statuses(root, plans)
        for key, status in after.items():
            was = statuses.get(key)
            if status != was:
                allowed = ALLOWED.get((was, command), set())
                assert status in allowed, (seed, kind, key, was, status)
        if kind == "rollback_step" and rolled_step is not None:
            ledger = load_ledger(root)
            rb = max((e for e in ledger.events if e.event == "rollback"), key=lambda e: e.seq)
            for f in rb.files:
                assert _digest(root / f.file) == f.after         # the epoch's before-text
        _check(root, plans, edited_after)
        _projected(root, plan)
    shutil.rmtree(root)


STATUSES = {"planned", "blocked", "stale", "present", "in_progress", "applied", "recorded",
            "skipped", "rolled_back", "not_in_capture", "verified", "failed", "needs_review",
            "needs_evidence"}


def _projected(root: Path, plan: MigrationPlan) -> None:
    """``project_steps`` is total and deterministic, and agrees with the ledger."""
    first = project_steps(root, plan)
    again = project_steps(root, plan)
    assert [(s.id, s.status, s.reasons) for s in first.steps] == [
        (s.id, s.status, s.reasons) for s in again.steps]
    assert [s.id for s in first.steps] == [s.id for s in plan.steps]
    assert {s.status for s in first.steps} <= STATUSES
    view = RunView.of(load_ledger(root), first.run_id)
    for s in first.steps:
        if view.in_effect(s.id):
            assert s.status in ("applied", "recorded"), (s.id, s.status)


def test_random_sequences_keep_the_invariants(template, tmp_path, monkeypatch):
    monkeypatch.setattr("pymolt.apply_check.compile_changes", lambda root, plan: [])
    root, plan1, plan2 = template
    pairs: Counter = Counter()
    for seed in range(SEQUENCES):
        _one(root, plan1, plan2, tmp_path, seed, pairs)
    missing = [(a, b) for a in KINDS for b in KINDS if not pairs[(a, b)]]
    assert not missing, f"pairs never generated: {missing}"

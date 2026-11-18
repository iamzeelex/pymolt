"""The concurrency guard (design 8), with two real processes: an apply and a rollback, or two
applies, never run at once (the second is refused by the lock, and writes nothing); an
invocation killed between its write and its ledger is recovered by the next; ``status`` read
during an apply sees a consistent state."""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from _stepwise import APP, RECIPES, events, plan_for, read

from pymolt.migration_plan import MigrationConflictError, MigrationPlan
from pymolt.migration_state import MigrationReceipt
from pymolt.status import build_status
from pymolt.workorder.apply import apply_steps
from pymolt.workorder.rollback import rollback_steps

M = "app/m.py"

# The child applies the plan in ``root`` with one function replaced: ``HOLD`` waits inside the
# transaction (the lock held) until ``go`` exists; ``KILL`` ends the process between the write
# and the ledger, as a crash would.
CHILD = textwrap.dedent("""
    import os, sys, time
    from pathlib import Path
    import pymolt.apply_check as check
    import pymolt.apply_ledger as ledger
    from pymolt.migration_plan import MigrationPlan
    from pymolt.workorder.apply import apply_steps

    root, mode = Path(sys.argv[1]), sys.argv[2]
    check.compile_changes = lambda root, plan: []
    real = ledger.append_ledger

    def hooked(*args, **kwargs):
        (root / "inside").write_text("1")
        if mode == "KILL":
            os._exit(9)
        while not (root / "go").exists():
            time.sleep(0.02)
        return real(*args, **kwargs)

    ledger.append_ledger = hooked
    apply_steps(root, MigrationPlan.load(root), allow_no_baseline=True)
""")


def _child(root: Path, mode: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", CHILD, str(root), mode],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _wait_inside(root: Path, proc: subprocess.Popen) -> None:
    deadline = time.monotonic() + 60
    while not (root / "inside").exists():
        if proc.poll() is not None:
            raise AssertionError(proc.stderr.read().decode())
        if time.monotonic() > deadline:
            proc.kill()
            raise AssertionError("the child never reached the ledger write")
        time.sleep(0.02)


@pytest.fixture
def no_compile(monkeypatch):
    monkeypatch.setattr("pymolt.apply_check.compile_changes", lambda root, plan: [])


def test_a_second_operation_is_refused_by_the_lock_and_writes_nothing(tmp_path, no_compile):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)}, baseline=False)
    proc = _child(tmp_path, "HOLD")
    try:
        _wait_inside(tmp_path, proc)
        written = read(tmp_path, M)                     # the child wrote, its ledger did not
        with pytest.raises(MigrationConflictError, match="another migration operation"):
            apply_steps(tmp_path, MigrationPlan.load(tmp_path), allow_no_baseline=True)
        with pytest.raises(MigrationConflictError, match="another migration operation"):
            rollback_steps(tmp_path)
        assert read(tmp_path, M) == written
        # Status reads while the transaction is open: the state from before it, consistent.
        status = build_status(tmp_path)
        assert status.phase("migration") is not None
        assert not (tmp_path / ".pymolt" / "migration_state.json").exists()
    finally:
        (tmp_path / "go").write_text("1")
        proc.wait(timeout=60)
    assert proc.returncode == 0, proc.stderr.read().decode()
    assert [e[2] for e in events(tmp_path)] == ["mech-1", "mech-2", "mech-3"]
    assert read(tmp_path, M) == plan.changes[0].new_source


def test_an_invocation_killed_between_its_write_and_its_ledger_is_recovered(tmp_path,
                                                                            no_compile):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)}, baseline=False)
    proc = _child(tmp_path, "KILL")
    proc.wait(timeout=60)
    assert proc.returncode == 9
    assert read(tmp_path, M) == plan.changes[0].new_source      # written, not recorded
    assert events(tmp_path) == []
    state = json.loads((tmp_path / ".pymolt" / "migrations" / plan.plan_id / "state.json")
                       .read_text())
    assert state["invocation"]["status"] == "prepared"

    result = apply_steps(tmp_path, plan, step="mech-1", allow_no_baseline=True)

    # Put back, then applied again from the text before the interruption.
    assert result.applied == ["mech-1"] and not result.unwritten and not result.rebased
    assert [e[2] for e in events(tmp_path)] == ["mech-1"]
    assert MigrationReceipt.load(tmp_path).steps[f"{plan.plan_id}:mech-1"].epoch == 1
    assert "a.g(v)" in read(tmp_path, M) and "a.h(x)" in read(tmp_path, M)

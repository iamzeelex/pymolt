"""Stepwise apply (design 3.1-3.4): selection, one transaction, the fast path, rules 1, 2, 4
and 5, idempotency, refusals and exit codes, the self-check per step, and the baseline check
for a run's later invocations."""
from __future__ import annotations

import json

import pytest
from _stepwise import APP, RECIPES, code_steps, events, ledger, plan_for, read, rename
from typer.testing import CliRunner

from pymolt.interfaces.cli.commands import app
from pymolt.migration_plan import (
    MigrationCheckError,
    MigrationConflictError,
    MigrationEvidenceError,
    MigrationPlan,
    apply_migration_plan,
    rollback_migration,
)
from pymolt.migration_state import MigrationReceipt
from pymolt.verify import service
from pymolt.workorder.apply import StepRefusal, apply_steps, skip_step
from pymolt.workorder.rules import PRESENT, STALE, decide

runner = CliRunner()
M = "app/m.py"


def _texts(plan):
    """T0, T1, T2, T3 of ``app/m.py``: each step's after-text, rebuilt from the plan."""
    from pymolt.workorder.edits import apply_hunks

    out = [plan.changes[0].old_source]
    for step in code_steps(plan):
        out.append(apply_hunks(out[-1], step.edit_for(M).hunks))
    return out


# ── selection ─────────────────────────────────────────────────────────────────────────────


def test_apply_writes_every_pymolt_step_one_event_each_and_the_v1_text(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})

    result = apply_steps(tmp_path, plan)

    assert result.outcome == "applied"
    assert result.applied == ["mech-1", "mech-2", "mech-3"] and not result.rebased
    assert read(tmp_path, M) == plan.changes[0].new_source
    assert events(tmp_path) == [(1, "apply", "mech-1", True, None),
                                (2, "apply", "mech-2", True, None),
                                (3, "apply", "mech-3", True, None)]
    receipt = MigrationReceipt.load(tmp_path)
    assert {k.split(":")[1]: s.epoch for k, s in receipt.steps.items()} == {
        "mech-1": 1, "mech-2": 2, "mech-3": 3}
    assert set(result.checks) == {"mech-1", "mech-2", "mech-3"}
    assert result.checks["mech-1"]["compile_base"] == "not checked: no base claim"
    # What remains: the env steps, for a person.
    assert result.remaining == {"planned": ["env-1", "env-2"]}


def test_step_one_at_a_time_in_plan_order_gives_the_same_files_and_ledger(tmp_path):
    whole, single = tmp_path / "whole", tmp_path / "single"
    whole.mkdir()
    single.mkdir()
    plan_a = plan_for(whole, {M: (APP, RECIPES)})
    plan_b = plan_for(single, {M: (APP, RECIPES)})
    apply_steps(whole, plan_a)

    for step in ("mech-1", "mech-2", "mech-3"):
        assert apply_steps(single, plan_b, step=step).applied == [step]

    assert read(single, M) == read(whole, M)
    assert events(single) == events(whole)
    strip = lambda d: [{k: v for k, v in e.items() if k != "run_id"} for e in d["entries"]]  # noqa: E731
    assert strip(ledger(single)) == strip(ledger(whole))


def test_through_applies_the_steps_up_to_it_and_their_dependencies(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    t0, t1, t2, _t3 = _texts(plan)

    result = apply_steps(tmp_path, plan, through="mech-2")

    assert result.applied == ["mech-1", "mech-2"]
    assert read(tmp_path, M) == t2
    assert result.remaining["planned"] == ["env-1", "env-2", "mech-3"]
    again = apply_steps(tmp_path, plan, through="mech-3")
    assert (again.applied, again.noop) == (["mech-3"], ["mech-1", "mech-2"])


def test_a_step_whose_dependency_is_not_in_effect_is_refused_and_nothing_written(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})

    with pytest.raises(StepRefusal, match="mech-3 depends on mech-1") as caught:
        apply_steps(tmp_path, plan, step="mech-3")

    assert caught.value.refused == {"mech-3": ["needs mech-1"]}
    assert read(tmp_path, M) == APP and events(tmp_path) == []


def test_unknown_steps_and_steps_pymolt_does_not_write_are_refused(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})

    with pytest.raises(StepRefusal, match="unknown step mech-9"):
        apply_steps(tmp_path, plan, step="mech-9")
    with pytest.raises(StepRefusal, match="env-2 is a person step"):
        apply_steps(tmp_path, plan, step="env-2")
    with pytest.raises(StepRefusal, match="cannot be combined"):
        apply_steps(tmp_path, plan, step="mech-1", through="mech-2")


# ── the state table, rows 1 to 4 ──────────────────────────────────────────────────────────


def test_row_1_planned_to_applied_with_the_event_seq_as_its_epoch(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    apply_steps(tmp_path, plan, step="mech-1")

    [(seq, kind, step, wrote, _r)] = events(tmp_path)
    assert (seq, kind, step, wrote) == (1, "apply", "mech-1", True)
    [entry] = ledger(tmp_path)["entries"]
    assert (entry["epoch"], entry["step_id"], entry["anchor_new"]["stmt"]) == (
        1, "mech-1", "x = a.g(v)")
    assert MigrationReceipt.load(tmp_path).steps[f"{plan.plan_id}:mech-1"].status == "applied"


def test_row_2_a_file_already_at_the_reviewed_text_gets_an_epoch_and_no_write(tmp_path):
    """Rule 2: the text is byte for byte what pymolt reviewed. The epoch names the plan's two
    texts, its entries come from the plan's change, and a rollback restores the plan's
    before-text from the store."""
    plan = plan_for(tmp_path, {M: (APP, [rename("a.f", "a.g")])})
    (tmp_path / M).write_text(plan.changes[0].new_source, encoding="utf-8")
    mtime = (tmp_path / M).stat().st_mtime_ns

    result = apply_steps(tmp_path, plan)

    assert (result.applied, result.unwritten) == (["mech-1"], ["mech-1"])
    assert (tmp_path / M).stat().st_mtime_ns == mtime
    [event] = ledger(tmp_path)["events"]
    edit = plan.step("mech-1").edit_for(M)
    assert (event["wrote"], event["files"][0]["before"], event["files"][0]["after"]) == (
        False, edit.before_sha256, edit.after_sha256)
    [entry] = ledger(tmp_path)["entries"]
    assert (entry["anchor_old"]["stmt"], entry["anchor_new"]["stmt"]) == (
        "x = a.f(v)", "x = a.g(v)")
    rollback_migration(tmp_path)
    assert read(tmp_path, M) == APP


def _hand_made(text: str) -> str:
    """The plan's change made by hand, with an unrelated edit above it."""
    return text.replace("import a\n", "import a\nimport os\n", 1)


def test_row_3_an_effect_already_in_its_slots_is_present_with_no_epoch(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, [rename("a.f", "a.g")])})
    (tmp_path / M).write_text(_hand_made(plan.changes[0].new_source), encoding="utf-8")
    before = read(tmp_path, M)

    result = apply_steps(tmp_path, plan)

    assert (result.outcome, result.present, result.applied) == ("present", ["mech-1"], [])
    assert read(tmp_path, M) == before and events(tmp_path) == []
    assert MigrationReceipt.load(tmp_path) is None
    assert result.remaining["present"] == ["mech-1"]


def test_rule_4_refuses_when_the_scope_does_not_resolve(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, [rename("a.f", "a.g")])})
    step = plan.step("mech-1")
    # The function was renamed: its new statement is there, in another scope.
    moved = plan.changes[0].new_source.replace("def run(v)", "def go(v)")
    decision = decide(step, step.edit_for(M), moved)
    assert decision.rule == STALE
    assert any("scope run is missing" in r for r in decision.reasons)
    # Defined twice: the slot is not unique either.
    twice = plan.changes[0].new_source + "\n\ndef run(v):\n    return 0\n"
    assert any("defined 2 times" in r for r in decide(step, step.edit_for(M), twice).reasons)


def test_rule_4_refuses_a_new_statement_elsewhere_in_the_scope(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, [rename("a.f", "a.g")])})
    step = plan.step("mech-1")
    # The old call is gone and the new one exists, but not between the old one's neighbours.
    elsewhere = ("import a\n\n\ndef run(v):\n    z = 1\n    x = a.g(v)\n    return a.h(x)\n")
    decision = decide(step, step.edit_for(M), elsewhere)
    assert decision.rule == STALE
    assert any("not between the neighbours" in r for r in decision.reasons)
    assert decide(step, step.edit_for(M), _hand_made(plan.changes[0].new_source)).rule \
        == PRESENT


def test_row_4_a_step_in_effect_is_a_no_op(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    apply_steps(tmp_path, plan)
    before = (tmp_path / ".pymolt" / "apply_ledger.json").read_bytes()

    again = apply_steps(tmp_path, plan)
    one = apply_steps(tmp_path, plan, step="mech-2")

    assert again.outcome == "already_applied" and again.noop == ["mech-1", "mech-2", "mech-3"]
    assert (one.outcome, one.noop) == ("already_applied", ["mech-2"])
    assert (tmp_path / ".pymolt" / "apply_ledger.json").read_bytes() == before


# ── rule 5 ────────────────────────────────────────────────────────────────────────────────


def test_a_stale_anchor_refuses_the_invocation_and_names_the_statement(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    (tmp_path / M).write_text(APP.replace("x = a.f(v)", "x = a.f(v, 2)"), encoding="utf-8")

    with pytest.raises(StepRefusal, match="stale migration plan") as caught:
        apply_steps(tmp_path, plan)

    reasons = caught.value.refused["mech-1"]
    assert any("`x = a.f(v)` in run is lost" in r for r in reasons)
    assert events(tmp_path) == [] and "a.f(v, 2)" in read(tmp_path, M)


def test_a_moved_text_without_re_basing_is_stale_for_a_whole_file_step(tmp_path):
    """Previews with no recorded chain give a whole-file step, which applies only to its own
    texts."""
    from _stepwise import project

    from pymolt.codemods.models import DependencyMigration
    from pymolt.codemods.models import FilePreview
    from pymolt.migration_plan import create_migration_plan

    target = project(tmp_path)
    (tmp_path / "w.py").write_text("import a\na.f()\n", encoding="utf-8")
    plan = create_migration_plan(
        tmp_path, [FilePreview(path=str(tmp_path / "w.py"), old_source="import a\na.f()\n",
                               new_source="import a\na.g()\n", sites=1)],
        [DependencyMigration("a", "1.0", "2.0")], target_python="3.12",
        source_manifest="requirements.txt", target_manifest_path=target, files_scanned=1,
        predict=False)
    (tmp_path / "w.py").write_text("import a\n\na.f()\n", encoding="utf-8")

    with pytest.raises(StepRefusal, match="whole-file change"):
        apply_steps(tmp_path, plan)


# ── one transaction, D per step ───────────────────────────────────────────────────────────


def test_a_compile_failure_of_any_step_writes_nothing(tmp_path, monkeypatch):
    from pymolt.pyversion import fixes

    def target_rejects(files, executable, *, runner=None, timeout=120.0):
        return {p: "SyntaxError: boom" for p, s in files.items() if "a.k(" in s}, None, "3.12"

    monkeypatch.setattr(fixes, "recompile_detailed", target_rejects)
    monkeypatch.setattr("pymolt.pyversion.facts.find_target_interpreter",
                        lambda root, python: "python3")
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})

    with pytest.raises(MigrationCheckError, match="does not compile"):
        apply_steps(tmp_path, plan)

    assert read(tmp_path, M) == APP and events(tmp_path) == []
    apply_steps(tmp_path, plan, step="mech-1")          # mech-1 alone compiles
    assert events(tmp_path) == [(1, "apply", "mech-1", True, None)]


def test_dry_run_checks_and_shows_the_diff_and_writes_nothing(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})

    result = apply_steps(tmp_path, plan, through="mech-2", dry_run=True)

    assert (result.outcome, result.applied, result.files) == (
        "dry_run", ["mech-1", "mech-2"], [M])
    assert "-    x = a.f(v)" in result.diff[0]["diff"]
    assert "+    return a.k(x)" in result.diff[0]["diff"]
    assert read(tmp_path, M) == APP and events(tmp_path) == []
    assert not (tmp_path / ".pymolt" / "migration_state.json").exists()
    assert not (tmp_path / ".pymolt" / "migrations").exists()


# ── the run's later invocations ───────────────────────────────────────────────────────────


def test_later_invocations_check_the_baseline_against_the_runs_start(tmp_path):
    """Design 3.2 step 2 (M9): from the run's second invocation on, a baseline that records no
    setup fingerprint is checked against the run's stamped starting fingerprint, so pins bumped
    after the first invocation (by pymolt's own env step, or a person) never make it stale."""
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    state = service.load_contract_state(tmp_path)
    state.baseline = state.baseline.model_copy(update={"env_setup_fingerprint": None})
    state.save(tmp_path / ".pymolt" / "contract_state.json")
    apply_steps(tmp_path, plan, step="mech-1")
    (tmp_path / "requirements.txt").write_text("a==2.0\n", encoding="utf-8")

    assert apply_steps(tmp_path, plan, step="mech-2").applied == ["mech-2"]

    # The same bump before a run's first invocation is a different starting point.
    other = tmp_path / "other"
    other.mkdir()
    plan2 = plan_for(other, {M: (APP, RECIPES)})
    state = service.load_contract_state(other)
    state.baseline = state.baseline.model_copy(update={"env_setup_fingerprint": None})
    state.save(other / ".pymolt" / "contract_state.json")
    (other / "requirements.txt").write_text("a==2.0\n", encoding="utf-8")
    with pytest.raises(MigrationEvidenceError, match="invalid or stale"):
        apply_steps(other, plan2, step="mech-1")


def test_a_plan_file_that_is_not_the_runs_plan_is_refused(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    apply_steps(tmp_path, plan, step="mech-1")
    changed = plan.model_copy(deep=True)
    changed.steps[-1].title = "edited by hand"

    with pytest.raises(StepRefusal, match="not the plan run"):
        apply_steps(tmp_path, changed, step="mech-2")


def test_an_interrupted_invocation_is_recovered_before_the_next(tmp_path):
    """Killed between the write and the ledger: the files went to disk, nothing else did. The
    next invocation puts them back (their texts are in the store), then applies."""
    from pymolt.migration_plan import (
        Invocation,
        InvocationFile,
        MigrationRunState,
        _save_run_state,
    )
    from pymolt.workorder.blobs import put_texts
    from pymolt.workorder.edits import sha256

    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    t3 = plan.changes[0].new_source
    put_texts(tmp_path, [APP, t3])
    run_dir = tmp_path / ".pymolt" / "migrations" / plan.plan_id
    run_dir.mkdir(parents=True)
    plan.save(tmp_path, run_dir / "plan.json")
    _save_run_state(run_dir, MigrationRunState(
        run_id=plan.plan_id, status="prepared", prepared_at="2026-11-17T00:00:00+00:00",
        invocation=Invocation(id="i1", operation="apply", status="prepared",
                              at="2026-11-17T00:00:00+00:00", steps=["mech-1"],
                              files=[InvocationFile(file=M, before=sha256(APP),
                                                    after=sha256(t3))])))
    (tmp_path / M).write_text(t3, encoding="utf-8")

    result = apply_steps(tmp_path, plan, step="mech-1")

    assert result.applied == ["mech-1"] and not result.unwritten
    assert read(tmp_path, M) == _texts(plan)[1]
    state = json.loads((run_dir / "state.json").read_text())
    assert state["invocation"]["status"] == "done"


def test_an_interrupted_invocation_whose_receipt_was_saved_had_committed(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    apply_steps(tmp_path, plan, step="mech-1")
    run_dir = tmp_path / ".pymolt" / "migrations" / plan.plan_id
    state = json.loads((run_dir / "state.json").read_text())
    state["invocation"]["status"] = "prepared"         # killed after the receipt was saved
    (run_dir / "state.json").write_text(json.dumps(state))

    result = apply_steps(tmp_path, plan, step="mech-2")

    assert result.applied == ["mech-2"]
    assert [e[2] for e in events(tmp_path)] == ["mech-1", "mech-2"]
    assert read(tmp_path, M) == _texts(plan)[2]


def test_an_interrupted_invocation_over_an_edited_file_is_refused(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    apply_steps(tmp_path, plan, step="mech-1")
    run_dir = tmp_path / ".pymolt" / "migrations" / plan.plan_id
    state = json.loads((run_dir / "state.json").read_text())
    state["invocation"]["status"] = "prepared"
    state["invocation"]["id"] = "not-the-receipts"
    (run_dir / "state.json").write_text(json.dumps(state))
    (tmp_path / M).write_text(read(tmp_path, M) + "# edited\n", encoding="utf-8")

    with pytest.raises(MigrationConflictError, match="was edited since"):
        apply_steps(tmp_path, plan, step="mech-2")


# ── skip ──────────────────────────────────────────────────────────────────────────────────


TWO = {M: ("import a\nx = a.f()\n", [rename("a.f", "a.g"), rename("a.g", "a.m")]),
       "app/n.py": ("import a\ny = a.h()\n", [rename("a.h", "a.k")])}


def test_a_skipped_step_stays_out_and_its_dependents_stay_blocked(tmp_path):
    plan = plan_for(tmp_path, TWO)
    assert [(s.id, s.files, s.depends_on) for s in code_steps(plan)] == [
        ("mech-1", [M], []), ("mech-2", [M], ["mech-1"]), ("mech-3", ["app/n.py"], [])]

    skipped = skip_step(tmp_path, plan, "mech-1", "we keep a.f for now")
    result = apply_steps(tmp_path, plan)

    assert skipped.outcome == "skipped"
    assert events(tmp_path)[0] == (1, "skip", "mech-1", None, None)
    assert ledger(tmp_path)["events"][0]["reason"] == "we keep a.f for now"
    assert result.applied == ["mech-3"]
    assert result.blocked == {"mech-2": ["needs mech-1 (skipped, pymolt step)"]}
    assert result.remaining["skipped"] == ["mech-1"]
    assert result.remaining["blocked"] == ["mech-2"]
    with pytest.raises(StepRefusal, match="mech-2 on the way to mech-2 depends on mech-1"):
        apply_steps(tmp_path, plan, through="mech-2")
    # Asked for by name, a skipped step applies after all.
    assert apply_steps(tmp_path, plan, step="mech-1").applied == ["mech-1"]
    assert apply_steps(tmp_path, plan).applied == ["mech-2"]
    assert read(tmp_path, M) == "import a\nx = a.m()\n"


def test_skip_needs_a_reason_and_refuses_a_step_in_effect(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    with pytest.raises(StepRefusal, match="needs --reason"):
        skip_step(tmp_path, plan, "mech-1", " ")
    apply_steps(tmp_path, plan, step="mech-1")
    with pytest.raises(StepRefusal, match="is in effect"):
        skip_step(tmp_path, plan, "mech-1", "too late")


# ── the CLI: options, exit codes, --agent ─────────────────────────────────────────────────


def test_the_cli_exit_codes(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    d = str(tmp_path)

    assert runner.invoke(app, ["apply", d, "--step", "mech-9"]).exit_code == 2
    assert runner.invoke(app, ["apply", d, "--step", "mech-3"]).exit_code == 2
    assert runner.invoke(app, ["apply", d, "--skip"]).exit_code == 2           # needs --step
    assert runner.invoke(app, ["apply", d, "--step", "mech-1", "--reason", "x"]).exit_code == 2
    dry = runner.invoke(app, ["apply", d, "--through", "mech-2", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "Dry run" in dry.output and "+    return a.k(x)" in dry.output
    one = runner.invoke(app, ["apply", d, "--step", "mech-1", "--json"])
    assert one.exit_code == 0, one.output
    payload = json.loads(one.stdout)
    assert (payload["outcome"], payload["applied"]) == ("applied", ["mech-1"])
    assert payload["remaining"]["planned"] == ["env-1", "env-2", "mech-2", "mech-3"]
    text = runner.invoke(app, ["apply", d])
    assert text.exit_code == 0, text.output
    assert "mech-2, mech-3" in text.output and "Steps remaining" in text.output
    skip = runner.invoke(app, ["apply", d, "--step", "env-2", "--skip", "--reason", "CI"])
    assert skip.exit_code == 0, skip.output and "Step skipped" in skip.output
    state = service.load_contract_state(tmp_path)
    state.baseline = None
    state.save(tmp_path / ".pymolt" / "contract_state.json")
    other = tmp_path / "other"
    other.mkdir()
    plan_for(other, {M: (APP, RECIPES)}, baseline=False)
    assert runner.invoke(app, ["apply", str(other)]).exit_code == 4
    assert plan is not None


def test_agent_refusals_and_what_it_may_do(tmp_path):
    plan_for(tmp_path, {M: (APP, RECIPES)})
    d = str(tmp_path)

    refused = runner.invoke(app, ["apply", d, "--agent"])
    assert refused.exit_code == 2
    failure = json.loads(refused.stdout)
    assert "approved for agents" in failure["error"]
    assert "pymolt apply . --approve-agent" in failure["hint"]
    assert runner.invoke(app, ["apply", d, "--agent", "--step", "mech-1", "--skip",
                               "--reason", "x"]).exit_code == 2
    assert runner.invoke(app, ["apply", d, "--agent", "--dry-run",
                               "--allow-no-baseline"]).exit_code == 2
    dry = runner.invoke(app, ["apply", d, "--agent", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    view = json.loads(dry.stdout)
    assert view["ok"] and view["data"]["outcome"] == "dry_run"
    assert [a["step"] for a in view["data"]["applied"]] == ["mech-1", "mech-2", "mech-3"]
    brief = runner.invoke(app, ["apply", d, "--agent", "--step", "mech-2", "--brief"])
    assert brief.exit_code == 0 and json.loads(brief.stdout)["step"] == "mech-2"
    assert read(tmp_path, M) == APP


def test_brief_prints_the_step_and_writes_nothing(tmp_path):
    plan_for(tmp_path, {M: (APP, RECIPES)})

    out = runner.invoke(app, ["apply", str(tmp_path), "--step", "mech-3", "--brief"])

    assert out.exit_code == 0, out.output
    assert out.output.startswith("# Step mech-3")
    assert not (tmp_path / ".pymolt" / "apply_ledger.json").exists()


def test_the_v1_entry_point_still_returns_the_receipt(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})

    receipt = apply_migration_plan(tmp_path, plan)

    assert receipt.outcome == "applied" and receipt.run_id == plan.plan_id
    assert apply_migration_plan(tmp_path, plan).outcome == "already_applied"
    assert MigrationPlan.load(tmp_path, tmp_path / ".pymolt" / "migrations" / plan.plan_id
                              / "plan.json") == plan


def test_steps_that_do_not_rebuild_the_plans_changes_apply_as_one_whole_file_step(tmp_path):
    """A plan whose step hunks do not chain to ``changes`` (an order the files disagree on)
    is written as its one whole-file step; its own steps cannot be applied one by one."""
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    plan.steps = [s for s in plan.steps if s.id != "mech-2"]

    with pytest.raises(StepRefusal, match="cannot be applied on its own"):
        apply_steps(tmp_path, plan, step="mech-1")
    result = apply_steps(tmp_path, plan)

    assert result.applied == ["v1"]
    assert read(tmp_path, M) == plan.changes[0].new_source

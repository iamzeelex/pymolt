"""Rollback phase 2 (design 4.2): a step whose file moved on is rolled back by the inverse of its
effect. Only its own change is undone; later steps and hand edits elsewhere in the file stay
byte for byte. A statement out of its slot, or lines a later edit touched, refuse it (the
file-level refusal stays the fallback). An import it inserted goes only when nothing uses it.
The metamorphic property: apply A, B, C, roll back B by its inverse, and the files are those of
applying A and C alone."""
from __future__ import annotations

import pytest
from _stepwise import ledger, plan_for, plan_next, read, rename

from pymolt.apply_ledger import in_effect_entries
from pymolt.codemods.models import CodemodPattern
from pymolt.migration_plan import MigrationConflictError
from pymolt.migration_state import MigrationReceipt
from pymolt.workorder.apply import apply_steps
from pymolt.workorder.blobs import get_text
from pymolt.workorder.coverage import ledger_coverage
from pymolt.workorder.edits import make_edit, sha256
from pymolt.workorder.inverse import invert
from pymolt.workorder.rollback import rollback_steps

M, N = "app/m.py", "app/n.py"
# Three statements three recipes change, with an unchanged statement between each two: no step
# depends on another, and each is in its slot whatever the others do.
APP = ("import a\n"
       "\n"
       "\n"
       "def run(v):\n"
       "    x = a.f(v)\n"
       "    y = 0\n"
       "    w = a.h(x)\n"
       "    z = 1\n"
       "    return a.q(w)\n")
RECIPES = [rename("a.f", "a.g"), rename("a.h", "a.k"), rename("a.q", "a.r")]
OTHER = "import a\n\n\ndef use():\n    return a.h(1)\n"
FILES = {M: (APP, RECIPES), N: (OTHER, [rename("a.h", "a.k")])}
HEAD = "# a hand edit above\nimport os\n"
TAIL = "\n\ndef extra():\n    return os.sep\n"

MAPPING = CodemodPattern(old_qualname="collections.Mapping",
                         new_qualname="collections.abc.Mapping", kind="rewrite-attr",
                         requires_import="collections.abc")
RELOAD = CodemodPattern(old_qualname="imp.reload", new_qualname="importlib.reload",
                        kind="replace-attr-path", scope="python")
KINDS = ("import a\n"
         "import collections\n"
         "\n"
         "\n"
         "def kinds(v):\n"
         "    m = collections.Mapping\n"
         "    z = 1\n"
         "    return a.h(v, m)\n")


@pytest.fixture(autouse=True)
def _compiles(monkeypatch):
    """D's compile passes (the CLI test in ``test_state_table`` runs the real one)."""
    from pymolt.apply_check import CompileCheck

    monkeypatch.setattr("pymolt.apply_check.compile_changes", lambda root, plan: [
        CompileCheck(side="target", status="passed", files=[c.path for c in plan.changes])])


def _edit(root, rel, old, new):
    text = read(root, rel)
    assert old in text
    (root / rel).write_text(text.replace(old, new, 1), encoding="utf-8")


def _snapshot(root):
    files = [root / M, root / N, root / ".pymolt" / "apply_ledger.json",
             root / ".pymolt" / "migration_state.json"]
    return {str(p): p.read_bytes() if p.exists() else None for p in files}


# ── only the step's own change is undone ──────────────────────────────────────────────────


def test_only_the_steps_own_change_is_undone_and_everything_else_is_kept(tmp_path):
    """mech-2 changed m.py and n.py; mech-3 and two hand edits (above and below) changed m.py
    after it. Its rollback undoes its statements only: m.py by the inverse, n.py file-level."""
    plan = plan_for(tmp_path, FILES)
    assert [s.depends_on for s in plan.steps if s.executor == "pymolt"] == [[], [], []]
    apply_steps(tmp_path, plan)
    (tmp_path / M).write_text(HEAD + read(tmp_path, M) + TAIL, encoding="utf-8")
    current = read(tmp_path, M)

    result = rollback_steps(tmp_path, step="mech-2")

    assert (result.steps, result.inverse, result.notes) == (["mech-2"], ["mech-2"], [])
    assert read(tmp_path, M) == current.replace("a.k(x)", "a.h(x)")
    assert read(tmp_path, M) == HEAD + APP.replace("a.f(", "a.g(").replace("a.q(", "a.r(") + TAIL
    assert read(tmp_path, N) == OTHER
    rb = ledger(tmp_path)["events"][-1]
    assert (rb["event"], rb["step_id"], rb["restored"]) == ("rollback", "mech-2", True)
    files = {f["file"]: (f["before"], f["after"]) for f in rb["files"]}
    assert files[M] == (sha256(current), sha256(read(tmp_path, M)))
    assert get_text(tmp_path, files[M][1]) == read(tmp_path, M)          # in the blob store
    receipt = MigrationReceipt.load(tmp_path)
    statuses = {k.split(":")[1]: (s.status, s.in_effect) for k, s in receipt.steps.items()}
    assert statuses == {"mech-1": ("applied", True), "mech-2": ("rolled_back", False),
                        "mech-3": ("applied", True)}
    assert receipt.steps[f"{plan.plan_id}:mech-2"].files_restored == [M, N]
    # The later steps' entries are still found, by their anchors, in the code on disk.
    entries, _note, _l = in_effect_entries(tmp_path, receipt)
    coverage = ledger_coverage({c.path: c.old_source for c in plan.changes},
                               {c.path: read(tmp_path, c.path) for c in plan.changes},
                               entries, python_old="3.11", python_new="3.12")
    located = {(e["step_id"], bool(e["located"]["new"])) for e in coverage.entries}
    assert located == {("mech-1", True), ("mech-3", True)}


def test_a_neighbour_a_later_step_rewrote_is_still_the_slot(tmp_path):
    """mech-2's statement is next to mech-1's, which mech-1 wrote first: its slot is found
    through mech-2's rewrite of it (a later step in effect)."""
    text = "import a\n\n\ndef run(v):\n    x = a.f(v)\n    return a.h(x)\n"
    plan = plan_for(tmp_path, {M: (text, [rename("a.f", "a.g"), rename("a.h", "a.k")])})
    apply_steps(tmp_path, plan)

    result = rollback_steps(tmp_path, step="mech-1")

    assert result.inverse == ["mech-1"]
    assert read(tmp_path, M) == text.replace("a.h(", "a.k(")


def test_a_statement_a_later_step_inserted_next_to_it_is_passed_over(tmp_path):
    """mech-2 inserted ``import collections.abc`` right above mech-1's module-level statement,
    and rewrote the one below it: mech-1's slot is still found."""
    text = ("import a\n"
            "import collections\n"
            "X = a.f(1)\n"
            "Y = collections.Mapping\n")
    plan = plan_for(tmp_path, {M: (text, [rename("a.f", "a.g"), MAPPING])})
    apply_steps(tmp_path, plan)
    current = read(tmp_path, M)
    assert current.index("import collections.abc\n") < current.index("X =")

    result = rollback_steps(tmp_path, step="mech-1")

    assert result.inverse == ["mech-1"]
    assert read(tmp_path, M) == current.replace("X = a.g(1)", "X = a.f(1)")


def test_a_recorded_step_whose_file_moved_on_is_undone_alone_and_recorded_again(tmp_path):
    """Design 4.2, recorded steps: the same two phases, from the record's own texts. A person
    recorded mech-2 with their own edit; mech-3 and a hand edit changed the file after it. Its
    rollback undoes the person's edit only, and the person doing it again and recording it
    gives the files back."""
    from pymolt.workorder.record import record_step

    plan = plan_for(tmp_path, FILES)
    apply_steps(tmp_path, plan, step="mech-1")
    record_step(tmp_path, plan, "mech-2")
    _edit(tmp_path, M, "w = a.h(x)", "w = a.k(x) or a.h(x)")
    record_step(tmp_path, plan, "mech-2", done=True)
    apply_steps(tmp_path, plan, step="mech-3")
    (tmp_path / M).write_text(HEAD + read(tmp_path, M) + TAIL, encoding="utf-8")
    current = read(tmp_path, M)
    epoch = next(e for e in ledger(tmp_path)["events"] if e["step_id"] == "mech-2")
    assert (epoch["event"], [f["file"] for f in epoch["files"]]) == ("record", [M])

    result = rollback_steps(tmp_path, step="mech-2")

    assert (result.steps, result.inverse) == (["mech-2"], ["mech-2"])
    assert read(tmp_path, M) == current.replace("w = a.k(x) or a.h(x)", "w = a.h(x)")
    record_step(tmp_path, plan, "mech-2")
    _edit(tmp_path, M, "w = a.h(x)", "w = a.k(x) or a.h(x)")
    record_step(tmp_path, plan, "mech-2", done=True)
    assert read(tmp_path, M) == current


def test_rolling_back_the_last_change_is_still_file_level(tmp_path):
    plan = plan_for(tmp_path, FILES)
    apply_steps(tmp_path, plan)

    result = rollback_steps(tmp_path, step="mech-3")

    assert (result.steps, result.inverse) == (["mech-3"], [])
    rb = ledger(tmp_path)["events"][-1]
    edit = plan.step("mech-3").edit_for(M)
    assert (rb["files"][0]["before"], rb["files"][0]["after"]) == (
        edit.after_sha256, edit.before_sha256)


def test_cascade_undoes_the_dependents_then_the_step_by_its_inverse(tmp_path):
    """mech-2 rewrites mech-1's statement (a dependent); mech-3 changed the file after both and
    stays: --cascade undoes mech-2 then mech-1, each by its inverse."""
    text = ("import a\n\n\ndef run(v):\n    x = a.f(v)\n    z = 1\n    return a.q(x)\n")
    plan = plan_for(tmp_path, {M: (text, [rename("a.f", "a.g"), rename("a.g", "a.m"),
                                          rename("a.q", "a.r")])})
    assert plan.step("mech-2").depends_on == ["mech-1"]
    apply_steps(tmp_path, plan)
    with pytest.raises(MigrationConflictError, match="has dependents in effect: mech-2"):
        rollback_steps(tmp_path, step="mech-1")

    result = rollback_steps(tmp_path, step="mech-1", cascade=True)

    assert (result.steps, result.inverse) == (["mech-2", "mech-1"], ["mech-2", "mech-1"])
    assert read(tmp_path, M) == text.replace("a.q(", "a.r(")
    first, second = ledger(tmp_path)["events"][-2:]
    assert first["files"][0]["after"] == second["files"][0]["before"]   # the texts chain
    assert second["files"][0]["after"] == sha256(read(tmp_path, M))


# ── refusals: the slot, the lines, the compile ────────────────────────────────────────────


@pytest.mark.parametrize(("old", "new", "why"), [
    ("    y = 0\n", "    y = 0\n    v = v or 1\n", "not in its slot"),      # next to it
    ("    w = a.k(x)\n", "    w = a.k(x) + 0\n", "lines it wrote there were changed"),
    ("    w = a.k(x)\n", "    w = a.k(x)\n    w = a.k(x)\n", "ambiguous"),          # a twin
])
def test_a_statement_out_of_its_slot_or_edited_refuses_and_writes_nothing(tmp_path, old, new,
                                                                          why):
    plan = plan_for(tmp_path, FILES)
    apply_steps(tmp_path, plan)
    _edit(tmp_path, M, old, new)
    before = _snapshot(tmp_path)

    with pytest.raises(MigrationConflictError) as caught:
        rollback_steps(tmp_path, step="mech-2")

    message = str(caught.value)
    # The file-level refusal still says what moved the file on; the inverse adds why it
    # could not undo the step alone.
    assert message.startswith("step mech-2 is not the last change to its files: ")
    assert "mech-3 changed it after mech-2, and its own change cannot be undone alone" in message
    assert why in message
    assert "--force" in caught.value.hint
    assert _snapshot(tmp_path) == before


def test_a_scope_defined_twice_has_no_slot(tmp_path):
    plan = plan_for(tmp_path, FILES)
    apply_steps(tmp_path, plan)
    (tmp_path / M).write_text(read(tmp_path, M) + "\n\ndef run(v):\n    return v\n",
                              encoding="utf-8")

    with pytest.raises(MigrationConflictError, match="scope run is defined 2 times"):
        rollback_steps(tmp_path, step="mech-2")


def test_an_inverse_that_does_not_compile_on_the_target_is_refused(tmp_path, monkeypatch):
    from pymolt.apply_check import CompileCheck

    seen = []

    def broken(root, plan):
        seen.extend((c.path, c.old_source, c.new_source) for c in plan.changes)
        return [CompileCheck(side="target", status="failed", files=[M],
                             failed={M: "SyntaxError: invalid syntax"})]

    plan = plan_for(tmp_path, FILES)
    apply_steps(tmp_path, plan)
    monkeypatch.setattr("pymolt.apply_check.compile_changes", broken)
    before = _snapshot(tmp_path)

    with pytest.raises(MigrationConflictError, match="does not compile on the target"):
        rollback_steps(tmp_path, step="mech-2")

    assert [path for path, _o, _n in seen] == [M]      # the inverse only: n.py is file-level
    assert _snapshot(tmp_path) == before


def test_a_compile_check_that_could_not_run_is_noted(tmp_path, monkeypatch):
    from pymolt.apply_check import CompileCheck

    plan = plan_for(tmp_path, FILES)
    apply_steps(tmp_path, plan)
    monkeypatch.setattr("pymolt.apply_check.compile_changes", lambda root, plan: [
        CompileCheck(side="target", status="skipped", reason="no interpreter for Python 3.12")])

    result = rollback_steps(tmp_path, step="mech-2")

    assert result.inverse == ["mech-2"]
    assert result.notes == [f"{M}: not compiled on the target (no interpreter for Python 3.12)"]


def test_texts_missing_from_the_store_refuse_the_inverse(tmp_path):
    """mech-2 applied first is re-based: the plan cannot rebuild the text it wrote."""
    plan = plan_for(tmp_path, FILES)
    assert apply_steps(tmp_path, plan, step="mech-2").rebased == ["mech-2"]
    apply_steps(tmp_path, plan)
    event = next(e for e in ledger(tmp_path)["events"] if e["step_id"] == "mech-2")
    after = next(f["after"] for f in event["files"] if f["file"] == M)
    (tmp_path / ".pymolt" / "blobs" / f"{after}.gz").unlink()

    with pytest.raises(MigrationConflictError, match="are not in the store"):
        rollback_steps(tmp_path, step="mech-2")


# ── imports the step inserted ─────────────────────────────────────────────────────────────


def _imports(root, text=KINDS, rel=M, extra=None):
    files = {rel: (text, [MAPPING, rename("a.h", "a.k")])}
    files.update(extra or {})
    plan = plan_for(root, files)
    assert plan.step("mech-1").edit_for(rel).effect.imports_inserted
    apply_steps(root, plan)
    return plan


def test_an_import_nothing_uses_any_more_is_removed(tmp_path):
    _imports(tmp_path)
    assert "import collections.abc\n" in read(tmp_path, M)

    result = rollback_steps(tmp_path, step="mech-1")

    assert (result.inverse, result.notes) == (["mech-1"], [])
    assert read(tmp_path, M) == KINDS.replace("a.h(", "a.k(")


@pytest.mark.parametrize("use", [
    "\n\ndef extra():\n    return collections.abc.Sequence\n",    # the submodule it loads
    "\n\nSEQ: 'collections.abc.Sequence' = None\n",               # a string annotation
])
def test_an_import_a_hand_edit_uses_stays(tmp_path, use):
    _imports(tmp_path)
    (tmp_path / M).write_text(read(tmp_path, M) + use, encoding="utf-8")

    result = rollback_steps(tmp_path, step="mech-1")

    assert result.inverse == ["mech-1"]
    assert result.notes == [f"{M}: `import collections.abc`, which mech-1 wrote, stays: "
                            "something else uses it now"]
    assert read(tmp_path, M) == (KINDS.replace("import collections\n",
                                               "import collections\nimport collections.abc\n")
                                 .replace("a.h(", "a.k(") + use)


def test_an_import_a_later_runs_step_uses_stays(tmp_path):
    """R2 (a re-plan after R1) rewrites another statement to use the import R1's mech-1
    inserted, so R2 inserts none of its own. Rolling back R1's mech-1 keeps it."""
    text = KINDS + "\n\nITER = collections.Iterable\n"
    plan1 = _imports(tmp_path, text=text)
    iterable = CodemodPattern(old_qualname="collections.Iterable",
                              new_qualname="collections.abc.Iterable", kind="rewrite-attr",
                              requires_import="collections.abc")
    plan2 = plan_next(tmp_path, {M: [iterable]})
    assert not plan2.step("mech-1").edit_for(M).effect.imports_inserted
    apply_steps(tmp_path, plan2)

    result = rollback_steps(tmp_path, run_id=plan1.plan_id, step="mech-1")

    assert result.inverse == ["mech-1"] and len(result.notes) == 1
    assert read(tmp_path, M) == (
        text.replace("import collections\n", "import collections\nimport collections.abc\n")
        .replace("a.h(", "a.k(").replace("collections.Iterable", "collections.abc.Iterable"))


def test_an_import_a_later_step_of_the_run_uses_makes_it_a_dependent(tmp_path):
    """In one run, a later step that needs the inserted import depends on its step (1.5.3 rule
    2): the step is refused, or rolled back with it under --cascade."""
    text = KINDS + "\n\nITER = collections.Iterable\n"
    iterable = CodemodPattern(old_qualname="collections.Iterable",
                              new_qualname="collections.abc.Iterable", kind="rewrite-attr",
                              requires_import="collections.abc")
    plan = plan_for(tmp_path, {M: (text, [MAPPING, rename("a.h", "a.k"), iterable])})
    assert plan.step("mech-3").depends_on == ["mech-1"]
    apply_steps(tmp_path, plan)

    with pytest.raises(MigrationConflictError, match="has dependents in effect: mech-3"):
        rollback_steps(tmp_path, step="mech-1")
    result = rollback_steps(tmp_path, step="mech-1", cascade=True)

    assert result.steps == ["mech-3", "mech-1"]
    assert read(tmp_path, M) == text.replace("a.h(", "a.k(")


INIT = "pkg/__init__.py"
RELOADER = ("import a\n"
            "import imp\n"
            "\n"
            "\n"
            "def again(mod):\n"
            "    return imp.reload(mod)\n"
            "\n"
            "\n"
            "def run(v):\n"
            "    return a.h(v)\n")


def _reloader(root, extra: str = ""):
    plan = plan_for(root, {INIT: (RELOADER + extra, [RELOAD, rename("a.h", "a.k")])})
    edit = plan.step("mech-1").edit_for(INIT)
    assert edit.hunks[0].old == ["import imp\n"] and edit.hunks[0].new == ["import importlib\n"]
    apply_steps(root, plan)
    return plan


@pytest.mark.parametrize("user", ["from pkg import importlib\n", "from . import importlib\n",
                                  "import pkg\n\nRELOAD = pkg.importlib.reload\n"])
def test_a_package_import_another_module_imports_from_it_stays(tmp_path, user):
    """A re-export: after mech-1 rewrote ``import imp`` to ``import importlib`` in
    ``pkg/__init__.py``, a hand edit made ``pkg/user.py`` import it from the package. The
    rollback puts ``import imp`` back and keeps ``import importlib`` next to it."""
    _reloader(tmp_path)
    (tmp_path / "pkg" / "user.py").write_text(user, encoding="utf-8")

    result = rollback_steps(tmp_path, step="mech-1")

    assert result.notes == [f"{INIT}: `import importlib`, which mech-1 wrote, stays: "
                            "something else uses it now"]
    assert read(tmp_path, INIT) == RELOADER.replace(
        "import imp\n", "import importlib\nimport imp\n").replace("a.h(", "a.k(")


@pytest.mark.parametrize("names", ['["again", "importlib"]', "('importlib',)"])
def test_a_package_import_listed_in_all_stays(tmp_path, names):
    _reloader(tmp_path, extra=f"\n\n__all__ = {names}\n")

    result = rollback_steps(tmp_path, step="mech-1")

    assert len(result.notes) == 1 and "import importlib" in result.notes[0]
    assert "import importlib\n" in read(tmp_path, INIT)


def test_a_package_import_nothing_imports_is_removed_and_the_removed_one_comes_back(tmp_path):
    _reloader(tmp_path, extra='\n\n__all__ = ["again"]\n')
    (tmp_path / "pkg" / "user.py").write_text("from pkg import again\n", encoding="utf-8")

    result = rollback_steps(tmp_path, step="mech-1")

    assert result.notes == []
    assert read(tmp_path, INIT) == (RELOADER + '\n\n__all__ = ["again"]\n').replace(
        "a.h(", "a.k(")


def test_a_star_import_cannot_tell_and_refuses():
    before = "import a\n\n\ndef f():\n    return a.x\n"
    after = "import a\nfrom b import *\n\n\ndef f():\n    return y\n"
    current = after + "\n\nZ = 1\n"

    got = invert(M, before, after, current)

    assert not got.ok and "`*` import" in got.reasons[0]


# ── the metamorphic property ──────────────────────────────────────────────────────────────


def _state(root, plan):
    files = {c.path: read(root, c.path) for c in plan.changes}
    entries, _note, _l = in_effect_entries(root, MigrationReceipt.load(root))
    coverage = ledger_coverage({c.path: c.old_source for c in plan.changes}, files, entries,
                               python_old="3.11", python_new="3.12")
    located = sorted((e["file"], e["step_id"], tuple(e["located"]["old"]),
                      tuple(e["located"]["new"])) for e in coverage.entries)
    return files, located


# B inserts an import; A and C change statements around the one B rewrites.
IMPORTS = KINDS + "\n\ndef again(mod):\n    return a.q(mod)\n"


@pytest.mark.parametrize(("files", "middle"), [
    (FILES, "mech-2"),
    ({M: (IMPORTS, [rename("a.h", "a.k"), MAPPING, rename("a.q", "a.r")])}, "mech-2"),
], ids=["statements", "an inserted import"])
def test_apply_a_b_c_then_roll_back_b_equals_applying_a_and_c_alone(tmp_path, files, middle):
    straight, alone = tmp_path / "abc", tmp_path / "ac"
    straight.mkdir()
    alone.mkdir()
    plan = plan_for(straight, files)
    ids = [s.id for s in plan.steps if s.executor == "pymolt"]
    assert len(ids) == 3 and not any(s.depends_on for s in plan.steps)
    apply_steps(straight, plan)
    assert rollback_steps(straight, step=middle).inverse == [middle]

    plan_alone = plan_for(alone, files)
    for sid in ids:
        if sid != middle:
            apply_steps(alone, plan_alone, step=sid)

    assert _state(straight, plan) == _state(alone, plan_alone)
    # And the step applied again gives the straight apply back.
    apply_steps(straight, plan, step=middle)
    assert {c.path: read(straight, c.path) for c in plan.changes} == {
        c.path: c.new_source for c in plan.changes}


# ── the whole run after an inverse ────────────────────────────────────────────────────────


def test_apply_after_an_inverse_sees_no_hand_edit(tmp_path):
    """The file a rollback by the inverse wrote is pymolt's text: a no-op apply does not take
    it for a hand edit, and a real one after it still is."""
    plan = plan_for(tmp_path, FILES)
    apply_steps(tmp_path, plan)
    rollback_steps(tmp_path, step="mech-2")

    assert apply_steps(tmp_path, plan, step="mech-1").notes == []
    (tmp_path / M).write_text(read(tmp_path, M) + TAIL, encoding="utf-8")
    assert apply_steps(tmp_path, plan, step="mech-1").notes == [
        f"{M} changed since its last step wrote it: verify judges the changed lines as hand "
        "edits"]


def test_the_whole_run_rolls_back_after_an_inverse(tmp_path):
    """The run's events on the file chain through the inverse's rollback: `rollback .` restores
    the text before the run, and its events name the one write."""
    plan = plan_for(tmp_path, FILES)
    apply_steps(tmp_path, plan)
    rollback_steps(tmp_path, step="mech-2")
    current = read(tmp_path, M)

    result = rollback_steps(tmp_path)

    assert result.steps == ["mech-3", "mech-1"]
    assert (read(tmp_path, M), read(tmp_path, N)) == (APP, OTHER)
    last = ledger(tmp_path)["events"][-2:]
    assert [(f["before"], f["after"]) for e in last for f in e["files"]] == [
        (sha256(current), sha256(APP))] * 2


def test_the_whole_run_refuses_a_hand_edit_before_an_inverse(tmp_path):
    plan = plan_for(tmp_path, FILES)
    apply_steps(tmp_path, plan)
    (tmp_path / M).write_text(read(tmp_path, M) + TAIL, encoding="utf-8")
    rollback_steps(tmp_path, step="mech-2")

    with pytest.raises(MigrationConflictError, match="edited between steps"):
        rollback_steps(tmp_path)
    assert read(tmp_path, M).endswith(TAIL)


# ── a manifest ────────────────────────────────────────────────────────────────────────────


def test_a_manifest_pin_whose_file_moved_on_is_undone_by_its_lines(tmp_path):
    pinned = "# app\na==1.0\nrequests\n"
    plan = plan_for(tmp_path, {M: (APP, RECIPES)}, manifest=pinned, target="a==2.0\n")
    apply_steps(tmp_path, plan)
    (tmp_path / "requirements.txt").write_text(
        read(tmp_path, "requirements.txt").replace("requests\n", "requests==2.31.0\nidna\n"),
        encoding="utf-8")

    result = rollback_steps(tmp_path, step="env-1")

    assert result.inverse == ["env-1"]
    assert read(tmp_path, "requirements.txt") == "# app\na==1.0\nrequests==2.31.0\nidna\n"


def test_a_manifest_pin_whose_line_was_edited_is_refused(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)}, manifest="a==1.0\n", target="a==2.0\n")
    apply_steps(tmp_path, plan)
    (tmp_path / "requirements.txt").write_text("a==2.1\n", encoding="utf-8")

    with pytest.raises(MigrationConflictError, match="lines it wrote there were changed"):
        rollback_steps(tmp_path, step="env-1")


# ── the inverse alone ─────────────────────────────────────────────────────────────────────


def test_invert_a_text_that_has_not_moved_on_is_the_before_text():
    after = APP.replace("a.f(", "a.g(")
    assert invert(M, APP, after, after).text == APP


def test_invert_follows_a_neighbour_through_later_steps_only_when_told():
    text = "import a\n\n\ndef run(v):\n    x = a.f(v)\n    return a.h(x)\n"
    t1 = text.replace("a.f(", "a.g(")
    t2 = t1.replace("a.h(", "a.k(")

    assert not invert(M, text, t1, t2).ok                       # a hand edit, as far as it knows
    later = [make_edit(M, t1, t2).effect]
    assert invert(M, text, t1, t2, later=later).text == text.replace("a.h(", "a.k(")

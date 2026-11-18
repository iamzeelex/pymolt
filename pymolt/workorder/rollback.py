"""``pymolt rollback``: end steps' epochs and restore the texts they wrote (design 4.1, 4.2).

HOST side. A rollback is one transaction under the migration lock, like an apply: its files are
named in the run's ``state.json`` before the first write, every text it writes or replaces is
in the blob store, and a failure after the write puts the files back.

**One step** (``--step ID``), per file of the step's current epoch, the first that applies:

1. **file-level** (phase 1): the file holds the epoch's after-text and no later step in effect
   changed it since. The epoch's before-text comes back from the blob store.
2. **inverse effect** (phase 2, ``workorder.inverse``): the file moved on, through later steps or
   hand edits elsewhere. Only the epoch's own change is undone, from its before- and after-text:
   its lines must be in the file unchanged, its new statements must resolve in their slots
   (their neighbours as the epoch left them, or as a later step in effect rewrote them), an
   import it inserted goes only when nothing uses it any more, and the result must parse, give
   back the epoch's old statements in their slots, and compile on the target (D).
3. Otherwise the step is refused, naming the later steps or the edits on its files and why the
   inverse could not be made. Nothing is written.

A step in effect that depends on it is refused too, unless ``--cascade`` (a person only), which
rolls the dependents back first, latest first. A step of an earlier run is refused while a step
of a later run in effect builds on its statements.

**The whole run** (``pymolt rollback .``: the open run; ``--run-id R``: another): every step of the
run with a current epoch, latest first. Each file those steps touched goes back to its text
before the run's events on it; the file must hold the text the run's last event on it left, and
the run's events on it (its steps' epochs, and the rollbacks of its steps) must chain since,
unless ``--force``, which restores the text before the run's first event on the file and
discards everything since (files the run never touched are never written). A run is refused
while a later run's step in effect touched one of its files: roll that run back first.

A step whose epoch changed no file keeps it (``restored: false``), as v1 kept a run that changed
nothing in effect.

**The post-migration capture** (row 5) is set aside, kept among the diagnostics, only when a
rolled-back step's new statements resolve in the capture's source snapshot and the capture was
taken after the step's epoch: it recorded the code the rollback took away. Otherwise it still
describes the code on disk and stays. ``--force`` sets it aside when a restored file is in its
snapshot.

Under ``--agent``, ``--force`` and ``--cascade`` are refused, and so is everything else: an agent
rolls a step back only on a plan a person approved for agents, which this pymolt does not record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pymolt.migration_plan import (
    Invocation,
    InvocationFile,
    MigrationConflictError,
    MigrationPlan,
    MigrationPlanError,
    _ledger_steps,
    _load_run_state,
    _migration_lock,
    _now,
    _project_file,
    _pythons,
    _restore_ledger,
    _run_dir,
    _save_run_state,
    _sha256,
    recover_invocation,
)


@dataclass
class RollbackResult:
    """What one rollback did. ``steps``: the steps whose epoch it ended or (``kept``) kept;
    ``inverse``: of ``steps``, those undone by their inverse effect in a file that moved on
    (``notes`` names the imports they inserted that stay); ``files_restored``: the project files
    it rewrote; ``post``: ``kept`` / ``set_aside`` / None (there was no post-migration
    capture)."""

    plan: MigrationPlan
    run_id: str
    rolled_back_at: str
    steps: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    inverse: list[str] = field(default_factory=list)
    files_restored: list[str] = field(default_factory=list)
    post: str | None = None
    post_trace: str | None = None
    notes: list[str] = field(default_factory=list)


AGENT_REFUSAL = ("under --agent, pymolt rolls a step back only on a plan a person approved for "
                 "agents, and this pymolt records no approval: a person runs `pymolt rollback`")


def rollback_steps(project_dir: str | Path, *, run_id: str | None = None,
                   step: str | None = None, force: bool = False, cascade: bool = False,
                   agent: bool = False) -> RollbackResult:
    """Roll back one step (``step``) or the run's steps in effect (module docstring). Raises
    ``MigrationConflictError`` (exit 2) or ``MigrationPlanError`` / ``OSError`` (exit 3)."""
    if agent and (force or cascade):
        raise MigrationConflictError(
            f"--{'force' if force else 'cascade'} is refused under --agent: a person discards "
            "edits or rolls back dependent steps")
    if agent:
        raise MigrationConflictError(AGENT_REFUSAL)
    if step is not None and force:
        raise MigrationConflictError("--force rolls back a whole run: it does not go with --step")
    if cascade and step is None:
        raise MigrationConflictError("--cascade goes with --step")
    with _migration_lock(project_dir, "rollback"):
        root = Path(project_dir).resolve()
        if step is not None:
            return _rollback_step(root, step, run_id=run_id, cascade=cascade)
        return _rollback(root, run_id=run_id, force=force)


def _load(root: Path, run_id: str | None):
    from pymolt.migration_state import MigrationReceipt

    receipt = MigrationReceipt.load(root)
    open_runs = [r.run_id for r in (receipt.runs if receipt else []) if not r.closed]
    effective = run_id or (open_runs[-1] if open_runs else None) or (
        receipt.run_id if receipt else None)
    if not effective:
        raise MigrationConflictError("no applied migration is available to roll back")
    run_dir = _run_dir(root, effective)
    state = _load_run_state(run_dir / "state.json")
    if state is None:
        raise MigrationConflictError(f"migration {effective[:8]} is not rollback-ready (missing)")
    plan = MigrationPlan.load(root, run_dir / "plan.json")
    if plan is None:
        raise MigrationPlanError(f"frozen plan for {effective[:8]} is unreadable")
    recovered = recover_invocation(root, state, plan)
    if recovered is not None:
        state = recovered
        receipt = MigrationReceipt.load(root)
    return receipt, effective, state, plan


def _ledger(root: Path, receipt):
    from pymolt.apply_ledger import Ledger, ledger_path, load_ledger

    ledger = load_ledger(root, receipt=receipt)
    if ledger is None and ledger_path(root).exists():
        raise MigrationPlanError(f"the apply ledger cannot be read: {ledger_path(root)}")
    return ledger if ledger is not None else Ledger()


def _epoch_events(ledger, run_id: str, state, plan: MigrationPlan) -> list:
    """The run's steps with a current epoch: their opening events, latest first. A run from
    before the ledger (no event at all) is read as one epoch of ``v1`` over the frozen plan."""
    from pymolt.apply_ledger import EventFile, LedgerEvent

    by_seq = {e.seq: e for e in ledger.events}
    current = [by_seq[seq] for (run, _sid), seq in ledger.current_epochs().items()
               if run == run_id and seq in by_seq]
    if current or any(e.run_id == run_id for e in ledger.events):
        return sorted(current, key=lambda e: -e.seq)
    if state.status == "applied" and plan.changes:
        return [LedgerEvent(seq=0, event="apply", run_id=run_id, step_id="v1", at="",
                            files=[EventFile(file=c.path, before=c.old_sha256,
                                             after=c.new_sha256) for c in plan.changes])]
    return []


class _Texts:
    """Texts by sha256: the blob store first, then every text the frozen plan can rebuild."""

    def __init__(self, root: Path, plan: MigrationPlan):
        self.root = root
        self._plan = plan
        self._known: dict[str, str] | None = None

    def get(self, digest: str | None) -> str | None:
        from pymolt.workorder.blobs import get_text

        if not digest:
            return None
        text = get_text(self.root, digest)
        if text is not None:
            return text
        if self._known is None:
            self._known = {}
            for change in self._plan.changes:
                self._known[change.old_sha256] = change.old_source
                self._known[change.new_sha256] = change.new_source
            try:
                for _step, files in _ledger_steps(self._plan):
                    for _path, before, after in files:
                        self._known[_sha256(before)] = before
                        self._known[_sha256(after)] = after
            except Exception:  # noqa: BLE001 - only a fallback
                pass
        return self._known.get(digest)


def _read(root: Path, rel: str) -> str:
    path = _project_file(root, rel)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise MigrationPlanError(f"cannot restore missing source: {rel}") from exc


def _rollback(root: Path, *, run_id: str | None, force: bool) -> RollbackResult:
    receipt, effective, state, plan = _load(root, run_id)
    ledger = _ledger(root, receipt)
    events = _epoch_events(ledger, effective, state, plan)
    if not events:
        if receipt is not None and receipt.run_id == effective and receipt.in_effect \
                and not receipt.steps_of(effective):
            return _rollback_nothing(root, receipt, effective, state, plan)
        status = "rolled_back" if state.status == "applied" else state.status
        raise MigrationConflictError(
            f"migration {effective[:8]} is not rollback-ready ({status}): none of its steps "
            "is in effect")
    _refuse_later_runs(ledger, receipt, effective, events)
    texts = _Texts(root, plan)
    restore: dict[str, tuple[str, str]] = {}           # file -> (current text, restored text)
    whole: dict[tuple[int, str], tuple[str, str]] = {}  # events that name the whole write
    problems: list[str] = []
    for rel in dict.fromkeys(f.file for e in reversed(events) for f in e.files):
        mine = sorted((e for e in events if any(f.file == rel for f in e.files)),
                      key=lambda e: e.seq)
        files = [next(f for f in e.files if f.file == rel) for e in mine]
        current = _read(root, rel)
        if force:
            first = _first_event_on(ledger, effective, rel) or files[0]
            target = first.before
        else:
            history = _history(ledger, effective, rel) or list(zip(mine, files, strict=True))
            if _sha256(current) != history[-1][1].after:
                problems.append(f"source changed after application: {rel}")
                continue
            # The run's own events on the file since the last edit that was not one of them,
            # from the first epoch among them (a rollback's text before is not the run's start).
            start = len(history) - 1
            while start > 0 and history[start - 1][1].after == history[start][1].before:
                start -= 1
            while start < len(history) - 1 and history[start][0].event == "rollback":
                start += 1
            since = {e.seq for e, _f in history[start:]}
            if any(e.seq not in since for e in mine):
                event = history[start][0]
                name = (event.step_id if event.event in ("apply", "record")
                        else f"the rollback of {event.step_id}")
                problems.append(f"{rel} was edited between steps (before {name})")
                continue
            target = history[start][1].before
        text = texts.get(target)
        if text is None:
            raise MigrationPlanError(
                f"cannot restore {rel}: its text before run {effective[:8]} is not in the "
                "store")
        restore[rel] = (current, text)
        chained = files[0].before == target and files[-1].after == _sha256(current) and all(
            files[i + 1].before == files[i].after for i in range(len(files) - 1))
        if not force and not chained:
            # A rollback by the inverse came between: each event names the one write.
            whole.update({(e.seq, rel): (current, text) for e in mine})
    if problems:
        raise MigrationConflictError(
            "refusing to overwrite post-migration edits: " + "; ".join(problems)
            + "; pass --force only if discarding them is intentional")
    return _commit(root, receipt, effective, state, plan, ledger, events, restore, force=force,
                   texts_of=whole)


def _rollback_step(root: Path, step_id: str, *, run_id: str | None,
                   cascade: bool) -> RollbackResult:
    """The step, and with ``cascade`` its dependents first: per file, file-level (phase 1), else
    by the inverse of the epoch's effect (phase 2), else refused."""
    receipt, effective, state, plan = _load(root, run_id)
    ledger = _ledger(root, receipt)
    events = {e.step_id: e for e in _epoch_events(ledger, effective, state, plan)}
    if plan.step(step_id) is None and step_id not in events:
        raise MigrationConflictError(
            f"unknown step {step_id}: the plan of run {effective[:8]} has "
            + (", ".join(s.id for s in plan.steps) or "no step"))
    if step_id not in events:
        last = ledger.step_states().get((effective, step_id), "planned")
        raise MigrationConflictError(
            f"step {step_id} of run {effective[:8]} is not in effect ({last}): there is nothing "
            "to roll back")
    dependents = _dependents(plan, step_id, set(events))
    if dependents and not cascade:
        raise MigrationConflictError(
            f"step {step_id} has dependents in effect: {', '.join(dependents)}",
            hint=f"Roll those back first, or `pymolt rollback --step {step_id} --cascade` "
                 "rolls them back with it, latest first.")
    order = sorted((events[sid] for sid in [*dependents, step_id]), key=lambda e: -e.seq)
    _refuse_built_on(root, ledger, receipt, effective, plan, order)
    texts = _Texts(root, plan)
    inverse = _InverseContext(root, plan, texts)
    now: dict[str, str] = {}
    restore: dict[str, tuple[str, str]] = {}
    written: dict[tuple[int, str], tuple[str, str]] = {}     # (epoch, file) -> (before, after)
    inverted: set[str] = set()                                # files an inverse rewrote
    undone: list[str] = []
    problems: list[str] = []
    rolled = {e.seq for e in order}
    for event in order:
        for f in event.files:
            if f.file not in now:
                now[f.file] = _read(root, f.file)
            later = _later_events(ledger, receipt, f.file, event.seq, rolled)
            if not later and _sha256(now[f.file]) == f.after:
                text = texts.get(f.before)
                if text is None:
                    raise MigrationPlanError(
                        f"cannot restore {f.file}: the text before {event.step_id} is not in "
                        "the store")
            else:
                moved = (f"{f.file}: {', '.join(name for name, _e in later)} changed it after "
                         f"{event.step_id}" if later else
                         f"{f.file} was edited after {event.step_id} wrote it")
                result = inverse.invert(event, f, now[f.file], [e for _n, e in later])
                if not result.ok:
                    problems.append(f"{moved}, and its own change cannot be undone alone: "
                                    + "; ".join(result.reasons))
                    continue
                text = result.text
                undone.append(event.step_id)
                inverted.add(f.file)
                inverse.notes += [f"{f.file}: `{imp}`, which {event.step_id} wrote, stays: "
                                  "something else uses it now" for imp in result.kept]
            first = restore[f.file][0] if f.file in restore else now[f.file]
            restore[f.file] = (first, text)
            written[(event.seq, f.file)] = (now[f.file], text)
            now[f.file] = text
    if problems:
        raise MigrationConflictError(
            f"step {step_id} is not the last change to its files: " + "; ".join(problems),
            hint="Roll back the later steps first (`--cascade` for its dependents), or the "
                 "whole run with `pymolt rollback . --force`, which discards the later edits.")
    inverse.compile(plan, {rel: restore[rel] for rel in inverted})
    result = _commit(root, receipt, effective, state, plan, ledger, order, restore,
                     force=False, texts_of=written)
    result.inverse = [e.step_id for e in order if e.step_id in undone]
    result.notes += inverse.notes
    return result


class _InverseContext:
    """What phase 2 needs beyond the texts: the Pythons, the later steps' effects, and whether
    another project file imports a name from a module."""

    def __init__(self, root: Path, plan: MigrationPlan, texts: _Texts):
        from pymolt.workorder.rules import Pythons

        self.root, self.texts = root, texts
        base, target = _pythons(root, plan)
        self.pythons = Pythons(base=base, target=target)
        self.notes: list[str] = []
        self._effects: dict[tuple[str, str, str], Any] = {}

    def invert(self, event, f, current: str, later: list):
        from pymolt.workorder.inverse import Inverse, invert

        before, after = self.texts.get(f.before), self.texts.get(f.after)
        if before is None or after is None:
            return Inverse(f.file, reasons=[f"{f.file}: the texts {event.step_id} saw are not "
                                            "in the store"])
        effects = [self._effect(f.file, other) for other in later]
        try:
            return invert(f.file, before, after, current, pythons=self.pythons,
                          later=[e for e in effects if e is not None],
                          imported_elsewhere=self.imported_elsewhere)
        except Exception as exc:  # noqa: BLE001 - an inverse that cannot be made is refused
            return Inverse(f.file, reasons=[f"{f.file}: the inverse could not be made "
                                            f"({type(exc).__name__}: {str(exc)[:120]})"])

    def _effect(self, rel: str, event):
        """A later epoch's effect on ``rel``, from its texts (None: they are not in the store)."""
        from pymolt.workorder.edits import make_edit

        f = next((x for x in event.files if x.file == rel), None)
        if f is None or not f.before or not f.after:
            return None
        key = (rel, f.before, f.after)
        if key not in self._effects:
            before, after = self.texts.get(f.before), self.texts.get(f.after)
            effect = None
            if before is not None and after is not None:
                try:
                    effect = make_edit(rel, before, after,
                                       index_before=self.pythons.index(before),
                                       index_after=self.pythons.index(after)).effect
                except Exception:  # noqa: BLE001 - no translation: the slot check is stricter
                    effect = None
            self._effects[key] = effect
        return self._effects[key]

    def imported_elsewhere(self, rel: str, names: set[str]) -> set[str]:
        """The ``names`` another project file imports from ``rel``'s module (a re-export)."""
        return imported_elsewhere(self.root, rel, names)

    def compile(self, plan: MigrationPlan, changed: dict[str, tuple[str, str]]) -> None:
        """D's compile on the target, over the files an inverse rewrote: a failure refuses."""
        from types import SimpleNamespace

        from pymolt.apply_check import compile_changes

        changes = [SimpleNamespace(path=rel, old_source=old, new_source=new, runs_on_base=None)
                   for rel, (old, new) in changed.items() if rel.endswith(".py") and old != new]
        if not changes:
            return
        checks = compile_changes(self.root, SimpleNamespace(
            changes=changes, target_python=plan.target_python))
        broken = [f"{path} ({error})" for c in checks for path, error in c.failed.items()]
        self.notes += [f"{', '.join(c.path for c in changes)}: not compiled on the target "
                       f"({check.reason})" for check in checks if check.status == "skipped"]
        if broken:
            raise MigrationConflictError(
                "undoing the step's own change would leave source that does not compile on "
                "the target: " + "; ".join(broken),
                hint="Nothing was written. Roll back the later steps first, or the whole run "
                     "with `pymolt rollback . --force`, which discards the later edits.")


def imported_elsewhere(root: Path, rel: str, names: set[str]) -> set[str]:
    """The ``names`` that another project ``.py`` file imports from the module ``rel`` is
    (``from <module> import name``, relative or not, or a ``*`` import of it), or names as
    ``<module>.name``. A file the host cannot parse counts when it mentions the name and the
    module (it may import it)."""
    import ast
    import re

    from pymolt.codemods.apply import _iter_py_files

    module = _module_of(root, rel)
    if not names or not module:
        return set()
    leaf = module.rsplit(".", 1)[-1]
    words = {n: re.compile(rf"(?<!\w){re.escape(n)}(?!\w)") for n in names}
    found: set[str] = set()
    for path in _iter_py_files(root):
        other = path.relative_to(root).as_posix()
        if other == rel:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        hit = {n for n in names if words[n].search(text)}
        if not hit or not re.search(rf"\b{re.escape(leaf)}\b|from\s+\.", text):
            continue
        found.update(n for n in hit if f"{module}.{n}" in text)
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            found.update(hit)
            continue
        importer = _module_of(root, other) or ""
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if _absolute(importer, other, node) != module:
                continue
            for alias in node.names:
                if alias.name == "*":
                    found.update(n for n in hit if not n.startswith("_"))
                elif alias.name in hit:
                    found.add(alias.name)
        if found >= names:
            break
    return found


def _module_of(root: Path, rel: str) -> str | None:
    """The importable module name of a project file (``pkg/__init__.py`` -> ``pkg``)."""
    from pymolt.apply_check import module_for

    found = module_for(root, rel)
    if isinstance(found, tuple):
        return found[1]
    stem = Path(rel).stem
    return stem if stem.isidentifier() else None


def _absolute(importer: str, rel: str, node) -> str | None:
    """The absolute module a ``from ... import`` names, from the importing module's package."""
    if not node.level:
        return node.module
    package = importer.split(".") if importer else []
    if not rel.endswith("__init__.py"):
        package = package[:-1]
    if node.level - 1 > len(package):
        return None
    base = package[:len(package) - (node.level - 1)]
    return ".".join([*base, *([node.module] if node.module else [])]) or None


def _dependents(plan: MigrationPlan, step_id: str, live: set[str]) -> list[str]:
    """The steps in effect that depend on ``step_id``, directly or not, in plan order."""
    found: set[str] = set()
    grew = True
    while grew:
        grew = False
        for s in plan.steps:
            if s.id in live and s.id not in found and s.id != step_id \
                    and (step_id in s.depends_on or found & set(s.depends_on)):
                found.add(s.id)
                grew = True
    return [s.id for s in plan.steps if s.id in found]


def _later_events(ledger, receipt, rel: str, seq: int, rolled: set[int]) -> list[tuple]:
    """The steps in effect (of any run on disk) whose epoch touched ``rel`` after ``seq``,
    leaving out the epochs this rollback ends: ``(name, epoch event)``, in epoch order."""
    from pymolt.apply_ledger import chain_runs

    chain = chain_runs(receipt) if receipt is not None else set()
    by_seq = {e.seq: e for e in ledger.events}
    out = []
    for (run, sid), current in sorted(ledger.current_epochs().items(), key=lambda kv: kv[1]):
        event = by_seq.get(current)
        if event is None or current <= seq or current in rolled or (chain and run not in chain):
            continue
        if any(f.file == rel for f in event.files):
            out.append((sid if not chain or len(chain) == 1 else f"{sid} of run {run[:8]}",
                        event))
    return out


def _refuse_built_on(root: Path, ledger, receipt, run_id: str, plan: MigrationPlan,
                     order) -> None:
    """Row 11: a step of a later run in effect whose old statements are this step's new ones
    (it rewrote what this step wrote) keeps this step in place."""
    from pymolt.apply_ledger import chain_runs

    chain = chain_runs(receipt) if receipt is not None else set()
    new_keys: dict[str, set[str]] = {}
    for event in order:
        step = plan.step(event.step_id)
        for edit in (step.edits if step is not None else []):
            new_keys.setdefault(edit.file, set()).update(edit.effect.new_keys())
    if not new_keys:
        return
    first = min(e.seq for e in order)
    by_seq = {e.seq: e for e in ledger.events}
    for (run, sid), seq in ledger.current_epochs().items():
        if run == run_id or seq < first or (chain and run not in chain):
            continue
        other = MigrationPlan.load(root, _run_dir(root, run) / "plan.json")
        step = other.step(sid) if other is not None else None
        event = by_seq.get(seq)
        if step is None or event is None:
            continue
        for edit in step.edits:
            if edit.effect.old_keys() & new_keys.get(edit.file, set()):
                raise MigrationConflictError(
                    f"step {sid} of run {run[:8]} is in effect and rewrote what "
                    f"{', '.join(e.step_id for e in order)} wrote in {edit.file}",
                    hint=f"Roll back run {run[:8]}'s step first.")


def _history(ledger, run_id: str, rel: str) -> list[tuple]:
    """The run's events that changed ``rel`` or named its texts (its steps' epochs, ended or
    not, and their rollbacks): ``(event, its file)``, in order."""
    out = []
    for event in sorted(ledger.events, key=lambda e: e.seq):
        if event.run_id != run_id or event.event not in ("apply", "record", "rollback"):
            continue
        for f in event.files:
            if f.file == rel and f.before and f.after:
                out.append((event, f))
    return out


def _first_event_on(ledger, run_id: str, rel: str):
    for event in sorted(ledger.events, key=lambda e: e.seq):
        if event.run_id == run_id and event.event in ("apply", "record"):
            for f in event.files:
                if f.file == rel:
                    return f
    return None


def _refuse_later_runs(ledger, receipt, run_id: str, events) -> None:
    """Design 4.1 row 13: a later run's step in effect on a file this run touched."""
    files = {f.file for e in events for f in e.files}
    if not files:
        return
    from pymolt.apply_ledger import chain_runs

    chain = chain_runs(receipt) if receipt is not None else set()
    first = min((e.seq for e in events), default=0)
    by_seq = {e.seq: e for e in ledger.events}
    later = []
    for (run, sid), seq in ledger.current_epochs().items():
        event = by_seq.get(seq)
        if run == run_id or event is None or seq < first or (chain and run not in chain):
            continue
        if files & {f.file for f in event.files}:
            later.append(f"{sid} of run {run[:8]}")
    if later:
        raise MigrationConflictError(
            f"run {run_id[:8]} is not the last change to its files: "
            + ", ".join(sorted(later)) + " is in effect on them; roll that back first")


def _rollback_nothing(root: Path, receipt, run_id: str, state, plan) -> RollbackResult:
    """A run that changed no file: nothing to restore, and it stays in effect (v1)."""
    now = _now()
    out = receipt.model_copy(deep=True)
    out.status, out.rolled_back_at, out.files_restored = "rolled_back", now, []
    out.save(root)
    _save_run_state(_run_dir(root, run_id), state.model_copy(update={
        "status": "rolled_back", "rolled_back_at": now}))
    return RollbackResult(plan=plan, run_id=run_id, rolled_back_at=now, post=_post_now(root))


def _post_now(root: Path) -> str | None:
    from pymolt.verify.service import load_contract_state

    return "kept" if load_contract_state(root).post_migration is not None else None


def _commit(root: Path, receipt, run_id: str, state, plan: MigrationPlan, ledger, events,
            restore: dict[str, tuple[str, str]], *, force: bool,
            texts_of: dict[tuple[int, str], tuple[str, str]] | None = None) -> RollbackResult:
    """Write ``restore`` (file -> (current, restored)) in one transaction, with a ``rollback``
    event per step. ``texts_of``: ``(epoch, file) -> (text before, text after)`` this step's
    rollback, when one rollback rewrites a file more than once (``--cascade``) or by an inverse;
    by default each event goes from the epoch's after-text back to its before-text."""
    import uuid

    from pymolt.apply_ledger import EventFile, LedgerEvent, LedgerSnapshot, append_ledger
    from pymolt.codemods.apply import write_previews
    from pymolt.codemods.models import FilePreview
    from pymolt.verify.service import (
        load_contract_state,
        restore_archived_capture,
        set_aside_post_migration,
    )
    from pymolt.workorder.blobs import put_texts

    now = _now()
    run_dir = _run_dir(root, run_id)
    written = {rel: pair for rel, pair in restore.items() if pair[0] != pair[1]}
    records = []
    ended, kept = [], []
    for event in events:                                  # latest first
        files = [f.file for f in event.files if f.file in restore]
        if not event.files:
            kept.append(event.step_id)
        else:
            ended.append(event.step_id)
        # Each event names its own texts: the epoch's after-text back to its before-text (a
        # forced rollback: the file as it was, back to the text before the run).
        records.append((LedgerEvent(
            event="rollback", run_id=run_id, step_id=event.step_id, at=now,
            executor="pymolt", actor="pymolt", restored=bool(event.files),
            files=[EventFile(file=f.file, before=_sha256(restore[f.file][0]),
                             after=_sha256(restore[f.file][1])) if force else
                   EventFile(file=f.file, before=_sha256(texts_of[(event.seq, f.file)][0]),
                             after=_sha256(texts_of[(event.seq, f.file)][1]))
                   if texts_of and (event.seq, f.file) in texts_of else
                   EventFile(file=f.file, before=f.after, after=f.before)
                   for f in event.files if f.file in files]), []))
    invocation = Invocation(
        id=uuid.uuid4().hex, operation="rollback", status="prepared", at=now,
        steps=[e.step_id for e in events], ledger_next_seq=ledger.next_seq(),
        files=[InvocationFile(file=rel, before=_sha256(old), after=_sha256(new))
               for rel, (old, new) in written.items()])
    put_texts(root, [t for old, new in written.values() for t in (old, new)]
              + [t for pair in (texts_of or {}).values() for t in pair])
    _save_run_state(run_dir, state.model_copy(update={"invocation": invocation}))
    previews = [FilePreview(path=str(_project_file(root, rel)), old_source=old,
                            new_source=new, sites=0) for rel, (old, new) in written.items()]
    contract = load_contract_state(root)
    original_contract = contract.model_copy(deep=True)
    original_receipt = receipt.model_copy(deep=True) if receipt is not None else None
    ledger_before = LedgerSnapshot.take(root)
    state_path = root / ".pymolt" / "contract_state.json"
    write_previews(previews)
    archived: str | None = None
    restored = list(written)
    try:
        if records:
            append_ledger(root, records, ledger=ledger)
        steps_state = {**(state.steps or {}), **{e.step_id: "rolled_back" for e in events}}
        new_receipt = None
        if receipt is not None:
            new_receipt = receipt_after_rollback(root, receipt, plan, run_id, now, events,
                                                 restore, invocation.id)
            new_receipt.save(root)
        live = new_receipt is not None and any(
            st.in_effect for st in new_receipt.steps_of(run_id).values())
        _save_run_state(run_dir, state.model_copy(update={
            "status": "applied" if live else "rolled_back", "rolled_back_at": now,
            "steps": steps_state,
            "invocation": invocation.model_copy(update={"status": "done"})}))
        # The post-migration capture stays while the code it recorded is still on disk;
        # otherwise it is set aside, kept among the diagnostics (row 5).
        post = "kept" if contract.post_migration is not None else None
        post_trace = contract.post_migration.trace_path if contract.post_migration else None
        if contract.post_migration is not None and restored and _capture_recorded(
                contract.post_migration, plan, events, restored, force=force):
            archived = set_aside_post_migration(root, contract, run_id)
            contract.save(state_path)
            aside = contract.diagnostic_captures[-1]
            post, post_trace = "set_aside", aside.trace_path
    except BaseException as metadata_exc:
        _restore_ledger(ledger_before)
        try:
            write_previews([FilePreview(path=p.path, old_source=p.new_source,
                                        new_source=p.old_source, sites=0) for p in previews])
        except BaseException as restore_exc:
            raise RuntimeError(
                "source rollback succeeded, metadata failed, and restoring the applied "
                f"state was incomplete: {restore_exc}"
            ) from metadata_exc
        try:
            _save_run_state(run_dir, state)
            if original_receipt is not None:
                original_receipt.save(root)
            if archived is not None and original_contract.post_migration is not None:
                restore_archived_capture(archived, original_contract.post_migration.trace_path)
            original_contract.save(state_path)
        except OSError:
            pass
        raise
    return RollbackResult(plan=plan, run_id=run_id, rolled_back_at=now, steps=ended,
                          kept=kept, files_restored=restored, post=post, post_trace=post_trace)


def _capture_recorded(post, plan: MigrationPlan, events, restored: list[str], *,
                      force: bool) -> bool:
    """Whether the post-migration capture recorded code this rollback took away (module
    docstring). A capture with no source snapshot cannot tell: it is set aside when it was taken
    after one of the steps, as before snapshots existed."""
    from pymolt.verify.service import load_sources

    snapshot = load_sources(post.sources_path) if post.sources_path else None
    if force:
        return snapshot is None or any(rel in snapshot for rel in restored)
    for event in events:
        if not event.files or not any(f.file in restored for f in event.files):
            continue
        if not _postdates(post.captured_at, event.at):
            continue
        if snapshot is None:
            return True
        step = plan.step(event.step_id)
        for edit in (step.edits if step is not None else []):
            text = snapshot.get(edit.file)
            if text is None:
                continue
            if edit.anchors_new:
                from pymolt.workorder.rules import Pythons

                index = Pythons(target=plan.target_python).index(text)
                if index is not None and any(index.resolve(a).ok for a in edit.anchors_new):
                    return True
            elif _sha256(text) == edit.after_sha256:
                return True
    return False


def _postdates(captured_at: str | None, at: str | None) -> bool:
    from datetime import UTC, datetime

    if not captured_at or not at:
        return True
    try:
        captured, done = datetime.fromisoformat(captured_at), datetime.fromisoformat(at)
    except ValueError:
        return True
    captured = captured if captured.tzinfo else captured.replace(tzinfo=UTC)
    done = done if done.tzinfo else done.replace(tzinfo=UTC)
    return captured >= done


def receipt_after_rollback(root: Path, receipt, plan: MigrationPlan, run_id: str, now: str,
                           events, restore: dict[str, Any], invocation: str):
    """The receipt after rolling back ``events`` of ``run_id``: each step whose epoch ended is
    ``rolled_back`` with no epoch; one whose epoch changed no file keeps it (in effect). The
    schema-1 summary changes only when ``run_id`` is the latest run, as in v1."""
    from pymolt.migration_state import StepReceipt
    from pymolt.workorder.apply import summarize_run

    out = receipt.model_copy(deep=True)
    out.last_invocation = invocation
    restored_all = [rel for rel, (old, new) in restore.items() if old != new]
    for event in events:
        key = f"{run_id}:{event.step_id}"
        step = out.steps.get(key) or StepReceipt(
            status="applied", epoch=event.seq or None, executor="pymolt", actor="pymolt",
            files=[f.file for f in event.files])
        mine = [f.file for f in event.files if f.file in restore]
        out.steps[key] = step.model_copy(update={
            "status": "rolled_back", "rolled_back_at": now, "files_restored": mine,
            "epoch": None if event.files else step.epoch})
    if out.run_id == run_id:
        # ``files_changed`` keeps its v1 value while no step is in effect (``summarize_run``).
        out.status, out.rolled_back_at, out.files_restored = "rolled_back", now, restored_all
        summarize_run(root, out, plan)
    return out


__all__ = ["RollbackResult", "receipt_after_rollback", "rollback_steps"]

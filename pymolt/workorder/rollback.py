"""``pymolt rollback``: end steps' epochs and restore the texts they wrote (design 4.1, 4.2).

HOST side. A rollback is one transaction under the migration lock, like an apply: its files are
named in the run's ``state.json`` before the first write, every text it writes or replaces is
in the blob store, and a failure after the write puts the files back.

**The whole run** (``pymolt rollback .``, or ``--run-id R``): every step of the run with a current
epoch, in reverse order. Each file those steps touched goes back to its text before the first
of them; the file must hold the last one's after-text, and the steps' texts must chain (no edit
between two of them), unless ``--force``, which restores the text before the run's first event
on the file and discards everything since. A run is refused while a later run's step in effect
touched one of its files: roll that run back first. A step whose epoch changed no file keeps
it (``restored: false``), as v1 kept a run that changed nothing in effect.

The post-migration capture is set aside (kept among the diagnostics) when the rollback restored
a file, and stays otherwise.
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
    _restore_ledger,
    _run_dir,
    _save_run_state,
    _sha256,
    recover_invocation,
)


@dataclass
class RollbackResult:
    """What one rollback did. ``steps``: the steps whose epoch it ended or (``kept``) kept;
    ``files_restored``: the project files it rewrote; ``post``: ``kept`` / ``set_aside`` / None
    (there was no post-migration capture)."""

    plan: MigrationPlan
    run_id: str
    rolled_back_at: str
    steps: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    files_restored: list[str] = field(default_factory=list)
    post: str | None = None
    post_trace: str | None = None
    notes: list[str] = field(default_factory=list)


def rollback_steps(project_dir: str | Path, *, run_id: str | None = None,
                   force: bool = False) -> RollbackResult:
    """Roll back the run's steps in effect (module docstring). Raises
    ``MigrationConflictError`` (exit 2) or ``MigrationPlanError`` / ``OSError`` (exit 3)."""
    with _migration_lock(project_dir, "rollback"):
        return _rollback(Path(project_dir).resolve(), run_id=run_id, force=force)


def _load(root: Path, run_id: str | None):
    from pymolt.migration_state import MigrationReceipt

    receipt = MigrationReceipt.load(root)
    effective = run_id or (receipt.run_id if receipt else None)
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
            target = files[0].before
            broken = [mine[i + 1].step_id for i in range(len(files) - 1)
                      if files[i + 1].before != files[i].after]
            if _sha256(current) != files[-1].after:
                problems.append(f"source changed after application: {rel}")
                continue
            if broken:
                problems.append(f"{rel} was edited between steps (before {', '.join(broken)})")
                continue
        text = texts.get(target)
        if text is None:
            raise MigrationPlanError(
                f"cannot restore {rel}: its text before run {effective[:8]} is not in the "
                "store")
        restore[rel] = (current, text)
    if problems:
        raise MigrationConflictError(
            "refusing to overwrite post-migration edits: " + "; ".join(problems)
            + "; pass --force only if discarding them is intentional")
    return _commit(root, receipt, effective, state, plan, ledger, events, restore)


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
            restore: dict[str, tuple[str, str]]) -> RollbackResult:
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
        records.append((LedgerEvent(
            event="rollback", run_id=run_id, step_id=event.step_id, at=now,
            executor="pymolt", actor="pymolt", restored=bool(event.files),
            files=[EventFile(file=rel, before=_sha256(restore[rel][0]),
                             after=_sha256(restore[rel][1])) for rel in files]), []))
    invocation = Invocation(
        id=uuid.uuid4().hex, operation="rollback", status="prepared", at=now,
        steps=[e.step_id for e in events], ledger_next_seq=ledger.next_seq(),
        files=[InvocationFile(file=rel, before=_sha256(old), after=_sha256(new))
               for rel, (old, new) in written.items()])
    put_texts(root, [t for old, new in written.values() for t in (old, new)])
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
        # The post-migration capture stays while the code it recorded is still on disk (the
        # rollback restored no file); otherwise it is set aside, kept among the diagnostics.
        post = "kept" if contract.post_migration is not None else None
        post_trace = contract.post_migration.trace_path if contract.post_migration else None
        if contract.post_migration is not None and restored:
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

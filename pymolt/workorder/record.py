"""``pymolt apply --step ID --record``: record an agent's or a person's edit as a step (design 3.3).

HOST side. pymolt never writes an agent or person step; it records what was written, with the
claim verify judges it by. Three phases, each under the migration lock:

- **``--record``** opens the record. Refused unless the step is planned, rolled back, stale or
  recorded (a re-record), its dependencies are in effect, and no other step of the run is
  ``in_progress``: two open records would attribute each other's edits, so a run has at most
  one. It snapshots every project ``.py`` file (``codemods.apply._iter_py_files``' skip rules)
  and the step's own files into the blob store, which de-duplicates them, and lists them in a
  per-step manifest, ``migrations/<run>/steps/<id>/snapshot-<seq>.json`` = ``{files: {file:
  sha256}}``. Past ``SNAPSHOT_NEW_BYTES_CAP`` of new blob bytes the remaining files are listed
  by hash only. No ledger event; the step receipt becomes ``in_progress``.
- **``--record --done``** diffs the snapshot against the files now. For a re-record, a file the
  step's previous epoch touched is diffed from that epoch's before-text instead, so the new
  epoch describes the whole step. It appends a ``record`` event (the new epoch; the previous one
  is superseded) and its entries: anchors on both texts, the claim and expectation within the
  ladder (``check_claim``), ``basis: manual``, the actor, and the step's symbol plus ``--pair``
  as pairing anchors. A file listed by hash only has no known before-text: it is left
  unrecorded (a hand edit). The changed Python files are compiled (the target, and the base when
  the step runs there); a failure goes into ``checks`` and the edit stays, since pymolt did not
  write it. No change is refused, except for a ``review`` or ``env`` step, which records a
  completion with no entries.
- **``--done`` with no ``--record`` first**: each of the step's files is diffed from its last
  known state: the after-text of the latest ledger event on it, whatever that event is (an
  apply, a record, a restoring rollback, a run of another plan), from the blob store; its text
  at plan time only when the ledger has no event on it. A text the store no longer holds is
  refused, never guessed: an older text would credit the step with changes nobody made for it
  (a rolled-back edit, say). Only the statements inside the step's anchor scopes are attributed
  to it; the other changes are listed, left unrecorded, and judged as hand edits. The event's
  before-text keeps them, so rolling the step back undoes only its own statements.
- **``--record --abort``** appends an ``abort`` event, drops the snapshot manifest (its blobs
  follow the store's retention) and restores the step's previous status. Files are untouched.

While a record is open, ``apply`` and ``rollback`` refuse to write: their changes would land in
the record's diff.

Claims (the ladder, design 5.1): a recorded edit defaults to the step's claim (``intended`` for
a step with none). ``--claim``, ``--expect`` and ``--pair`` can only keep or tighten its
judgement: ``preserve`` over ``intended``, ``representation`` over ``any``, and a pairing
anchor. Loosening one, or a person-only expectation (``raise:<T>``, ``return:<T>``), needs a
person's attestation (``pymolt.attest``); without one it is refused. ``--by person`` without an
attestation is recorded as an unattested person.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pymolt.migration_plan import (
    PERSON_RUNS_IT,
    MigrationPlan,
    MigrationPlanError,
    MigrationRunState,
    _migration_lock,
    _now,
    _project_file,
    _pythons,
    _restore_ledger,
    _run_dir,
    _save_run_state,
    _sha256,
    _step_risk_ids,
    inspect_migration_plan,
    recover_invocation,
)
from pymolt.migration_state import MigrationReceipt
from pymolt.workorder.apply import (
    ApplyResult,
    StepRefusal,
    _check_frozen,
    _finish,
    _load_ledger,
    _receipt_with_run,
    run_for,
    summarize_run,
)
from pymolt.workorder.models import PlanStep
from pymolt.workorder.state import RunView, applicable_steps

#: Past this many bytes of new blobs, a snapshot lists the remaining files by hash only.
SNAPSHOT_NEW_BYTES_CAP = 50 * 1024 * 1024
#: Expectations anyone can set (they keep or tighten a judgement).
AGENT_EXPECT = ("any", "representation")
_PERSON_EXPECT = re.compile(r"^(raise|return):[A-Za-z_][\w.]*$")
_CLAIM_RANK = {"preserve": 1, "intended": 0}


def person_only_expect(expect: str | None) -> bool:
    """``raise:<T>`` / ``return:<T>``: a person's attested expectation (5.2)."""
    return bool(expect) and bool(_PERSON_EXPECT.match(expect or ""))


def valid_expect(expect: str | None) -> bool:
    return expect is None or expect in AGENT_EXPECT or person_only_expect(expect)


def in_progress_steps(receipt: Any, run_id: str | None = None) -> list[tuple[str, str]]:
    """``(run, step)`` of every step with an open record (of ``run_id`` only, when given)."""
    if receipt is None:
        return []
    out = []
    for key, step in receipt.steps.items():
        run, _sep, sid = key.partition(":")
        if step.status == "in_progress" and (run_id is None or run == run_id):
            out.append((run, sid))
    return out


def refuse_open_record(receipt: Any, operation: str, shown: str = ".") -> None:
    """``apply`` and ``rollback`` write nothing while a record is open (its diff would hold
    their changes). ``shown``: the project dir as the hint's commands name it
    (``status.shown_dir``)."""
    from pymolt.status import command

    busy = in_progress_steps(receipt)
    if busy:
        run, sid = busy[0]
        raise StepRefusal(
            f"step {sid} of run {run[:8]} is being recorded: {operation} would write into its "
            "record",
            hint=f"Finish it with `{command(shown, 'apply', '--step', sid, '--record', '--done')}`"
                 f", or drop it with "
                 f"`{command(shown, 'apply', '--step', sid, '--record', '--abort')}`.")


# ── the claim ladder at record time ─────────────────────────────────────────────────────────


def check_claim(step: PlanStep, *, claim: str | None, expect: str | None,
                attested: bool) -> tuple[str, str | None, list[str]]:
    """``(claim, expect, notes)`` a record carries: the step's claim (``intended`` for a step
    with none) unless ``--claim`` tightens it, and its expectation unless ``--expect`` tightens
    it. Loosening, or a person-only expectation, needs ``attested``; otherwise ``StepRefusal``."""
    base_claim = step.claim or "intended"
    chosen = claim or base_claim
    if chosen not in _CLAIM_RANK:
        raise StepRefusal(f"--claim takes preserve or intended, not {chosen!r}", conflict=False)
    if _CLAIM_RANK[chosen] < _CLAIM_RANK[base_claim] and not attested:
        raise StepRefusal(
            f"step {step.id} claims {base_claim}: recording it as {chosen} loosens how verify "
            "judges it, which needs a person's attestation",
            hint="A person runs the command on a terminal (no --agent, no --json) and types "
                 "the nonce it prints.", conflict=False)
    if not valid_expect(expect):
        raise StepRefusal(f"--expect takes any, representation, raise:<Type> or return:<Type>, "
                          f"not {expect!r}", conflict=False)
    base_expect = step.verify.expect
    chosen_expect = expect if expect is not None else base_expect
    if chosen_expect is not None and chosen == "preserve":
        if expect is not None:
            raise StepRefusal("--expect goes with --claim intended: a preserve claim already "
                              "expects every outcome to stay the same", conflict=False)
        chosen_expect = None
    if person_only_expect(chosen_expect) and not attested:
        raise StepRefusal(
            f"--expect {chosen_expect} is a person's attested expectation: it is refused "
            "without an attestation (under --agent, --json or MCP, or off a terminal)",
            hint="An agent can set --expect any or representation.", conflict=False)
    if base_expect == "representation" and chosen_expect in (None, "any") \
            and chosen == "intended" and not attested:
        raise StepRefusal(
            f"step {step.id} expects only a representation change: --expect any loosens it, "
            "which needs a person's attestation", conflict=False)
    notes: list[str] = []
    if claim is not None and chosen != base_claim:
        notes.append(f"claim {chosen} (the step's is {base_claim})")
    return chosen, chosen_expect, notes


def parse_pairs(pairs: list[str] | tuple[str, ...] | None) -> list[tuple[str, str]]:
    out = []
    for item in pairs or ():
        old, sep, new = str(item).partition("=")
        if not sep or not old.strip() or not new.strip():
            raise StepRefusal(f"--pair takes OLD=NEW (two dotted names), not {item!r}",
                              conflict=False)
        out.append((old.strip(), new.strip()))
    return out


# ── entry point ─────────────────────────────────────────────────────────────────────────────


def record_step(project_dir: str | Path, plan: MigrationPlan, step_id: str, *,
                done: bool = False, abort: bool = False, agent: bool = False,
                by: str | None = None, actor: str | None = None, claim: str | None = None,
                expect: str | None = None, pairs: list[str] | tuple[str, ...] = (),
                attestation: Any = None) -> ApplyResult:
    """One phase of a record (module docstring). ``attestation``: a ``pymolt.attest.Attester``
    the interface layer supplies when a person may be on a terminal (None: nothing is
    attested). Raises ``StepRefusal`` (exit 2) or ``MigrationPlanError`` / ``OSError`` (exit
    3)."""
    if done and abort:
        raise StepRefusal("--done and --abort cannot be combined", conflict=False)
    if by not in (None, "agent", "person"):
        raise StepRefusal(f"--by takes agent or person, not {by!r}", conflict=False)
    if agent and by == "person":
        raise StepRefusal("--by person is refused under --agent: an agent records as an agent",
                          hint=PERSON_RUNS_IT, conflict=False)
    if (claim or expect or pairs) and not done:
        raise StepRefusal("--claim, --expect and --pair go with --record --done",
                          conflict=False)
    pair_list = parse_pairs(pairs)
    with _migration_lock(project_dir, "record"):
        root = Path(project_dir).resolve()
        ctx = _Context.load(root, plan, step_id)
        if abort:
            return _abort(ctx)
        if done:
            return _done(ctx, by=by, actor=actor, claim=claim, expect=expect,
                         pairs=pair_list, agent=agent, attestation=attestation)
        return _begin(ctx, by=by or ("agent" if agent else "person"), actor=actor)


class _Context:
    """What every phase reads: the run, its receipt, ledger and state, and the step."""

    def __init__(self, root: Path, plan: MigrationPlan, step: PlanStep, run_id: str,
                 state: MigrationRunState | None, receipt, ledger) -> None:
        self.root, self.plan, self.step = root, plan, step
        self.run_id, self.state, self.receipt, self.ledger = run_id, state, receipt, ledger
        self.view = RunView.of(ledger, run_id)

    @classmethod
    def load(cls, root: Path, plan: MigrationPlan, step_id: str) -> _Context:
        receipt = MigrationReceipt.load(root)
        run_id, state = run_for(root, plan, receipt)
        _check_frozen(root, run_id, plan)
        if state is not None:
            state = recover_invocation(root, state, plan) or state
            receipt = MigrationReceipt.load(root)
        ledger = _load_ledger(root, receipt)
        step = plan.step(step_id)
        if step is None:
            raise StepRefusal(f"unknown step {step_id}: the plan has "
                              + (", ".join(s.id for s in plan.steps) or "no step"))
        return cls(root, plan, step, run_id, state, receipt, ledger)

    @property
    def key(self) -> str:
        return f"{self.run_id}:{self.step.id}"

    def mine(self):
        return self.receipt.steps.get(self.key) if self.receipt is not None else None

    def steps_dir(self) -> Path:
        return _run_dir(self.root, self.run_id) / "steps" / self.step.id

    def result(self, outcome: str, **fields) -> ApplyResult:
        return ApplyResult(outcome=outcome, plan_id=self.plan.plan_id, run_id=self.run_id,
                           **fields)


def _refuse_stale_plan(ctx: _Context) -> None:
    inspection = inspect_migration_plan(ctx.root, ctx.plan, check_files=False)
    if not inspection.valid:
        raise StepRefusal("stale migration plan: " + "; ".join(inspection.reasons),
                          hint="Every step not in effect is stale. Re-plan with `pymolt plan .`.")


def _recordable(ctx: _Context, *, reopening: bool = False) -> None:
    """The step may be recorded: planned, rolled back, stale or recorded, its dependencies in
    effect, and (``--record``) no record open in the run."""
    step = ctx.step
    if step.type == "target-env":
        raise StepRefusal(
            f"step {step.id} builds the target environment: status sees it from the "
            "environment check, there is nothing to record",
            hint="Build the environment and pass it to `pymolt setup --target-env PATH`.")
    busy = [sid for _run, sid in in_progress_steps(ctx.receipt, ctx.run_id)]
    if busy and busy[0] == step.id and not reopening:
        raise StepRefusal(
            f"step {step.id} is already being recorded",
            hint=f"Finish it with `pymolt apply --step {step.id} --record --done`, or drop it "
                 f"with `--record --abort`.")
    if busy and busy[0] != step.id:
        raise StepRefusal(
            f"step {busy[0]} of this run is being recorded: a run records one step at a time, "
            "or the two records would attribute each other's edits",
            hint=f"Finish it (`pymolt apply --step {busy[0]} --record --done`) or abort it "
                 "first.")
    last = ctx.view.status(step.id)
    if last == "applied":
        raise StepRefusal(
            f"step {step.id} is in effect (applied by pymolt): there is nothing to record",
            hint=f"To make it by hand instead, roll it back first (`pymolt rollback --step "
                 f"{step.id}`).")
    if last == "skipped":
        raise StepRefusal(
            f"step {step.id} was skipped: a skipped step is not recorded",
            hint="Re-plan (`pymolt plan .`) to plan it again.")
    missing = [d for d in step.depends_on if not ctx.view.in_effect(d)]
    if missing:
        raise StepRefusal(
            f"step {step.id} depends on " + ", ".join(
                f"{d} ({ctx.view.status(d) or 'planned'})" for d in missing)
            + ", not in effect",
            refused={step.id: [f"needs {d}" for d in missing]},
            hint="Put the dependencies in effect first.")


# ── --record ────────────────────────────────────────────────────────────────────────────────


def snapshot_files(root: Path, step: PlanStep) -> list[str]:
    """Every project ``.py`` file (``_iter_py_files``' skip rules) and the step's own files."""
    from pymolt.codemods.apply import _iter_py_files

    out = [p.relative_to(root).as_posix() for p in _iter_py_files(root)]
    for rel in step.files:
        if rel not in out and _project_file(root, rel).is_file():
            out.append(rel)
    return out


def _read_text(root: Path, rel: str) -> str | None:
    try:
        path = _project_file(root, rel)
        if path.is_symlink():
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, MigrationPlanError):
        return None


def _begin(ctx: _Context, *, by: str, actor: str | None) -> ApplyResult:
    from pymolt.migration_state import StepReceipt
    from pymolt.workorder.blobs import has_blob, put_text, sha256

    _refuse_stale_plan(ctx)
    _recordable(ctx)
    root = ctx.root
    files: dict[str, Any] = {}
    new_bytes = 0
    hash_only = 0
    for rel in snapshot_files(root, ctx.step):
        text = _read_text(root, rel)
        if text is None:
            continue
        digest = sha256(text)
        size = len(text.encode("utf-8", "surrogatepass"))
        if has_blob(root, digest):
            files[rel] = digest
        elif new_bytes + size > SNAPSHOT_NEW_BYTES_CAP:
            files[rel] = {"sha256": digest, "hash_only": True}
            hash_only += 1
        else:
            put_text(root, text)
            new_bytes += size
            files[rel] = digest
    now = _now()
    seq = ctx.ledger.next_seq()
    folder = ctx.steps_dir()
    folder.mkdir(parents=True, exist_ok=True)
    manifest = folder / f"snapshot-{seq}.json"
    previous = ctx.mine()
    payload = {"schema": "pymolt.record_snapshot/1", "run_id": ctx.run_id,
               "step_id": ctx.step.id, "taken_at": now, "by": by, "actor": actor,
               "previous": previous.model_dump(mode="json") if previous is not None else None,
               "files": files}
    from pymolt.state_io import atomic_write_json

    atomic_write_json(manifest, payload)
    try:
        receipt = ctx.receipt.model_copy(deep=True) if ctx.receipt is not None else None
        receipt = _receipt_with_run(root, receipt, ctx.plan, ctx.run_id, now)
        receipt.steps[ctx.key] = (previous.model_copy(update={"status": "in_progress"})
                                  if previous is not None else StepReceipt(
                                      status="in_progress", executor=by, actor=actor or by,
                                      claim=ctx.step.claim))
        if receipt.run_id == ctx.run_id:
            summarize_run(root, receipt, ctx.plan)
        receipt.save(root)
        _save_state(ctx, now, "in_progress")
    except BaseException:
        manifest.unlink(missing_ok=True)
        raise
    notes = [f"{len(files)} file(s) snapshotted" + (
        f", {hash_only} by hash only (over {SNAPSHOT_NEW_BYTES_CAP // (1024 * 1024)} MB of new "
        "text): edits to those are judged as hand edits" if hash_only else "")]
    result = ctx.result("in_progress", in_progress=[ctx.step.id], notes=notes,
                        snapshot=str(manifest), receipt=receipt)
    _finish(result, root, ctx.plan, applicable_steps(ctx.plan), ctx.ledger, ctx.run_id)
    return result


def _save_state(ctx: _Context, now: str, status: str | None) -> None:
    """The run's ``state.json`` (and its frozen plan) with the step's status in it (None:
    ``planned`` again. The entry stays: a run state with no step and no invocation reads as a
    pre-invocation run left prepared, which would ask for recovery)."""
    run_dir = _run_dir(ctx.root, ctx.run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    if not (run_dir / "plan.json").is_file():
        ctx.plan.save(ctx.root, run_dir / "plan.json")
    base = ctx.state or MigrationRunState(
        run_id=ctx.run_id, status="prepared", prepared_at=now,
        plan_id=None if ctx.run_id == ctx.plan.plan_id else ctx.plan.plan_id)
    steps = dict(base.steps or {})
    steps[ctx.step.id] = status or "planned"
    _save_run_state(run_dir, base.model_copy(update={"steps": steps}))


def latest_snapshot(root: Path, run_id: str, step_id: str) -> tuple[Path, dict] | None:
    folder = _run_dir(root, run_id) / "steps" / step_id
    found = []
    for path in folder.glob("snapshot-*.json") if folder.is_dir() else ():
        try:
            n = int(path.stem.split("-", 1)[1])
        except (IndexError, ValueError):
            continue
        found.append((n, path))
    for _n, path in sorted(found, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("files"), dict):
            return path, data
    return None


# ── --record --abort ────────────────────────────────────────────────────────────────────────


def _abort(ctx: _Context) -> ApplyResult:
    from pymolt.apply_ledger import LedgerEvent, LedgerSnapshot, append_ledger

    mine = ctx.mine()
    if mine is None or mine.status != "in_progress":
        raise StepRefusal(f"step {ctx.step.id} is not being recorded: there is nothing to abort")
    found = latest_snapshot(ctx.root, ctx.run_id, ctx.step.id)
    previous = None
    if found is not None and found[1].get("previous"):
        from pymolt.migration_state import StepReceipt

        previous = StepReceipt.model_validate(found[1]["previous"])
    now = _now()
    before = LedgerSnapshot.take(ctx.root)
    try:
        saved = append_ledger(ctx.root, [(LedgerEvent(
            event="abort", run_id=ctx.run_id, step_id=ctx.step.id, at=now,
            executor=mine.executor, actor=mine.actor,
            snapshot=str(found[0].relative_to(ctx.root)) if found else None), [])],
            ledger=ctx.ledger)
        receipt = ctx.receipt.model_copy(deep=True)
        if previous is not None:
            receipt.steps[ctx.key] = previous
        else:
            receipt.steps.pop(ctx.key, None)
        if receipt.run_id == ctx.run_id:
            summarize_run(ctx.root, receipt, ctx.plan)
        receipt.save(ctx.root)
        _save_state(ctx, now, previous.status if previous is not None else None)
    except BaseException:
        _restore_ledger(before)
        raise
    folder = ctx.steps_dir()
    for path in folder.glob("snapshot-*.json") if folder.is_dir() else ():
        path.unlink(missing_ok=True)
    from pymolt.migration_plan import _collect_blobs

    _collect_blobs(ctx.root)
    status = previous.status if previous is not None else "planned"
    result = ctx.result("aborted", notes=[f"step {ctx.step.id} is {status} again; no file was "
                                          "touched"], receipt=receipt)
    _finish(result, ctx.root, ctx.plan, applicable_steps(ctx.plan), saved, ctx.run_id)
    return result


# ── --record --done ─────────────────────────────────────────────────────────────────────────


class _Change:
    """One file a record describes: its text before the step and now."""

    def __init__(self, file: str, before: str, after: str, *, before_is_t0: bool) -> None:
        self.file, self.before, self.after = file, before, after
        self.before_is_t0 = before_is_t0


def _blob(root: Path, digest: str | None) -> str | None:
    from pymolt.workorder.blobs import get_text

    return get_text(root, digest) if digest else None


def _plan_t0(ctx: _Context, rel: str) -> str | None:
    """The file's text at plan time: the plan's ``old_source``, or the plan's baseline capture's
    source snapshot (the plan keeps only the files its pymolt steps change)."""
    for change in ctx.plan.changes:
        if change.path == rel:
            return change.old_source
    from pymolt.verify.service import load_contract_state, load_sources, same_slot_path

    baseline = load_contract_state(ctx.root).baseline
    if (baseline is None or not baseline.sources_path or not ctx.plan.baseline_trace_path
            or not same_slot_path(ctx.root, ctx.plan.baseline_trace_path, baseline.trace_path)
            or ctx.plan.baseline_captured_at != baseline.captured_at):
        return None
    sources = load_sources(baseline.sources_path) or {}
    return sources.get(rel)


def _last_known(ctx: _Context, rel: str) -> tuple[str | None, bool]:
    """``(text, is plan-time text)``: the after-text of the latest ledger event on ``rel``,
    whatever it is (an apply, a record, a restoring rollback, a run of another plan), else, when
    the ledger has none, its text at plan time. Not only the epochs in effect: a step rolled
    back, or a run of an earlier plan, also left the file at a text pymolt knows, and starting
    from an older one would credit the step with their changes. Raises ``StepRefusal`` when that
    text is not in the blob store."""
    latest = None
    for event in sorted(ctx.ledger.events, key=lambda e: e.seq):
        for f in event.files:
            if f.file == rel:
                latest = (event, f)
    if latest is None:
        return _plan_t0(ctx, rel), True
    event, f = latest
    text = _blob(ctx.root, f.after) if f.after else None
    if text is None:
        raise StepRefusal(
            f"step {ctx.step.id}: the text {rel} had after ledger event {event.seq} "
            f"({event.event} of {event.step_id}) is not in the blob store"
            + (f" ({f.after[:12]})" if f.after else " (the event names no text)")
            + ", so pymolt cannot tell which changes since are the step's",
            refused={ctx.step.id: [f"{rel}: last known text missing"]},
            hint=f"Start with `pymolt apply --step {ctx.step.id} --record` before editing, "
                 "then `--record --done`: the snapshot then says what changed.")
    t0 = _plan_t0(ctx, rel)
    return text, t0 is not None and text == t0


def _previous_befores(ctx: _Context) -> dict[str, str | None]:
    """A re-record: each file the step's current epoch touched -> that epoch's before-text."""
    event = ctx.ledger.epoch_event(ctx.run_id, ctx.step.id)
    if event is None:
        return {}
    return {f.file: _blob(ctx.root, f.before) for f in event.files}


def _scopes(step: PlanStep) -> dict[str, set[str]]:
    """file -> the scopes the step's anchors name (``where``, and its edits' anchors)."""
    out: dict[str, set[str]] = {}
    for anchor in step.where:
        out.setdefault(anchor.file, set()).add(anchor.scope)
    for edit in step.edits:
        for anchor in [*edit.anchors_old, *edit.anchors_new]:
            out.setdefault(edit.file, set()).add(anchor.scope)
    return out


def _inside(scope: str, scopes: set[str]) -> bool:
    return any(scope == s or scope.startswith(s + ".") for s in scopes if s != "<module>") \
        or (scope == "<module>" and "<module>" in scopes)


def _split_by_scope(rel: str, before: str, after: str, scopes: set[str], python_before,
                    python_after) -> tuple[str, list[dict[str, Any]]]:
    """``(before-text with the out-of-scope changes applied, the out-of-scope changes)``: what
    ``--done`` with no ``--record`` first attributes to the step is the rest."""
    import difflib

    from pymolt.workorder.edits import _met, build_index, split_lines

    a, b = split_lines(before), split_lines(after)
    index_a, index_b = build_index(before, python_before), build_index(after, python_after)
    out: list[str] = []
    left: list[dict[str, Any]] = []
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            out.extend(a[i1:i2])
            continue
        stmts = _met(index_a, i1, i2) + _met(index_b, j1, j2)
        mine = bool(stmts) and all(_inside(st.scope, scopes) for st in stmts)
        if mine:
            out.extend(a[i1:i2])            # the step's own change: undone in its before-text
        else:
            out.extend(b[j1:j2])            # someone else's: kept on both sides
            names = sorted({st.scope for st in stmts}) or ["?"]
            left.append({"file": rel, "lines": [j1 + 1, max(j1 + 1, j2)],
                         "scope": ", ".join(names),
                         "why": "outside the step's statements: judged as a hand edit"})
    return "".join(out), left


def _changes(ctx: _Context) -> tuple[list[_Change], list[dict[str, Any]], str, str | None]:
    """``(changes, unrecorded, how, snapshot path)`` for ``--done``."""
    root = ctx.root
    base, target = _pythons(root, ctx.plan)
    previous = _previous_befores(ctx)
    unrecorded: list[dict[str, Any]] = []
    changes: list[_Change] = []
    mine = ctx.mine()
    if mine is not None and mine.status == "in_progress":
        found = latest_snapshot(root, ctx.run_id, ctx.step.id)
        if found is None:
            raise MigrationPlanError(
                f"the snapshot of step {ctx.step.id}'s record is missing: abort it "
                f"(`pymolt apply --step {ctx.step.id} --record --abort`) and record again")
        path, data = found
        files = dict(data["files"])
        names = list(dict.fromkeys([*files, *snapshot_files(root, ctx.step), *previous]))
        for rel in names:
            now = _read_text(root, rel)
            entry = files.get(rel)
            hashed = isinstance(entry, dict) and entry.get("hash_only")
            digest = entry.get("sha256") if isinstance(entry, dict) else entry
            if now is None:
                if digest is not None:
                    unrecorded.append({"file": rel, "lines": None, "scope": None,
                                       "why": "deleted or unreadable: not recorded"})
                continue
            if rel in previous and previous[rel] is not None:
                before = previous[rel]
            elif digest is not None and _sha256(now) == digest:
                continue                         # unchanged since the snapshot
            elif digest is None:
                before = ""                      # a new file
            elif hashed:
                if _sha256(now) != digest:
                    unrecorded.append({"file": rel, "lines": None, "scope": None,
                                       "why": "before text unknown (snapshotted by hash only): "
                                              "judged as a hand edit"})
                continue
            else:
                before = _blob(root, digest)
                if before is None:
                    raise MigrationPlanError(f"the snapshot text of {rel} is not in the blob "
                                             "store: abort the record and record again")
            if before != now:
                t0 = _plan_t0(ctx, rel)
                changes.append(_Change(rel, before, now, before_is_t0=t0 is not None
                                       and before == t0))
        return changes, unrecorded, "snapshot", str(path.relative_to(root))
    scopes = _scopes(ctx.step)
    names = list(dict.fromkeys([*scopes, *previous]))
    for rel in names:
        now = _read_text(root, rel)
        if now is None:
            continue
        if rel in previous and previous[rel] is not None:
            before, is_t0 = previous[rel], False
        else:
            before, is_t0 = _last_known(ctx, rel)
        if before is None:
            unrecorded.append({"file": rel, "lines": None, "scope": None,
                               "why": "its text at plan time is not known: start with "
                                      "`--record` before editing"})
            continue
        if before == now:
            continue
        pb = base if is_t0 else target
        own, left = _split_by_scope(rel, before, now, scopes.get(rel, set()), pb, target)
        unrecorded += left
        if own != now:
            changes.append(_Change(rel, own, now, before_is_t0=is_t0 and own == before))
    return changes, unrecorded, "last-known", None


def _done(ctx: _Context, *, by: str | None, actor: str | None, claim: str | None,
          expect: str | None, pairs: list[tuple[str, str]], agent: bool,
          attestation) -> ApplyResult:
    from pymolt.apply_ledger import (
        EntryAttrs,
        EventFile,
        LedgerEntry,
        LedgerEvent,
        LedgerSnapshot,
        append_ledger,
        entries_for,
    )
    from pymolt.migration_state import StepReceipt
    from pymolt.workorder.blobs import put_texts

    _refuse_stale_plan(ctx)
    mine = ctx.mine()
    opened = mine is not None and mine.status == "in_progress"
    _recordable(ctx, reopening=opened)
    step, root = ctx.step, ctx.root
    snapshot = latest_snapshot(root, ctx.run_id, step.id) if opened else None
    begun = snapshot[1] if snapshot is not None else {}
    by = "agent" if agent else (by or begun.get("by") or "person")
    if agent:
        attestation = None               # whatever terminal it runs on, an agent never attests
    actor = actor or (begun.get("actor") if begun.get("by") == by else None)
    changes, unrecorded, how, snap_path = _changes(ctx)
    completion = step.type == "review" or step.phase == "env"
    if not opened and ctx.view.in_effect(step.id) and _same_as_epoch(ctx, changes):
        changes = []                     # row 4: the epoch in effect already describes it
        completion = False
    if not changes and not completion:
        if not opened and ctx.view.in_effect(step.id):
            result = ctx.result("already_applied", noop=[step.id], unrecorded=unrecorded,
                                notes=[f"step {step.id} is {ctx.view.status(step.id)} and "
                                       "nothing changed since: nothing to record"])
            _finish(result, root, ctx.plan, applicable_steps(ctx.plan), ctx.ledger, ctx.run_id)
            return result
        raise StepRefusal(
            f"step {step.id}: no change to record"
            + (" since `--record`" if opened else " in the step's statements"),
            refused={step.id: ["no change"]},
            hint=("Make the change, then `--record --done` again, or drop the record with "
                  "`--record --abort`." if opened else
                  "Start with `pymolt apply --step ID --record`, make the change, then "
                  "`--record --done`. A behaviour change with no code edit is accepted with "
                  "`pymolt verify . --accept FINDING_ID`."))
    # Who attests, and what the record may claim (design 5.6: --by person, --claim intended
    # and a person-only --expect are attested commands).
    wants_attest = by == "person" or claim == "intended" or person_only_expect(expect)
    final_claim, final_expect, _n = check_claim(step, claim=claim, expect=expect, attested=True)
    att_id, why_not = None, None
    if wants_attest:
        record, why_not = _attest(ctx, attestation, by=by, claim=final_claim,
                                  expect=final_expect if final_claim == "intended" else None,
                                  changes=changes)
        att_id = record["id"] if record else None
    try:
        chosen, chosen_expect, claim_notes = check_claim(step, claim=claim, expect=expect,
                                                         attested=att_id is not None)
    except StepRefusal as exc:
        if why_not:
            raise StepRefusal(f"{exc} (not attested: {why_not})", hint=exc.hint) from None
        raise
    attested_note = [] if att_id is not None or not wants_attest else [
        f"recorded as {'an unattested person' if by == 'person' else 'an agent'}: no person "
        f"attested it ({why_not or 'an attestation needs a terminal, with no --agent or --json'})"]
    base, target = _pythons(root, ctx.plan)
    symbol_old = step.symbol.old
    symbol_new = step.symbol.new
    if pairs and not symbol_new:
        symbol_old, symbol_new = pairs[0]
    attrs = EntryAttrs(kind=step.type, rule=None, claim=chosen, basis="manual",
                       expect=chosen_expect if chosen == "intended" else None,
                       old_symbol=symbol_old, new_symbol=symbol_new)
    entries: list[LedgerEntry] = []
    for change in changes:
        if step.type == "manifest-pin" or not change.file.endswith(".py"):
            continue                      # a manifest is not code: the event names its texts
        kwargs = {"run_id": ctx.run_id, "step_id": step.id,
                  "python_before": base if change.before_is_t0 else target,
                  "python_after": target}
        try:
            entries += entries_for(change.file, change.before, change.after, attrs, **kwargs)
        except Exception:  # noqa: BLE001 - provenance degrades to lines, the record stands
            entries += entries_for(change.file, change.before, change.after, attrs,
                                   anchored=False, **kwargs)
    for old, new in pairs:
        if (old, new) != (symbol_old, symbol_new):
            entries.append(LedgerEntry(run_id=ctx.run_id, step_id=step.id,
                                       file=(changes[0].file if changes else ""), kind="pair",
                                       claim=chosen, basis="manual",
                                       expect=attrs.expect, old_symbol=old, new_symbol=new))
    check = _compile(root, ctx.plan, step, changes)
    from pymolt.migration_state import step_checks

    files = [c.file for c in changes]
    checks = step_checks(check, files, runs_on_base=step.runs_on_base)
    checks["smoke_import"] = "not checked: pymolt did not write this step"
    failed = any(v.startswith("failed") for v in checks.values())
    now = _now()
    event = LedgerEvent(
        event="record", run_id=ctx.run_id, step_id=step.id, at=now, executor=by,
        actor=actor or by, attestation=att_id, risk_ids=_step_risk_ids(ctx.plan, step),
        files=[EventFile(file=c.file, before=_sha256(c.before), after=_sha256(c.after))
               for c in changes],
        claim=chosen, expect=attrs.expect, pairs=[list(p) for p in pairs], recorded_from=how,
        snapshot=snap_path, unrecorded=unrecorded[:50])
    put_texts(root, [t for c in changes for t in (c.before, c.after)])
    ledger_before = LedgerSnapshot.take(root)
    try:
        saved = append_ledger(root, [(event, entries)], ledger=ctx.ledger)
        seq = saved.events[-1].seq
        receipt = ctx.receipt.model_copy(deep=True) if ctx.receipt is not None else None
        receipt = _receipt_with_run(root, receipt, ctx.plan, ctx.run_id, now)
        receipt.steps[ctx.key] = StepReceipt(
            status="recorded", epoch=seq, executor=by, actor=actor or by, claim=chosen,
            applied_at=now, files=files, risk_ids=list(event.risk_ids), checks=checks)
        if receipt.run_id == ctx.run_id:
            summarize_run(root, receipt, ctx.plan)
        receipt.save(root)
        _save_state(ctx, now, "recorded")
    except BaseException:
        _restore_ledger(ledger_before)
        raise
    notes = [*claim_notes, *attested_note]
    if unrecorded:
        notes.append(f"{len(unrecorded)} change(s) left unrecorded: verify judges them as hand "
                     "edits")
    if not changes:
        notes.append(f"step {step.id} recorded as done with no code change")
    from pymolt.migration_plan import _collect_blobs

    _collect_blobs(root)
    result = ctx.result("recorded", recorded=[step.id], files=files, unrecorded=unrecorded,
                        checks={step.id: checks}, notes=notes, receipt=receipt,
                        attestation=att_id, check_failed=failed, self_check=check)
    _finish(result, root, ctx.plan, applicable_steps(ctx.plan), saved, ctx.run_id)
    return result


def _attest(ctx: _Context, attestation, *, by: str, claim: str, expect: str | None,
            changes: list[_Change]) -> tuple[dict | None, str | None]:
    """Ask a person to attest the record (``pymolt.attest``): bound to the project, the run,
    the step, the claim and expectation, and the recorded change (its files' hashes)."""
    from pymolt.attest import files_digest, project_id

    if attestation is None:
        return None, "no person can attest here (--agent, --json or MCP)"
    if by != "person":
        return None, "an agent's record is never attested"
    files = [{"file": c.file, "before": _sha256(c.before), "after": _sha256(c.after)}
             for c in changes]
    step = ctx.step
    names = ", ".join(c.file for c in changes) or "no file"
    text = (f"Record step {step.id} ({' '.join(step.title.split())[:80]}) of plan "
            f"{ctx.plan.plan_id[:8]} as made by a {by}: claim {claim}"
            + (f", expecting {expect}" if expect else "") + f". Files: {names}.")
    return attestation.ask(text, {
        "kind": "record", "project": project_id(ctx.root), "run_id": ctx.run_id,
        "plan_id": ctx.plan.plan_id, "step": step.id, "by": by, "claim": claim,
        "expect": expect, "files": files_digest(files)})


def _same_as_epoch(ctx: _Context, changes: list[_Change]) -> bool:
    """The changes are the step's current epoch's, file for file (nothing new to record)."""
    event = ctx.ledger.epoch_event(ctx.run_id, ctx.step.id)
    if event is None:
        return False
    have = {(c.file, _sha256(c.before), _sha256(c.after)) for c in changes}
    return have == {(f.file, f.before, f.after) for f in event.files}


def _compile(root: Path, plan: MigrationPlan, step: PlanStep, changes: list[_Change]):
    """D's compile check on the recorded Python files: the target, and the base when the step
    runs there."""
    from pymolt.apply_check import SelfCheck, compile_changes

    code = [SimpleNamespace(path=c.file, old_source=c.before, new_source=c.after,
                            runs_on_base=step.runs_on_base)
            for c in changes if c.file.endswith(".py")]
    if not code:
        return SelfCheck(compile=[])
    return SelfCheck(compile=compile_changes(
        root, SimpleNamespace(changes=code, target_python=plan.target_python)))


__all__ = ["AGENT_EXPECT", "SNAPSHOT_NEW_BYTES_CAP", "check_claim", "in_progress_steps",
           "latest_snapshot", "parse_pairs", "person_only_expect", "record_step",
           "refuse_open_record", "snapshot_files", "valid_expect"]

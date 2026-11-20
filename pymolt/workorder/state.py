"""Each step's status, projected (design 1.3).

HOST side, read-only (but for the anchor cache, ``workorder.anchor_cache``). The plan file is
immutable: a step's status there is always ``planned``. ``project_steps`` projects the live
status of every step of the saved plan from the receipt set, the ledger of the run that applies
the plan, the saved verification, the post-migration capture's source snapshot and the current
files. ``status``, the apply summary and the views read it. Precedence, first match:

1. run-level ``stale``: the setup fingerprint or the target manifest changed since the plan, the
   baseline it is bound to was replaced, or its run is closed (a later plan's run replaced it).
   Steps in effect stay in effect;
2. ``in_progress``: a ``--record`` took its snapshot;
3. ``rolled_back`` / ``skipped``;
4. ``not_in_capture``: in effect, but the post-migration capture holds the code from before it
   or an older epoch of it (its current epoch's new statements do not resolve there,
   ``workorder.coverage``);
5. a verify judgement made for the step's current epoch (``verified``, ``failed``,
   ``needs_review``, ``needs_evidence``; written by ``verify`` per step);
6. ``applied`` / ``recorded``;
7. ``present``: no epoch, but its effect is in its slots (or, for the target environment, the
   environment exists);
8. ``stale``: a statement it rewrites is lost or ambiguous (an anchor check; ``apply`` does the
   full check), or a dependency is stale; ``blocked``: a dependency is not in effect;
9. ``planned``.

``applicable_steps`` names the steps ``pymolt apply`` writes: the manifest pin pymolt makes
(decision 1), then the plan's pymolt code steps when their hunks rebuild every changed file from
``changes[].old_source`` to ``new_source``, else the plan's one whole-file step (``v1``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pymolt.workorder.models import PlanStep

#: Step types ``pymolt apply`` writes.
CODE_TYPES = ("codemod", "python-fix")


def applicable_steps(plan) -> list[PlanStep]:
    """The steps ``apply`` writes, in plan order (see the module docstring): the pymolt
    ``manifest-pin`` steps of the env phase (decision 1), then the code steps."""
    from pymolt.migration_plan import _ledger_steps

    pins = [s for s in plan.steps if s.executor == "pymolt" and s.type == "manifest-pin"
            and s.edits and s.do.recipe is not None and s.do.recipe.kind == "manifest-pin"]
    return pins + [step for step, _files in _ledger_steps(plan)]


@dataclass
class RunView:
    """What the ledger says about one run's steps. ``current``: step id -> the seq of its
    current epoch; ``last``: step id -> what the ledger last says of it (``applied``,
    ``recorded``, ``rolled_back``, ``skipped``); ``kinds``: seq -> event kind."""

    run_id: str | None
    current: dict[str, int] = field(default_factory=dict)
    last: dict[str, str] = field(default_factory=dict)
    kinds: dict[int, str] = field(default_factory=dict)

    @classmethod
    def of(cls, ledger: Any, run_id: str | None) -> RunView:
        view = cls(run_id)
        if ledger is None or run_id is None:
            return view
        view.current = {sid: seq for (run, sid), seq in ledger.current_epochs().items()
                        if run == run_id}
        view.last = {sid: state for (run, sid), state in ledger.step_states().items()
                     if run == run_id}
        view.kinds = {e.seq: e.event for e in ledger.events}
        return view

    def in_effect(self, step_id: str) -> bool:
        return step_id in self.current

    def status(self, step_id: str) -> str | None:
        """``applied`` / ``recorded`` (a current epoch), ``rolled_back``, ``skipped``, or None
        (the ledger never saw the step)."""
        seq = self.current.get(step_id)
        if seq is not None:
            return "recorded" if self.kinds.get(seq) == "record" else "applied"
        return self.last.get(step_id)


def basic_statuses(steps: Iterable[PlanStep], view: RunView,
                   extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """``step id -> status`` from the ledger alone: its ledger status, else ``blocked`` (a
    dependency not in effect) or ``planned``. ``extra``: statuses the caller knows better
    (``present``, ``stale``)."""
    out: dict[str, str] = {}
    for step in steps:
        known = (extra or {}).get(step.id) or view.status(step.id)
        if known is not None:
            out[step.id] = known
        elif any(not view.in_effect(dep) for dep in step.depends_on):
            out[step.id] = "blocked"
        else:
            out[step.id] = "planned"
    return out


def remaining(steps: Iterable[PlanStep], statuses: Mapping[str, str]) -> dict[str, list[str]]:
    """The steps not in effect, by status, in plan order."""
    out: dict[str, list[str]] = {}
    for step in steps:
        status = statuses.get(step.id, "planned")
        if status in ("applied", "recorded"):
            continue
        out.setdefault(status, []).append(step.id)
    return out


# ── the projection ────────────────────────────────────────────────────────────────────────

_UNSET: Any = object()
#: Statuses a step in effect can have.
IN_EFFECT = ("applied", "recorded", "not_in_capture", "verified", "failed", "needs_review",
             "needs_evidence")
JUDGEMENTS = ("verified", "failed", "needs_review", "needs_evidence")


@dataclass
class StepState:
    """One step's projected status, with why when it is not plain."""

    id: str
    status: str
    executor: str
    title: str
    type: str
    run_id: str | None = None
    epoch: int | None = None
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "status": self.status, "executor": self.executor,
                "title": self.title}


@dataclass
class Projection:
    """``project_steps``'s answer. ``run_stale``: why the whole plan is stale (empty: it is
    not)."""

    plan_id: str | None = None
    run_id: str | None = None
    created_at: str | None = None
    run_stale: list[str] = field(default_factory=list)
    steps: list[StepState] = field(default_factory=list)
    post_newer: bool = False          # set by ``status``: a post capture newer than the plan

    def step(self, step_id: str) -> StepState | None:
        return next((s for s in self.steps if s.id == step_id), None)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.steps:
            out[s.status] = out.get(s.status, 0) + 1
        return out

    def first(self, status: str, *, executor: str | None = None,
              pymolt: bool | None = None) -> StepState | None:
        for s in self.steps:
            if s.status != status:
                continue
            if executor is not None and s.executor != executor:
                continue
            if pymolt is not None and (s.executor == "pymolt") != pymolt:
                continue
            return s
        return None

    def summary(self) -> str:
        """"6 steps: 3 applied, 1 recorded, 1 planned (person), 1 stale"."""
        parts: list[str] = []
        order = ["applied", "recorded", "verified", "failed", "needs_review", "needs_evidence",
                 "not_in_capture", "in_progress", "present", "planned", "blocked", "stale",
                 "rolled_back", "skipped"]
        for status in order:
            group = [s for s in self.steps if s.status == status]
            if not group:
                continue
            if status == "planned":
                for who in ("pymolt", "agent", "person"):
                    n = sum(1 for s in group if s.executor == who)
                    if n:
                        parts.append(f"{n} planned" + ("" if who == "pymolt" else f" ({who})"))
            else:
                parts.append(f"{len(group)} {status.replace('_', ' ')}")
        return f"{len(self.steps)} step(s)" + (f": {', '.join(parts)}" if parts else "")


def project_steps(project_dir, plan=None, *, receipt: Any = _UNSET, ledger: Any = _UNSET,
                  cache=None) -> Projection:
    """Every step of ``plan`` (default: the saved plan) with its projected status (module
    docstring). Deterministic for the same files and state; writes nothing but the anchor
    cache."""
    from pathlib import Path

    from pymolt.migration_plan import MigrationPlan
    from pymolt.workorder.anchor_cache import AnchorCache

    root = Path(project_dir).resolve()
    if plan is None:
        plan = MigrationPlan.load(root)
    if plan is None or not plan.steps:
        return Projection()
    if receipt is _UNSET:
        from pymolt.migration_state import MigrationReceipt

        receipt = MigrationReceipt.load(root)
    if ledger is _UNSET:
        from pymolt.apply_ledger import load_ledger

        ledger = load_ledger(root, receipt=receipt)
    own_cache = cache is None
    cache = cache or AnchorCache(root)
    run_id = _run_of(root, plan)
    view = RunView.of(ledger, run_id)
    out = Projection(plan_id=plan.plan_id, run_id=run_id, created_at=plan.created_at,
                     run_stale=_run_stale(root, plan, receipt, run_id))
    steps = list(plan.steps)
    extra = [s for s in applicable_steps(plan) if plan.step(s.id) is None]
    steps += extra
    capture = _post_not_in(root, receipt, ledger)
    judged = _judgements(root, run_id)
    from pymolt.migration_plan import _pythons
    from pymolt.workorder.rules import Pythons

    base, target = _pythons(root, plan)
    pythons = Pythons(base=base, target=target)
    applicable = {s.id for s in applicable_steps(plan)}
    by_id: dict[str, StepState] = {}
    for step in steps:
        state = StepState(id=step.id, status="planned", executor=step.executor,
                          title=step.title, type=step.type, run_id=run_id,
                          epoch=view.current.get(step.id))
        key = f"{run_id}:{step.id}" if run_id else None
        mine = receipt.steps.get(key) if receipt is not None and key else None
        last = view.status(step.id)
        live = view.in_effect(step.id)
        if not live and out.run_stale:
            state.status, state.reasons = "stale", list(out.run_stale)
        elif mine is not None and mine.status == "in_progress":
            state.status = "in_progress"
        elif last in ("rolled_back", "skipped"):
            state.status = last
        elif live:
            judgement = judged.get(step.id)
            if (run_id, step.id) in capture:
                state.status = "not_in_capture"
            elif judgement and judgement.get("epoch") == state.epoch \
                    and judgement.get("status") in JUDGEMENTS:
                state.status = judgement["status"]
            else:
                state.status = last or "applied"
        else:
            found, why = _unapplied(root, step, step.id in applicable, cache, pythons)
            # Then its dependencies: a step's statements are its dependency's output, so with
            # the dependency not in effect they are not there yet either (blocked, not stale).
            deps = [by_id.get(d) for d in step.depends_on]
            stale = [d.id for d in deps if d is not None and d.status == "stale"]
            waiting = [d for d in step.depends_on if not view.in_effect(d)]
            if found == "present":
                state.status = "present"
            elif stale:
                state.status = "stale"
                state.reasons = [f"depends on {', '.join(stale)}, which is stale"]
            elif waiting:
                state.status = "blocked"
                state.reasons = [f"needs {d}" for d in waiting]
            else:
                state.status, state.reasons = found, why
        by_id[step.id] = state
        out.steps.append(state)
    if own_cache:
        cache.save()
    return out


def _run_of(root, plan) -> str:
    from pymolt.migration_plan import _plan_runs

    runs = _plan_runs(root, plan.plan_id)
    return runs[-1].run_id if runs else plan.plan_id


def _run_stale(root, plan, receipt, run_id: str) -> list[str]:
    from pymolt.migration_plan import inspect_migration_plan
    from pymolt.verify.service import load_contract_state, same_slot_path

    reasons = list(inspect_migration_plan(root, plan, check_files=False).reasons)
    record = receipt.run(run_id) if receipt is not None else None
    if record is not None and record.closed:
        reasons.append(f"run {run_id[:8]} is closed: a later plan's run replaced it")
    baseline = load_contract_state(root).baseline
    if plan.baseline_trace_path and plan.baseline_captured_at and (
            baseline is None
            or not same_slot_path(root, plan.baseline_trace_path, baseline.trace_path)
            or plan.baseline_captured_at != baseline.captured_at):
        reasons.append("the baseline the plan is bound to was replaced")
    return reasons


def _post_not_in(root, receipt, ledger) -> set[tuple[str, str]]:
    """The steps in effect the post-migration capture does not hold: none of their current
    epoch's new statements resolves in its snapshot P (design 1.3, 5.4; ``ledger_coverage``).
    Coverage calls a step none of whose new or old statements resolves in P ``superseded``:
    overtaken by a later edit, so in the capture all the same. That does not hold when P has an
    OLDER epoch of the same step (its new statements resolve there): the capture predates the
    current epoch (a re-record diffs from the step's first before-text, which P no longer has),
    so the step is not in it."""
    if receipt is None or ledger is None:
        return set()
    from pymolt.apply_ledger import in_effect_entries
    from pymolt.verify.service import load_contract_state, load_sources
    from pymolt.workorder.coverage import ledger_coverage, setup_pythons

    state = load_contract_state(root)
    post = state.post_migration
    if post is None or not post.sources_path:
        return set()
    new = load_sources(post.sources_path)
    if not new:
        return set()
    old = load_sources(state.baseline.sources_path) if state.baseline is not None \
        and state.baseline.sources_path else None
    entries, _note, _l = in_effect_entries(root, receipt)
    if not entries:
        return set()
    base, target = setup_pythons(root)
    # Only P decides here; B gives the old side where the baseline recorded the file.
    coverage = ledger_coverage({**new, **(old or {})}, new, entries, python_old=base,
                               python_new=target)
    return set(coverage.not_in_capture) | {
        ref for ref in coverage.superseded if _older_epoch_in(new, ledger, ref, target)}


def _older_epoch_in(post_sources, ledger, ref: tuple[str, str], python) -> bool:
    """A new statement of an epoch of step ``ref`` older than its current one resolves in the
    post-migration snapshot: the capture holds that epoch, not the current one."""
    from pymolt.workorder.edits import build_index

    current = ledger.current_epochs().get(ref)
    if current is None:
        return False
    indexes: dict[str, object] = {}
    for entry in ledger.entries:
        if (entry.run_id, entry.step_id) != ref or entry.epoch >= current \
                or entry.anchor_new is None or entry.file not in post_sources:
            continue
        if entry.file not in indexes:
            indexes[entry.file] = build_index(post_sources[entry.file], python)
        index = indexes[entry.file]
        if index is not None and index.resolve(entry.anchor_new).ok:
            return True
    return False


def _judgements(root, run_id: str) -> dict[str, dict]:
    """``verification.json``'s ``steps`` (``{id: {status, epoch, ...}}``) when it judged this
    run; {} otherwise (``verify`` writes it per step)."""
    from pathlib import Path

    from pymolt.state_io import read_state_json
    from pymolt.verify.unified import (
        VERIFICATION_KIND,
        VERIFICATION_PATH,
        VERIFICATION_REMEDY,
        VERIFICATION_SCHEMA_NEWEST,
    )

    # A damaged saved verdict raises StateFileError: it is not read as "no judgement".
    data = read_state_json(Path(root) / VERIFICATION_PATH, kind=VERIFICATION_KIND,
                           newest=VERIFICATION_SCHEMA_NEWEST, remedy=VERIFICATION_REMEDY)
    if data is None:
        return {}
    steps = data.get("steps")
    judged_run = data.get("steps_run") or data.get("run_id")
    if not isinstance(steps, dict) or judged_run not in (None, run_id):
        return {}
    compared = ((data.get("contract") or {}).get("compared") or {})
    if compared and compared.get("after_capture") not in (None, "post-migration"):
        return {}                   # a verdict on another recording judges no step (5.4)
    return {k: v for k, v in steps.items() if isinstance(v, dict)}


def _unapplied(root, step: PlanStep, applicable: bool, cache, pythons
               ) -> tuple[str, list[str]]:
    """A step with no epoch: ``present``, ``stale`` (with why) or ``planned``."""
    if step.type == "target-env":
        return ("present", []) if _target_env_exists(root) else ("planned", [])
    if not applicable or not step.edits:
        return "planned", []
    from pymolt.workorder.rules import PRESENT, STALE, decide

    present, reasons = 0, []
    for edit in step.edits:
        digest = cache.sha(edit.file)
        if digest is None:
            reasons.append(f"{edit.file} is missing")
            continue
        if digest in (edit.before_sha256, edit.after_sha256):
            continue
        if step.type == "manifest-pin":
            from pymolt.workorder.rebase import rerun

            decision = decide(step, edit, cache.text(edit.file) or "", rerun=rerun)
            if decision.rule == PRESENT:
                present += 1
            elif decision.rule == STALE:
                reasons += decision.reasons
            continue
        found = [(a, cache.resolve(edit.file, a, pythons)) for a in edit.anchors_old]
        lost = [(a, status) for a, status in found if status != "match"]
        if not lost and found:
            continue
        if len(lost) == len(found) and cache.present(edit.file, edit, pythons):
            present += 1
            continue
        reasons += [f"{a.file}: `{' '.join(a.stmt.split())[:60]}` in {a.scope} is {status}"
                    for a, status in lost] or [f"{edit.file}: its change is not in its place"]
    if reasons:
        return "stale", reasons
    if present and present == len(step.edits):
        return "present", []
    if present:
        return "stale", ["its change is made by hand in some of its files but not in others"]
    return "planned", []


def _target_env_exists(root) -> bool:
    from pathlib import Path

    from pymolt.ingestion.config import EnvConfig

    config = EnvConfig.load(Path(root) / ".pymolt" / "env_config.json")
    if config is None or not config.target_env_path:
        return False
    env = Path(config.target_env_path)
    env = env if env.is_absolute() else Path(root) / env
    return any(p.is_file() for p in (env / "bin" / "python", env / "Scripts" / "python.exe"))


__all__ = ["CODE_TYPES", "IN_EFFECT", "JUDGEMENTS", "Projection", "RunView", "StepState",
           "applicable_steps", "basic_statuses", "project_steps", "remaining"]

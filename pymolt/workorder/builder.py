"""The step builder: the plan's automated steps from the preview walk's recipe chains (1.5.1).

HOST side. The preview walk reports each recipe's change to each file it keeps
(``on_step(recipe, path, before, after)``: rules in bundle order, then patterns and move groups,
then Python fixes on the dependency result). ``StepRecorder`` collects them; per file they form
the chain T0 → T1 → … → Tn, and Tn is the file's ``changes[].new_source``.

``automated_steps`` makes one step per recipe, or per move group, across every file it changes,
ordered by phase and then by application order (a merge of the per-file orders, so every file's
composition is reproduced). Each step's edits are made on the step's own before-text and
after-text (``workorder.edits.make_edit``), and its ``depends_on`` comes from effect overlap
(``workorder.depends``). A file whose chain was not recorded, or does not run
from its ``old_source`` to its ``new_source``, keeps its whole change in one ``whole-file`` step,
with the reason in the step's evidence: the plan never guesses where one recipe ended.
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pymolt.anchors import StatementIndex
from pymolt.codemods.models import CodemodPattern, MoveGroup, recipe_key
from pymolt.codemods.rules import CodemodRule
from pymolt.workorder.depends import assign_depends_on
from pymolt.workorder.edits import apply_hunks, build_index, make_edit, sha256
from pymolt.workorder.models import (
    TEXT_CAP,
    PlanStep,
    StepDo,
    StepEdit,
    StepEffort,
    StepRecipe,
    StepSymbol,
    StepVerify,
    StepWhy,
    step_id,
    step_key,
)

# Stage of a recipe within the mechanical phase: the walk's order (rules, then patterns and
# move groups, then Python fixes). Used only to order recipes that never meet in one file.
_STAGE = {"rule": 0, "pattern": 1, "move-group": 1, "python": 2}


@dataclass
class _Event:
    recipe: Any
    before: str
    after: str


class StepRecorder:
    """Collects ``on_step(recipe, path, before, after)`` events per file (resolved path)."""

    def __init__(self) -> None:
        self.events: dict[str, list[_Event]] = defaultdict(list)
        self.packages: dict[str, str] = {}

    def record(self, recipe: Any, path: str, before: str, after: str) -> None:
        self.events[str(Path(path).resolve())].append(_Event(recipe, before, after))

    def note_packages(self, by_pkg: Mapping[str, Iterable[Any]]) -> None:
        """Which package each fetched recipe belongs to (``preview_codemods``'s ``by_pkg``)."""
        for package, items in by_pkg.items():
            for item in items or ():
                self.packages.setdefault(recipe_key(item), package)

    def chain(self, path: str | Path, old: str, new: str) -> list[_Event] | None:
        """The file's events when they run exactly from ``old`` to ``new``; None otherwise."""
        events = self.events.get(str(Path(path).resolve())) or []
        if not events:
            return None
        text = old
        for event in events:
            if event.before != text:
                return None
            text = event.after
        return events if text == new else None


# ── recipe identity ───────────────────────────────────────────────────────────────────────


@dataclass
class _Recipe:
    """One recipe (or move group) as a step: its identity and what each file got from it."""

    ident: str
    kind: str                 # rule | pattern | move-group
    stage: int
    python: bool
    rule: CodemodRule | None = None
    patterns: list[CodemodPattern] = field(default_factory=list)
    move: tuple[str, str] | None = None
    files: list[tuple[str, str, str]] = field(default_factory=list)   # (rel, before, after)


def _identify(recipe: Any) -> tuple[str, str, bool]:
    """``(ident, kind, python)`` for an observed recipe."""
    if isinstance(recipe, MoveGroup):
        python = any(getattr(m, "scope", "dependency") == "python" for m in recipe.members)
        return (f"move:{recipe.old_module}→{recipe.new_module}"
                + ("|python" if python else ""), "move-group", python)
    if isinstance(recipe, CodemodRule):
        return f"rule|{recipe_key(recipe)}", "rule", False
    python = getattr(recipe, "scope", "dependency") == "python"
    return f"pattern|{recipe_key(recipe)}" + ("|python" if python else ""), "pattern", python


def _members(r: _Recipe) -> list[Any]:
    return [r.rule] if r.rule is not None else list(r.patterns)


def _merge_order(sequences: list[list[str]], stage: Mapping[str, int]) -> list[str]:
    """One order consistent with every file's order of recipes (each is a subsequence of the
    walk's global order), ties broken by stage, then by first appearance."""
    first: dict[str, int] = {}
    succ: dict[str, set[str]] = defaultdict(set)
    indeg: dict[str, int] = defaultdict(int)
    for seq in sequences:
        for ident in seq:
            first.setdefault(ident, len(first))
        for a, b in zip(seq, seq[1:], strict=False):
            if b not in succ[a]:
                succ[a].add(b)
                indeg[b] += 1
    heap = [(stage[i], first[i], i) for i in first if not indeg[i]]
    heapq.heapify(heap)
    out: list[str] = []
    while heap:
        _s, _f, ident = heapq.heappop(heap)
        out.append(ident)
        for nxt in succ[ident]:
            indeg[nxt] -= 1
            if not indeg[nxt]:
                heapq.heappush(heap, (stage[nxt], first[nxt], nxt))
    if len(out) != len(first):          # a cycle: the walk's order was not one order
        out += sorted((i for i in first if i not in out), key=lambda i: (stage[i], first[i]))
    return out


# ── claims and titles ─────────────────────────────────────────────────────────────────────


def _claim(members: list[Any]) -> str | None:
    """The basis every member claims (``apply_ledger.claim_for``); None when they differ or
    one makes none (its edit is judged as a hand edit)."""
    from pymolt.apply_ledger import claim_for

    claims = {claim_for(m) for m in members}
    return claims.pop() if len(claims) == 1 else None


def _short(qualname: str | None) -> str:
    if not qualname:
        return "?"
    parts = qualname.split(".")
    return ".".join(parts[-2:]) if len(parts) > 1 else qualname


def _cap(text: str, cap: int = 120) -> str:
    one = " ".join(str(text).split())
    return one if len(one) <= cap else one[:cap - 1] + "…"


def _title(r: _Recipe) -> str:
    if r.kind == "move-group":
        old, new = r.move or ("?", "?")
        names = [m.old_qualname.rsplit(".", 1)[-1] for m in r.patterns]
        if len(names) == 1:
            only = r.patterns[0]
            return f"`{_short(only.old_qualname)}` → `{_short(only.new_qualname)}`"
        return _cap(f"move {', '.join(names)}: `{old}` → `{new}`")
    if r.rule is not None:
        rule = r.rule
        if rule.old_qualname and rule.new_qualname:
            return f"`{_short(rule.old_qualname)}` → `{_short(rule.new_qualname)}`"
        return _cap(f"`{rule.match}` → `{(rule.rewrite or [''])[0]}`")
    p = r.patterns[0]
    return f"`{_short(p.old_qualname)}` → `{_short(p.new_qualname)}`"


def _symbol(r: _Recipe) -> StepSymbol:
    if r.rule is not None:
        return StepSymbol(old=r.rule.old_qualname, new=r.rule.new_qualname)
    if len(r.patterns) == 1:
        return StepSymbol(old=r.patterns[0].old_qualname, new=r.patterns[0].new_qualname)
    old, new = r.move or (None, None)
    return StepSymbol(old=old, new=new)


def _evidence(members: list[Any]) -> list[str]:
    out: list[str] = []
    for m in members:
        for line in getattr(m, "evidence", None) or ():
            text = _cap(str(line), TEXT_CAP)
            if text not in out:
                out.append(text)
    return out[:10]


def _runs_on_base(members: list[Any]) -> bool | None:
    flags = [getattr(m, "runs_on_base", None) for m in members
             if getattr(m, "scope", "dependency") == "python"]
    if not flags:
        return None
    if all(f is True for f in flags):
        return True
    return False if any(f is False for f in flags) else None


def _checks(files: Iterable[str], fix_checks: Mapping[str, Mapping[str, str]]
            ) -> dict[str, str] | None:
    """A python step's plan-time re-compile results over its files: ``ok`` when every file's
    was, else the first other result (with its file)."""
    rows = [(rel, fix_checks[rel]) for rel in files if rel in fix_checks]
    if not rows:
        return None
    out: dict[str, str] = {}
    for name in ("compile_target", "compile_base"):
        values = [(rel, row.get(name, "not checked: not recorded")) for rel, row in rows]
        other = next(((rel, v) for rel, v in values if v != "ok"), None)
        out[name] = "ok" if other is None else (
            other[1] if len(rows) == 1 else f"{other[1]} ({other[0]})")
    return out


# ── the build ─────────────────────────────────────────────────────────────────────────────


class _Texts:
    """Statement indexes per text, shared between a step's after-text and the next step's
    before-text. T0 is the base Python's code; every later text the target's."""

    def __init__(self, base: Any, target: Any):
        self.base, self.target = base, target
        self._cache: dict[tuple[str, str], StatementIndex | None] = {}

    def index(self, text: str, *, t0: bool) -> StatementIndex | None:
        key = (sha256(text), "t0" if t0 else "t")
        if key not in self._cache:
            self._cache[key] = build_index(text, self.base if t0 else self.target)
        return self._cache[key]


def automated_steps(changes: list[Any], recorder: StepRecorder | None, *, root: str | Path,
                    dependencies: list[Any] = (), python_move: tuple[str, str] | None = None,
                    fix_checks: Mapping[str, Mapping[str, str]] | None = None,
                    target_python: str | None = None) -> list[PlanStep]:
    """The mechanical phase's steps for ``changes`` (``PlannedFileChange``-like: ``path``,
    ``old_source``, ``new_source``, ``recipes``, ``claims``, ``sites``)."""
    root = Path(root)
    base = python_move[0] if python_move else None
    target = python_move[1] if python_move else target_python
    texts = _Texts(base, target)
    fix_checks = fix_checks or {}
    recipes: dict[str, _Recipe] = {}
    sequences: list[list[str]] = []
    whole: list[Any] = []
    t0_of: dict[str, str] = {}
    for change in changes:
        t0_of[change.path] = change.old_source
        events = recorder.chain(root / change.path, change.old_source,
                                change.new_source) if recorder else None
        if events is None:
            whole.append(change)
            continue
        seq: list[str] = []
        for event in events:
            ident, kind, python = _identify(event.recipe)
            r = recipes.get(ident)
            if r is None:
                r = recipes[ident] = _Recipe(
                    ident=ident, kind=kind, python=python,
                    stage=_STAGE["python" if python else kind])
                if isinstance(event.recipe, MoveGroup):
                    r.move = (event.recipe.old_module, event.recipe.new_module)
                    r.patterns = list(event.recipe.members)
                elif isinstance(event.recipe, CodemodRule):
                    r.rule = event.recipe
                else:
                    r.patterns = [event.recipe]
            elif isinstance(event.recipe, MoveGroup):
                known = {recipe_key(m) for m in r.patterns}
                r.patterns += [m for m in event.recipe.members if recipe_key(m) not in known]
            r.files.append((change.path, event.before, event.after))
            seq.append(ident)
        sequences.append(seq)

    order = _merge_order(sequences, {i: r.stage for i, r in recipes.items()})
    steps: list[PlanStep] = []
    befores: dict[tuple[str, str], str] = {}
    for ident in order:
        step = _step(recipes[ident], len(steps) + 1, texts, t0_of, recorder, dependencies,
                     python_move, fix_checks)
        steps.append(step)
        for rel, before, _after in recipes[ident].files:
            befores[(step.id, rel)] = before
    for group in _whole_groups(whole):
        steps.append(_whole_step(group, len(steps) + 1, recorder is not None, base, target))
    assign_depends_on(steps, lambda sid, rel: befores.get((sid, rel)))
    return steps


def _package(r: _Recipe, recorder: StepRecorder | None,
             dependencies: list[Any]) -> tuple[str | None, str | None, str | None]:
    """``(package, from, to)`` of a dependency step."""
    if r.rule is not None:
        return r.rule.library or None, r.rule.from_version or None, r.rule.to_version or None
    names = {(recorder.packages if recorder else {}).get(recipe_key(m)) for m in r.patterns}
    names.discard(None)
    if len(names) == 1:
        name = names.pop()
        dep = next((d for d in dependencies if getattr(d, "name", None) == name), None)
        return name, getattr(dep, "from_version", None), getattr(dep, "to_version", None)
    return None, None, None


def _step(r: _Recipe, n: int, texts: _Texts, t0_of: Mapping[str, str],
          recorder: StepRecorder | None, dependencies: list[Any],
          python_move: tuple[str, str] | None,
          fix_checks: Mapping[str, Mapping[str, str]]) -> PlanStep:
    members = _members(r)
    edits: list[StepEdit] = []
    where = []
    for rel, before, after in r.files:
        t0 = t0_of[rel]
        index_before = texts.index(before, t0=before == t0)
        index_after = texts.index(after, t0=False)
        edit = make_edit(rel, before, after, index_before=index_before,
                         index_after=index_after)
        edits.append(edit)
        where.extend(_on_t0(edit, before == t0, texts.index(t0, t0=True), rel))
    sites = sum(max(1, len(e.effect.replace) + len(e.effect.delete) + len(e.effect.insert))
                for e in edits)
    basis = _claim(members)
    if r.python:
        package, from_, to = None, *(python_move or (None, None))
    else:
        package, from_, to = _package(r, recorder, dependencies)
    step_type = "python-fix" if r.python else "codemod"
    keys = [recipe_key(m) for m in members]
    return PlanStep(
        id=step_id("mechanical", n),
        key=step_key(step_type, r.ident),
        phase="mechanical", type=step_type, title=_title(r), executor="pymolt",
        claim="preserve" if basis else None, basis=basis,
        runs_on_base=_runs_on_base(members),
        scope="python" if r.python else "dependency",
        package=package, from_=from_, to=to, symbol=_symbol(r),
        where=where,
        why=StepWhy(evidence=_evidence(members)),
        do=StepDo(recipe=StepRecipe(kind=r.kind, keys=keys, rule=r.rule,
                                    patterns=[] if r.rule is not None else r.patterns,
                                    move=r.move)),
        edits=edits,
        verify=StepVerify(
            judge="strict" if basis else "none",
            note=None if basis else "the recipe makes no claim: verify judges its lines as "
                                    "hand edits"),
        effort=StepEffort(size="none", sites=sites, files=len(edits),
                          why="executor pymolt: pymolt applies it"),
        checks_at_plan=_checks([rel for rel, _b, _a in r.files], fix_checks) if r.python
        else None,
    )


def _on_t0(edit: StepEdit, before_is_t0: bool, index_t0: StatementIndex | None,
           rel: str) -> list:
    """The statements a step rewrites, as anchors on T0 (``where``): its own old anchors when
    it ran on T0, else those of them T0 still has, resolved there."""
    if before_is_t0:
        return list(edit.anchors_old)
    if index_t0 is None:
        return []
    out = []
    for anchor in edit.anchors_old:
        found = index_t0.resolve(anchor)
        if found.ok:
            out.append(index_t0.anchor(found.statement, file=rel))
    return out


def _whole_groups(changes: list[Any]) -> list[list[Any]]:
    groups: dict[tuple[str, ...], list[Any]] = {}
    for change in changes:
        groups.setdefault(tuple(change.recipes), []).append(change)
    return list(groups.values())


def _whole_step(changes: list[Any], n: int, recorded: bool, base: Any, target: Any
                ) -> PlanStep:
    """One step for files whose change was not split by recipe (see the module docstring)."""
    recipes = list(dict.fromkeys(r for c in changes for r in c.recipes))
    claims = [v for c in changes for v in c.claims.values()]
    found = set(claims)
    basis = found.pop() if len(found) == 1 else None
    edits = [make_edit(c.path, c.old_source, c.new_source, python_before=base,
                       python_after=target) for c in changes]
    why = ("the walk did not report this file's recipes one by one" if recorded else
           "the plan was made from previews with no recorded recipe chain")
    python = bool(claims) and all(c == "python-fixer" for c in claims)
    flags = [c.runs_on_base for c in changes if c.runs_on_base is not None]
    step_type = "python-fix" if python else "codemod"
    return PlanStep(
        id=step_id("mechanical", n),
        key=step_key(step_type, "whole|" + "|".join(sorted(recipes))),
        phase="mechanical", type=step_type,
        title=_cap(f"{len(changes)} file(s) rewritten by {len(recipes)} recipe(s), as one "
                   "change"),
        executor="pymolt", claim="preserve" if basis else None, basis=basis,
        runs_on_base=None if not flags else all(flags),
        scope="python" if python else "dependency",
        where=[a for e in edits for a in e.anchors_old],
        why=StepWhy(evidence=[f"one step holds each file's whole change: {why}"]),
        do=StepDo(recipe=StepRecipe(kind="whole-file", keys=recipes, sites="all")),
        edits=edits,
        verify=StepVerify(judge="strict" if basis else "none"),
        effort=StepEffort(size="none", sites=sum(c.sites for c in changes),
                          files=len(changes), why="executor pymolt: pymolt applies it"),
    )


def replay_changes(changes: list[Any], steps: list[PlanStep]) -> dict[str, str]:
    """Each changed file's text after every pymolt code step, rebuilt from T0 by the steps'
    hunks in order: the ``changes`` oracle (it must equal ``new_source``)."""
    out = {c.path: c.old_source for c in changes}
    for step in steps:
        if step.executor != "pymolt" or step.phase != "mechanical":
            continue
        for edit in step.edits:
            if edit.file in out:
                if sha256(out[edit.file]) != edit.before_sha256:
                    raise ValueError(f"{step.id} does not start from the text before it in "
                                     f"{edit.file}")
                out[edit.file] = apply_hunks(out[edit.file], edit.hunks)
    return out

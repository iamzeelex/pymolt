"""
axiom_graph/analyzers/ssa.py

Static Single Assignment (SSA) construction over the CFG from cfg.py.

Pipeline:
  1. build_cfg_from_source(...)            → ControlFlowGraph
  2. dominator tree (Cooper-Harvey-Kennedy "A Simple, Fast Dominance Algorithm")
  3. dominance frontiers (Cytron et al.)
  4. phi-function placement (minimal SSA, per Cytron)
  5. variable renaming (dominator-tree DFS, version stacks)

Result: every local variable is assigned exactly once; at control-flow join
points a phi-function `x_3 = φ(x_1, x_2)` selects the value flowing in from each
predecessor. Free names (builtins / globals / imported symbols — never defined
or passed as a parameter inside the function) are left un-versioned.

def/use extraction is LibCST-structural:
  - Attribute `.attr` and keyword-argument names are NOT uses.
  - `a.b = …` / `a[i] = …` define nothing local (a, i become uses).
  - AugAssign target (`x += 1`) is both a use and a def.

Scope inherits cfg.py's: try/with/match flow is approximated; comprehension and
nested-function scoping are not modeled. Honest by construction — see notes.

Pure and offline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import libcst as cst

from axiom_graph.analyzers.cfg import (
    ControlFlowGraph,
    Statement,
    build_cfg_from_source,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# SSA models
# ─────────────────────────────────────────────────────────────────────────────


def ssa_name(var: str, version: int) -> str:
    return f"{var}_{version}"


@dataclass
class PhiFunction:
    """`target = φ(args[pred] for pred in predecessors)` at a join block."""

    var: str
    target: str  # versioned, e.g. "x_3"
    args: dict[int, str] = field(default_factory=dict)  # pred_block_id -> versioned name

    def render(self) -> str:
        parts = ", ".join(
            f"{self.args[p]}@B{p}" for p in sorted(self.args)
        )
        return f"{self.target} = φ({parts})"


@dataclass
class SSAInstruction:
    """One lowered statement with versioned defs/uses alongside the source."""

    code: str
    defs: list[str] = field(default_factory=list)   # versioned
    uses: list[str] = field(default_factory=list)   # versioned or free name
    node: object | None = None
    """The source LibCST node (cst.CSTNode) for value-flow extraction; None
    for synthesized param/phi instructions."""

    def render(self) -> str:
        return self.code


@dataclass
class SSABlock:
    id: int
    label: str
    phis: list[PhiFunction] = field(default_factory=list)
    instructions: list[SSAInstruction] = field(default_factory=list)


@dataclass
class SSAForm:
    func_name: str
    blocks: dict[int, SSABlock]
    cfg: ControlFlowGraph
    free_names: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)

    def all_phis(self) -> list[PhiFunction]:
        out: list[PhiFunction] = []
        for b in self.blocks.values():
            out.extend(b.phis)
        return out

    def render(self) -> str:
        """Human-readable SSA dump in block order."""
        lines: list[str] = [f"function {self.func_name}:"]
        for bid in sorted(self.blocks):
            b = self.blocks[bid]
            preds = self.cfg.predecessors(bid)
            succs = self.cfg.successors(bid)
            lines.append(f"  B{bid} [{b.label}]  preds={preds} succs={succs}")
            for phi in b.phis:
                lines.append(f"      {phi.render()}")
            for ins in b.instructions:
                tag = ""
                if ins.defs or ins.uses:
                    tag = f"   ; def={ins.defs} use={ins.uses}"
                lines.append(f"      {ins.code}{tag}")
        if self.free_names:
            lines.append(f"  free (global/builtin): {sorted(self.free_names)}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "func_name": self.func_name,
            "free_names": sorted(self.free_names),
            "blocks": {
                str(bid): {
                    "label": b.label,
                    "phis": [phi.render() for phi in b.phis],
                    "instructions": [
                        {"code": i.code, "defs": i.defs, "uses": i.uses}
                        for i in b.instructions
                    ],
                }
                for bid, b in self.blocks.items()
            },
            "notes": self.notes,
        }


# ─────────────────────────────────────────────────────────────────────────────
# def / use extraction (LibCST-structural)
# ─────────────────────────────────────────────────────────────────────────────


# Python keyword-constants parse as cst.Name but are not variables.
_NAME_CONSTANTS = frozenset({"True", "False", "None"})


class _UseCollector(cst.CSTVisitor):
    """Collect load-context Names, skipping attribute tails and kw names."""

    def __init__(self) -> None:
        self.names: list[str] = []

    def visit_Attribute(self, node: cst.Attribute) -> bool:
        # Visit the object (`a` in `a.b`) but never the attribute name (`b`).
        node.value.visit(self)
        return False

    def visit_Arg(self, node: cst.Arg) -> bool:
        # Skip the keyword name (`x` in `f(x=1)`); visit the value.
        node.value.visit(self)
        return False

    def visit_Name(self, node: cst.Name) -> None:
        if node.value not in _NAME_CONSTANTS:
            self.names.append(node.value)


def _expr_uses(node: cst.CSTNode | None) -> list[str]:
    if node is None:
        return []
    c = _UseCollector()
    node.visit(c)
    return c.names


def _target_def_use(target: cst.BaseExpression) -> tuple[list[str], list[str]]:
    """Return (defs, uses) for an assignment target expression."""
    defs: list[str] = []
    uses: list[str] = []

    def walk(t: cst.BaseExpression) -> None:
        if isinstance(t, cst.Name):
            defs.append(t.value)
        elif isinstance(t, (cst.Tuple, cst.List)):
            for el in t.elements:
                walk(el.value)
        elif isinstance(t, cst.StarredElement):
            walk(t.value)
        elif isinstance(t, (cst.Attribute, cst.Subscript)):
            # `a.b = …` / `a[i] = …` bind no local var; a, i are reads.
            uses.extend(_expr_uses(t))
        else:
            uses.extend(_expr_uses(t))

    walk(target)
    return defs, uses


def _stmt_def_use(node: cst.CSTNode) -> tuple[list[str], list[str]]:
    """Extract (defs, uses) from a single lowered statement node."""
    defs: list[str] = []
    uses: list[str] = []

    if isinstance(node, cst.SimpleStatementLine):
        # Unwrap a line into its small statements (e.g. a synthesized Assign).
        for small in node.body:
            d, u = _stmt_def_use(small)
            defs.extend(d)
            uses.extend(u)
        return _dedup(defs), _dedup(uses)

    if isinstance(node, cst.Assign):
        for at in node.targets:
            d, u = _target_def_use(at.target)
            defs.extend(d)
            uses.extend(u)
        uses.extend(_expr_uses(node.value))

    elif isinstance(node, cst.AnnAssign):
        if node.value is not None:
            d, u = _target_def_use(node.target)
            defs.extend(d)
            uses.extend(u)
            uses.extend(_expr_uses(node.value))
        # bare `x: int` (no value) binds nothing.

    elif isinstance(node, cst.AugAssign):
        # target is both read and written
        if isinstance(node.target, cst.Name):
            defs.append(node.target.value)
            uses.append(node.target.value)
        else:
            uses.extend(_expr_uses(node.target))
        uses.extend(_expr_uses(node.value))

    elif isinstance(node, cst.If):       # if-test marker
        uses.extend(_expr_uses(node.test))
    elif isinstance(node, cst.While):     # while-test marker
        uses.extend(_expr_uses(node.test))
    elif isinstance(node, cst.Return):
        uses.extend(_expr_uses(node.value))
    elif isinstance(node, cst.Raise):
        uses.extend(_expr_uses(node.exc))
    elif isinstance(node, cst.Expr):
        uses.extend(_expr_uses(node.value))
    elif isinstance(node, (cst.FunctionDef, cst.ClassDef)):
        defs.append(node.name.value)

    # De-dup preserving order.
    return _dedup(defs), _dedup(uses)


def _dedup(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Dominators (Cooper-Harvey-Kennedy)
# ─────────────────────────────────────────────────────────────────────────────


def _postorder(cfg: ControlFlowGraph) -> list[int]:
    """Postorder over blocks reachable from entry."""
    order: list[int] = []
    visited: set[int] = set()

    def dfs(b: int) -> None:
        visited.add(b)
        for s in cfg.successors(b):
            if s not in visited:
                dfs(s)
        order.append(b)

    dfs(cfg.entry)
    return order


def compute_idoms(cfg: ControlFlowGraph) -> dict[int, int]:
    """
    Immediate dominators for every reachable block.

    Returns idom[b] for each reachable block; idom[entry] == entry.
    """
    postorder = _postorder(cfg)
    reachable = set(postorder)
    # Reverse-postorder index for the intersect routine.
    rpo = list(reversed(postorder))
    rpo_index = {b: i for i, b in enumerate(rpo)}

    idom: dict[int, int] = {cfg.entry: cfg.entry}

    def intersect(b1: int, b2: int) -> int:
        finger1, finger2 = b1, b2
        while finger1 != finger2:
            while rpo_index[finger1] > rpo_index[finger2]:
                finger1 = idom[finger1]
            while rpo_index[finger2] > rpo_index[finger1]:
                finger2 = idom[finger2]
        return finger1

    changed = True
    while changed:
        changed = False
        for b in rpo:
            if b == cfg.entry:
                continue
            preds = [p for p in cfg.predecessors(b) if p in reachable]
            # Pick first processed predecessor as the running idom.
            new_idom: int | None = None
            for p in preds:
                if p in idom:
                    new_idom = p if new_idom is None else intersect(p, new_idom)
            if new_idom is not None and idom.get(b) != new_idom:
                idom[b] = new_idom
                changed = True

    return idom


def dominance_frontiers(
    cfg: ControlFlowGraph, idom: dict[int, int]
) -> dict[int, set[int]]:
    """Dominance frontier of every reachable block (Cytron et al.)."""
    df: dict[int, set[int]] = {b: set() for b in idom}
    for b in idom:
        preds = [p for p in cfg.predecessors(b) if p in idom]
        if len(preds) < 2:
            continue
        for p in preds:
            runner = p
            while runner != idom[b]:
                df[runner].add(b)
                if runner == idom.get(runner):  # reached entry self-dominator
                    break
                runner = idom[runner]
    return df


def _dom_children(idom: dict[int, int]) -> dict[int, list[int]]:
    """Dominator-tree children adjacency (entry excluded as its own child)."""
    children: dict[int, list[int]] = {b: [] for b in idom}
    for b, d in idom.items():
        if b != d:
            children[d].append(b)
    return children


# ─────────────────────────────────────────────────────────────────────────────
# SSA construction
# ─────────────────────────────────────────────────────────────────────────────


def build_ssa(cfg: ControlFlowGraph) -> SSAForm:
    """Construct SSA form from a control-flow graph."""
    idom = compute_idoms(cfg)
    reachable = set(idom)
    df = dominance_frontiers(cfg, idom)

    # --- 1. Gather raw defs/uses per block (unversioned). ------------------
    block_defs: dict[int, list[str]] = {b: [] for b in cfg.blocks}
    block_raw: dict[int, list[tuple[Statement, list[str], list[str]]]] = {
        b: [] for b in cfg.blocks
    }
    all_def_vars: set[str] = set()
    all_used_vars: set[str] = set()

    for bid, block in cfg.blocks.items():
        for stmt in block.statements:
            d, u = _stmt_def_use(stmt.node)
            block_raw[bid].append((stmt, d, u))
            block_defs[bid].extend(d)
            all_def_vars.update(d)
            all_used_vars.update(u)

    # Parameters are defined on entry.
    for p in cfg.params:
        block_defs[cfg.entry].append(p)
        all_def_vars.add(p)

    # Free names: used somewhere but never locally defined / parameter.
    free_names = {n for n in all_used_vars if n not in all_def_vars}
    # Variables we actually version (locals + params).
    ssa_vars = set(all_def_vars)

    # --- 2. Phi placement (minimal SSA). ----------------------------------
    phi_sites: dict[int, set[str]] = {b: set() for b in cfg.blocks}
    for var in ssa_vars:
        defsites = [b for b in reachable if var in set(block_defs[b])]
        worklist = list(defsites)
        seen_defsites = set(defsites)
        while worklist:
            b = worklist.pop()
            for frontier in df.get(b, set()):
                if var not in phi_sites[frontier]:
                    phi_sites[frontier].add(var)
                    if frontier not in seen_defsites:
                        seen_defsites.add(frontier)
                        worklist.append(frontier)

    # --- 3. Build SSA blocks with empty phis to fill during rename. -------
    ssa_blocks: dict[int, SSABlock] = {}
    for bid, block in cfg.blocks.items():
        ssa_blocks[bid] = SSABlock(id=bid, label=block.label)
    # Insert (unversioned-for-now) phi placeholders.
    phi_for: dict[tuple[int, str], PhiFunction] = {}
    for bid in reachable:
        for var in sorted(phi_sites[bid]):
            phi = PhiFunction(var=var, target="")
            ssa_blocks[bid].phis.append(phi)
            phi_for[(bid, var)] = phi

    # --- 4. Renaming (dominator-tree DFS with version stacks). ------------
    counters: dict[str, int] = {v: 0 for v in ssa_vars}
    stacks: dict[str, list[int]] = {v: [] for v in ssa_vars}
    dom_children = _dom_children(idom)

    def new_version(var: str) -> str:
        counters[var] += 1
        ver = counters[var]
        stacks[var].append(ver)
        return ssa_name(var, ver)

    def top_name(var: str) -> str:
        if stacks.get(var):
            return ssa_name(var, stacks[var][-1])
        # Used before any definition on this path: free/undefined.
        return var

    def rename(bid: int) -> None:
        pushed: list[str] = []

        # 4a. params defined at entry, then phi targets get fresh versions.
        if bid == cfg.entry:
            for p in cfg.params:
                ssa_blocks[bid].instructions.append(
                    SSAInstruction(code=f"{p} = <param>", defs=[new_version(p)])
                )
                pushed.append(p)

        for phi in ssa_blocks[bid].phis:
            phi.target = new_version(phi.var)
            pushed.append(phi.var)

        # 4b. ordinary statements: rename uses (top), then defs (fresh).
        for stmt, d, u in block_raw[bid]:
            used = [top_name(x) if x in ssa_vars else x for x in u]
            defined = [new_version(x) for x in d]
            for x in d:
                pushed.append(x)
            ssa_blocks[bid].instructions.append(
                SSAInstruction(code=stmt.code, defs=defined, uses=used, node=stmt.node)
            )

        # 4c. fill phi args in CFG successors for the vars we currently define.
        for succ in cfg.successors(bid):
            for phi in ssa_blocks[succ].phis:
                phi.args[bid] = top_name(phi.var)

        # 4d. recurse into dominator-tree children.
        for child in dom_children.get(bid, []):
            rename(child)

        # 4e. pop everything defined in this block.
        for var in pushed:
            if stacks.get(var):
                stacks[var].pop()

    if cfg.entry in reachable:
        rename(cfg.entry)

    notes = list(cfg.notes)
    unreached = set(cfg.blocks) - reachable
    if unreached:
        notes.append(f"{len(unreached)} unreachable block(s) skipped in SSA")

    return SSAForm(
        func_name=cfg.func_name,
        blocks=ssa_blocks,
        cfg=cfg,
        free_names=free_names,
        notes=notes,
    )


def build_ssa_from_source(source: str, func_name: str | None = None) -> SSAForm:
    """Parse source, build CFG, then SSA — the one-call entry point."""
    cfg = build_cfg_from_source(source, func_name)
    return build_ssa(cfg)

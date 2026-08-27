"""
axiom_graph/analyzers/cfg.py

Control-Flow Graph (CFG) builder for a single Python function, driven by LibCST.

We parse a function with LibCST, then walk its statement list and lower the
structured control flow (if / while / for / break / continue / return) into an
explicit graph of basic blocks. Each basic block is a maximal run of straight-
line statements with a single entry and single exit; branches and loops become
edges between blocks.

This CFG is the substrate for SSA construction (see ssa.py): SSA needs explicit
join points (where phi-functions go) and a dominator tree, both of which are
graph properties that the structured AST hides.

Scope (v1 — honest):
  Supported : function params, assignments, AnnAssign, AugAssign, expression
              statements, pass, if/elif/else, while, for, break, continue,
              return, raise (as a terminator).
  Deferred  : try/except/finally, with, match, async constructs, comprehension
              scoping, nested def/lambda bodies, global/nonlocal. These parse
              fine but their control flow is approximated (treated as straight
              line) — flagged in ControlFlowGraph.notes.

Pure and offline: LibCST parse only, no imports, no network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

import libcst as cst

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────────


class EdgeKind(str, Enum):
    """Why control flows from one block to another."""

    FALL = "fall"          # straight-line fall-through
    TRUE = "true"          # condition evaluated true
    FALSE = "false"        # condition evaluated false
    BACK = "back"          # loop back-edge (body → header)
    BREAK = "break"        # break → loop exit
    CONTINUE = "continue"  # continue → loop header
    RETURN = "return"      # return/raise → function exit


@dataclass
class Statement:
    """A single small/simple statement inside a basic block."""

    code: str
    """Rendered source (whitespace-normalized)."""

    node: cst.CSTNode
    """Original LibCST node — kept for def/use extraction in SSA."""

    def __repr__(self) -> str:
        return self.code


@dataclass
class BasicBlock:
    """A maximal straight-line run of statements: one entry, one exit."""

    id: int
    label: str
    statements: list[Statement] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.statements

    def __repr__(self) -> str:
        return f"B{self.id}({self.label})"


@dataclass(frozen=True)
class CFGEdge:
    src: int
    dst: int
    kind: EdgeKind


@dataclass
class ControlFlowGraph:
    """Explicit control-flow graph for one function."""

    func_name: str
    blocks: dict[int, BasicBlock]
    edges: list[CFGEdge]
    entry: int
    exit: int
    params: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # -- graph queries -------------------------------------------------------

    def successors(self, block_id: int) -> list[int]:
        return [e.dst for e in self.edges if e.src == block_id]

    def predecessors(self, block_id: int) -> list[int]:
        return [e.src for e in self.edges if e.dst == block_id]

    def edges_from(self, block_id: int) -> list[CFGEdge]:
        return [e for e in self.edges if e.src == block_id]

    def reachable_blocks(self) -> set[int]:
        """Block ids reachable from entry (forward DFS)."""
        seen: set[int] = set()
        stack = [self.entry]
        while stack:
            b = stack.pop()
            if b in seen:
                continue
            seen.add(b)
            stack.extend(self.successors(b))
        return seen

    def to_dict(self) -> dict:
        """JSON-friendly representation for debugging / visualization."""
        return {
            "func_name": self.func_name,
            "entry": self.entry,
            "exit": self.exit,
            "params": self.params,
            "blocks": {
                str(bid): {
                    "label": b.label,
                    "statements": [s.code for s in b.statements],
                }
                for bid, b in self.blocks.items()
            },
            "edges": [
                {"src": e.src, "dst": e.dst, "kind": e.kind.value}
                for e in self.edges
            ],
            "notes": self.notes,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Builder
# ─────────────────────────────────────────────────────────────────────────────

# Compound statements whose control flow v1 does NOT model precisely.
_APPROXIMATED = (
    cst.Try,
    cst.With,
    cst.Match,
)


@dataclass
class _LoopCtx:
    """Targets for break/continue inside a loop."""

    header: int
    exit: int


class _CFGBuilder:
    """Lowers a LibCST FunctionDef body into a ControlFlowGraph."""

    def __init__(self, func: cst.FunctionDef, module: cst.Module) -> None:
        self.func = func
        self.module = module
        self.blocks: dict[int, BasicBlock] = {}
        self.edges: set[CFGEdge] = set()
        self._next_id = 0
        self._loops: list[_LoopCtx] = []
        self.notes: list[str] = []

        self.entry = self._new_block("entry")
        self.exit = self._new_block("exit")

    # -- block / edge helpers ------------------------------------------------

    def _new_block(self, label: str) -> int:
        bid = self._next_id
        self._next_id += 1
        self.blocks[bid] = BasicBlock(id=bid, label=label)
        return bid

    def _add_edge(self, src: int, dst: int, kind: EdgeKind) -> None:
        self.edges.add(CFGEdge(src, dst, kind))

    def _emit(self, block_id: int, node: cst.CSTNode) -> None:
        code = self.module.code_for_node(node).strip()
        self.blocks[block_id].statements.append(Statement(code=code, node=node))

    # -- params --------------------------------------------------------------

    def _param_names(self) -> list[str]:
        names: list[str] = []
        params = self.func.params
        groups = [
            params.params,
            params.posonly_params,
            list(params.kwonly_params),
        ]
        for group in groups:
            for p in group:
                if isinstance(p.name, cst.Name):
                    names.append(p.name.value)
        if isinstance(params.star_arg, cst.Param) and isinstance(
            params.star_arg.name, cst.Name
        ):
            names.append(params.star_arg.name.value)
        if isinstance(params.star_kwarg, cst.Param) and isinstance(
            params.star_kwarg.name, cst.Name
        ):
            names.append(params.star_kwarg.name.value)
        return names

    # -- statement-list lowering ---------------------------------------------

    def _process_body(self, body: list[cst.BaseStatement], cur: int | None) -> int | None:
        """
        Process a sequence of statements starting in block `cur`.

        Returns the block control falls through to, or None if the sequence
        terminates (return/break/continue make the tail unreachable).
        """
        for stmt in body:
            if cur is None:
                # Unreachable tail after a terminator; record once and stop.
                self.notes.append("unreachable code after terminator (dropped)")
                return None
            cur = self._process_statement(stmt, cur)
        return cur

    def _process_statement(self, stmt: cst.BaseStatement, cur: int) -> int | None:
        if isinstance(stmt, cst.SimpleStatementLine):
            return self._process_simple_line(stmt, cur)
        if isinstance(stmt, cst.If):
            return self._process_if(stmt, cur)
        if isinstance(stmt, cst.While):
            return self._process_while(stmt, cur)
        if isinstance(stmt, cst.For):
            return self._process_for(stmt, cur)
        if isinstance(stmt, _APPROXIMATED):
            # Approximate: treat the whole compound as one straight-line stmt.
            self.notes.append(
                f"{type(stmt).__name__} control flow approximated (treated linear)"
            )
            self._emit(cur, stmt)
            return cur
        if isinstance(stmt, (cst.FunctionDef, cst.ClassDef)):
            # Nested definition: a binding in the current scope, body not lowered.
            self.notes.append(f"nested {type(stmt).__name__} body not lowered")
            self._emit(cur, stmt)
            return cur
        # Fallback: emit as-is.
        self._emit(cur, stmt)
        return cur

    def _process_simple_line(
        self, line: cst.SimpleStatementLine, cur: int
    ) -> int | None:
        """A line of one or more small statements (semicolon-separated)."""
        for small in line.body:
            if isinstance(small, cst.Return):
                self._emit(cur, small)
                self._add_edge(cur, self.exit, EdgeKind.RETURN)
                return None
            if isinstance(small, cst.Raise):
                self._emit(cur, small)
                self._add_edge(cur, self.exit, EdgeKind.RETURN)
                return None
            if isinstance(small, cst.Break):
                if self._loops:
                    self._add_edge(cur, self._loops[-1].exit, EdgeKind.BREAK)
                else:
                    self.notes.append("break outside loop (ignored)")
                return None
            if isinstance(small, cst.Continue):
                if self._loops:
                    self._add_edge(cur, self._loops[-1].header, EdgeKind.CONTINUE)
                else:
                    self.notes.append("continue outside loop (ignored)")
                return None
            # Ordinary small statement (Assign, AnnAssign, AugAssign, Expr, Pass…)
            self._emit(cur, small)
        return cur

    def _process_if(self, node: cst.If, cur: int) -> int | None:
        # The test is evaluated in `cur`. We attach it as a marker statement.
        test_src = self.module.code_for_node(node.test).strip()
        self._emit(cur, cst.parse_statement(f"if {test_src}: ..."))

        then_entry = self._new_block("then")
        self._add_edge(cur, then_entry, EdgeKind.TRUE)
        then_exit = self._process_body(node.body.body, then_entry)

        merge = self._new_block("merge")

        # else / elif chain
        if node.orelse is None:
            self._add_edge(cur, merge, EdgeKind.FALSE)
        elif isinstance(node.orelse, cst.If):
            else_entry = self._new_block("elif")
            self._add_edge(cur, else_entry, EdgeKind.FALSE)
            else_exit = self._process_if(node.orelse, else_entry)
            if else_exit is not None:
                self._add_edge(else_exit, merge, EdgeKind.FALL)
        else:  # cst.Else
            else_entry = self._new_block("else")
            self._add_edge(cur, else_entry, EdgeKind.FALSE)
            else_exit = self._process_body(node.orelse.body.body, else_entry)
            if else_exit is not None:
                self._add_edge(else_exit, merge, EdgeKind.FALL)

        if then_exit is not None:
            self._add_edge(then_exit, merge, EdgeKind.FALL)

        return merge

    def _process_while(self, node: cst.While, cur: int) -> int | None:
        header = self._new_block("while.head")
        self._add_edge(cur, header, EdgeKind.FALL)
        self._emit(header, cst.parse_statement(
            f"while {self.module.code_for_node(node.test).strip()}: ..."
        ))

        exit_block = self._new_block("while.exit")
        body_entry = self._new_block("while.body")
        self._add_edge(header, body_entry, EdgeKind.TRUE)
        self._add_edge(header, exit_block, EdgeKind.FALSE)

        self._loops.append(_LoopCtx(header=header, exit=exit_block))
        body_exit = self._process_body(node.body.body, body_entry)
        self._loops.pop()

        if body_exit is not None:
            self._add_edge(body_exit, header, EdgeKind.BACK)

        # while/else is rare; approximate by ignoring the else suite if present.
        if node.orelse is not None:
            self.notes.append("while/else suite not modeled")

        return exit_block

    def _process_for(self, node: cst.For, cur: int) -> int | None:
        header = self._new_block("for.head")
        self._add_edge(cur, header, EdgeKind.FALL)
        target = self.module.code_for_node(node.target).strip()
        iterable = self.module.code_for_node(node.iter).strip()
        # The loop target is (re)bound each iteration. We model it as a plain
        # binding from the iterable ("x = items"): this captures the data
        # dependency (target def, iterable use) without inventing phantom
        # next()/iter() builtins that would pollute the free-name set.
        self._emit(header, cst.parse_statement(f"{target} = {iterable}"))

        exit_block = self._new_block("for.exit")
        body_entry = self._new_block("for.body")
        self._add_edge(header, body_entry, EdgeKind.TRUE)
        self._add_edge(header, exit_block, EdgeKind.FALSE)

        self._loops.append(_LoopCtx(header=header, exit=exit_block))
        body_exit = self._process_body(node.body.body, body_entry)
        self._loops.pop()

        if body_exit is not None:
            self._add_edge(body_exit, header, EdgeKind.BACK)

        if node.orelse is not None:
            self.notes.append("for/else suite not modeled")

        return exit_block

    # -- driver --------------------------------------------------------------

    def build(self) -> ControlFlowGraph:
        params = self._param_names()
        tail = self._process_body(self.func.body.body, self.entry)
        if tail is not None:
            # Implicit fall-through to function exit (implicit return None).
            self._add_edge(tail, self.exit, EdgeKind.FALL)

        return ControlFlowGraph(
            func_name=self.func.name.value,
            blocks=self.blocks,
            edges=sorted(self.edges, key=lambda e: (e.src, e.dst, e.kind.value)),
            entry=self.entry,
            exit=self.exit,
            params=params,
            notes=self.notes,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def build_cfg_from_function(func: cst.FunctionDef, module: cst.Module) -> ControlFlowGraph:
    """Build a CFG from an already-parsed LibCST FunctionDef."""
    return _CFGBuilder(func, module).build()


def build_cfg_from_source(source: str, func_name: str | None = None) -> ControlFlowGraph:
    """
    Parse `source` and build a CFG for the named function (or the first
    function found if func_name is None).

    Raises ValueError if no matching function is found.
    """
    module = cst.parse_module(source)
    target = _find_function(module, func_name)
    if target is None:
        what = f"function {func_name!r}" if func_name else "any function"
        raise ValueError(f"Could not find {what} in source")
    return build_cfg_from_function(target, module)


def _find_function(
    module: cst.Module, func_name: str | None
) -> cst.FunctionDef | None:
    """
    Find a FunctionDef in `module`.

    - None        → the first function anywhere (first match).
    - "name"      → the first function named `name` anywhere (first match).
    - "A.b.run"   → CLASS-AWARE navigation: descend class/function `A` → `b` →
                    the method `run`, exactly (no leaf-name collisions).
    """
    if func_name and "." in func_name:
        return _navigate_dotted(module, func_name.split("."))

    found: list[cst.FunctionDef] = []

    class _Finder(cst.CSTVisitor):
        def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
            if func_name is None or node.name.value == func_name:
                found.append(node)
                return False  # don't descend into matched function
            return True

    module.visit(_Finder())
    return found[0] if found else None


def _named_in_scope(
    statements, name: str
) -> cst.ClassDef | cst.FunctionDef | None:
    """First top-level ClassDef/FunctionDef named `name` in a statement list."""
    for stmt in statements:
        if isinstance(stmt, (cst.ClassDef, cst.FunctionDef)) and stmt.name.value == name:
            return stmt
    return None


def _navigate_dotted(
    module: cst.Module, parts: list[str]
) -> cst.FunctionDef | None:
    """Walk a dotted in-module path (Class.method / outer.inner) exactly."""
    scope = list(module.body)
    node: cst.ClassDef | cst.FunctionDef | None = None
    for i, part in enumerate(parts):
        node = _named_in_scope(scope, part)
        if node is None:
            return None
        if i < len(parts) - 1:
            if isinstance(node.body, cst.IndentedBlock):
                scope = list(node.body.body)
            else:
                return None
    return node if isinstance(node, cst.FunctionDef) else None

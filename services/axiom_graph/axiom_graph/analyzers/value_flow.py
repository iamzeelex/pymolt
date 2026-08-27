"""
axiom_graph/analyzers/value_flow.py

Layer-2 value-flow analysis over SSA — the "heavy artillery" applied only to
functions Griffe (Layer 1) flagged as changed/deprecated.

Goal: detect the **Proxy / delegation pattern** that deprecated functions use.
A deprecated function rarely just disappears — it usually warns and then
forwards its work to a new API:

    def old_api(x, y):
        warnings.warn("use new_api", DeprecationWarning)
        return new_api(x, y)          # ← data flow redirected into new_api

We answer: *into which callee is the return value's data flow redirected?*

Approach (use-def chains over SSA, the Scalpel idea minus its ast/networkx
machinery): build a per-version value map (versioned-name → assigned RHS), then
trace each `return` back through copy/assignment chains until we hit a call.
That call's callee is the delegation target.

Cases handled:
  - direct        : `return new_api(x)`
  - via-variable  : `r = new_api(x); return r`
  - copy-chain    : `a = new_api(x); b = a; return b`
  - method/proxy  : `return self._impl(x)`  → callee "self._impl"

Honest limits: data flowing through a phi (branch/loop merge) is not traced
past the merge (recorded as a note); only the call's *name* is resolved, no
type/points-to of the receiver object.

Pure and offline. Builds on cfg.py + ssa.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import libcst as cst

from axiom_graph.analyzers.ssa import SSAForm, build_ssa_from_source

log = logging.getLogger(__name__)

_MAX_CHAIN_DEPTH = 16  # guard against pathological copy chains / cycles


# ─────────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class DelegationTarget:
    """A call the function's return value is redirected into."""

    callee: str
    """Dotted callee name, e.g. 'new_api', 'self._impl', 'pd.concat'."""

    via: str
    """How the flow reaches the call: 'direct' | 'variable' | 'copy-chain'."""

    return_code: str
    """Source of the return statement this delegation was traced from."""


@dataclass
class ValueFlowResult:
    """Outcome of value-flow analysis for one function."""

    func_name: str
    delegations: list[DelegationTarget] = field(default_factory=list)
    return_exprs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def is_proxy(self) -> bool:
        """
        True when the function is a pure forwarder: it has at least one return,
        and every return that produces a value delegates to a call.
        """
        value_returns = [r for r in self.return_exprs if r.strip() != "return"]
        return bool(self.delegations) and len(self.delegations) >= len(value_returns)

    def primary_callee(self) -> str | None:
        """The single callee if the function delegates consistently, else None."""
        callees = {d.callee for d in self.delegations}
        return next(iter(callees)) if len(callees) == 1 else None

    def to_dict(self) -> dict:
        return {
            "func_name": self.func_name,
            "is_proxy": self.is_proxy,
            "primary_callee": self.primary_callee(),
            "delegations": [
                {"callee": d.callee, "via": d.via, "return_code": d.return_code}
                for d in self.delegations
            ],
            "return_exprs": self.return_exprs,
            "notes": self.notes,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — dotted call names
# ─────────────────────────────────────────────────────────────────────────────


def _dotted(node: cst.BaseExpression) -> str | None:
    """Render a call target (`f`, `a.b.c`) as a dotted name, or None."""
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr.value}" if base else node.attr.value
    return None


def _call_callee(node: cst.BaseExpression) -> str | None:
    """If node is a Call, return its dotted callee name."""
    if isinstance(node, cst.Call):
        return _dotted(node.func)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Value map: versioned-name → (rhs expr, versioned uses in that rhs)
# ─────────────────────────────────────────────────────────────────────────────


def _build_value_map(
    ssa: SSAForm,
) -> dict[str, tuple[cst.BaseExpression, list[str]]]:
    """
    Map each single-target assigned SSA version to its RHS expression and the
    versioned names used in that RHS. Tuple targets / augassign / phi are
    skipped (no single clean value to attribute).
    """
    value_map: dict[str, tuple[cst.BaseExpression, list[str]]] = {}
    for block in ssa.blocks.values():
        for ins in block.instructions:
            node = ins.node
            if (
                isinstance(node, cst.Assign)
                and len(ins.defs) == 1
                and len(node.targets) == 1
                and isinstance(node.targets[0].target, cst.Name)
            ):
                value_map[ins.defs[0]] = (node.value, list(ins.uses))
    return value_map


# ─────────────────────────────────────────────────────────────────────────────
# Return-flow tracing
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_versioned(
    vname: str,
    value_map: dict[str, tuple[cst.BaseExpression, list[str]]],
    depth: int = 0,
) -> tuple[str, str] | None:
    """
    Resolve a versioned variable to the call it ultimately holds, if any.

    Returns (callee, via) or None. `via` is 'variable' for a direct
    assignment-from-call, 'copy-chain' when followed through ≥1 copy.
    """
    if depth > _MAX_CHAIN_DEPTH or vname not in value_map:
        return None

    rhs, rhs_uses = value_map[vname]

    callee = _call_callee(rhs)
    if callee is not None:
        return callee, ("variable" if depth == 0 else "copy-chain")

    # Pure copy `b = a` → follow the single versioned source.
    if isinstance(rhs, cst.Name) and rhs_uses:
        return _resolve_versioned(rhs_uses[0], value_map, depth + 1)

    return None


def analyze_value_flow(ssa: SSAForm) -> ValueFlowResult:
    """Run delegation/proxy detection over an SSA form."""
    value_map = _build_value_map(ssa)
    result = ValueFlowResult(func_name=ssa.func_name)

    for block in ssa.blocks.values():
        for ins in block.instructions:
            node = ins.node
            if not isinstance(node, cst.Return):
                continue

            if node.value is None:
                result.return_exprs.append("return")
                continue

            result.return_exprs.append(ins.code)

            # Case 1 — direct: `return new_api(x)`
            callee = _call_callee(node.value)
            if callee is not None:
                result.delegations.append(
                    DelegationTarget(callee=callee, via="direct", return_code=ins.code)
                )
                continue

            # Case 2 — through a variable: `return r` where r ← call(...)
            if isinstance(node.value, cst.Name) and ins.uses:
                resolved = _resolve_versioned(ins.uses[0], value_map)
                if resolved is not None:
                    callee, via = resolved
                    result.delegations.append(
                        DelegationTarget(callee=callee, via=via, return_code=ins.code)
                    )
                    continue
                # Returned a name that traces back to a phi merge — can't follow.
                if ins.uses[0] not in value_map:
                    result.notes.append(
                        f"return value {ins.uses[0]} flows from a merge/param "
                        f"(not traced past phi)"
                    )

    return result


def detect_delegation(source: str, func_name: str | None = None) -> ValueFlowResult:
    """Parse source, build SSA, and run delegation/proxy detection — one call."""
    ssa = build_ssa_from_source(source, func_name)
    return analyze_value_flow(ssa)

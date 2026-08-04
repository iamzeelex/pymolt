"""
pymolt/codemods/rules.py

Structured codemod RULES — the declarative, data-only successor to the flat
`old → new` CodemodPattern. A rule is pure JSON (it crosses the Axiom Graph
boundary as data); the *executor* lives here in pymolt and is trusted/versioned
with the client. No code is ever shipped from the server.

A rule carries a metavariable `match` pattern and a `rewrite` template (a list of
statements). The engine binds `$METAVARS` from the match and substitutes them
into the rewrite — so a single rule can express a COMPOUND expansion (the
`lookup → melt/loc` case) with real variable names from the call site, not just
a 1:1 rename. `condition` names a static guard from a fixed catalog. `test_before`
/`test_after` are a golden pair: a rule is only trusted if applying it to
`test_before` yields `test_after` (see `verify_rule`).

Four execution modes, discriminated by SHAPE (not a mandatory schema field):

  - RENAME          bare-expression match + single bare-expression rewrite, no
                    unbound metavars → rewrite the sub-expression in place,
                    everywhere (a Tier-1-style rename, `$X.iteritems()`).
  - STATEMENT_A     full-statement match (`$RESULT = $DF.lookup(...)`) → replace
                    the whole statement. The explicit assignment-shape form.
  - ASSIGN_EXPAND_B bare-CALL match (`$DF.lookup(...)`) + a rewrite block that
                    references one metavar the match does not bind (canonically
                    `$RESULT`). The engine binds it from the enclosing single-
                    Name-target `X = <call>` assignment and splices the block.
                    EVERY other matched call site (return/nested/aug/…) surfaces
                    as an ADVISORY — the deprecated call never vanishes silently.
  - LEGACY          the lossless Tier-1 adapter: a rule tagged
                    kind ∈ {"rename-call","rewrite-import"} with old/new qualnames
                    DELEGATES to the binding-aware LibCST visitors (ScopeProvider)
                    in `pymolt.codemods.apply`; its match/rewrite strings are
                    display-only.

The vocabulary is deliberately small and grows only when a real case needs it.
Genuinely computational migrations that no template can express stay advisory
(not represented as an auto-applicable rule).
"""

from __future__ import annotations

import ast
import logging
import re
from collections.abc import Callable
from dataclasses import fields as dataclass_fields

import libcst as cst
from libcst.metadata import (
    MetadataWrapper,
    ParentNodeProvider,
    PositionProvider,
    ScopeProvider,
)
from pydantic import BaseModel, Field, model_validator

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Condition catalogs — named guards, implemented HERE (trusted); the rule
# references one by name (data). Two catalogs, distinguished by kind:
#
#   CONDITION_CATALOG      STATIC predicates. Evaluated during matching; a
#                          failure at a matched site produces an ADVISORY (never
#                          a silent skip). Gate whether a site is auto-applicable.
#   RUNTIME_PRECONDITIONS  RUNTIME-only conditions that cannot be checked
#                          statically. They NEVER gate application: the rewrite is
#                          applied, a guard comment is inserted, and a
#                          `precondition` advisory is surfaced. We never inject an
#                          `assert` (that would silently add new runtime behavior).
# ─────────────────────────────────────────────────────────────────────────────

CONDITION_CATALOG: dict[str, Callable[[dict, cst.CSTNode], bool]] = {
    "always": lambda binds, node: True,
    # every captured metavar bound to a bare name (safe to reuse inline). The
    # lookup rewrite duplicates `$DF` 3× and `$ROWS`/`$COLS` once each — a
    # non-Name receiver/arg would duplicate a possibly side-effecting expression.
    "simple_name_args": lambda binds, node: all(
        isinstance(v, cst.Name) for v in binds.values()
    ),
}

RUNTIME_PRECONDITIONS: dict[str, str] = {
    "unique_index_and_columns": (
        "df.index and df.columns must be unique for the lookup rewrite to be equivalent"
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# Result / advisory models — the honest application channel. A matched call that
# could not be safely auto-rewritten must NOT vanish: it is reported here.
# ─────────────────────────────────────────────────────────────────────────────


class RuleAdvisory(BaseModel):
    """A matched site that needs human attention (not-applied), or an applied
    site carrying a runtime caveat (precondition)."""

    line: int
    column: int
    site_kind: str
    """Machine tag: return | nested-call | augassign | multi-target | tuple-target
    | attr-target | subscript-target | annassign | walrus | expr-stmt |
    compound-clause | non-simple-args | precondition | other."""
    severity: str
    """`not-applied` (blocking; matched but not rewritten) |
    `precondition` (applied, but a runtime caveat must be verified)."""
    reason: str
    snippet: str | None = None


class RuleApplication(BaseModel):
    """Result of applying one rule to one source: the rewritten text, the count
    of AUTO rewrites, and every advisory raised along the way."""

    new_source: str
    sites: int
    advisories: list[RuleAdvisory] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Rule model (pure data — this is what the API emits as JSON)
# ─────────────────────────────────────────────────────────────────────────────


class CodemodRule(BaseModel):
    library: str
    from_version: str
    to_version: str
    match: str
    """A statement or bare-call pattern with `$METAVARS`, e.g.
    `$DF.lookup($ROWS, $COLS)` (B-form) or `$RESULT = $DF.lookup($ROWS, $COLS)`."""
    rewrite: list[str]
    """Replacement statements (a template) referencing the same `$METAVARS`."""
    condition: str = "always"
    """Name of a STATIC guard from CONDITION_CATALOG."""
    runtime_precondition: str | None = None
    """Name of a RUNTIME_PRECONDITIONS entry — a caveat surfaced on apply, never a gate."""
    result_var: str | None = None
    """Optional explicit B-form result binding. When omitted, the engine infers
    it as the sole metavar referenced by `rewrite` but not bound by `match`."""
    confidence: str = "heuristic"
    """`verified` (passed its golden pair) | `heuristic` (signal only)."""
    test_before: str | None = None
    test_after: str | None = None
    doc_link: str | None = None

    # -- Tier-1 envelope (pure JSON; mirrors the server CodemodPattern) --------
    kind: str | None = None
    """None / "template" ⇒ a template rule (metavar engine). "rename-call" /
    "rewrite-import" (with both qualnames) ⇒ LEGACY: apply via the binding-aware
    visitors. This is the lossless Tier-1 adapter — no binding-awareness lost."""
    old_qualname: str | None = None
    new_qualname: str | None = None
    evidence: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _lift_runtime_condition(self) -> CodemodRule:
        """Accept the format owner's original JSON verbatim: a `condition` that
        names a runtime precondition is normalized into `runtime_precondition`
        (with `condition` reset to the always-true static guard)."""
        if self.condition in RUNTIME_PRECONDITIONS and self.runtime_precondition is None:
            self.runtime_precondition = self.condition
            self.condition = "always"
        return self


# ─────────────────────────────────────────────────────────────────────────────
# Metavariable pattern compilation
# ─────────────────────────────────────────────────────────────────────────────

_MV_PREFIX = "AXMV_"
_MV_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def _encode_metavars(src: str) -> str:
    """`$DF` → `AXMV_DF` so the pattern parses as ordinary Python."""
    return _MV_RE.sub(lambda m: _MV_PREFIX + m.group(1), src)


def _metavars(src: str) -> set[str]:
    """The set of `$METAVAR` names referenced in a pattern/template string."""
    return {m.group(1) for m in _MV_RE.finditer(src)}


def _metavar_name(node: cst.CSTNode) -> str | None:
    """The metavariable name if `node` is a metavar placeholder Name, else None."""
    if isinstance(node, cst.Name) and node.value.startswith(_MV_PREFIX):
        return node.value[len(_MV_PREFIX):]
    return None


# Whitespace / syntactic-trivia nodes we compare leniently (ignore).
_TRIVIA = (
    cst.BaseParenthesizableWhitespace,
    cst.EmptyLine,
    cst.TrailingWhitespace,
    cst.Comment,
    cst.Newline,
    cst.Comma,
    cst.Semicolon,
    cst.LeftParen,
    cst.RightParen,
    cst.Dot,
)


def _ignorable(value) -> bool:
    if value is None or value is cst.MaybeSentinel.DEFAULT:
        return True
    if isinstance(value, _TRIVIA):
        return True
    if isinstance(value, (list, tuple)):
        return all(_ignorable(v) for v in value)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# The matcher — structural equality with metavariable capture
# ─────────────────────────────────────────────────────────────────────────────


def _match(pattern, target, binds: dict[str, cst.CSTNode]) -> bool:
    # Metavariable: capture (or check consistency if already bound).
    if isinstance(pattern, cst.CSTNode):
        name = _metavar_name(pattern)
        if name is not None:
            if name in binds:
                return binds[name].deep_equals(target)
            if not isinstance(target, cst.CSTNode):
                return False
            binds[name] = target
            return True

    if _ignorable(pattern) and _ignorable(target):
        return True

    if isinstance(pattern, cst.CSTNode) and isinstance(target, cst.CSTNode):
        if type(pattern) is not type(target):
            return False
        for f in dataclass_fields(pattern):
            pv = getattr(pattern, f.name)
            tv = getattr(target, f.name)
            if _ignorable(pv):
                continue  # whitespace / optional trivia — don't care
            if not _match(pv, tv, binds):
                return False
        return True

    if isinstance(pattern, (list, tuple)) and isinstance(target, (list, tuple)):
        p = [x for x in pattern if not _ignorable(x)]
        t = [x for x in target if not _ignorable(x)]
        if len(p) != len(t):
            return False
        return all(_match(a, b, binds) for a, b in zip(p, t))

    return pattern == target


def _first_statement(source: str) -> cst.BaseStatement:
    return cst.parse_module(_encode_metavars(source)).body[0]


def match_statement(pattern_src: str, node: cst.CSTNode) -> dict[str, cst.CSTNode] | None:
    """Return metavar bindings if `node` matches the pattern, else None."""
    pattern = _first_statement(pattern_src)
    binds: dict[str, cst.CSTNode] = {}
    return binds if _match(pattern, node, binds) else None


# ─────────────────────────────────────────────────────────────────────────────
# The rewriter — substitute bound metavariables (and rename block-local temps)
# into the template
# ─────────────────────────────────────────────────────────────────────────────


class _Substituter(cst.CSTTransformer):
    def __init__(
        self, binds: dict[str, cst.CSTNode], rename: dict[str, str] | None = None
    ) -> None:
        self.binds = binds
        self.rename = rename or {}

    def leave_Name(self, original_node: cst.Name, updated_node: cst.Name):
        name = _metavar_name(updated_node)
        if name is not None and name in self.binds:
            return self.binds[name]
        if updated_node.value in self.rename:
            return cst.Name(self.rename[updated_node.value])
        return updated_node


def render_rewrite(
    rewrite: list[str],
    binds: dict[str, cst.CSTNode],
    rename: dict[str, str] | None = None,
) -> list[cst.BaseStatement]:
    """Build the replacement statements, substituting captured metavariables and
    renaming any collision-uniquified block-local temporaries."""
    out: list[cst.BaseStatement] = []
    for line in rewrite:
        stmt = _first_statement(line)
        out.append(stmt.visit(_Substituter(binds, rename)))
    return out


def _block_local_targets(rewrite: list[str]) -> set[str]:
    """Names ASSIGNED within the rewrite block that are not metavars — i.e. the
    engine-introduced temporaries (`_ridx`, `_cidx`)."""
    names: set[str] = set()
    for line in rewrite:
        stmt = _first_statement(line)
        if not isinstance(stmt, cst.SimpleStatementLine):
            continue
        for small in stmt.body:
            targets: list[cst.BaseExpression] = []
            if isinstance(small, cst.Assign):
                targets = [t.target for t in small.targets]
            elif isinstance(small, (cst.AnnAssign, cst.AugAssign)):
                targets = [small.target]
            for target in targets:
                if isinstance(target, cst.Name):
                    names.add(target.value)
    return {n for n in names if not n.startswith(_MV_PREFIX)}


# ─────────────────────────────────────────────────────────────────────────────
# Site classification + the SHARED auto-binding predicate
# ─────────────────────────────────────────────────────────────────────────────


def _bare_expression(stmt: cst.BaseStatement) -> cst.BaseExpression | None:
    """The single bare expression of a statement like `$X.foo()`, else None."""
    if (
        isinstance(stmt, cst.SimpleStatementLine)
        and len(stmt.body) == 1
        and isinstance(stmt.body[0], cst.Expr)
    ):
        return stmt.body[0].value
    return None


def _classify_site(parent) -> tuple[str, str]:
    """Classify a matched call that is NOT a safe simple-assignment RHS, by its
    enclosing (parent) node — for the advisory the engineer reviews."""
    if isinstance(parent, cst.Return):
        return "return", "matched call is a return value, not a simple assignment"
    if isinstance(parent, cst.Yield):
        return "return", "matched call is a yielded value, not a simple assignment"
    if isinstance(parent, cst.Arg):
        return "nested-call", "matched call is nested inside another call or expression"
    if isinstance(parent, cst.Attribute):
        return "nested-call", "matched call is the receiver of a chained attribute/method"
    if isinstance(parent, cst.AugAssign):
        return "augassign", "matched call is the value of an augmented assignment (+=, etc.)"
    if isinstance(parent, cst.AnnAssign):
        return (
            "annassign",
            "matched call is an annotated assignment; the annotation would be dropped",
        )
    if isinstance(parent, cst.NamedExpr):
        return "walrus", "matched call is the value of a walrus (:=) expression"
    if isinstance(parent, cst.Assign):
        if len(parent.targets) > 1:
            return "multi-target", "matched call feeds multiple assignment targets"
        target = parent.targets[0].target
        if isinstance(target, (cst.Tuple, cst.List)):
            return "tuple-target", "matched call feeds a tuple/list unpacking target"
        if isinstance(target, cst.Attribute):
            return "attr-target", "matched call is assigned to an attribute target"
        if isinstance(target, cst.Subscript):
            return "subscript-target", "matched call is assigned to a subscript target"
        return "other", "matched call is assigned to a non-name target"
    if isinstance(parent, cst.Expr):
        return "expr-stmt", "matched call's result is discarded (bare expression statement)"
    compound = (cst.For, cst.While, cst.If, cst.IfExp, cst.CompFor, cst.With, cst.Lambda)
    if isinstance(parent, compound):
        return (
            "compound-clause",
            "matched call sits inside a compound-statement clause or comprehension",
        )
    return "other", "matched call is not the RHS of a simple assignment"


def _auto_binding(
    call: cst.Call,
    parent,
    binds: dict[str, cst.CSTNode],
    static_cond: Callable[[dict, cst.CSTNode], bool],
    pos: tuple[int, int],
) -> tuple[str | None, RuleAdvisory | None]:
    """The SINGLE predicate deciding AUTO vs advisory for a matched call. Both the
    advisory emitter (`leave_Call`) and the splicer (`leave_SimpleStatementLine`)
    consult it, so their counts can never diverge.

    Returns (target_name, None) for a safe auto site; (None, advisory) otherwise.
    """
    line, column = pos
    if isinstance(parent, cst.Assign) and parent.value is call and len(parent.targets) == 1:
        target = parent.targets[0].target
        if isinstance(target, cst.Name):
            if static_cond(binds, call):
                return target.value, None
            return None, RuleAdvisory(
                line=line,
                column=column,
                site_kind="non-simple-args",
                severity="not-applied",
                reason=(
                    "matched call has non-simple receiver/arguments; auto-rewrite "
                    "would duplicate a possibly side-effecting expression"
                ),
            )
    site_kind, reason = _classify_site(parent)
    return None, RuleAdvisory(
        line=line, column=column, site_kind=site_kind, severity="not-applied", reason=reason
    )


def _is_auto_assign(stmt: cst.CSTNode) -> tuple[cst.Assign, cst.Name] | None:
    """If `stmt` is a single-target `Name = <expr>` statement, return (assign, name)."""
    if (
        isinstance(stmt, cst.SimpleStatementLine)
        and len(stmt.body) == 1
        and isinstance(stmt.body[0], cst.Assign)
    ):
        assign = stmt.body[0]
        if len(assign.targets) == 1 and isinstance(assign.targets[0].target, cst.Name):
            return assign, assign.targets[0].target
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Transformers
# ─────────────────────────────────────────────────────────────────────────────


class _RuleTransformer(cst.CSTTransformer):
    """RENAME + STATEMENT_A modes (no advisories):
      - RENAME (`match` is a bare expression, `$X.iteritems()`): rewrite the
        matching sub-expression anywhere it appears.
      - STATEMENT_A (`match` is a full statement, `$R = $DF.lookup(...)`): replace
        the whole statement with the rewrite block.
    """

    def __init__(self, rule: CodemodRule) -> None:
        self.rule = rule
        self.guard = CONDITION_CATALOG.get(rule.condition, CONDITION_CATALOG["always"])
        self.sites = 0
        stmt = _first_statement(rule.match)
        self.pattern_expr = _bare_expression(stmt)
        self.pattern_stmt = None if self.pattern_expr is not None else stmt

    def on_leave(self, original_node, updated_node):
        updated_node = super().on_leave(original_node, updated_node)
        if self.pattern_expr is not None and isinstance(updated_node, cst.BaseExpression):
            binds: dict[str, cst.CSTNode] = {}
            if _match(self.pattern_expr, original_node, binds) and self.guard(
                binds, original_node
            ):
                self.sites += 1
                repl = _bare_expression(_first_statement(self.rule.rewrite[0]))
                return (repl or updated_node).visit(_Substituter(binds))
        return updated_node

    def leave_SimpleStatementLine(self, original_node, updated_node):
        if self.pattern_stmt is None:
            return updated_node
        binds: dict[str, cst.CSTNode] = {}
        if not _match(self.pattern_stmt, original_node, binds):
            return updated_node
        if not self.guard(binds, original_node):
            return updated_node
        self.sites += 1
        return cst.FlattenSentinel(render_rewrite(self.rule.rewrite, binds))


class _AssignExpandTransformer(cst.CSTTransformer):
    """ASSIGN_EXPAND_B: match a bare deprecated call, bind the result metavar from
    the enclosing single-Name-target assignment, and splice the rewrite block.
    Every matched call NOT in that safe position surfaces as an advisory.

    Metadata lookups are on ORIGINAL nodes (a lookup on the updated node silently
    returns the default) — run under a MetadataWrapper.
    """

    METADATA_DEPENDENCIES = (ParentNodeProvider, PositionProvider, ScopeProvider)

    def __init__(
        self,
        rule: CodemodRule,
        pattern_call: cst.BaseExpression,
        result_var: str,
        static_cond: Callable[[dict, cst.CSTNode], bool],
        precondition_text: str | None,
    ) -> None:
        self.rule = rule
        self.pattern_call = pattern_call
        self.result_var = result_var
        self.static_cond = static_cond
        self.precondition_text = precondition_text
        self.sites = 0
        self.advisories: list[RuleAdvisory] = []

    def _position(self, node: cst.CSTNode) -> tuple[int, int]:
        try:
            pos = self.get_metadata(PositionProvider, node)
        except KeyError:
            return 0, 0
        return pos.start.line, pos.start.column

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call):
        binds: dict[str, cst.CSTNode] = {}
        if not _match(self.pattern_call, original_node, binds):
            return updated_node
        parent = self.get_metadata(ParentNodeProvider, original_node, None)
        _target, advisory = _auto_binding(
            original_node, parent, binds, self.static_cond, self._position(original_node)
        )
        # AUTO sites (advisory is None) are rewritten by leave_SimpleStatementLine.
        if advisory is not None:
            self.advisories.append(advisory)
        return updated_node

    def leave_SimpleStatementLine(self, original_node, updated_node):
        auto = _is_auto_assign(original_node)
        if auto is None:
            return updated_node
        assign, target_name = auto
        binds: dict[str, cst.CSTNode] = {}
        if not _match(self.pattern_call, assign.value, binds):
            return updated_node
        # Same predicate as leave_Call — the advisory it would emit was already
        # recorded there; here we only need the AUTO target name.
        tname, _advisory = _auto_binding(
            assign.value, assign, binds, self.static_cond, self._position(assign.value)
        )
        if tname is None:
            return updated_node

        rename = self._temp_renames(target_name)
        binds[self.result_var] = cst.Name(tname)
        block = render_rewrite(self.rule.rewrite, binds, rename)
        if self.precondition_text is not None and block:
            block = self._attach_guard(block, self._position(assign.value))
        self.sites += 1
        return cst.FlattenSentinel(block)

    def _temp_renames(self, target_name_node: cst.Name) -> dict[str, str]:
        """Uniquify block-local temporaries that collide with names already in
        scope, so the rewrite never silently clobbers the author's binding."""
        locals_ = _block_local_targets(self.rule.rewrite)
        if not locals_:
            return {}
        scope = self.get_metadata(ScopeProvider, target_name_node, None)
        if scope is None:
            log.debug(
                "rules: no scope for temp-collision check near %r; using literal names",
                target_name_node.value,
            )
            return {}
        rename: dict[str, str] = {}
        taken: set[str] = set()
        for name in sorted(locals_):
            if name in scope:
                k = 2
                candidate = f"{name}_{k}"
                while candidate in scope or candidate in taken:
                    k += 1
                    candidate = f"{name}_{k}"
                rename[name] = candidate
                taken.add(candidate)
        return rename

    def _attach_guard(
        self, block: list[cst.BaseStatement], pos: tuple[int, int]
    ) -> list[cst.BaseStatement]:
        """Prepend a non-executable guard comment and record the runtime caveat as
        a `precondition` advisory. We never inject an `assert`."""
        name = self.rule.runtime_precondition
        text = self.precondition_text
        comment = cst.Comment(f"# pymolt: requires {name} — {text}; verify before shipping")
        first = block[0]
        guarded = first.with_changes(
            leading_lines=[*first.leading_lines, cst.EmptyLine(indent=True, comment=comment)]
        )
        line, column = pos
        self.advisories.append(
            RuleAdvisory(
                line=line,
                column=column,
                site_kind="precondition",
                severity="precondition",
                reason=f"applied, but requires {name}: {text}; verify before shipping",
            )
        )
        return [guarded, *block[1:]]


# ─────────────────────────────────────────────────────────────────────────────
# Mode selection
# ─────────────────────────────────────────────────────────────────────────────

_MODE_LEGACY = "legacy"
_MODE_RENAME = "rename"
_MODE_STATEMENT_A = "statement_a"
_MODE_ASSIGN_EXPAND_B = "assign_expand_b"


def _rule_mode(rule: CodemodRule) -> str:
    if (
        rule.kind in ("rename-call", "rewrite-import")
        and rule.old_qualname
        and rule.new_qualname
    ):
        return _MODE_LEGACY
    if _bare_expression(_first_statement(rule.match)) is None:
        return _MODE_STATEMENT_A
    match_vars = _metavars(rule.match)
    rewrite_vars: set[str] = set()
    for line in rule.rewrite:
        rewrite_vars |= _metavars(line)
    unbound = rewrite_vars - match_vars
    single_bare = (
        len(rule.rewrite) == 1
        and _bare_expression(_first_statement(rule.rewrite[0])) is not None
    )
    if not unbound and single_bare:
        return _MODE_RENAME
    return _MODE_ASSIGN_EXPAND_B


def _resolve_result_var(rule: CodemodRule) -> str:
    """The B-form result metavar: the explicit override, or (inferred) the sole
    metavar referenced by `rewrite` but not bound by `match`. 0 or ≥2 unbound is
    malformed — raise so the caller (and verify_rule) treat the rule as invalid."""
    if rule.result_var:
        return rule.result_var
    match_vars = _metavars(rule.match)
    rewrite_vars: set[str] = set()
    for line in rule.rewrite:
        rewrite_vars |= _metavars(line)
    unbound = rewrite_vars - match_vars
    if len(unbound) != 1:
        raise ValueError(
            "B-form rule needs exactly one metavar bound from the enclosing "
            f"assignment (or an explicit result_var); found unbound={sorted(unbound)}"
        )
    return next(iter(unbound))


# ─────────────────────────────────────────────────────────────────────────────
# Applying a rule
# ─────────────────────────────────────────────────────────────────────────────


def apply_rule_detailed(source: str, rule: CodemodRule) -> RuleApplication:
    """Apply `rule` to `source`, returning the rewritten text, the count of AUTO
    rewrites, and every advisory raised (the honest channel — matched-but-not-
    rewritten sites never vanish). Raises on a malformed B-form rule."""
    mode = _rule_mode(rule)

    if mode == _MODE_LEGACY:
        # Delegate to the binding-aware visitors — the lossless Tier-1 path. Import
        # lazily: apply.py will later import this module for repo-level application.
        from pymolt.codemods.apply import apply_pattern
        from pymolt.codemods.models import CodemodPattern

        pattern = CodemodPattern(
            old_qualname=rule.old_qualname or "",
            new_qualname=rule.new_qualname or "",
            kind=rule.kind or "rename-call",
            confidence=rule.confidence,
            evidence=list(rule.evidence),
        )
        new_source, sites = apply_pattern(source, pattern)
        return RuleApplication(new_source=new_source, sites=sites, advisories=[])

    if mode == _MODE_ASSIGN_EXPAND_B:
        pattern_call = _bare_expression(_first_statement(rule.match))
        result_var = _resolve_result_var(rule)  # raises on malformed B
        static_cond = CONDITION_CATALOG.get(rule.condition, CONDITION_CATALOG["always"])
        precondition_text = (
            RUNTIME_PRECONDITIONS.get(rule.runtime_precondition)
            if rule.runtime_precondition
            else None
        )
        module = cst.parse_module(source)
        transformer = _AssignExpandTransformer(
            rule, pattern_call, result_var, static_cond, precondition_text
        )
        new_module = MetadataWrapper(module).visit(transformer)
        return RuleApplication(
            new_source=new_module.code,
            sites=transformer.sites,
            advisories=transformer.advisories,
        )

    # RENAME / STATEMENT_A
    module = cst.parse_module(source)
    tr = _RuleTransformer(rule)
    new = module.visit(tr)
    return RuleApplication(new_source=new.code, sites=tr.sites, advisories=[])


def apply_rule(source: str, rule: CodemodRule) -> tuple[str, int]:
    """Apply `rule` to `source`; return (new_source, sites_changed). The stable
    2-tuple wrapper over `apply_rule_detailed` (advisories are dropped here)."""
    result = apply_rule_detailed(source, rule)
    return result.new_source, result.sites


# ─────────────────────────────────────────────────────────────────────────────
# Golden-pair verification — the objective quality gate
# ─────────────────────────────────────────────────────────────────────────────


def _normalize(code: str) -> str:
    """Formatting-insensitive canonical form (ignores whitespace/comments)."""
    return ast.dump(ast.parse(code.strip()))


def verify_rule(rule: CodemodRule) -> bool:
    """
    True iff the rule has a golden pair AND applying it to `test_before` produces
    `test_after` (modulo formatting/comments). A rule with no valid golden pair —
    the `mad → groupby` kind of artifact — cannot be `verified`. A B-form pair
    must exercise the enclosing-binding path (its `test_before` is an assignment);
    a rule that raises or fires zero AUTO sites on its own pair is not verified.
    """
    if rule.test_before is None or rule.test_after is None:
        return False
    try:
        result, sites = apply_rule(rule.test_before, rule)
    except Exception:
        return False
    if sites == 0:
        return False
    try:
        return _normalize(result) == _normalize(rule.test_after)
    except SyntaxError:
        return False

"""
axiom_graph/core/policy.py

ProposalPolicy — the single, tunable knob-board that decides which raw codemod
proposals survive into the result. Split out from the proposal engine on purpose:
precision is **project-specific**, so the rules a team wants are data, not code.

Four independent filters, each individually switchable:

  1. prose_sources      — which prose markers to trust. Deprecation *warnings*
                          are authoritative; *docstrings* and *comments* are
                          noisy (they yield `safe_sort → "with"` from an English
                          sentence). Default: warnings only.
  2. dispatcher_targets — generic dispatch methods a body forwards through
                          (`self.operate(...)`, `apply`, `process`). The data
                          flow "sees" them but they are not real successors
                          (`isnot → operate`). Default: a small known set.
  3. drop_noop          — a proposal whose target leaf equals the old leaf
                          (`DataFrame.merge → merge`, `Rolling.std → std`) is a
                          same-name internal delegation: the public method still
                          exists, so it is a no-op for the user. Default: on.
  4. public-API surface — which SOURCE symbols are user-facing at all. This is
                          the deliberately-manipulable one: `internal_segments`
                          is a denylist of module-path segments
                          (`pandas.core.internals.*`), and `public_predicate` is
                          a full escape hatch. Defaults are conservative — they
                          never touch risky segments like `core`/`dtypes`, so
                          real codemods are not silently lost; tighten per
                          project.

Note on caution (why public-API is opt-in-ish, not aggressive): a team may build
an internal library directly on top of a dependency's *internals* and genuinely
need a codemod for `pandas.core.internals.*`. What is "noise" for an app author
is "signal" for them. So the public-API filter is easy to widen, narrow, or turn
off entirely (`ProposalPolicy.permissive()`), without code changes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace

# Generic dispatch sinks: a deprecated method often forwards through one of these
# (`return self.operate(op, other)`), so the value-flow names the dispatcher
# rather than the real successor. They are never themselves a migration target.
DEFAULT_DISPATCHERS: frozenset[str] = frozenset({
    "operate", "apply", "apply_with_block", "process", "_process",
    "dispatch", "_dispatch", "__finalize__", "__getattr__", "getattr",
    "klass", "cls", "meth", "func",
})

# Module-path segments that mark a SOURCE symbol as library-internal. Kept
# deliberately MINIMAL and unambiguous — `core`, `dtypes`, `arrays` are NOT here
# because public classes legitimately live under them (`pandas.core.frame`).
DEFAULT_INTERNAL_SEGMENTS: frozenset[str] = frozenset({
    "internals", "_internal", "impl", "_impl",
})

# A broader, opt-in denylist for "app-author" runs that want only the
# user-facing surface. Tightens recall in exchange for precision; teams that
# build on a library's internals should NOT use this.
STRICT_INTERNAL_SEGMENTS: frozenset[str] = frozenset({
    "internals", "_internal", "impl", "_impl",
    "arrays", "groupby", "computation", "indexers", "nanops", "array_algos",
    "compiler", "operators", "visitors", "strategies", "evaluator", "loading",
    "ddl", "crud",
})


def _leaf(q: str) -> str:
    return q.rsplit(".", 1)[-1]


# Confidence ordering. `low` is the baseline floor tier: a guessed target — data-flow
# and prose disagreed, or the same bare successor was fanned out across many
# unrelated symbols (a prose-extraction artifact). Not emitted by default.
CONFIDENCE_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}

# How many distinct source symbols must share one BARE target before it looks
# like a prose artifact (`count`, `mad`, … all → "groupby") rather than a real
# rename. Dotted targets (`pandas.concat`) are never demoted this way.
RECURRENCE_DEMOTE_THRESHOLD = 3


@dataclass(frozen=True)
class ProposalPolicy:
    """Tunable acceptance rules for codemod proposals. All defaults are safe."""

    prose_sources: frozenset[str] = frozenset({"warning"})
    dispatcher_targets: frozenset[str] = DEFAULT_DISPATCHERS
    drop_noop: bool = True
    internal_segments: frozenset[str] = DEFAULT_INTERNAL_SEGMENTS
    min_confidence: str = "medium"
    # Ultimate escape hatch: a custom predicate on the SOURCE qualname. When set,
    # it OVERRIDES internal_segments. Returns True if the symbol is user-facing.
    public_predicate: Callable[[str], bool] | None = field(default=None, compare=False)

    # ── source-level gate (is this changed symbol user-facing?) ──────────────

    def accepts_source(self, qualname: str) -> bool:
        """True if a changed SOURCE symbol is worth proposing a codemod for."""
        if self.public_predicate is not None:
            return self.public_predicate(qualname)
        if not self.internal_segments:
            return True
        return not (self.internal_segments & set(qualname.split(".")))

    # ── prose gate ───────────────────────────────────────────────────────────

    def accepts_prose(self, source: str | None) -> bool:
        """True if a prose marker from `source` ('warning'|'docstring'|...) counts."""
        return source is not None and source in self.prose_sources

    # ── target gate (is this proposed successor real & actionable?) ──────────

    def accepts_confidence(self, confidence: str) -> bool:
        """True if `confidence` meets the floor (drops the `low` baseline tier)."""
        return CONFIDENCE_RANK.get(confidence, 0) >= CONFIDENCE_RANK.get(self.min_confidence, 1)

    def accepts_target(self, old_qualname: str, target: str) -> bool:
        """
        True if `target` is a real successor (not a dispatcher / no-op).

        A no-op is a *bare* same-name delegation (`DataFrame.merge → merge`): the
        body forwards to a same-named symbol in scope, so the public method still
        exists and nothing migrates. A *dotted* same-leaf target is NOT a no-op —
        it is a cross-module move (`flask.helpers.safe_join →
        werkzeug.security.safe_join`), the most valuable rewrite-import there is.
        """
        leaf = _leaf(target)
        if leaf in self.dispatcher_targets:
            return False
        if self.drop_noop and "." not in target and leaf == _leaf(old_qualname):
            return False
        return True

    # ── presets / ergonomic tweaks ───────────────────────────────────────────

    @classmethod
    def permissive(cls) -> ProposalPolicy:
        """No filtering — every raw proposal survives (the old behaviour)."""
        return cls(
            prose_sources=frozenset({"warning", "docstring", "comment"}),
            dispatcher_targets=frozenset(),
            drop_noop=False,
            internal_segments=frozenset(),
            min_confidence="low",
        )

    @classmethod
    def strict_public(cls) -> ProposalPolicy:
        """App-author preset: only the user-facing surface (broad internal denylist)."""
        return replace(cls(), internal_segments=STRICT_INTERNAL_SEGMENTS)

    def with_internal_segments(self, segments: frozenset[str] | set[str]) -> ProposalPolicy:
        """Return a copy with the public-API denylist swapped (easy per-project tuning)."""
        return replace(self, internal_segments=frozenset(segments))

    def allowing_docstrings(self) -> ProposalPolicy:
        """Return a copy that also trusts docstring/comment prose."""
        return replace(self, prose_sources=self.prose_sources | {"docstring", "comment"})


DEFAULT_POLICY = ProposalPolicy()

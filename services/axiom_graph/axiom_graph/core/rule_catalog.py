"""
axiom_graph/core/rule_catalog.py

Curated catalog of hand-authored CodemodRule templates for deprecated APIs
whose real fix is a multi-statement expansion (pattern-1 ASSIGN_EXPAND_B
form: a bare-call match, an ordered rewrite block, one unbound $RESULT
metavar the client binds from the enclosing assignment) — not a rename the
automatic value-flow/prose engine (core.propose) can derive on its own.
Keyed by (package, symbol leaf), so a pipeline step only needs the set of
changed/removed symbol names already on hand to look up a hit.

Pure data: no execution, no LibCST. The client (pymolt/codemods/rules.py) is
the sole executor and the sole verification authority — `confidence` here is
only a claim pymolt re-derives locally before trusting it.
"""

from __future__ import annotations

from collections.abc import Iterable

from axiom_graph.core.models import CodemodRule

# ─────────────────────────────────────────────────────────────────────────────
# Catalog
#
# library/from_version/to_version below are the API's own historical
# deprecation window (informational) — the caller (core.pipeline) stamps the
# concrete requested migration range onto each hit via model_copy.
# ─────────────────────────────────────────────────────────────────────────────

_CATALOG: dict[tuple[str, str], list[CodemodRule]] = {
    ("pandas", "lookup"): [
        CodemodRule(
            library="pandas",
            from_version="1.2.0",
            to_version="2.0.0",
            match="$DF.lookup($ROWS, $COLS)",
            rewrite=[
                "_ridx = $DF.index.get_indexer($ROWS)",
                "_cidx = $DF.columns.get_indexer($COLS)",
                "$RESULT = $DF.to_numpy()[_ridx, _cidx]",
            ],
            condition="simple_name_args",
            runtime_precondition="unique_index_and_columns",
            confidence="verified",
            test_before="vals = df.lookup(rows, cols)",
            test_after=(
                "_ridx = df.index.get_indexer(rows)\n"
                "_cidx = df.columns.get_indexer(cols)\n"
                "vals = df.to_numpy()[_ridx, _cidx]"
            ),
            doc_link="https://pandas.pydata.org/docs/whatsnew/v1.2.0.html",
        ),
    ],
}


# ─────────────────────────────────────────────────────────────────────────────
# Lookup
# ─────────────────────────────────────────────────────────────────────────────


def _leaf(symbol: str) -> str:
    return symbol.rsplit(".", 1)[-1]


def rules_for(package: str, symbols: Iterable[str]) -> list[CodemodRule]:
    """
    Return curated rules for `package` whose catalog key leaf matches the
    leaf of any qualname in `symbols` (e.g. the ("pandas", "lookup") key hits
    both a bare "lookup" and a dotted "pandas.core.frame.DataFrame.lookup").

    Each hit is a fresh copy (model_copy) — callers are free to stamp the
    concrete from_version/to_version onto the result without mutating the
    catalog singleton.
    """
    leaves = {_leaf(s) for s in symbols}
    return [
        rule.model_copy()
        for (pkg, leaf), rules in _CATALOG.items()
        if pkg == package and leaf in leaves
        for rule in rules
    ]

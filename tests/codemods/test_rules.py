"""Tests for pymolt.codemods.rules — declarative metavariable codemod rules."""

from __future__ import annotations

import pytest

from pymolt.codemods.rules import (
    CodemodRule,
    apply_rule,
    apply_rule_detailed,
    match_statement,
    verify_rule,
)

# The hard case: a COMPOUND expansion (lookup → indexer block) with real
# variable capture — the thing a flat old→new string cannot express.
LOOKUP = CodemodRule(
    library="pandas", from_version="1.2.0", to_version="2.0.0",
    match="$RESULT = $DF.lookup($ROWS, $COLS)",
    rewrite=[
        "_ridx = $DF.index.get_indexer($ROWS)",
        "_cidx = $DF.columns.get_indexer($COLS)",
        "$RESULT = $DF.to_numpy()[_ridx, _cidx]",
    ],
    condition="simple_name_args", confidence="verified",
    doc_link="https://pandas.pydata.org/docs/whatsnew/v1.2.0.html",
    test_before="vals = df.lookup(rows, cols)",
    test_after=(
        "_ridx = df.index.get_indexer(rows)\n"
        "_cidx = df.columns.get_indexer(cols)\n"
        "vals = df.to_numpy()[_ridx, _cidx]"
    ),
)

RENAME = CodemodRule(
    library="pandas", from_version="1.5", to_version="2.0",
    match="$X.iteritems()", rewrite=["$X.items()"], confidence="verified",
    test_before="for k, v in s.iteritems():\n    pass",
    test_after="for k, v in s.items():\n    pass",
)


class TestMatch:
    def test_captures_metavars(self):
        import libcst as cst

        node = cst.parse_module("out = frame.lookup(a, b)").body[0]
        binds = match_statement("$RESULT = $DF.lookup($ROWS, $COLS)", node)
        assert binds is not None
        assert {k: v.value for k, v in binds.items()} == {
            "RESULT": "out", "DF": "frame", "ROWS": "a", "COLS": "b",
        }

    def test_non_match_returns_none(self):
        import libcst as cst

        node = cst.parse_module("out = frame.merge(a, b)").body[0]
        assert match_statement("$RESULT = $DF.lookup($ROWS, $COLS)", node) is None


class TestApplyCompound:
    def test_lookup_expands_with_real_names(self):
        src = (
            "def score(frame, r, c):\n"
            "    out = frame.lookup(r, c)\n"
            "    return out\n"
        )
        new, sites = apply_rule(src, LOOKUP)
        assert sites == 1
        assert "frame.index.get_indexer(r)" in new
        assert "frame.columns.get_indexer(c)" in new
        assert "out = frame.to_numpy()[_ridx, _cidx]" in new
        assert ".lookup(" not in new
        # indentation preserved (still inside the function body)
        assert "    _ridx =" in new


class TestApplyExpression:
    def test_rename_inside_a_compound_statement(self):
        # the call sits inside a `for` clause, not a top-level statement
        new, sites = apply_rule("for a, b in frame.iteritems():\n    use(a)\n", RENAME)
        assert sites == 1
        assert "frame.items()" in new and "iteritems" not in new

    def test_rename_hits_every_site(self):
        new, sites = apply_rule("x = a.iteritems(); y = b.iteritems()", RENAME)
        assert sites == 2


class TestVerifyGate:
    def test_verified_rules_pass_their_golden_pair(self):
        assert verify_rule(LOOKUP) is True
        assert verify_rule(RENAME) is True

    def test_rule_without_golden_pair_is_unverified(self):
        r = CodemodRule(library="x", from_version="1", to_version="2",
                        match="$X.foo()", rewrite=["$X.bar()"])
        assert verify_rule(r) is False

    def test_artifact_whose_rewrite_misses_the_real_after_is_rejected(self):
        # mad has no 1:1 successor; claiming `mad()->groupby()` cannot reproduce
        # the real replacement, so it can never be verified.
        artifact = CodemodRule(
            library="pandas", from_version="1.5", to_version="2.0",
            match="$X.mad()", rewrite=["$X.groupby()"],
            test_before="m = df.mad()",
            test_after="m = (df - df.mean()).abs().mean()",
        )
        assert verify_rule(artifact) is False

    def test_rule_that_does_not_fire_is_unverified(self):
        r = CodemodRule(library="x", from_version="1", to_version="2",
                        match="$X.foo()", rewrite=["$X.bar()"],
                        test_before="y = 1", test_after="y = 1")
        assert verify_rule(r) is False  # zero sites → not a real transform


# ─────────────────────────────────────────────────────────────────────────────
# B-form (ASSIGN_EXPAND_B): a bare deprecated-call match with the result bound
# from the enclosing `X = <call>` assignment; every other matched site advises.
# ─────────────────────────────────────────────────────────────────────────────

# The canonical lookup rule from final-design.md — carries a runtime precondition.
LOOKUP_B = CodemodRule(
    library="pandas", from_version="1.2.0", to_version="2.0.0",
    match="$DF.lookup($ROWS, $COLS)",
    rewrite=[
        "_ridx = $DF.index.get_indexer($ROWS)",
        "_cidx = $DF.columns.get_indexer($COLS)",
        "$RESULT = $DF.to_numpy()[_ridx, _cidx]",
    ],
    condition="simple_name_args",
    runtime_precondition="unique_index_and_columns",
    confidence="verified",
    doc_link="https://pandas.pydata.org/docs/whatsnew/v1.2.0.html",
    test_before="vals = df.lookup(rows, cols)",
    test_after=(
        "_ridx = df.index.get_indexer(rows)\n"
        "_cidx = df.columns.get_indexer(cols)\n"
        "vals = df.to_numpy()[_ridx, _cidx]"
    ),
)

# Same transform without the runtime precondition — clean advisory assertions.
LOOKUP_B_PLAIN = CodemodRule(
    library="pandas", from_version="1.2.0", to_version="2.0.0",
    match="$DF.lookup($ROWS, $COLS)",
    rewrite=LOOKUP_B.rewrite,
    condition="simple_name_args",
)


class TestBFormAcceptance:
    def test_verifies_true(self):
        assert verify_rule(LOOKUP_B) is True

    def test_applies_with_real_names_and_indentation(self):
        src = (
            "def score(frame, r, c):\n"
            "    out = frame.lookup(r, c)\n"
            "    return out\n"
        )
        result = apply_rule_detailed(src, LOOKUP_B_PLAIN)
        new = result.new_source
        assert result.sites == 1
        assert "frame.index.get_indexer(r)" in new
        assert "frame.columns.get_indexer(c)" in new
        assert "out = frame.to_numpy()[_ridx, _cidx]" in new
        assert ".lookup(" not in new
        assert "    _ridx =" in new  # still inside the function body

    def test_temp_uniquified_on_collision(self):
        src = (
            "def f(df, rows, cols):\n"
            "    _ridx = 1\n"
            "    vals = df.lookup(rows, cols)\n"
            "    return vals, _ridx\n"
        )
        new, sites = apply_rule(src, LOOKUP_B_PLAIN)
        assert sites == 1
        assert "_ridx = 1" in new  # author's binding preserved (not clobbered)
        assert "_ridx_2 = df.index.get_indexer(rows)" in new
        assert "vals = df.to_numpy()[_ridx_2, _cidx]" in new
        assert "_cidx = df.columns.get_indexer(cols)" in new  # no false collision


class TestOwnerOriginalShape:
    def test_condition_naming_runtime_precondition_is_normalized(self):
        # The format owner's original JSON puts the runtime name in `condition`.
        rule = CodemodRule(
            library="pandas", from_version="1.2.0", to_version="2.0.0",
            match="$DF.lookup($ROWS, $COLS)",
            rewrite=LOOKUP_B.rewrite,
            condition="unique_index_and_columns",
            confidence="verified",
            test_before=LOOKUP_B.test_before, test_after=LOOKUP_B.test_after,
        )
        assert rule.condition == "always"
        assert rule.runtime_precondition == "unique_index_and_columns"
        assert verify_rule(rule) is True


class TestRuntimePrecondition:
    def test_guard_comment_and_precondition_advisory(self):
        result = apply_rule_detailed("vals = df.lookup(rows, cols)", LOOKUP_B)
        assert result.sites == 1
        assert "# pymolt: requires unique_index_and_columns" in result.new_source
        preconditions = [a for a in result.advisories if a.severity == "precondition"]
        assert len(preconditions) == 1
        assert preconditions[0].site_kind == "precondition"
        assert [a for a in result.advisories if a.severity == "not-applied"] == []

    def test_golden_pair_verifies_despite_injected_comment(self):
        # ast.dump ignores comments → the guard does not perturb verification.
        assert verify_rule(LOOKUP_B) is True


# Each fixture has exactly one `df.lookup(r, c)` in the named site shape.
ADVISORY_SITES = {
    "return": ("def f(df, r, c):\n    return df.lookup(r, c)\n", "return"),
    "nested-call": ("def f(df, r, c):\n    return foo(df.lookup(r, c))\n", "nested-call"),
    "augassign": ("x += df.lookup(r, c)\n", "augassign"),
    "multi-target": ("a = b = df.lookup(r, c)\n", "multi-target"),
    "tuple-target": ("a, b = df.lookup(r, c)\n", "tuple-target"),
    "attr-target": ("obj.x = df.lookup(r, c)\n", "attr-target"),
    "subscript-target": ("d[k] = df.lookup(r, c)\n", "subscript-target"),
    "annassign": ("x: int = df.lookup(r, c)\n", "annassign"),
    "walrus": ("y = (x := df.lookup(r, c))\n", "walrus"),
    "expr-stmt": ("df.lookup(r, c)\n", "expr-stmt"),
    "compound-clause": ("for k in df.lookup(r, c):\n    pass\n", "compound-clause"),
}


class TestAdvisoryTable:
    @pytest.mark.parametrize("name", list(ADVISORY_SITES))
    def test_site_yields_not_applied_advisory(self, name):
        src, expected_kind = ADVISORY_SITES[name]
        result = apply_rule_detailed(src, LOOKUP_B_PLAIN)
        assert result.sites == 0
        not_applied = [a for a in result.advisories if a.severity == "not-applied"]
        assert len(not_applied) == 1
        assert not_applied[0].site_kind == expected_kind
        # The honesty invariant: a matched call is either applied or advised.
        assert result.sites + len(not_applied) == src.count(".lookup(")

    def test_invariant_holds_across_mixed_sites(self):
        src = (
            "def f(df, rows, cols):\n"
            "    good = df.lookup(rows, cols)\n"          # AUTO
            "    other = also = df.lookup(rows, cols)\n"  # multi-target advisory
            "    return df.lookup(rows, cols)\n"          # return advisory
        )
        result = apply_rule_detailed(src, LOOKUP_B_PLAIN)
        not_applied = [a for a in result.advisories if a.severity == "not-applied"]
        assert result.sites == 1
        assert len(not_applied) == 2
        assert result.sites + len(not_applied) == src.count(".lookup(") == 3


class TestNonSimpleArgs:
    def test_non_name_receiver_is_advisory_not_rewrite(self):
        src = "x = get_df().lookup(r, c)\n"
        result = apply_rule_detailed(src, LOOKUP_B_PLAIN)
        assert result.sites == 0
        assert result.new_source == src  # untouched — never a silent skip
        not_applied = [a for a in result.advisories if a.severity == "not-applied"]
        assert len(not_applied) == 1
        assert not_applied[0].site_kind == "non-simple-args"


# ─────────────────────────────────────────────────────────────────────────────
# LEGACY delegation — a Tier-1 rename routed through the binding-aware visitors.
# ─────────────────────────────────────────────────────────────────────────────

LEGACY_RENAME = CodemodRule(
    library="flask", from_version="2.0.0", to_version="3.0.0",
    match="safe_join(...)", rewrite=["secure_filename(...)"],  # display-only strings
    kind="rename-call",
    old_qualname="flask.helpers.safe_join",
    new_qualname="werkzeug.utils.secure_filename",
    confidence="high",
    test_before="from flask.helpers import safe_join\nresult = safe_join(a, b)",
    test_after="from werkzeug.utils import secure_filename\nresult = secure_filename(a, b)",
)


class TestLegacyDelegation:
    def test_verifies_via_binding_aware_visitors(self):
        assert verify_rule(LEGACY_RENAME) is True

    def test_bare_call_without_import_is_not_rewritten(self):
        # No import binding → the binding-aware visitor leaves the call alone. A
        # textual matcher would have renamed it — this proves the adapter did not
        # degrade Tier-1 binding-awareness.
        new, sites = apply_rule("result = safe_join(a, b)", LEGACY_RENAME)
        assert sites == 0
        assert new == "result = safe_join(a, b)"


class TestMalformedBForm:
    def test_two_unbound_metavars_raises_and_is_unverified(self):
        rule = CodemodRule(
            library="x", from_version="1", to_version="2",
            match="$DF.lookup($ROWS, $COLS)",
            rewrite=["$A = $DF.foo()", "$B = $DF.bar()"],  # $A and $B both unbound
            test_before="vals = df.lookup(rows, cols)",
            test_after="vals = df.foo()",
        )
        with pytest.raises(ValueError):
            apply_rule_detailed("vals = df.lookup(rows, cols)", rule)
        assert verify_rule(rule) is False

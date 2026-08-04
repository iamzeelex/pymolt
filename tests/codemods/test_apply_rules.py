"""Tests for pymolt.codemods.apply's Tier-2 (rule) repo-level appliers —
apply_rules_to_repo / preview_rules_repo. Unit-level rule semantics (mode
detection, advisories, condition catalogs) are covered by test_rules.py; this
file covers the file-walking + write-gate + advisory-surfacing behavior these
two wrap around apply_rule_detailed."""

from __future__ import annotations

from pymolt.codemods.apply import apply_rules_to_repo, preview_rules_repo
from pymolt.codemods.rules import CodemodRule

LOOKUP_RULE = CodemodRule(
    library="pandas",
    from_version="1.0.0",
    to_version="1.2.0",
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
)

LEGACY_RENAME_RULE = CodemodRule(
    library="flask",
    from_version="2.0.3",
    to_version="3.0.0",
    match="$X.safe_join(...)",
    rewrite=["$X.secure_filename(...)"],
    confidence="verified",
    kind="rename-call",
    old_qualname="flask.helpers.safe_join",
    new_qualname="werkzeug.utils.secure_filename",
)


class TestApplyRulesToRepoWrite:
    def test_verified_rule_writes_on_assignment_site(self, tmp_path):
        (tmp_path / "app.py").write_text("vals = df.lookup(rows, cols)\n")
        result = apply_rules_to_repo(tmp_path, [LOOKUP_RULE], write=True)
        out = (tmp_path / "app.py").read_text()
        assert "get_indexer" in out
        assert result.files_changed == 1
        assert result.dry_run is False

    def test_heuristic_rule_never_writes(self, tmp_path):
        (tmp_path / "app.py").write_text("vals = df.lookup(rows, cols)\n")
        heuristic = LOOKUP_RULE.model_copy(update={"confidence": "heuristic"})
        result = apply_rules_to_repo(tmp_path, [heuristic], write=True)
        assert (tmp_path / "app.py").read_text() == "vals = df.lookup(rows, cols)\n"
        assert result.changes == []

    def test_heuristic_rule_still_previews_in_dry_run(self, tmp_path):
        (tmp_path / "app.py").write_text("vals = df.lookup(rows, cols)\n")
        heuristic = LOOKUP_RULE.model_copy(update={"confidence": "heuristic"})
        result = apply_rules_to_repo(tmp_path, [heuristic], write=False)
        assert result.files_changed == 1
        assert result.dry_run is True

    def test_legacy_rule_writes_via_binding_aware_visitor(self, tmp_path):
        (tmp_path / "app.py").write_text(
            "from flask.helpers import safe_join\nx = safe_join(a, b)\n"
        )
        result = apply_rules_to_repo(tmp_path, [LEGACY_RENAME_RULE], write=True)
        out = (tmp_path / "app.py").read_text()
        assert "secure_filename" in out
        assert result.files_changed == 1


class TestAdvisorySurfacing:
    def test_non_assignment_site_yields_advisory_not_silent_skip(self, tmp_path):
        (tmp_path / "app.py").write_text("def f():\n    return df.lookup(rows, cols)\n")
        result = apply_rules_to_repo(tmp_path, [LOOKUP_RULE], write=True)
        assert (tmp_path / "app.py").read_text() == (
            "def f():\n    return df.lookup(rows, cols)\n"
        )
        assert result.changes == []
        assert len(result.advisories_by_file) == 1
        advisories = next(iter(result.advisories_by_file.values()))
        assert advisories[0].severity in ("not-applied", "precondition")

    def test_runtime_precondition_applies_with_caveat_advisory(self, tmp_path):
        (tmp_path / "app.py").write_text("vals = df.lookup(rows, cols)\n")
        result = apply_rules_to_repo(tmp_path, [LOOKUP_RULE], write=True)
        assert result.files_changed == 1
        advisories = result.advisories_by_file[str(tmp_path / "app.py")]
        assert any(a.severity == "precondition" for a in advisories)


class TestPreviewRulesRepo:
    def test_advisory_only_file_still_yields_a_preview(self, tmp_path):
        (tmp_path / "app.py").write_text("def f():\n    return df.lookup(rows, cols)\n")
        previews = preview_rules_repo(tmp_path, [LOOKUP_RULE])
        assert len(previews) == 1
        preview = previews[0]
        assert preview.sites == 0
        assert preview.old_source == preview.new_source
        assert len(preview.advisories) == 1

    def test_rewritten_file_preview_carries_the_rule(self, tmp_path):
        (tmp_path / "app.py").write_text("vals = df.lookup(rows, cols)\n")
        previews = preview_rules_repo(tmp_path, [LOOKUP_RULE])
        assert len(previews) == 1
        preview = previews[0]
        assert preview.sites == 1
        assert preview.rules == [LOOKUP_RULE]
        assert preview.old_source != preview.new_source

    def test_untouched_file_yields_no_preview(self, tmp_path):
        (tmp_path / "app.py").write_text("y = 1 + 2\n")
        assert preview_rules_repo(tmp_path, [LOOKUP_RULE]) == []

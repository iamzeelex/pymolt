"""axiom_graph/analyzers/__init__.py — lazy imports to avoid heavy deps at collection time."""

__all__ = [
    "griffe_diff",
    "RawBreakingChange",
    "mine_deprecation_hints",
    "mine_test_examples",
    "extract_patterns",
    "build_cfg_from_source",
    "ControlFlowGraph",
    "build_ssa_from_source",
    "SSAForm",
    "detect_delegation",
    "analyze_value_flow",
    "ValueFlowResult",
]


def __getattr__(name: str):
    if name in ("griffe_diff", "RawBreakingChange"):
        from axiom_graph.analyzers.griffe_diff import RawBreakingChange, griffe_diff
        globals()["griffe_diff"] = griffe_diff
        globals()["RawBreakingChange"] = RawBreakingChange
        return globals()[name]
    if name == "mine_deprecation_hints":
        from axiom_graph.analyzers.ast_miner import mine_deprecation_hints
        globals()["mine_deprecation_hints"] = mine_deprecation_hints
        return mine_deprecation_hints
    if name == "mine_test_examples":
        from axiom_graph.analyzers.test_miner import mine_test_examples
        globals()["mine_test_examples"] = mine_test_examples
        return mine_test_examples
    if name == "extract_patterns":
        from axiom_graph.analyzers.pattern_extractor import extract_patterns
        globals()["extract_patterns"] = extract_patterns
        return extract_patterns
    if name in ("build_cfg_from_source", "ControlFlowGraph"):
        from axiom_graph.analyzers.cfg import ControlFlowGraph, build_cfg_from_source
        globals()["build_cfg_from_source"] = build_cfg_from_source
        globals()["ControlFlowGraph"] = ControlFlowGraph
        return globals()[name]
    if name in ("build_ssa_from_source", "SSAForm"):
        from axiom_graph.analyzers.ssa import SSAForm, build_ssa_from_source
        globals()["build_ssa_from_source"] = build_ssa_from_source
        globals()["SSAForm"] = SSAForm
        return globals()[name]
    if name in ("detect_delegation", "analyze_value_flow", "ValueFlowResult"):
        from axiom_graph.analyzers.value_flow import (
            ValueFlowResult,
            analyze_value_flow,
            detect_delegation,
        )
        globals()["detect_delegation"] = detect_delegation
        globals()["analyze_value_flow"] = analyze_value_flow
        globals()["ValueFlowResult"] = ValueFlowResult
        return globals()[name]
    raise AttributeError(f"module 'axiom_graph.analyzers' has no attribute {name!r}")


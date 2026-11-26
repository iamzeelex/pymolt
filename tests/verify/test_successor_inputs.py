"""A successor rewrite's inputs read back in the old call's terms (``verify.successor_inputs``)
and the maps the ledger keeps for it (``apply_ledger.successor_maps``)."""

from __future__ import annotations

from pymolt.apply_ledger import LedgerEntry, successor_maps
from pymolt.codemods.models import CodemodPattern
from pymolt.verify.facts import Fact
from pymolt.verify.successor_inputs import normalize

POK = "POSITIONAL_OR_KEYWORD"


def _fact(line, bound, defaulted=None, file="app/x.py"):
    return Fact(symbol="pkg.Expr.shift", file=file, func="f", line=line, lines=[line],
                input={"bound": bound}, defaulted=defaulted, count=1)


def test_a_renamed_keyword_takes_its_old_name_back_at_the_rewritten_statement():
    edits = [{"file": "app/x.py", "located": {"new": [3]}, "param_map": {"periods": "n"},
              "old_symbol": "pkg.Expr.shift", "new_symbol": "pkg.Expr.shift"}]
    here, elsewhere, other_file = normalize(
        [_fact(3, {"n": 1, "self": "e"}), _fact(9, {"n": 2}), _fact(3, {"n": 3}, file="b.py")],
        edits)
    assert here.input["bound"] == {"periods": 1, "self": "e"}
    assert elsewhere.input["bound"] == {"n": 2} and other_file.input["bound"] == {"n": 3}


def test_a_written_out_default_goes_back_to_the_defaulted_parameters_only_when_equal():
    edits = [{"file": "app/x.py", "new_lines": [5, 5], "materialized": {"name": "'row_nr'"},
              "old_symbol": "pkg.Expr.shift", "new_symbol": "pkg.Expr.shift"}]
    same, other = normalize([_fact(5, {"name": "row_nr"}, [["offset"]]),
                             _fact(5, {"name": "index"}, [["offset"]])], edits)
    assert same.input["bound"] == {} and same.defaulted == [["name", "offset"]]
    assert other.input["bound"] == {"name": "index"} and other.defaulted == [["offset"]]


def test_no_successor_edit_changes_nothing():
    facts = [_fact(3, {"n": 1})]
    assert normalize(facts, [{"file": "app/x.py", "located": {"new": [3]}}]) is facts


def test_the_ledger_keeps_a_successor_steps_maps():
    kw = CodemodPattern(old_qualname="pkg.f", new_qualname="pkg.f", kind="rename-keyword",
                        params={"periods": "n"})
    old = [["self", POK, None], ["name", POK, "'row_nr'"], ["offset", POK, "0"]]
    new = [["self", POK, None], ["name", POK, "'index'"], ["offset", POK, "0"]]
    fwd = CodemodPattern(
        old_qualname="pkg.F.with_row_count", new_qualname="pkg.F.with_row_index",
        kind="forward-template",
        template={"member_leaf": "with_row_count", "args": [{"p": "name"}, {"p": "offset"}],
                  "kwargs": [], "old_shape": old, "callee_new_shape": new})
    maps = successor_maps([kw, fwd])
    assert maps == {"param_map": {"periods": "n"},
                    "member_map": {"with_row_count": "with_row_index"},
                    "materialized": {"name": "'row_nr'"}}
    entry = LedgerEntry(run_id="r", step_id="mech-1", file="a.py", **maps)
    assert entry.as_dict()["param_map"] == {"periods": "n"}
    assert "param_map" not in LedgerEntry(run_id="r", step_id="mech-1", file="a.py").as_dict()


def test_only_the_rewritten_callables_own_calls_are_taken_back():
    """``df.shift(periods=1).tail(2)`` -> ``df.shift(n=1).tail(2)``: an unproven ``tail``
    ``periods`` -> ``n`` on the same statement is an input change verify must show."""
    edits = [{"file": "app/x.py", "located": {"new": [3]}, "param_map": {"periods": "n"},
              "old_symbol": "pkg.Frame.shift", "new_symbol": "pkg.Frame.shift"}]
    shift = Fact(symbol="pkg.Frame.shift", file="app/x.py", func="f", line=3, lines=[3],
                 input={"bound": {"n": 1}}, count=1)
    tail = Fact(symbol="pkg.Frame.tail", file="app/x.py", func="f", line=3, lines=[3],
                input={"bound": {"n": 2}}, count=1)
    got_shift, got_tail = normalize([shift, tail], edits)
    assert got_shift.input["bound"] == {"periods": 1}
    assert got_tail.input["bound"] == {"n": 2}
    # an entry that names no symbol maps nothing back
    assert normalize([shift], [{**edits[0], "old_symbol": None, "new_symbol": None}]) == [shift]


def test_a_callable_that_moved_within_its_package_is_still_the_entrys():
    """numpy 2 moved ``percentile`` to ``numpy.lib._function_base_impl``; the ledger names the
    old module."""
    edits = [{"file": "app/x.py", "located": {"new": [3]}, "param_map": {"interpolation": "method"},
              "old_symbol": "numpy.lib.function_base.percentile",
              "new_symbol": "numpy.lib.function_base.percentile"}]
    moved = Fact(symbol="numpy.lib._function_base_impl.percentile", file="app/x.py", func="f",
                 line=3, lines=[3], input={"bound": {"method": "'lower'"}}, count=1)
    other = Fact(symbol="scipy.stats.percentile", file="app/x.py", func="f", line=3, lines=[3],
                 input={"bound": {"method": "'lower'"}}, count=1)
    got_moved, got_other = normalize([moved, other], edits)
    assert got_moved.input["bound"] == {"interpolation": "'lower'"}
    assert got_other.input["bound"] == {"method": "'lower'"}

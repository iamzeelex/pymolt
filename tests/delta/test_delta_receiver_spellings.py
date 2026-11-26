"""Receiver spellings: the delta probes what a call site reaches through its receiver.

The old version records ``df.groupby(...)`` as ``NDFrame.groupby``, the class that defined it
there. Whether the call still works depends on the receiver's class (``DataFrame.groupby``),
so the engine probes that spelling too, names every spelling it compared in some fact, and
says where a removed spelling stopped resolving."""

from __future__ import annotations

import json
from types import SimpleNamespace

from pymolt.delta import touched
from pymolt.delta.engine import DiffInput, derive_symbols, diff
from pymolt.delta.models import TouchedSymbol

from ._fakes import res, shape, step

POK = "POSITIONAL_OR_KEYWORD"
GROUPBY = shape(("self", POK, None), ("by", POK, "None"))
FRAME = "pkg.core.frame.DataFrame"
NDFRAME = "pkg.core.generic.NDFrame"


def _fact(symbol, receiver, *, marker="__structure__", key="self", text=None):
    return SimpleNamespace(symbol=symbol, text=text, file="app/a.py", func="f",
                           input={"bound": {key: {marker: receiver}}})


# ── which spellings ─────────────────────────────────────────────────────────────────────


def test_a_call_on_a_receiver_is_spelled_through_the_receivers_class_and_its_candidates():
    found = touched.receiver_spellings([_fact(f"{NDFRAME}.groupby", FRAME)])
    assert found == {f"{FRAME}.groupby": FRAME, "pkg.core.DataFrame.groupby": FRAME,
                     "pkg.DataFrame.groupby": FRAME}


def test_a_class_receiver_and_every_value_marker_name_the_receiver():
    ctor = _fact("pkg._libs.tslib.Timedelta.__new__", "pkg._libs.tslib.Timedelta",
                 marker="__type__", key="cls")
    assert "pkg.Timedelta.__new__" in touched.receiver_spellings([ctor])
    for marker in ("__opaque__", "__object__", "__shape__", "__dataclass__"):
        assert touched.receiver_spellings([_fact("pkg.m.C.run", "pkg.m.C", marker=marker)])


def test_generated_names_stdlib_receivers_and_bare_calls_give_no_spelling():
    assert touched.receiver_spellings([
        _fact("pkg.core.generic.NDFrame._create_indexer.<locals>._indexer", FRAME),
        _fact("collections.OrderedDict.get", "collections.OrderedDict"),
        SimpleNamespace(symbol="pkg.mod.f", text=None, input={"bound": {"x": 1}}),
    ]) == {}


def test_a_project_receiver_resolves_to_its_first_dependency_base(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "models.py").write_text(
        "import pkg\n\nclass Frame(pkg.DataFrame):\n    pass\n")
    found = touched.receiver_spellings([_fact(f"{NDFRAME}.groupby", "app.models.Frame")],
                                       tmp_path)
    assert found == {"pkg.DataFrame.groupby": "pkg.DataFrame"}


# ── what the diff says about them ───────────────────────────────────────────────────────


def _cls_step(attr, owner, defined_as, **kw):
    return step(attr, owner, kind="class", canonical=defined_as, defined_as=defined_as, **kw)


def _old():
    groupby = step("groupby", f"{NDFRAME}.groupby", canonical=f"{NDFRAME}.groupby",
                   shape=GROUPBY)
    return {
        f"{NDFRAME}.groupby": res("pkg.core.generic", _cls_step("NDFrame", NDFRAME, NDFRAME),
                                  groupby),
        f"{FRAME}.groupby": res("pkg.core.frame", _cls_step("DataFrame", FRAME, FRAME),
                                groupby),
        "pkg.DataFrame.groupby": res("pkg", _cls_step("DataFrame", FRAME, FRAME), groupby),
        # a candidate that names another class in the old version: not the receiver's
        "pkg.core.DataFrame.groupby": res("pkg.core",
                                          _cls_step("DataFrame", "pkg.core.Other",
                                                    "pkg.core.Other"),
                                          step("groupby", "pkg.core.Other.groupby",
                                               shape=GROUPBY)),
    }


def _new():
    groupby = step("groupby", f"{FRAME}.groupby", canonical=f"{FRAME}.groupby", shape=GROUPBY)
    return {
        f"{NDFRAME}.groupby": res("pkg.core.generic", _cls_step("NDFrame", NDFRAME, NDFRAME),
                                  step("groupby", f"{NDFRAME}.groupby", exists=False)),
        f"{FRAME}.groupby": res("pkg.core.frame", _cls_step("DataFrame", FRAME, FRAME),
                                groupby),
        "pkg.DataFrame.groupby": res("pkg", _cls_step("DataFrame", FRAME, FRAME), groupby),
        "pkg.core.DataFrame.groupby": res("pkg.core",
                                          _cls_step("DataFrame", "pkg.core.Other",
                                                    "pkg.core.Other"),
                                          step("groupby", "pkg.core.Other.groupby",
                                               exists=False)),
    }


def _touched():
    out = {f"{NDFRAME}.groupby": TouchedSymbol(path=f"{NDFRAME}.groupby", origin="trace")}
    for path in (f"{FRAME}.groupby", "pkg.DataFrame.groupby", "pkg.core.DataFrame.groupby"):
        out[path] = TouchedSymbol(path=path, origin="receiver", receiver=FRAME)
    return out


def test_the_receivers_spelling_survives_the_defining_class_losing_the_method():
    facts = diff(DiffInput(old=_old(), new=_new(), touched=_touched(),
                           root_to_package={"pkg": "pkg"}))["pkg"]
    gone = next(f for f in facts if f.symbol == f"{NDFRAME}.groupby")
    assert gone.kind in ("MOVED", "REMOVED")
    # The defining class is still there: only its last name went.
    assert gone.vanished_at == {f"{NDFRAME}.groupby": f"{NDFRAME}.groupby"}
    # Every spelling the engine compared is named by a fact: the receiver's still resolve.
    (kept,) = [f for f in facts if f.kind == "UNCHANGED"]
    assert set(kept.spellings()) == {f"{FRAME}.groupby", "pkg.DataFrame.groupby"}
    # The candidate that named another class is compared on neither side.
    assert all("pkg.core.DataFrame.groupby" not in f.spellings() for f in facts)
    # The receiver's class is no touched symbol of its own.
    assert all(f.symbol != FRAME and FRAME not in f.aliases for f in facts)


def test_a_removal_says_where_each_spelling_stopped_resolving():
    meth = shape(("self", POK, None))
    old = {
        "pkg.mod.Keep.gone": res("pkg.mod", _cls_step("Keep", "pkg.mod.Keep", "pkg.mod.Keep"),
                                 step("gone", "pkg.mod.Keep.gone", shape=meth)),
        "pkg.old.Thing.run": res("pkg.old", _cls_step("Thing", "pkg.old.Thing",
                                                      "pkg.old.Thing"),
                                 step("run", "pkg.old.Thing.run", shape=meth)),
    }
    new = {
        "pkg.mod.Keep.gone": res("pkg.mod", _cls_step("Keep", "pkg.mod.Keep", "pkg.mod.Keep"),
                                 step("gone", "pkg.mod.Keep.gone", exists=False)),
        "pkg.old.Thing.run": res("pkg", step("old", "pkg.old", exists=False)),
    }
    facts = diff(DiffInput(old=old, new=new, touched={
        p: TouchedSymbol(path=p, origin="trace") for p in old}, root_to_package={"pkg": "pkg"}))
    by = {f.symbol: f for f in facts["pkg"]}
    assert by["pkg.mod.Keep.gone"].vanished_at == {"pkg.mod.Keep.gone": "pkg.mod.Keep.gone"}
    assert by["pkg.old.Thing.run"].vanished_at == {"pkg.old.Thing.run": "pkg.old"}
    dumped = by["pkg.old.Thing.run"].model_dump()
    assert dumped["vanished_at"] == {"pkg.old.Thing.run": "pkg.old"}
    # A fact with nothing vanished dumps as before: no key.
    assert "vanished_at" not in next(iter(
        diff(DiffInput(old=old, new=old, touched={}, root_to_package={"pkg": "pkg"}))["pkg"]
    )).model_dump()


def test_derive_symbols_adds_the_baseline_calls_receiver_spellings(tmp_path):
    trace = tmp_path / ".pymolt" / "contract_traces" / "baseline.jsonl"
    trace.parent.mkdir(parents=True)
    trace.write_text(json.dumps({
        "v": 2, "t": "return", "q": f"{NDFRAME}.groupby", "sym": f"{NDFRAME}.groupby",
        "in": {"bound": {"self": {"__structure__": FRAME, "shape": [2, 1]}, "by": "k"}},
        "result": 1, "where": {"file": str(tmp_path / "app.py"), "line": 3, "func": "f"},
    }) + "\n", encoding="utf-8")
    found, notes = derive_symbols(tmp_path, trace)
    assert found[f"{NDFRAME}.groupby"].origin == "trace"
    assert found["pkg.DataFrame.groupby"].origin == "receiver"
    assert found["pkg.DataFrame.groupby"].receiver == FRAME

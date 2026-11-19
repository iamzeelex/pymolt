"""Python behaviour changes: the knowledge entries, the TypedDict annotation-order detector,
its BEHAVIOR delta facts and the medium predicted risk at each iteration site."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from pymolt import knowledge
from pymolt.delta.models import DeltaFact
from pymolt.pyversion.behavior import CAUSE, predicted_order_causes, scan_order_causes

DATA = Path(__file__).parent / "data"

# Verified on CPython 3.8.20 and 3.12.11 (stdlib_behavior.json names how): locust's two
# TypedDicts over StatsBaseDict.
ENTRY_38 = ["last_request_timestamp", "start_time", "num_requests", "num_none_requests",
            "num_failures", "total_response_time", "max_response_time", "min_response_time",
            "total_content_length", "response_times", "num_reqs_per_sec", "num_fail_per_sec",
            "name", "method"]
ENTRY_312 = ["name", "method", *ENTRY_38[:-2]]


def _project(root: Path, files: dict[str, str]) -> Path:
    for rel, source in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
    return root


def _locust(root: Path) -> Path:
    (root / "locust").mkdir()
    (root / "locust" / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy(DATA / "locust_stats" / "stats.py", root / "locust" / "stats.py")
    return root


def _by_class(causes):
    return {c["class"]: c for c in causes}


LAYERED = """\
    from typing import TypedDict

    class Base(TypedDict):
        name: str
        method: str

    class Single(TypedDict):
        a: int
        b: int

    class Layered(Base):
        error: str
        occurrences: int

    def rows(obj):
        one = [k for k in Single.__annotations__]
        two = {k: getattr(obj, k) for k in Layered.__annotations__.keys()}
        return one, two
"""


# ── knowledge ─────────────────────────────────────────────────────────────────────────────


def test_the_behavior_entries_load_validated_with_sources_and_windows():
    entries = {e.id: e for e in knowledge.stdlib_behavior()}
    td, hashing = entries[CAUSE], entries["str-hash-siphash13"]
    assert (td.changed_in, td.effect, td.detector) == (
        "3.9", "dict-order", "typeddict-inherited-annotations-iteration")
    assert (hashing.changed_in, hashing.effect, hashing.detector) == ("3.11", "hash-order", None)
    assert (hashing.old, hashing.new) == ("siphash24", "siphash13")
    for entry in entries.values():
        assert entry.source and entry.date and entry.hint and "verified" in entry.source
    assert td.applies("3.8", "3.12") and td.applies("3.8.20", "3.9")
    assert not td.applies("3.9", "3.12") and not td.applies("3.8", "3.8")
    assert not td.applies("3.12", "3.8") and not td.applies("", "3.12")
    assert hashing.applies("3.10", "3.11") and not hashing.applies("3.11", "3.13")


def test_a_malformed_or_duplicated_behavior_entry_is_refused(monkeypatch):
    good = json.loads((Path(knowledge.__file__).parent / "stdlib_behavior.json").read_text())
    entry = good["entries"][0]
    loader = knowledge.stdlib_behavior.__wrapped__
    monkeypatch.setattr(knowledge, "_load", lambda name: {"entries": [entry, entry]})
    with pytest.raises(knowledge.KnowledgeError, match="duplicate id"):
        loader()
    monkeypatch.setattr(knowledge, "_load",
                        lambda name: {"entries": [{**entry, "effect": "faster"}]})
    with pytest.raises(knowledge.KnowledgeError, match="stdlib_behavior.json"):
        loader()
    monkeypatch.setattr(knowledge, "_load",
                        lambda name: {"entries": [{**entry, "changed_in": "soon"}]})
    with pytest.raises(knowledge.KnowledgeError, match="not a version"):
        loader()


# ── the detector ──────────────────────────────────────────────────────────────────────────


def test_the_locust_shapes_give_both_orders_and_skip_the_membership_test(tmp_path):
    causes = _by_class(predicted_order_causes(_locust(tmp_path), "3.8", "3.12"))
    assert sorted(causes) == ["locust.stats.StatsEntryDict", "locust.stats.StatsErrorDict"]
    entry = causes["locust.stats.StatsEntryDict"]
    assert entry["old"] == ENTRY_38 and entry["new"] == ENTRY_312
    assert entry["keys"] == sorted(ENTRY_38) and entry["cause"] == CAUSE
    assert (entry["file"], entry["line"], entry["bases"]) == ("locust/stats.py", 21,
                                                              ["StatsBaseDict"])
    assert [(s["file"], s["line"]) for s in entry["sites"]] == [("locust/stats.py", 48)]
    assert "StatsEntryDict (locust/stats.py:21)" in entry["detail"]
    assert "locust/stats.py:48" in entry["detail"] and ":53" not in entry["detail"]
    error = causes["locust.stats.StatsErrorDict"]
    assert error["old"] == ["error", "occurrences", "name", "method"]
    assert error["new"] == ["name", "method", "error", "occurrences"]
    assert [s["line"] for s in error["sites"]] == [68]


def test_only_a_move_across_3_9_predicts(tmp_path):
    root = _locust(tmp_path)
    assert len(predicted_order_causes(root, "3.8", "3.9")) == 2
    assert predicted_order_causes(root, "3.9", "3.12") == []
    assert predicted_order_causes(root, "3.12", "3.8") == []
    assert predicted_order_causes(root, None, "3.12") == []


def test_a_single_typeddict_keeps_its_order_a_layered_one_does_not(tmp_path):
    causes = predicted_order_causes(_project(tmp_path, {"app.py": LAYERED}), "3.8", "3.12")
    assert [c["class"] for c in causes] == ["app.Layered"]
    assert causes[0]["old"] == ["error", "occurrences", "name", "method"]
    assert causes[0]["new"] == ["name", "method", "error", "occurrences"]


def test_three_levels_two_bases_a_diamond_and_a_redeclared_field(tmp_path):
    # Expected orders are what CPython 3.8.20 and 3.12.11 printed for these classes.
    source = """\
        from typing import TypedDict

        class A(TypedDict):
            a1: int
            a2: str

        class B(A):
            b1: int
            b2: int

        class C(B):
            c1: int
            c2: int

        class P(TypedDict):
            p1: int

        class Q(TypedDict, total=False):
            q1: int

        class M(P, Q):
            m1: int

        class X(TypedDict):
            x: int

        class DB(X):
            b: int

        class DC(X):
            c: int

        class D(DB, DC):
            d: int

        class O1(TypedDict):
            x: int
            y: int

        class O2(O1):
            y: str
            z: int

        class E(A):
            pass

        ROWS = [list(cls.__annotations__) for cls in (A,)]
        ORDERS = [tuple(C.__annotations__), tuple(M.__annotations__), tuple(D.__annotations__),
                  tuple(O2.__annotations__), tuple(E.__annotations__)]
    """
    causes = _by_class(predicted_order_causes(_project(tmp_path, {"h.py": source}), "3.8",
                                              "3.12"))
    orders = {name.split(".")[-1]: (c["old"], c["new"]) for name, c in causes.items()}
    assert orders == {
        "C": (["c1", "c2", "b1", "b2", "a1", "a2"], ["a1", "a2", "b1", "b2", "c1", "c2"]),
        "M": (["m1", "p1", "q1"], ["p1", "q1", "m1"]),
        "D": (["d", "b", "x", "c"], ["x", "b", "c", "d"]),
        "O2": (["y", "z", "x"], ["x", "y", "z"]),
    }                                               # E has no own fields: no change


HIERARCHY = """\
    from typing import TypedDict, get_type_hints

    class A(TypedDict):
        a1: int
        a2: str

    class B(A):
        b1: int

    class C(B):
        c1: int

    class P(TypedDict):
        p1: int

    class M(P, B):
        m1: int

    class O2(A):
        a2: bytes
        z: int

    F = TypedDict("F", {"f1": int, "f2": str})

    class G(F):
        g1: int

    import json, sys
    print(json.dumps({n: [list(c.__annotations__), list(get_type_hints(c))]
                      for n, c in (("C", C), ("M", M), ("O2", O2), ("G", G))}))
"""


def _interpreter(minor: str) -> str | None:
    uv = shutil.which("uv")
    if uv is None:
        return None
    proc = subprocess.run([uv, "python", "find", "--no-project", minor], capture_output=True,
                          text=True, timeout=30, cwd="/")
    found = proc.stdout.strip()
    return found if proc.returncode == 0 and found and Path(found).is_file() else None


def test_the_computed_orders_match_real_3_8_and_3_12_interpreters(tmp_path):
    old_python, new_python = _interpreter("3.8"), _interpreter("3.12")
    if old_python is None or new_python is None:
        pytest.skip("needs uv-managed Python 3.8 and 3.12 interpreters")
    root = _project(tmp_path, {"h.py": HIERARCHY})
    real = {exe: json.loads(subprocess.run([exe, "-I", str(root / "h.py")], capture_output=True,
                                           text=True, timeout=60, check=True).stdout)
            for exe in (old_python, new_python)}
    root.joinpath("use.py").write_text(
        "from h import C, M, O2, G\n"
        "ROWS = [list(C.__annotations__), list(M.__annotations__), list(O2.__annotations__),\n"
        "        list(G.__annotations__)]\n", encoding="utf-8")
    causes = {c["class"]: c for c in predicted_order_causes(root, "3.8", "3.12")}
    assert sorted(causes) == ["h.C", "h.G", "h.M", "h.O2"]
    for name in ("C", "M", "O2", "G"):
        old, new = real[old_python][name], real[new_python][name]
        assert old[0] == old[1] and new[0] == new[1]       # get_type_hints: the same order
        assert (causes[f"h.{name}"]["old"], causes[f"h.{name}"]["new"]) == (old[0], new[0])


def test_guarded_imports_follow_the_branch_the_base_runs(tmp_path):
    body = """
        class Base(TypedDict):
            a: int

        class Child(Base):
            b: int

        KEYS = list(Child.__annotations__)
    """
    guarded = "try:\n    from typing import TypedDict\nexcept ImportError:\n" \
              "    from typing_extensions import TypedDict\n"
    versioned = "import sys\nif sys.version_info >= (3, 8):\n    from typing import TypedDict\n" \
                "else:\n    from typing_extensions import TypedDict\n"
    aliased = "import typing as t\nTypedDict = t.TypedDict\n"
    cases = {"guarded": guarded, "versioned": versioned, "aliased": aliased,
             "star": "from typing import *  # noqa: F403\n",
             "extensions": "from typing_extensions import TypedDict\n",
             "checking": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
                         "    from typing import TypedDict\n"}
    for name, head in cases.items():
        _project(tmp_path / name, {"m.py": head + textwrap.dedent(body)})
    on_38 = {n for n in cases if predicted_order_causes(tmp_path / n, "3.8", "3.12")}
    assert on_38 == {"guarded", "versioned", "aliased", "star"}
    # On 3.7 typing has no TypedDict: the fallback (typing_extensions) runs, already 3.9-ordered.
    assert predicted_order_causes(tmp_path / "guarded", "3.7", "3.12") == []
    causes, notes = scan_order_causes(tmp_path / "extensions", "3.8", "3.12")
    assert causes == [] and len(notes) == 1 and "typing_extensions" in notes[0]


def test_typing_module_attribute_and_get_type_hints_are_followed(tmp_path):
    source = """\
        import typing

        class Base(typing.TypedDict):
            a: int

        class Child(Base):
            b: int

        def hints():
            return [name for name, _ in typing.get_type_hints(Child).items()]
    """
    causes = predicted_order_causes(_project(tmp_path, {"m.py": source}), "3.8", "3.12")
    assert [(c["class"], [s["line"] for s in c["sites"]]) for c in causes] == [("m.Child", [10])]
    assert "get_type_hints(Child).items()" in causes[0]["sites"][0]["how"]


def test_order_free_uses_are_never_flagged(tmp_path):
    source = """\
        from typing import TypedDict

        class Base(TypedDict):
            a: int

        class Child(Base):
            b: int

        def order_free(key, data):
            known = Child.__annotations__.keys()
            return (key in Child.__annotations__, key not in known, len(Child.__annotations__),
                    sorted(Child.__annotations__), set(Child.__annotations__.keys()),
                    {k for k in Child.__annotations__}, sorted(k for k in Child.__annotations__),
                    [k for k in data if k in Child.__annotations__], any(k == key for k in known))
    """
    assert predicted_order_causes(_project(tmp_path, {"m.py": source}), "3.8", "3.12") == []


def test_every_order_sensitive_use_is_flagged(tmp_path):
    source = """\
        from collections import OrderedDict
        from typing import TypedDict

        class Base(TypedDict):
            a: int

        class Child(Base):
            b: int

        def uses(values):
            known = Child.__annotations__.keys()
            for k in known:
                print(k)
            first, second = Child.__annotations__
            return (tuple(Child.__annotations__), dict(Child.__annotations__),
                    list(enumerate(Child.__annotations__)),
                    list(zip(values, Child.__annotations__)),
                    ",".join(Child.__annotations__), [*Child.__annotations__],
                    {**Child.__annotations__}, OrderedDict(Child.__annotations__),
                    list(k for k in Child.__annotations__.values()))
    """
    causes = predicted_order_causes(_project(tmp_path, {"m.py": source}), "3.8", "3.12")
    hows = [s["how"].split(" Child")[0].split(" known")[0] for s in causes[0]["sites"]]
    assert [s["line"] for s in causes[0]["sites"]] == [12, 14, 15, 15, 16, 17, 18, 18, 19, 19,
                                                       20]
    assert "for loop over" in hows and "unpacking of" in hows and "join() of" in hows
    assert "known (= Child.__annotations__.keys())" in causes[0]["sites"][0]["how"]


def test_a_same_named_class_elsewhere_is_not_confused(tmp_path):
    root = _project(tmp_path, {
        "pkg/__init__.py": "from .typed import Entry\n",
        "pkg/typed.py": """\
            from typing import TypedDict

            class Base(TypedDict):
                name: str

            class Entry(Base):
                count: int
        """,
        # a plain class with the same name, iterated: not a TypedDict
        "pkg/plain.py": """\
            class Base:
                name: str

            class Entry(Base):
                count: int

            FIELDS = list(Entry.__annotations__)
        """,
        # the TypedDict, reached through the package re-export and a relative import
        "pkg/use.py": """\
            from . import Entry as E
            from .typed import Entry

            def rows(Entry):
                return list(Entry.__annotations__)      # a parameter shadows the class

            ROWS = [list(E.__annotations__)]
        """,
        "other/plain_td.py": """\
            from typing import TypedDict

            class Entry(TypedDict):
                name: str
                count: int

            NAMES = list(Entry.__annotations__)
        """,
    })
    causes = predicted_order_causes(root, "3.8", "3.12")
    assert [(c["class"], c["file"], [(s["file"], s["line"]) for s in c["sites"]])
            for c in causes] == [("pkg.typed.Entry", "pkg/typed.py", [("pkg/use.py", 7)])]


def test_unreadable_fields_and_broken_files_are_notes_not_guesses(tmp_path):
    root = _project(tmp_path, {
        "m.py": """\
            import sys
            from typing import TypedDict

            class Base(TypedDict):
                a: int

            class Child(Base):
                if sys.platform == "win32":
                    b: int

            KEYS = list(Child.__annotations__)
        """,
        "broken.py": "def broken(:\n    x.__annotations__\n",
        ".venv/lib/site.py": "from typing import TypedDict\nclass A(TypedDict):\n    a: int\n"
                             "class B(A):\n    b: int\nK = list(B.__annotations__)\n",
    })
    causes, notes = scan_order_causes(root, "3.8", "3.12")
    assert causes == []
    assert len(notes) == 1 and "Child (m.py:7)" in notes[0] and "not predicted" in notes[0]


def test_a_functional_typeddict_base_is_read(tmp_path):
    source = """\
        from typing import TypedDict

        F = TypedDict("F", {"f1": int, "f2": str})

        class G(F):
            g1: int

        ORDER = list(G.__annotations__)
    """
    (cause,) = predicted_order_causes(_project(tmp_path, {"m.py": source}), "3.8", "3.12")
    assert (cause["old"], cause["new"]) == (["g1", "f1", "f2"], ["f1", "f2", "g1"])


# ── delta facts and predicted risks ───────────────────────────────────────────────────────


def test_python_delta_emits_a_behavior_fact_per_class(tmp_path):
    from pymolt.pyversion.facts import python_delta

    report = python_delta(_locust(tmp_path), [], "3.8", "3.12", target_executable=sys.executable)
    facts = {f.symbol: f for f in report.facts}
    fact = facts["locust.stats.StatsEntryDict.__annotations__"]
    assert fact.kind == "BEHAVIOR" and fact.changed_in == "3.9"
    assert fact.old.split(", ") == ENTRY_38 and fact.new.split(", ") == ENTRY_312
    assert fact.behavior["cause"] == CAUSE and fact.behavior["effect"] == "dict-order"
    assert fact.behavior["sites"] == [{"file": "locust/stats.py", "line": 48,
                                       "how": "comprehension over "
                                              "StatsEntryDict.__annotations__.keys()"}]
    assert "fix: advisory" in fact.evidence and any(e.startswith("hint: ")
                                                    for e in fact.evidence)
    dumped = report.model_dump()["facts"][0]
    assert dumped["kind"] == "BEHAVIOR" and dumped["changed_in"] == "3.9"
    assert python_delta(tmp_path, [], "3.9", "3.12", target_executable=sys.executable).facts == []


def test_an_ordinary_fact_dumps_without_the_behavior_additions():
    dumped = DeltaFact(symbol="imp", kind="REMOVED").model_dump()
    assert "changed_in" not in dumped and "behavior" not in dumped


def test_each_iteration_site_is_a_medium_python_risk(tmp_path):
    from pymolt.pyversion.facts import python_delta
    from pymolt.verify.predict import RiskTier, predict_python_risks

    report = python_delta(_locust(tmp_path), [], "3.8", "3.12", target_executable=sys.executable)
    out = predict_python_risks(report, [], base="3.8", target="3.12")
    risks = sorted(out.risks, key=lambda r: r.site.line)
    assert [(r.tier, r.site.label(), r.scope, r.basis, r.fix) for r in risks] == [
        (RiskTier.MEDIUM, "locust/stats.py:48", "python", "static", "advisory"),
        (RiskTier.MEDIUM, "locust/stats.py:68", "python", "static", "advisory")]
    assert risks[0].reason.startswith("Python 3.9 puts StatsEntryDict's base fields first")
    assert "StatsEntryDict.__annotations__.keys()" in risks[0].reason
    assert any(e.startswith("order on Python 3.8: last_request_timestamp")
               for e in risks[0].evidence)
    assert out.deltas[0].changes == 2 and not out.guarded and not out.pre_existing

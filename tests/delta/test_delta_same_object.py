"""Same-object successors: the old-side probe finds public spellings of the very object (``is``)
and the diff turns a vanished spelling with a live one into MOVED."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from pymolt.delta import probe as probe_mod
from pymolt.delta.engine import DiffInput, as_touched, diff
from pymolt.delta.provider import _pattern

from ._fakes import missing, res, shape, step

POK = "POSITIONAL_OR_KEYWORD"
ROOTS = {"pkg": "pkg"}


def _diff(old, new, extra=None):
    touched = as_touched(list(old))
    return diff(DiffInput(
        old=old, new=new, touched=touched, root_to_package=ROOTS,
        resolve_new=lambda paths: {p: r for p, r in (extra or {}).items() if p in paths},
    ))["pkg"]


def _old(same):
    return {"pkg.sub.f": res("pkg.sub", step(
        "f", "pkg.sub.f", shape=shape(("a", POK, None)), same_object_as=same))}


# ── engine ──────────────────────────────────────────────────────────────────────────────


def test_vanished_spelling_with_live_same_object_is_moved():
    new = {"pkg.sub.f": missing("pkg.sub", "f")}
    extra = {"pkg.f": res("pkg", step("f", "pkg.f", canonical="pkg._impl.f"))}
    (fact,) = _diff(_old(["pkg.f"]), new, extra)
    assert (fact.kind, fact.symbol, fact.successor) == ("MOVED", "pkg.sub.f", "pkg.f")
    assert fact.detail["successor_importable"] is True
    assert fact.detail["successor_same_signature"] is True
    assert fact.detail["importable"] == ["pkg.sub.f"]
    pattern = _pattern(fact)
    assert pattern is not None and pattern.new_qualname == "pkg.f"


def test_shortest_public_candidate_wins():
    new = {"pkg.sub.f": missing("pkg.sub", "f")}
    extra = {
        "pkg.api.deep.f": res("pkg.api.deep", step("f", "pkg.api.deep.f")),
        "pkg._h.f": res("pkg._h", step("f", "pkg._h.f")),
        "pkg.other.f": res("pkg.other", step("f", "pkg.other.f")),
    }
    old = _old(["pkg.api.deep.f", "pkg._h.f", "pkg.other.f"])
    (fact,) = _diff(old, new, extra)
    assert fact.successor == "pkg.other.f"


def test_deprecated_candidate_is_never_the_successor():
    new = {"pkg.sub.f": missing("pkg.sub", "f")}
    extra = {
        "pkg.f": res("pkg", step("f", "pkg.f", deprecated_attr="use g")),
        "pkg.api.f": res("pkg.api", step("f", "pkg.api.f")),
    }
    (fact,) = _diff(_old(["pkg.f", "pkg.api.f"]), new, extra)
    assert fact.successor == "pkg.api.f"
    only = _diff(_old(["pkg.f"]), new, {"pkg.f": extra["pkg.f"]})
    assert [f.kind for f in only] == ["REMOVED"]


def test_candidate_absent_on_new_side_stays_removed():
    new = {"pkg.sub.f": missing("pkg.sub", "f")}
    (fact,) = _diff(_old(["pkg.f"]), new, {"pkg.f": missing("pkg", "f")})
    assert fact.kind == "REMOVED" and fact.successor is None


def test_no_same_object_info_stays_removed():
    (fact,) = _diff(_old([]), {"pkg.sub.f": missing("pkg.sub", "f")})
    assert fact.kind == "REMOVED"


# ── probe, for real, on the host interpreter ────────────────────────────────────────────


def _run_probe(tmp_path: Path, paths, **request) -> dict:
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    req = {"paths": paths, "roots": ["pkg"], "per_path_timeout": 20, **request}
    run = subprocess.run([sys.executable, probe_mod.__file__], input=json.dumps(req),
                         capture_output=True, text=True, env=env, cwd=str(tmp_path / ".."),
                         timeout=60)
    out = {}
    for line in run.stdout.splitlines():
        if line.startswith(probe_mod.MARK):
            msg = json.loads(line[len(probe_mod.MARK):])
            if "path" in msg:
                out[msg["path"]] = msg["result"]
    return out


def _make_pkg(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    (pkg / "api").mkdir(parents=True)
    (pkg / "_priv").mkdir()
    (pkg / "__init__.py").write_text(
        "from pkg.sub import f, g\nfrom pkg._priv import h\nVALUE = None\nmod = __import__('os')\n")
    (pkg / "sub.py").write_text(
        "def f(a):\n    return a\n\ndef g(a):\n    return a\n\nSHARED = None\n")
    (pkg / "api" / "__init__.py").write_text("from pkg.sub import f\n")
    (pkg / "_priv" / "__init__.py").write_text("def h():\n    pass\n")


def test_probe_finds_public_spellings_of_the_same_object(tmp_path):
    _make_pkg(tmp_path)
    out = _run_probe(tmp_path, ["pkg.sub.f", "pkg.sub.SHARED", "pkg.f"])
    step_f = out["pkg.sub.f"]["steps"][0]
    assert step_f["same_object_as"] == ["pkg.f", "pkg.api.f"]  # public-first, shortest first
    assert "same_object_as" not in out["pkg.sub.SHARED"]["steps"][0]  # a singleton (None)
    # own excluded; the object's own home (pkg.sub.f) first, then the public re-export
    assert out["pkg.f"]["steps"][0]["same_object_as"] == ["pkg.sub.f", "pkg.api.f"]
    assert out["pkg.f"]["steps"][0]["same_object_home"] == "pkg.sub.f"


def test_probe_uses_identity_hints(tmp_path):
    _make_pkg(tmp_path)
    out = _run_probe(tmp_path, ["pkg.sub.g"], identity_hints=["pkg._priv.h", "pkg._priv.nope"])
    # g is exported at top level; hints add spellings only for the very same object
    assert out["pkg.sub.g"]["steps"][0]["same_object_as"] == ["pkg.g"]
    out = _run_probe(tmp_path, ["pkg._priv.h"], identity_hints=["pkg.sub.f"])
    assert out["pkg._priv.h"]["steps"][0]["same_object_as"] == ["pkg.h"]


def test_probe_skips_modules_and_builtins(tmp_path):
    _make_pkg(tmp_path)
    out = _run_probe(tmp_path, ["pkg.mod", "pkg.sub"], any_root=False)
    assert "same_object_as" not in out["pkg.mod"]["steps"][0]
    assert out["pkg.sub"]["steps"] == []  # a module path resolves to no attribute step


def test_real_probe_feeds_the_diff(tmp_path):
    _make_pkg(tmp_path)
    old = _run_probe(tmp_path, ["pkg.sub.f"])
    new = {"pkg.sub.f": missing("pkg.sub", "f")}
    extra = {"pkg.f": res("pkg", step("f", "pkg.f"))}
    facts = diff(DiffInput(old=old, new=new, touched=as_touched(list(old)),
                           root_to_package=ROOTS,
                           resolve_new=lambda ps: {p: r for p, r in extra.items() if p in ps}))
    (fact,) = facts["pkg"]
    assert (fact.kind, fact.successor) == ("MOVED", "pkg.f")

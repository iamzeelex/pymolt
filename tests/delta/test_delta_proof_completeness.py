"""A proof rewrites code only when the probe's summary of the body is complete (successors v2,
review round): every statement, wrapper and parameter is accounted for. Each case runs the real
probe over an old and a new version of a small package, diffs them, makes the successor
patterns, rewrites a call and runs it on both versions: the rewrite must not change the result,
and a summary that misses something names the successor but rewrites nothing."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from pymolt.codemods.apply import apply_pattern
from pymolt.delta import probe as probe_mod
from pymolt.delta.engine import DiffInput, _param_aliases, as_touched, diff
from pymolt.delta.models import DeltaReport
from pymolt.delta.provider import successor_patterns

from ._fakes import res, shape, step

POK = "POSITIONAL_OR_KEYWORD"


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, src in files.items():
        p = root / "fakepkg" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(src))
    return root


def _probe(root: Path, paths: list[str]) -> dict:
    req = {"paths": paths, "roots": ["fakepkg"], "per_path_timeout": 20}
    run = subprocess.run([sys.executable, probe_mod.__file__], input=json.dumps(req),
                         capture_output=True, text=True, cwd=str(root.parent),
                         env={**os.environ, "PYTHONPATH": str(root)}, timeout=120)
    out = {}
    for line in run.stdout.splitlines():
        if line.startswith(probe_mod.MARK):
            msg = json.loads(line[len(probe_mod.MARK):])
            if "path" in msg:
                out[msg["path"]] = msg["result"]
    return out


def _run(root: Path, src: str):
    sys.path.insert(0, str(root))
    try:
        for m in [m for m in sys.modules if m == "fakepkg" or m.startswith("fakepkg.")]:
            del sys.modules[m]
        ns: dict = {}
        exec(src, ns)
        return ns["y"]
    finally:
        sys.path.remove(str(root))
        for m in [m for m in sys.modules if m == "fakepkg" or m.startswith("fakepkg.")]:
            del sys.modules[m]


def _rewrite(tmp_path, old_files, new_files, path, src):
    old_root, new_root = _write(tmp_path / "old", old_files), _write(tmp_path / "new", new_files)
    facts = diff(DiffInput(
        old=_probe(old_root, [path]), new=_probe(new_root, [path]), touched=as_touched([path]),
        root_to_package={"fakepkg": "fakepkg"},
        resolve_new=lambda ps: _probe(new_root, list(ps)),
        resolve_old=lambda ps: _probe(old_root, list(ps)))).get("fakepkg", [])
    report = DeltaReport(package="fakepkg", from_version="1", to_version="2",
                         quality="complete", facts=facts)
    patterns, _ = successor_patterns(report, None)
    out = src
    for p in patterns:
        out = apply_pattern(out, p, path="app.py")[0]
    before = _run(old_root, src)
    after = _run(new_root, out) if out != src else before
    return before, after, out, facts


PLAIN_NEW = {"__init__.py": '''
    def total(values, scale=1):
        return sum(values) * scale
'''}


def test_a_plain_forward_is_still_rewritten(tmp_path):
    old = {"__init__.py": PLAIN_NEW["__init__.py"] + '''
    import warnings
    def old_total(values, scale=1):
        warnings.warn("old_total is deprecated; use total instead", DeprecationWarning)
        return total(values, scale)
'''}
    before, after, out, _ = _rewrite(tmp_path, old, PLAIN_NEW, "fakepkg.old_total",
                                     "from fakepkg import old_total\ny = old_total([1, 2])\n")
    assert "total([1, 2])" in out and "old_total" not in out
    assert before == after


def test_a_chain_on_the_forwards_result_is_no_rename(tmp_path):
    """``return isin(a, b).ravel()``: renaming to ``isin`` would drop ``.ravel()``."""
    lib = '''
    class Arr(list):
        def ravel(self):
            return sum(self, [])
    def isin(a, b):
        return Arr([[x in b] for x in a])
'''
    old = {"__init__.py": lib + '''
    import warnings
    def in1d(a, b):
        warnings.warn("`in1d` is deprecated. Use `isin` instead.", DeprecationWarning)
        return isin(a, b).ravel()
'''}
    before, after, out, _ = _rewrite(tmp_path, old, {"__init__.py": lib}, "fakepkg.in1d",
                                     "from fakepkg import in1d\ny = in1d([1, 2], [2])\n")
    assert before == after and "isin(" not in out


def test_a_default_the_forward_hands_on_is_kept(tmp_path):
    """``def old(a, b=1): return new(a, b)`` with ``new(a, b=2)``: ``new(a)`` would pass b=2."""
    new = {"__init__.py": "def new(a, b=2):\n    return (a, b)\n"}
    old = {"__init__.py": new["__init__.py"] + '''
import warnings
def old(a, b=1):
    warnings.warn("old is deprecated; use new instead", DeprecationWarning)
    return new(a, b)
'''}
    before, after, out, _ = _rewrite(tmp_path, old, new, "fakepkg.old",
                                     "from fakepkg import old\ny = old(5)\n")
    assert before == after and "new(5)" not in out


def test_an_alias_beside_the_forward_is_no_complete_summary(tmp_path):
    """``if size is not None: height = size`` then ``return newplot(data, height=height)``."""
    new = {"__init__.py": "def newplot(data, height=5):\n    return ('plot', data, height)\n"}
    old = {"__init__.py": new["__init__.py"] + '''
import warnings
def oldplot(data, height=5, size=None):
    if size is not None:
        warnings.warn("`size` is deprecated; use `newplot(height=)`", UserWarning)
        height = size
    return newplot(data, height=height)
'''}
    before, after, out, _ = _rewrite(tmp_path, old, new, "fakepkg.oldplot",
                                     "import fakepkg as fp\ny = fp.oldplot('d', size=3)\n")
    assert before == after and "newplot" not in out


def test_a_pop_alias_beside_the_forward_is_read_and_refuses_the_rename(tmp_path):
    """seaborn's ``kwargs["height"] = kwargs.pop("size")`` (read on 3.9+ too)."""
    new = {"__init__.py": '''
    def newplot(*args, **kwargs):
        return ("plot", args, sorted(kwargs.items()))
'''}
    old = {"__init__.py": new["__init__.py"] + '''
    import warnings
    def oldplot(*args, **kwargs):
        if "size" in kwargs:
            warnings.warn("`size` was renamed to `height`", UserWarning)
            kwargs["height"] = kwargs.pop("size")
        return newplot(*args, **kwargs)
'''}
    before, after, out, facts = _rewrite(tmp_path, old, new, "fakepkg.oldplot",
                                         "from fakepkg import oldplot\ny = oldplot('d', size=3)\n")
    assert before == after and "newplot" not in out
    rejected = [r for f in facts for r in (f.succession or {}).get("rejected") or ()]
    assert any("alias" in r["why"] for r in rejected), rejected


def test_a_helper_named_like_a_warning_that_receives_a_parameter_is_a_statement(tmp_path):
    old = {"__init__.py": PLAIN_NEW["__init__.py"] + '''
    import warnings
    def _handle_deprecated_kwargs(kwargs):
        warnings.warn("old_total is deprecated, use total instead", DeprecationWarning)
        kwargs.setdefault("scale", 10)
    def old_total(*args, **kwargs):
        _handle_deprecated_kwargs(kwargs)
        return total(*args, **kwargs)
'''}
    before, after, out, _ = _rewrite(tmp_path, old, PLAIN_NEW, "fakepkg.old_total",
                                     "from fakepkg import old_total\ny = old_total([1, 2])\n")
    assert before == after and "total([1, 2])" not in out.replace("old_total", "")


def test_a_wrapper_that_changes_the_result_makes_no_proof(tmp_path):
    new = {"__init__.py": "def total(values):\n    return sum(values)\n"}
    old = {"__init__.py": new["__init__.py"] + '''
import functools, warnings
def _legacy_units(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        warnings.warn("old_total is deprecated, use total", DeprecationWarning)
        return fn(*a, **k) * 100
    return wrapper
@_legacy_units
def old_total(values):
    return total(values)
'''}
    before, after, out, facts = _rewrite(tmp_path, old, new, "fakepkg.old_total",
                                         "from fakepkg import old_total\ny = old_total([1, 2])\n")
    assert before == after and "old_total" in out
    rejected = [r for f in facts for r in (f.succession or {}).get("rejected") or ()]
    assert any("wrapped" in r["why"] or "decorator" in r["why"] for r in rejected), rejected


def test_a_twin_in_another_module_is_no_proof(tmp_path):
    """``v1.load`` and ``v2.load`` both read ``return _impl(x)``; each module has its own
    ``_impl``."""
    files = {"__init__.py": "", "v1.py": '''
    import warnings
    def _impl(x):
        return x + 1
    def load(x):
        warnings.warn("fakepkg.v1.load is deprecated; use fakepkg.v2.load instead",
                      DeprecationWarning)
        return _impl(x)
''', "v2.py": '''
    def _impl(x):
        return x * 10
    def load(x):
        return _impl(x)
'''}
    before, after, out, _ = _rewrite(tmp_path, files, files, "fakepkg.v1.load",
                                     "from fakepkg.v1 import load\ny = load(3)\n")
    assert before == after and "fakepkg.v1" in out


def test_a_rename_helper_that_maps_values_proves_no_plain_rename():
    """pandas ``@deprecate_kwarg("take_last", "keep", mapping={True: "last"})``."""
    deco = [{"name": "deprecate_kwarg", "qualname": "pandas.util._decorators.deprecate_kwarg",
             "args": [], "kwargs": {"old_arg_name": "take_last", "new_arg_name": "keep",
                                    "mapping": {"expr": "Dict"}}}]
    sig = shape(("frame", POK, None), ("keep", POK, "'first'"))
    body = {"layers": [], "loads": {}, "decorators": ["pandas.util._decorators.deprecate_kwarg"]}
    old = {"pkg.dedupe": res("pkg", step("dedupe", "pkg.dedupe", shape=sig,
                                         decorator_calls=deco, body=body))}
    new = {"pkg.dedupe": res("pkg", step("dedupe", "pkg.dedupe", shape=sig))}
    facts = diff(DiffInput(old=old, new=new, touched=as_touched(list(old)),
                           root_to_package={"pkg": "pkg"}))["pkg"]
    fact = next(f for f in facts if f.param == "take_last")
    assert fact.successor == "keep" and fact.succession["trust"] == "named"
    assert "maps the values" in fact.succession["caveats"][0]
    report = DeltaReport(package="pkg", from_version="1", to_version="2", quality="complete",
                         facts=facts)
    assert successor_patterns(report, None)[0] == []


def test_an_alias_whose_old_parameter_is_also_read_elsewhere_is_none(tmp_path):
    (tmp_path / "m_alias.py").write_text(textwrap.dedent('''
        def g(*a): return a
        def old(x, size=None, height=None):
            if size is not None:
                height = size
            return g(x, height, size)
    '''))
    sys.path.insert(0, str(tmp_path))
    try:
        import m_alias
        body = probe_mod._mine(m_alias.old)["body"]
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("m_alias", None)
    assert body["aliases"] and body["loads"]["size"] == 3      # the test, the alias, g(...)
    assert _param_aliases({"body": body}) == {}


def test_mining_never_raises_on_a_forward_to_cls(tmp_path):
    (tmp_path / "m_cls.py").write_text("class A:\n    def __init__(self, a): pass\n"
                                       "    @classmethod\n    def old(cls, a):\n"
                                       "        return cls(a)\n")
    sys.path.insert(0, str(tmp_path))
    try:
        import m_cls
        mined = probe_mod._mine(m_cls.A.__dict__["old"])
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("m_cls", None)
    assert mined["body"]["forward"]["callee"] == "cls"


def test_an_incomplete_forward_still_names_its_callee(tmp_path):
    """The text names no successor; the body forwards to ``total`` under a wrapper pymolt does
    not know (django's ``keep_lazy_text``): the successor is named from the code, not
    rewritten."""
    new = {"__init__.py": "def total(values):\n    return sum(values)\n"}
    old = {"__init__.py": new["__init__.py"] + '''
import functools, warnings
def _lazy(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        return fn(*a, **k)
    return wrapper
@_lazy
def old_total(values):
    warnings.warn("old_total is deprecated", DeprecationWarning)
    return total(values)
'''}
    before, after, out, facts = _rewrite(tmp_path, old, new, "fakepkg.old_total",
                                         "from fakepkg import old_total\ny = old_total([1, 2])\n")
    assert before == after and "old_total" in out
    fact = next(f for f in facts if f.symbol == "fakepkg.old_total" and not f.param)
    assert fact.successor == "fakepkg.total"
    assert fact.succession["trust"] == "named" and "code" in fact.succession["sources"]

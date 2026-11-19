"""The static old side: acquiring a release's source and reading probe-shaped answers from it.

Synthetic wheels and sdists are built in ``tmp_path``; nothing touches the network or uv.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from pymolt.delta import compute_deltas
from pymolt.delta import engine as engine_mod
from pymolt.delta.acquire import (
    Acquired,
    AcquireError,
    UnsafeArchive,
    acquire,
    extract_archive,
    read_capped,
)
from pymolt.delta.engine import ProbeRun, param_changes
from pymolt.delta.models import Interpreter
from pymolt.delta.static_side import UNEVALUATED, static_probe

from ._fakes import missing, res, shape, step

POK = "POSITIONAL_OR_KEYWORD"

SOURCES = {
    "pkg/__init__.py": (
        "from ._impl import helper as public_helper\n"
        "from .child import Child\n"
        "from markupsafe import Markup\n"
        "def keep(x=1, y=SOME_CONST, *, z='a'):\n"
        "    '''.. deprecated:: 1.0\n       use other'''\n"
        "def gone(x):\n"
        "    import warnings\n"
        "    warnings.warn('gone is deprecated, use keep instead', DeprecationWarning)\n"
    ),
    "pkg/_impl.py": "def helper(a, b=None): ...\n",
    # the base lives in a namespace subpackage (no __init__.py): griffe cannot load it
    "pkg/child.py": (
        "from pkg.sub.base import Base\n"
        "class Child(Base):\n"
        "    def own(self, q): ...\n"
    ),
    "pkg/sub/base.py": "class Base:\n    def inherited(self): ...\n",
}


def _tree(base: Path, files: dict[str, str] = SOURCES) -> Path:
    for rel, text in files.items():
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return base


def _acquired(base: Path) -> Acquired:
    return Acquired("pkg", "1.0", "wheel", "pkg-1.0-py3-none-any.whl", "uv-cache", base, ["pkg"])


def _probe(tmp_path, *paths):
    base = _tree(tmp_path / "old")
    meta, results = static_probe({"pkg": _acquired(base)}, paths)
    return meta, results


def _last(result):
    return result["steps"][-1]


# ── the reader ──────────────────────────────────────────────────────────────────────────


def test_inherited_member_of_an_unloadable_base_is_unknown_never_absent(tmp_path):
    _, results = _probe(tmp_path, "pkg.Child.inherited", "pkg.Child.own")
    inherited = _last(results["pkg.Child.inherited"])
    assert inherited["exists"] is None  # not False: the diff must not read it as missing
    assert "does not resolve" in inherited["unknown"]
    assert results["pkg.Child.inherited"]["unknown"]
    own = _last(results["pkg.Child.own"])
    assert own["exists"] is True and own["kind"] == "callable"
    assert own["canonical"] == "pkg.child.Child.own"


def test_a_member_in_an_unloadable_namespace_subpackage_is_unknown(tmp_path):
    _, results = _probe(tmp_path, "pkg.sub.base.Base")
    assert _last(results["pkg.sub.base.Base"])["exists"] is None


def test_alias_is_followed_whatever_all_says(tmp_path):
    _, results = _probe(tmp_path, "pkg.public_helper", "pkg.Markup", "pkg.Markup.escape")
    helper = _last(results["pkg.public_helper"])
    assert helper["exists"] is True
    assert helper["canonical"] == "pkg._impl.helper"  # the defining name, as the probe spells it
    assert helper["shape"] == [["a", POK, None], ["b", POK, "None"]]
    # an alias to a name outside the archive exists; the target is its canonical name
    markup = _last(results["pkg.Markup"])
    assert markup["exists"] is True and markup["canonical"] == "markupsafe.Markup"
    # ... and the walk stops there: the next attribute is not the dependency's
    assert _last(results["pkg.Markup.escape"])["dep"] is False


def test_literal_defaults_are_kept_and_the_rest_are_never_compared(tmp_path):
    _, results = _probe(tmp_path, "pkg.keep")
    old_shape = _last(results["pkg.keep"])["shape"]
    assert old_shape == [["x", POK, "1"], ["y", POK, UNEVALUATED],
                         ["z", "KEYWORD_ONLY", "'a'"]]
    # a literal that changed is a DEFAULT change ...
    new = shape(("x", POK, "2"), ("y", POK, "<object>"), ("z", "KEYWORD_ONLY", "'a'"))
    kinds = [(c.kind, c.param) for c in param_changes(old_shape, new)]
    assert kinds == [("DEFAULT", "x")]  # ... the non-literal one (y) is not


def test_a_name_in_the_old_source_is_found_and_a_missing_one_is_absent(tmp_path):
    _, results = _probe(tmp_path, "pkg.gone", "pkg.nothing_like_it")
    gone = _last(results["pkg.gone"])
    assert gone["exists"] is True
    assert gone["warns"][0]["message"].startswith("gone is deprecated")
    assert _last(results["pkg.nothing_like_it"])["exists"] is False


def test_deprecation_prose_comes_from_the_docstring(tmp_path):
    _, results = _probe(tmp_path, "pkg.keep")
    assert _last(results["pkg.keep"])["doc"]["directive"] == [".. deprecated:: 1.0"]


def test_pep562_names_are_present_with_their_warning(tmp_path):
    base = _tree(tmp_path / "old", {
        "pkg/__init__.py": (
            "import warnings\n"
            "def __getattr__(name):\n"
            "    if name == 'Old':\n"
            "        warnings.warn('Old is deprecated, use New instead', DeprecationWarning)\n"
            "        return 1\n"
            "    raise AttributeError(name)\n"),
    })
    _, results = static_probe({"pkg": _acquired(base)}, ["pkg.Old", "pkg.Other"])
    old = _last(results["pkg.Old"])
    assert old["exists"] is True
    assert "use New instead" in old["getattr_warns"][0]["message"]
    # a module with a __getattr__ that does not name it cannot say it is absent
    assert _last(results["pkg.Other"])["exists"] is None


def test_meta_says_it_is_static_and_names_the_source(tmp_path):
    meta, _ = _probe(tmp_path, "pkg.keep")
    assert meta["basis"] == "static"
    assert meta["source"] == "pkg: wheel pkg-1.0-py3-none-any.whl"
    assert meta["dists"]["pkg"] == {"version": "1.0", "roots": ["pkg"]}


# ── acquiring ───────────────────────────────────────────────────────────────────────────


def _wheel(path: Path, files: dict[str, str | bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return path


def test_zip_slip_wheel_is_refused_and_nothing_escapes(tmp_path):
    evil = _wheel(tmp_path / "evil-1.0-py3-none-any.whl",
                  {"pkg/__init__.py": "x = 1\n", "../escaped.py": "boom = 1\n"})
    dest = tmp_path / "out" / "src"
    with pytest.raises(UnsafeArchive):
        extract_archive(evil, dest)
    assert not (tmp_path / "out" / "escaped.py").exists()
    assert not (dest / "pkg").exists()  # refused whole, before anything was written


def test_absolute_and_dotdot_tar_members_are_refused(tmp_path):
    for name in ("../escaped.py", "/etc/escaped.py"):
        archive = tmp_path / "evil.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            data = b"boom = 1\n"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        with pytest.raises(UnsafeArchive):
            extract_archive(archive, tmp_path / "out")
    assert not (tmp_path / "escaped.py").exists()


def test_extraction_keeps_python_and_placeholders_for_compiled_members(tmp_path):
    whl = _wheel(tmp_path / "w-1.0-py3-none-any.whl", {
        "pkg/__init__.py": "x = 1\n", "pkg/_speed.cpython-312-darwin.so": b"\x7fELF" * 1000,
        "pkg/data.bin": b"junk", "pkg-1.0.dist-info/top_level.txt": "pkg\n"})
    out = tmp_path / "out"
    extract_archive(whl, out)
    assert (out / "pkg" / "__init__.py").read_text() == "x = 1\n"
    assert (out / "pkg" / "_speed.cpython-312-darwin.so").stat().st_size == 0
    assert not (out / "pkg" / "data.bin").exists()


def test_read_capped_refuses_an_oversized_body():
    with pytest.raises(UnsafeArchive):
        read_capped(io.BytesIO(b"x" * 100), max_bytes=10)


def _uv_cache(tmp_path, monkeypatch, name="pkg", version="1.0"):
    cache = tmp_path / "uvcache"
    unpacked = cache / "wheels-v5" / "pypi" / name / f"{version}-py3-none-any"
    _tree(unpacked)
    (cache / "wheels-v5" / "pypi" / name / f"{version}-py3-none-any.http").write_bytes(b"")
    monkeypatch.setenv("UV_CACHE_DIR", str(cache))
    return unpacked


def test_acquire_reads_uvs_cache_offline(tmp_path, monkeypatch):
    unpacked = _uv_cache(tmp_path, monkeypatch)
    got = acquire(tmp_path / "proj", "pkg", "1.0")
    assert got.origin == "uv-cache" and got.kind == "wheel"
    assert got.search_dir == unpacked and got.roots == ["pkg"]
    assert got.source.startswith("wheel pkg-1.0-py3-none-any")


def test_acquire_offline_without_a_cache_says_to_use_online(tmp_path, monkeypatch):
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "empty"))
    with pytest.raises(AcquireError, match="--online"):
        acquire(tmp_path / "proj", "pkg", "1.0")


class _Fetcher:
    def __init__(self, files):
        self.files, self.bytes, self.requests = files, 0, 0

    def releases(self, dist):
        return {"1.0": self.files}

    def close(self):
        pass


def _pypi_file(archive: Path, **extra):
    return {"filename": archive.name, "url": "https://files.example/" + archive.name,
            "packagetype": "bdist_wheel",
            "digests": {"sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}, **extra}


def test_acquire_online_downloads_checks_the_digest_and_caches(tmp_path, monkeypatch):
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "empty"))
    whl = _wheel(tmp_path / "pkg-1.0-py3-none-any.whl", {
        "pkg/__init__.py": "def f(a=1): ...\n", "pkg-1.0.dist-info/top_level.txt": "pkg\n"})
    downloads = []

    def download(fetcher, url, dest):
        downloads.append(url)
        dest.write_bytes(whl.read_bytes())
        return whl.stat().st_size

    project = tmp_path / "proj"
    got = acquire(project, "pkg", "1.0", online=True, fetcher=_Fetcher([_pypi_file(whl)]),
                  download=download)
    assert got.origin == "pypi" and got.bytes_downloaded > 0
    assert got.search_dir.is_relative_to(project / ".pymolt" / "cache" / "static")
    # the second time nothing is fetched, even offline
    again = acquire(project, "pkg", "1.0", download=download)
    assert again.origin == "project-cache" and len(downloads) == 1
    _, results = static_probe({"pkg": again}, ["pkg.f"])
    assert _last(results["pkg.f"])["shape"] == [["a", POK, "1"]]


def test_acquire_online_refuses_a_digest_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "empty"))
    whl = _wheel(tmp_path / "pkg-1.0-py3-none-any.whl", {"pkg/__init__.py": "x = 1\n"})

    def download(fetcher, url, dest):
        dest.write_bytes(whl.read_bytes())
        return 1

    bad = _pypi_file(whl)
    bad["digests"] = {"sha256": "0" * 64}
    with pytest.raises(AcquireError, match="sha256"):
        acquire(tmp_path / "p1", "pkg", "1.0", online=True, fetcher=_Fetcher([bad]),
                download=download)


def test_acquire_online_refuses_a_zip_slip_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "empty"))
    (tmp_path / "evil").mkdir()
    evil = _wheel(tmp_path / "evil" / "pkg-1.0-py3-none-any.whl",
                  {"pkg/__init__.py": "x = 1\n", "../../escaped.py": "boom\n"})

    def evil_download(fetcher, url, dest):
        dest.write_bytes(evil.read_bytes())
        return 1

    with pytest.raises(UnsafeArchive):
        acquire(tmp_path / "p2", "pkg", "1.0", online=True, fetcher=_Fetcher([_pypi_file(evil)]),
                download=evil_download)
    assert not list(tmp_path.rglob("escaped.py"))


# ── the engine, with no old interpreter ─────────────────────────────────────────────────


OLD_EXE, NEW_EXE = "/envs/old/bin/python", "/envs/new/bin/python"


def test_engine_reads_the_old_side_statically_and_never_invents_a_removal(tmp_path, monkeypatch):
    base = _tree(tmp_path / "old")
    static = Interpreter(source="static", reason="no container is running",
                         static={"pkg": _acquired(base)},
                         note="old side read statically from wheel pkg-1.0-py3-none-any.whl")
    monkeypatch.setattr(engine_mod.envs_mod, "resolve_old",
                        lambda root, wants, online=False: static)
    monkeypatch.setattr(engine_mod.envs_mod, "resolve_new",
                        lambda root, wants, online=False: Interpreter(executable=NEW_EXE,
                                                                      source="target-env"))
    new_results = {
        "pkg.gone": missing("pkg", "gone"),
        "pkg.keep": res("pkg", step("keep", "pkg.keep", shape=shape(
            ("x", POK, "2"), ("y", POK, "5"), ("z", "KEYWORD_ONLY", "'a'")))),
        # absent in the new version, but the old side cannot see it either way: unknown
        "pkg.Child.inherited": res(
            "pkg", step("Child", "pkg.Child", kind="class", canonical="pkg.child.Child"),
            step("inherited", "pkg.child.Child.inherited", exists=False)),
    }
    seen = []

    def fake_probe(executable, root, request, *, use_cache=True, timeout=0, runner=None):
        seen.append(executable)
        meta = {"python": "3.12.0", "dists": {"pkg": {"version": "2.0", "roots": ["pkg"]}}}
        return ProbeRun(meta=meta, results={p: r for p, r in new_results.items()
                                            if p in request["paths"]})

    monkeypatch.setattr(engine_mod, "run_probe", fake_probe)
    [report] = compute_deltas(tmp_path / "proj", [("pkg", "1.0", "2.0")],
                              symbols=["pkg.gone", "pkg.keep", "pkg.Child.inherited"])
    assert set(seen) == {NEW_EXE}  # the old side never ran a probe
    assert report.quality == "partial"
    assert report.sides["old"].startswith("static:wheel pkg-1.0")
    assert any("read statically from wheel pkg-1.0-py3-none-any.whl" in n for n in report.notes)
    by_kind = {(f.kind, f.symbol, f.param) for f in report.facts}
    assert ("REMOVED", "pkg.gone", None) in by_kind        # present in the old source, gone now
    assert ("DEFAULT", "pkg.keep", "x") in by_kind          # a literal default that changed
    assert not any(k[2] == "y" for k in by_kind)            # a non-literal default is not compared
    assert not any("inherited" in k[1] for k in by_kind)    # unknown, so no REMOVED
    assert not any(k[0] == "REMOVED" and "Child" in k[1] for k in by_kind)
    assert all(f.basis == {"old": "static", "new": "probe"} for f in report.facts)
    assert report.model_dump()["facts"][0]["basis"] == {"old": "static", "new": "probe"}


def test_resolve_old_falls_back_to_the_static_side(tmp_path, monkeypatch):
    from pymolt.delta import envs

    monkeypatch.setattr(envs, "_config", lambda root: None)
    monkeypatch.setattr(envs, "_baseline_slot", lambda root: None)
    monkeypatch.setattr(envs, "_baseline_trace", lambda root: None)
    monkeypatch.setattr(envs, "throwaway_env", lambda *a, **k: (None, "no uv", None))
    _uv_cache(tmp_path, monkeypatch)
    old = envs.resolve_old(tmp_path / "proj", [envs.Want("pkg", "1.0")])
    assert old.is_static and not old.available and old.reason == "no uv"
    assert list(old.static) == ["pkg"] and "wheel pkg-1.0" in old.note
    # a release that is in no cache leaves the side unavailable, with the reason
    missing_one = envs.resolve_old(tmp_path / "proj", [envs.Want("other", "9.9")])
    assert not missing_one.is_static and not missing_one.available

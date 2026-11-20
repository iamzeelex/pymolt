"""compute_deltas end to end with fake interpreters and probes: quality and reasons,
the schema, symbol derivation, and the offline guarantee (no fetch without online=True)."""

from __future__ import annotations

import json

import httpx
import pytest

from pymolt.delta import compute_deltas
from pymolt.delta import engine as engine_mod
from pymolt.delta import fetch as fetch_mod
from pymolt.delta.engine import ProbeRun
from pymolt.delta.models import Interpreter

from ._fakes import missing, res, shape, step

POK = "POSITIONAL_OR_KEYWORD"
OLD_EXE, NEW_EXE = "/envs/old/bin/python", "/envs/new/bin/python"


def _results_old():
    return {
        "pkg.gone": res("pkg", step("gone", "pkg.gone", shape=shape(("x", POK, None)),
                                    owner_file="pkg/__init__.py"), module_file="pkg/__init__.py"),
        "pkg.keep": res("pkg", step("keep", "pkg.keep", shape=shape(("x", POK, "1")))),
    }


def _results_new():
    return {
        "pkg.gone": missing("pkg", "gone"),
        "pkg.keep": res("pkg", step("keep", "pkg.keep", shape=shape(("x", POK, "2")))),
    }


@pytest.fixture
def fake_world(monkeypatch):
    """Both interpreters available, pkg 1.0 → 2.0; records every probe request."""
    state = {"requests": [], "old": Interpreter(executable=OLD_EXE, source="trace-metadata"),
             "new": Interpreter(executable=NEW_EXE, source="target-env"),
             "versions": {"old": "1.0", "new": "2.0"},
             "results": {"old": _results_old(), "new": _results_new()}}
    monkeypatch.setattr(engine_mod.envs_mod, "resolve_old",
                        lambda root, wants, online=False: state["old"])
    monkeypatch.setattr(engine_mod.envs_mod, "resolve_new",
                        lambda root, wants, online=False: state["new"])

    def fake_probe(executable, root, request, *, use_cache=True, timeout=0):
        side = "old" if executable == OLD_EXE else "new"
        state["requests"].append((side, request))
        meta = {"python": "3.11.0" if side == "old" else "3.12.0",
                "dists": {"pkg": {"version": state["versions"][side], "roots": ["pkg"]}}}
        results = {p: r for p, r in state["results"][side].items() if p in request["paths"]}
        return ProbeRun(meta=meta, results=results)

    monkeypatch.setattr(engine_mod, "run_probe", fake_probe)
    return state


@pytest.fixture
def no_network(monkeypatch):
    """Any attempt to fetch fails the test."""
    calls = []

    def refuse(*a, **k):
        calls.append((a, k))
        raise AssertionError("network used without online=True")

    monkeypatch.setattr(fetch_mod, "intermediate_hint", refuse)
    monkeypatch.setattr(fetch_mod.Fetcher, "__init__", refuse)
    monkeypatch.setattr(httpx, "Client", refuse)
    monkeypatch.setattr(httpx, "get", refuse)
    return calls


def test_report_dump_is_the_documented_schema(tmp_path, fake_world, no_network):
    (report,) = compute_deltas(tmp_path, [("pkg", "1.0", "2.0")], symbols=["pkg.gone", "pkg.keep"])
    dump = report.model_dump()
    assert set(dump) == {"schema", "package", "from", "to", "old_python", "new_python",
                         "quality", "facts", "notes"}
    assert dump["schema"] == "pymolt.delta/1"
    assert (dump["from"], dump["to"], dump["old_python"], dump["new_python"]) == (
        "1.0", "2.0", "3.11.0", "3.12.0")
    assert dump["quality"] == "complete"
    assert set(dump["facts"][0]) == {"symbol", "aliases", "kind", "param", "old", "new",
                                     "deprecated_in_old", "successor", "evidence"}
    kinds = {(f["kind"], f["symbol"]) for f in dump["facts"]}
    assert kinds == {("REMOVED", "pkg.gone"), ("DEFAULT", "pkg.keep")}
    json.loads(report.model_dump_json())  # serializable as-is


def test_missing_interpreter_is_inconclusive_with_the_reason(tmp_path, fake_world, no_network):
    fake_world["new"] = Interpreter(source="none", reason="uv is not on PATH")
    (report,) = compute_deltas(tmp_path, [("pkg", "1.0", "2.0")], symbols=["pkg.gone"])
    assert report.quality == "inconclusive"
    assert report.facts == []  # never "no changes": no facts, and the reason says why
    assert any("uv is not on PATH" in n for n in report.notes)


def test_wrong_version_in_an_interpreter_is_inconclusive(tmp_path, fake_world, no_network):
    fake_world["versions"]["old"] = "1.1"
    (report,) = compute_deltas(tmp_path, [("pkg", "1.0", "2.0")], symbols=["pkg.gone"])
    assert report.quality == "inconclusive"
    assert any("has pkg 1.1, not 1.0" in n for n in report.notes)


def test_transient_probe_error_makes_the_report_partial(tmp_path, fake_world, no_network):
    fake_world["results"]["new"]["pkg.slow"] = {"error": "timeout", "transient": True,
                                                "steps": [], "module": None}
    fake_world["results"]["old"]["pkg.slow"] = res("pkg", step("slow", "pkg.slow"))
    (report,) = compute_deltas(tmp_path, [("pkg", "1.0", "2.0")],
                               symbols=["pkg.gone", "pkg.slow"])
    assert report.quality == "partial"
    assert any("could not resolve pkg.slow" in n for n in report.notes)


def test_offline_by_default_never_touches_the_fetch_layer(tmp_path, fake_world, no_network):
    (report,) = compute_deltas(tmp_path, [("pkg", "1.0", "2.0")], symbols=["pkg.gone"])
    (removed,) = [f for f in report.facts if f.kind == "REMOVED"]
    assert removed.successor is None
    assert no_network == []


def test_online_fetches_versions_only_and_caches_the_hint(tmp_path, fake_world, monkeypatch):
    seen = []

    class _Fetcher:
        def __init__(self, cache_dir, *, use_cache=True):
            self.bytes = self.requests = 0

        def close(self):
            pass

    def fake_hint(fetcher, dist, member, chain, old_v, new_v):
        seen.append((dist, member, chain, old_v, new_v))
        return {"hint": "pkg.successor", "last_version_with_symbol": "1.5",
                "messages": ["gone is deprecated; use pkg.successor instead"]}

    monkeypatch.setattr(fetch_mod, "Fetcher", _Fetcher)
    monkeypatch.setattr(fetch_mod, "intermediate_hint", fake_hint)
    (report,) = compute_deltas(tmp_path, [("pkg", "1.0", "2.0")], symbols=["pkg.gone"],
                               online=True)
    removed = next(f for f in report.facts if f.kind == "REMOVED")
    assert removed.successor == "pkg.successor"
    assert "successor:intermediate:1.5 (fetched)" in removed.evidence
    # only the package, its versions and a module path inside its own wheel
    assert seen == [("pkg", "pkg/__init__.py", ["gone"], "1.0", "2.0")]
    assert (tmp_path / ".pymolt" / "cache" / "delta" / "hints.json").is_file()

    # offline again: the cached hint is reused, nothing is fetched
    monkeypatch.setattr(fetch_mod, "intermediate_hint",
                        lambda *a, **k: pytest.fail("fetched while offline"))
    monkeypatch.setattr(fetch_mod, "Fetcher",
                        lambda *a, **k: pytest.fail("fetcher built while offline"))
    (again,) = compute_deltas(tmp_path, [("pkg", "1.0", "2.0")], symbols=["pkg.gone"])
    removed = next(f for f in again.facts if f.kind == "REMOVED")
    assert removed.successor == "pkg.successor"
    assert "successor:intermediate:1.5 (cached)" in removed.evidence


def test_symbols_are_derived_from_the_contact_map(tmp_path, fake_world, no_network):
    (tmp_path / "app.py").write_text("from pkg import keep\n\nkeep(1)\n", encoding="utf-8")
    (report,) = compute_deltas(tmp_path, [("pkg", "1.0", "2.0")])
    (_, request), *_ = fake_world["requests"]
    assert "pkg.keep" in request["paths"]
    assert [f.kind for f in report.facts] == ["DEFAULT"]


def test_no_migrations_no_work(tmp_path, fake_world, no_network):
    assert compute_deltas(tmp_path, []) == []
    assert fake_world["requests"] == []


def test_probe_contexts_come_from_knowledge(tmp_path, fake_world, no_network):
    compute_deltas(tmp_path, [("Flask", "2.0.3", "3.0.0")], symbols=["flask.request"])
    (_, request), *_ = fake_world["requests"]
    assert len(request["contexts"]) == 1 and "test_request_context" in request["contexts"][0]
    compute_deltas(tmp_path, [("pkg", "1.0", "2.0")], symbols=["pkg.gone"])
    assert fake_world["requests"][-1][1]["contexts"] == []


def test_throwaway_env_build_is_offline_unless_online(tmp_path, monkeypatch):
    """Without --online, uv may only use its cache: it is never asked to download."""
    from pymolt.delta import envs

    calls = []

    class _Done:
        returncode = 1
        stdout = ""
        stderr = "No solution found when resolving dependencies (offline)"

    monkeypatch.setattr(envs.shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(envs.subprocess, "run", lambda cmd, **kw: (calls.append(cmd), _Done())[1])

    exe, reason, _ = envs.throwaway_env(tmp_path, "3.12", ["pandas==2.2.3"])
    assert exe is None
    assert all("--offline" in cmd for cmd in calls)
    assert "--online" in reason

    calls.clear()
    envs.throwaway_env(tmp_path, "3.12", ["pandas==2.2.4"], online=True)
    assert calls and not any("--offline" in cmd for cmd in calls)

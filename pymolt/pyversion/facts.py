"""``python_delta``: Python itself as a pseudo-migration, as a ``pymolt.delta/1`` report.

``subject="python"``, ``package="python"``. For every stdlib spelling the project touches
(the contact map's ``stdlib_sites``):

- **target side**: probed on the target interpreter through the delta engine's probe (a bare
  interpreter needs no dependencies), so absence is observed, and a third-party shim
  (``distutils`` through setuptools) shows as present with its ``origin``;
- **base side**: from shipped data (``core.stdlib.present_in``) unless a base interpreter or
  runner is given, so the report is ``partial`` and says the base was not probed.

Fact kinds: absent on the target and present on the base is ``MOVED`` (with ``successor``) when
the curated successor is the same object and the module still exists, else ``REMOVED`` (with the
curated hint in ``evidence``); present but deprecated is ``DEPRECATED``; absent on both sides
is an ``UNCHANGED`` fact whose evidence says so (pre-existing). Never fabricates: with no target
interpreter the facts rest on data alone and the quality says why.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pymolt.delta.models import DeltaFact, DeltaReport
from pymolt.pyversion.guards import parse_version

PYTHON_PACKAGE = "python"
_ORIGIN_TIMEOUT = 30.0


def _stdlib():
    """``pymolt.core.stdlib`` (the lifetimes data), or None when it is unavailable."""
    try:
        from pymolt.core import stdlib
    except ImportError:
        return None
    return stdlib


def _minor(value: str | None) -> str | None:
    parsed = parse_version(value)
    return f"{parsed[0]}.{parsed[1]}" if parsed else None


def next_minor(version: str) -> str | None:
    parsed = parse_version(version)
    return f"{parsed[0]}.{parsed[1] + 1}" if parsed else None


def _ver_le(a: str | None, b: str | None) -> bool:
    pa, pb = parse_version(a), parse_version(b)
    return pa is not None and pb is not None and pa <= pb


def _call(module, name: str, *args: Any) -> Any:
    if module is None:
        return None
    try:
        return getattr(module, name)(*args)
    except Exception:  # noqa: BLE001 - data lookups degrade to "unknown"
        return None


def find_target_interpreter(project_dir: str | Path, target_python: str | None) -> str | None:
    """The target interpreter: the configured target env's python, else the bare target
    Python (``uv python find``, then the project's / system interpreters)."""
    root = Path(project_dir)
    minor = _minor(target_python)
    try:
        from pymolt.delta.envs import python_in
        from pymolt.ingestion.config import EnvConfig

        config = EnvConfig.load(root / ".pymolt" / "env_config.json")
        if config is not None and config.target_env_path:
            env_dir = Path(config.target_env_path).expanduser()
            exe = python_in(env_dir if env_dir.is_absolute() else root / env_dir)
            if exe is not None:
                return str(exe)
    except Exception:  # noqa: BLE001 - fall through to the bare interpreter
        pass
    if minor is None:
        return None
    uv = shutil.which("uv")
    if uv and parse_version(minor) >= (3, 7):
        try:
            proc = subprocess.run([uv, "python", "find", "--no-project", minor], cwd="/",
                                  capture_output=True, text=True, timeout=30)
            found = proc.stdout.strip()
            if proc.returncode == 0 and found and Path(found).is_file():
                return found
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        from pymolt.ingestion.fallback_compiler import find_python_interpreter

        found_path = find_python_interpreter(minor, root)
        return str(found_path) if found_path else None
    except Exception:  # noqa: BLE001
        return None


def python_delta(
    project_dir: str | Path,
    sites: Iterable[Any],
    base_python: str | None,
    target_python: str | None,
    *,
    target_executable: str | Path | None = None,
    base_executable: str | Path | None = None,
    base_runner: Any = None,
    runtime_symbols: Iterable[str] = (),
) -> DeltaReport:
    """The python pseudo-migration's delta report for the stdlib spellings in ``sites``.

    ``runtime_symbols``: stdlib symbols the baseline capture observed at runtime
    (``asyncio.tasks.sleep``, ``unittest.case.TestCase.assertEquals``). They are checked like
    a site's spelling (presence on both sides, from the probes), their presence on the base
    rests on the observation, and when both sides are probed a changed signature becomes a
    SIGNATURE fact (a removed or renamed parameter, a new required one)."""
    root = Path(project_dir).resolve()
    data = _stdlib()
    base = _minor(base_python) or base_python
    target = _minor(target_python) or target_python
    exe = str(target_executable) if target_executable else find_target_interpreter(
        root, target_python)
    report = DeltaReport(package=PYTHON_PACKAGE, from_version=base or "unknown",
                         to_version=target or "unknown", old_python=base_python,
                         new_python=target_python, quality="partial", subject="python")
    stamp = _call(data, "data_stamp") or "knowledge:stdlib_lifetimes"
    report.sides = {"old": stamp, "new": stamp}

    static = {s for s in (getattr(site, "spelling", None) for site in sites or ()) if s}
    observed = {s for s in runtime_symbols or () if s} - static
    spellings = sorted(static | observed)
    if not spellings:
        report.quality = "complete"
        report.sides = {}
        report.notes.append("no stdlib spellings in the project's sites: nothing to check")
        return report

    candidates: dict[str, list[str]] = {sp: _successor_candidates(data, sp) for sp in spellings}
    paths = sorted({*spellings, *(c for cs in candidates.values() for c in cs)})
    roots = sorted({p.split(".")[0] for p in paths})
    new_probe = base_probe = None
    if exe:
        new_probe = _probe(exe, root, paths, roots)
        if new_probe.failure:
            report.notes.append(f"target probe failed ({new_probe.failure}); "
                                "facts rest on data alone")
        else:
            report.sides["new"] = f"probe:{exe}"
            probed_python = (new_probe.meta or {}).get("python")
            if probed_python:
                report.new_python = probed_python
                target = _minor(probed_python) or target
    else:
        report.notes.append(f"no interpreter found for Python {target_python}: "
                            "facts rest on data alone, absence is not observed")
    if base_executable or base_runner:
        base_probe = _probe(str(base_executable or ""), root, paths, roots, runner=base_runner)
        if base_probe.failure:
            report.notes.append(f"base probe failed ({base_probe.failure}); "
                                "base presence comes from data")
            base_probe = None
        elif _minor((base_probe.meta or {}).get("python") or "") not in (None, _minor(base)):
            report.notes.append(
                f"base probe ran Python {(base_probe.meta or {}).get('python')}, not {base}; "
                "base presence comes from data")
            base_probe = None
        else:
            where = getattr(base_runner, "name", None)
            report.sides["old"] = (f"probe:container {where}" if where
                                   else f"probe:{base_executable}")
            if (base_probe.meta or {}).get("python"):
                report.old_python = base_probe.meta["python"]
    target_probed = new_probe is not None and not new_probe.failure
    need_origin: list[str] = []
    states: dict[str, dict[str, Any]] = {}
    for sp in spellings:
        state = _target_state(data, sp, target, new_probe if target_probed else None, report)
        states[sp] = state
        if state["state"] == "present" and _maybe_shimmed(data, sp, target):
            need_origin.append(sp.split(".")[0])
    origins = _origins(exe, root, sorted(set(need_origin))) if (target_probed and need_origin) \
        else {}

    for sp in spellings:
        fact = _fact_for(data, sp, states[sp], base, target, base_probe, new_probe if
                         target_probed else None, candidates[sp], origins, report,
                         observed=sp in observed)
        if fact is not None:
            report.facts.append(fact)
    if target_probed and base_probe is not None:
        have = {f.symbol for f in report.facts if f.kind != "UNCHANGED"}
        for sp in spellings:
            if sp not in have and states[sp]["state"] == "present":
                report.facts.extend(_signature_facts(sp, base_probe, new_probe))

    problems = []
    if not target_probed:
        problems.append("the target interpreter was not probed")
    if base_probe is None:
        problems.append(f"the base ({base}) was not probed: base presence comes from "
                        f"{report.sides['old']}")
    report.quality = "complete" if not problems else "partial"
    if problems:
        report.notes.append("; ".join(problems))
    return report


def _final_shape(probe, spelling: str) -> list | None:
    """The parameter shape of what a probe resolved ``spelling`` to (None: not a callable)."""
    result = probe.results.get(spelling) or {}
    steps = result.get("steps") or []
    if not result.get("exists") or not steps:
        return None
    return steps[-1].get("shape")


def _signature_facts(spelling: str, base_probe, new_probe) -> list[DeltaFact]:
    """SIGNATURE facts for a stdlib callable present on both probed sides, reusing the delta
    engine's parameter comparison. Default changes are not reported here."""
    from pymolt.delta.engine import param_changes

    old, new = _final_shape(base_probe, spelling), _final_shape(new_probe, spelling)
    if not old or not new:
        return []
    return [
        DeltaFact(symbol=spelling, kind="SIGNATURE", param=ch.param, old=ch.old, new=ch.new,
                  evidence=["signature", ch.note, "signature probed on both interpreters"],
                  basis={"old": "probe", "new": "probe"})
        for ch in param_changes(old, new) if ch.kind == "SIGNATURE" and ch.param
    ]


def _probe(exe: str, root: Path, paths: list[str], roots: list[str], *, runner: Any = None):
    from pymolt.delta.engine import run_probe

    # any_root: a stdlib class is often owned by a private C module (asyncio.Task by
    # _asyncio); without it the probe would stop at Task and never test current_task.
    request = {"paths": paths, "roots": roots, "any_root": True}
    if runner is not None:
        return run_probe(exe, root, request, runner=runner)
    return run_probe(exe, root, request)


def _successor_candidates(data, spelling: str) -> list[str]:
    out: list[str] = []
    leaf = spelling.rsplit(".", 1)[-1]
    for change in _call(data, "changes_for", spelling) or ():
        succ = getattr(change, "successor", None)
        if not succ:
            continue
        cands = [succ.replace("{name}", leaf)]
        if "{name}" not in succ and _call(data, "module_lifetime", succ) is not None:
            cands.append(f"{succ}.{leaf}")        # a module successor: the same name in it
        for cand in cands:
            if cand != spelling and cand not in out:
                out.append(cand)
    return out


def _best_change(data, spelling: str):
    changes = list(_call(data, "changes_for", spelling) or ())
    exact = [c for c in changes if spelling in (getattr(c, "symbols", None) or ())]
    pool = exact or changes
    return pool[0] if pool else None


def _covers(lifetime, version: str | None) -> bool | None:
    """Does a ``(first, last-or-None)`` lifetime include ``version``?"""
    if not lifetime or parse_version(version) is None:
        return None
    first, last = lifetime
    return _ver_le(first, version) and (last is None or _ver_le(version, last))


def _base_presence(data, spelling: str, module: str, base: str | None, change) -> bool | None:
    """Was ``spelling`` on the base? The name data, else its module's lifetime (a submodule
    spelling), else a curated entry that was deprecated or removed after the base."""
    if not base:
        return None
    present = _call(data, "present_in", spelling, base)
    if present is not None:
        return present
    if spelling == module:
        covered = _covers(_call(data, "module_lifetime", spelling), base)
        if covered is not None:
            return covered
    if change is not None and getattr(change, "removed_in", None):
        symbols = getattr(change, "symbols", None) or ()
        if any(spelling == sym or spelling.startswith(sym + ".") for sym in symbols):
            if parse_version(base) is not None and not _ver_le(change.removed_in, base):
                return True
    return None


def _module_prefix(data, spelling: str) -> str:
    """The longest proper dotted prefix of ``spelling`` that is a module in the data (the
    spelling itself when it has one part)."""
    parts = spelling.split(".")
    for i in range(max(len(parts) - 1, 1), 0, -1):
        prefix = ".".join(parts[:i])
        if _call(data, "module_lifetime", prefix) is not None:
            return prefix
    return parts[0]


def _target_state(data, spelling: str, target: str | None, probe, report: DeltaReport
                  ) -> dict[str, Any]:
    """``{"state": present|absent|unknown, "how": probe|knowledge, "module_gone": bool, ...}``"""
    module = _module_prefix(data, spelling)
    if probe is not None:
        result = probe.results.get(spelling)
        if result is None:
            return {"state": "unknown", "how": "probe", "module_gone": False}
        raw_import = result.get("import_error") or result.get("import_errors")
        if raw_import:
            _import_error(report, spelling, module, str(raw_import), result)
            return {"state": "unknown", "how": "probe", "module_gone": False}
        if result.get("exists"):
            return {"state": "present", "how": "probe", "module_gone": False, "result": result}
        error = str(result.get("error") or "")
        if result.get("transient") or error.startswith("timeout"):
            report.notes.append(f"probe of {spelling} did not finish: {error}")
            return {"state": "unknown", "how": "probe", "module_gone": False}
        steps = result.get("steps") or []
        step_error = str(steps[-1].get("error") or "") if steps else ""
        module_gone = result.get("module") is None
        if module_gone and error and not error.startswith("no importable prefix"):
            _import_error(report, spelling, module, error, result)
            return {"state": "unknown", "how": "probe", "module_gone": False}
        if not module_gone and step_error and not step_error.startswith("AttributeError"):
            _import_error(report, spelling, module, step_error, result)
            return {"state": "unknown", "how": "probe", "module_gone": False}
        if module_gone and _call(data, "present_in", module, target) is True:
            _import_error(report, spelling, module, error or "not importable on the target",
                          result)
            return {"state": "unknown", "how": "probe", "module_gone": False}
        return {"state": "absent", "how": "probe", "module_gone": module_gone,
                "detail": step_error or error}
    present = _call(data, "present_in", spelling, target) if target else None
    if present is None:
        return {"state": "unknown", "how": "knowledge", "module_gone": False}
    gone = _call(data, "present_in", module, target) is False
    return {"state": "present" if present else "absent", "how": "knowledge",
            "module_gone": gone and not present}


def _import_error(report: DeltaReport, spelling: str, module: str, error: str,
                  result: dict) -> None:
    report.import_errors.append({"module": module, "side": "new", "error": error[:300],
                                 "origin": None, "cause": f"while resolving {spelling}"})
    report.notes.append(f"{spelling}: not resolved on the target ({error[:120]}); "
                        "not reported as removed")


def _maybe_shimmed(data, spelling: str, target: str | None) -> bool:
    """Present on the target although the data (or a curated entry) says it left the stdlib."""
    if _call(data, "present_in", spelling, target) is False:
        return True
    change = _best_change(data, spelling)
    return bool(change is not None and getattr(change, "shimmed_by", None))


def _fact_for(data, spelling: str, state: dict, base: str | None, target: str | None,
              base_probe, new_probe, candidates: list[str], origins: dict[str, str | None],
              report: DeltaReport, observed: bool = False) -> DeltaFact | None:
    change = _best_change(data, spelling)
    module = _module_prefix(data, spelling)
    old_basis = "knowledge"
    if base_probe is not None and spelling in base_probe.results:
        base_present: bool | None = bool(base_probe.results[spelling].get("exists"))
        old_basis = "probe"
    else:
        base_present = _base_presence(data, spelling, module, base, change)
        if base_present is None and observed:
            base_present, old_basis = True, "baseline-trace"  # the baseline ran it
        elif base_present is None:
            old_basis = "import-statement"
    new_basis = state["how"] if state["state"] != "unknown" else None
    evidence: list[str] = []
    if state["state"] == "unknown":
        return None
    removed_in = getattr(change, "removed_in", None) if change else None
    if removed_in is None:
        lifetime = _call(data, "name_lifetime", spelling) or _call(data, "module_lifetime",
                                                                  module)
        if lifetime and lifetime[1]:
            removed_in = next_minor(lifetime[1])
    deprecated_in = getattr(change, "deprecated_in", None) if change else None
    warns_since = getattr(change, "warns_since", None) if change else None
    hint = getattr(change, "hint", None) if change else None
    fix = getattr(change, "fix", None) if change else None
    common: dict[str, Any] = {
        "basis": {"old": old_basis, "new": new_basis or "knowledge"},
        "removed_in": removed_in, "deprecated_in": deprecated_in, "warns_since": warns_since,
    }
    if state["state"] == "absent":
        if state["how"] == "probe":
            evidence.append(f"probe: absent on Python {target}"
                            + (f" ({state['detail'][:160]})" if state.get("detail") else ""))
        else:
            evidence.append(f"knowledge: absent on Python {target}")
        if base_present is False:
            evidence.append(f"absent on the base ({base}) too: pre-existing, not caused by "
                            "the migration")
            return DeltaFact(symbol=spelling, kind="UNCHANGED", evidence=evidence, **common)
        evidence.append(
            f"{old_basis}: present on Python {base}" if base_present
            else "the project imports it; no data on the base")
        if change is not None:
            evidence.append(f"change: {getattr(change, 'id', '?')}")
        if hint:
            evidence.append(f"hint: {hint}")
        if fix:
            evidence.append(f"fix: {fix}")
        successor = _valid_successor(data, change, candidates, new_probe, target)
        if (successor and getattr(change, "same_object", False)
                and not state.get("module_gone")):
            return DeltaFact(symbol=spelling, kind="MOVED", old=spelling, new=successor,
                             successor=successor, evidence=evidence, **common)
        if successor:
            evidence.append(f"successor: {successor} (not the same object)"
                            if not state.get("module_gone") or not getattr(
                                change, "same_object", False)
                            else f"successor: {successor} (the module is gone)")
        return DeltaFact(symbol=spelling, kind="REMOVED", evidence=evidence, **common)

    # present on the target
    origin = _origin_for(spelling, origins)
    shim = origin is not None and origin != "stdlib"
    in_window = change is not None and bool(deprecated_in) and _ver_le(deprecated_in, target)
    if shim:
        evidence.append(f"present on Python {target} only through {origin}"
                        + (f" (removed from the stdlib in {removed_in})" if removed_in else ""))
        if hint:
            evidence.append(f"hint: {hint}")
        if fix:
            evidence.append(f"fix: {fix}")
        return DeltaFact(symbol=spelling, kind="DEPRECATED", deprecated_in_old=False,
                         evidence=["deprecated-in-new: shimmed by a third-party package",
                                   *evidence], origin=origin, **common)
    if in_window and base_present is not False:
        warned = bool(warns_since and _ver_le(warns_since, target))
        evidence.append(f"deprecated since {deprecated_in}"
                        + (f", removed in {removed_in}" if removed_in else "")
                        + ("; warns at runtime" if warned else ""))
        if hint:
            evidence.append(f"hint: {hint}")
        if fix:
            evidence.append(f"fix: {fix}")
        in_base = bool(base and _ver_le(deprecated_in, base))
        if not in_base:
            evidence.insert(0, "deprecated-in-new: still present")
        return DeltaFact(symbol=spelling, kind="DEPRECATED", deprecated_in_old=in_base,
                         evidence=evidence, origin=origin or "stdlib", **common)
    return None


def _valid_successor(data, change, candidates: list[str], probe, target: str | None) -> str | None:
    """The first curated successor candidate that exists on the target (probed, else data)."""
    if change is None or not getattr(change, "successor", None):
        return None
    for cand in candidates:
        if probe is not None and cand in probe.results:
            if probe.results[cand].get("exists"):
                return cand
            continue
        found = _call(data, "present_in", cand, target)
        if found is True or (found is None and candidates and cand == candidates[0]
                             and _ver_le(getattr(change, "successor_since", None), target)):
            return cand       # curated: the successor exists since ``successor_since``
    return None


def _origin_for(spelling: str, origins: dict[str, str | None]) -> str | None:
    return origins.get(spelling.split(".")[0])


_ORIGIN_SCRIPT = r'''
import importlib.util, json, sys
out = {}
for name in json.loads(sys.stdin.read()):
    try:
        spec = importlib.util.find_spec(name)
    except BaseException:
        spec = None
    locations = list(getattr(spec, "submodule_search_locations", None) or [])
    out[name] = (getattr(spec, "origin", None) if spec else None) or (locations[0] if locations
                                                                      else None)
print("@@origins@@ " + json.dumps(out))
'''


def _origins(exe: str | None, root: Path, modules: list[str]) -> dict[str, str | None]:
    """``{module: "stdlib" | "site-packages:<dist dir>" | None}`` on the target interpreter."""
    if not exe or not modules:
        return {}
    try:
        proc = subprocess.run([exe, "-B", "-c", _ORIGIN_SCRIPT], input=json.dumps(modules),
                              cwd=str(root), capture_output=True, text=True,
                              timeout=_ORIGIN_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return {}
    for line in proc.stdout.splitlines():
        if line.startswith("@@origins@@ "):
            try:
                raw = json.loads(line[len("@@origins@@ "):])
            except ValueError:
                return {}
            return {name: classify_origin(path) for name, path in raw.items()}
    return {}


def classify_origin(path: str | None) -> str | None:
    if not path:
        return None
    parts = Path(path).parts
    for marker in ("site-packages", "dist-packages"):
        if marker in parts:
            rest = parts[parts.index(marker) + 1:]
            if rest:
                dist = rest[0].removesuffix(".py")
                return f"site-packages:{dist}"
    return "stdlib"

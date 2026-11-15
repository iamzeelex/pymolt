# ruff: noqa: E501
"""Generate ``pymolt/knowledge/stdlib_lifetimes.json`` (repo-only; see README.md).

    uv run --python 3.12 python bench/stdlib_sweep/sweep.py [--date YYYY-MM-DD] [--only 3.12 3.13]

Module lifetimes come from typeshed ``stdlib/VERSIONS`` (a vendored snapshot) plus the
Python 2 table; name lifetimes come from running ``probe.py`` on real interpreters.
"""

from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = ROOT / "pymolt" / "knowledge" / "stdlib_lifetimes.json"
PROBE = HERE / "probe.py"
VERSIONS_FILE = HERE / "typeshed_VERSIONS.txt"

sys.path.insert(0, str(ROOT))

#: swept interpreters. 3.6/3.7 come from the official images; 3.8-3.14 from uv. Everything
#: runs on linux/amd64 so that platform-specific names (os.getxattr, ...) do not read as
#: removals between the two groups; ``--native`` runs uv on the host instead (skewed on macOS).
DOCKER = {"3.6": "python:3.6", "3.7": "python:3.7"}
UV = ["3.8", "3.9", "3.10", "3.11", "3.12", "3.13", "3.14"]
ALL = list(DOCKER) + UV
UV_IMAGE = "ghcr.io/astral-sh/uv:bookworm-slim"
UV_VOLUME = "pymolt_stdlib_sweep_uv"  # caches the downloaded interpreters between runs
NATIVE = False

#: never imported: side effects (browser, prints, GUIs), test suites, generated codec tables.
SKIP = {
    "antigravity", "this", "idlelib", "turtledemo", "test", "tests", "__main__", "__phello__",
    "__hello__", "encodings", "fixes", "pydoc_data", "_bundled",
}
#: top-level stdlib packages that typeshed does not list (it has no stubs for them).
EXTRA_TOPS = {"idlelib": ["3.0", None], "turtledemo": ["3.0", None], "test": ["3.0", None]}

#: classes swept one level deep (what the curated table references, plus the commonly used).
CLASSES = [
    "unittest.TestCase", "logging.Logger", "logging.LoggerAdapter", "logging.Handler", "ssl.SSLContext",
    "ssl.SSLSocket", "configparser.RawConfigParser", "configparser.ConfigParser", "threading.Thread",
    "threading.Event", "threading.Condition", "array.array", "xml.etree.ElementTree.Element",
    "xml.etree.ElementTree.ElementTree", "asyncio.Task", "asyncio.AbstractEventLoop", "pathlib.Path",
    "pathlib.PurePath", "html.parser.HTMLParser", "datetime.datetime", "datetime.date",
    "http.client.HTTPConnection", "http.client.HTTPSConnection", "http.server.BaseHTTPRequestHandler",
    "socket.socket", "subprocess.Popen", "zipfile.ZipFile", "tarfile.TarFile", "email.message.Message",
    "argparse.ArgumentParser", "io.IOBase", "urllib.request.OpenerDirector", "urllib.request.Request",
    "collections.OrderedDict", "collections.Counter", "collections.deque", "sqlite3.Connection",
    "sqlite3.Cursor", "decimal.Decimal", "fractions.Fraction", "random.Random", "string.Template",
    "types.ModuleType", "importlib.abc.Loader", "importlib.abc.MetaPathFinder", "importlib.abc.PathEntryFinder",
]


def parse_versions(text: str) -> dict[str, list]:
    out = {}
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        name, _, span = line.partition(":")
        lo, _, hi = span.strip().partition("-")
        out[name.strip()] = [lo, hi or None]
    return out


def _vkey(version: str) -> tuple[int, int]:
    major, minor = version.split(".")[:2]
    return int(major), int(minor)


def _cmd(version: str) -> list[str]:
    script = PROBE.read_text(encoding="utf-8")
    if version in DOCKER:
        return ["docker", "run", "--rm", "-i", "--platform", "linux/amd64", DOCKER[version],
                "python", "-I", "-B", "-c", script]
    if NATIVE:
        return ["uv", "run", "--no-project", "--python", version, "python", "-I", "-B", "-c", script]
    return ["docker", "run", "--rm", "-i", "--platform", "linux/amd64", "-v", f"{UV_VOLUME}:/root/.local/share/uv",
            UV_IMAGE, "uv", "run", "--no-project", "--python", version, "python", "-I", "-B", "-c", script]


def run_probe(version: str, mode: str, request: dict, timeout: int = 1800) -> dict:
    proc = subprocess.run(
        [*_cmd(version), mode], input=json.dumps(request), capture_output=True, text=True,
        timeout=timeout, cwd=str(HERE),
    )
    if proc.returncode != 0:
        raise SystemExit(f"[{version}] {mode} failed ({proc.returncode}):\n{proc.stderr[-2000:]}")
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise SystemExit(f"[{version}] {mode}: unparseable output: {proc.stdout[-500:]!r}") from None


def discover_tops(typeshed: dict[str, list]) -> list[str]:
    tops = {m.split(".")[0] for m in typeshed}
    for version in ("3.10", "3.14"):  # sys.stdlib_module_names exists from 3.10
        proc = subprocess.run(
            ["uv", "run", "--no-project", "--python", version, "python", "-I", "-c",
             "import sys, json; print(json.dumps(sorted(sys.stdlib_module_names)))"],
            capture_output=True, text=True, cwd=str(HERE),
        )
        if proc.returncode == 0:
            tops |= set(json.loads(proc.stdout))
    # modules that died before 3.10 (formatter, parser, ...) appear in neither typeshed nor
    # sys.stdlib_module_names: read the oldest interpreter's stdlib directory instead
    tops |= set(run_probe(ALL[0], "tops", {})["tops"])
    tops |= set(EXTRA_TOPS)
    return sorted(t for t in tops if t not in SKIP and (not t.startswith("_") or t in ("_thread",)))


def sweep(versions: list[str], date: str) -> dict:
    from pymolt.core.stdlib import PY2_STDLIB

    typeshed = parse_versions(VERSIONS_FILE.read_text(encoding="utf-8"))
    tops = discover_tops(typeshed)
    request = {"tops": tops, "classes": CLASSES, "skip": sorted(SKIP)}

    def pool_map(fn, items):
        with ThreadPoolExecutor(max_workers=4) as pool:
            return list(pool.map(fn, items))

    print(f"pass 1: collect on {versions} ({len(tops)} top-level modules)", flush=True)
    collected = dict(zip(versions, pool_map(lambda v: run_probe(v, "collect", request), versions), strict=True))
    exact = {v: collected[v]["version"] for v in versions}
    print("interpreters:", exact, flush=True)

    # (module, relname) pairs seen anywhere: module attributes and one class level
    seen: set[tuple[str, str]] = set()
    module_state: dict[str, dict[str, str]] = {}
    for v, data in collected.items():
        for mod, info in data["modules"].items():
            module_state.setdefault(mod, {})[v] = info["status"]
            for name in info.get("names", []):
                seen.add((mod, name))
        for dotted, attrs in data["classes"].items():
            mod, _, cls = dotted.rpartition(".")
            for attr in attrs:
                seen.add((mod, f"{cls}.{attr}"))
    # class attributes belong to the module their class lives in; modules that never imported are skipped
    dotted_names = sorted({f"{m}.{n}" for m, n in seen})
    owner = {f"{m}.{n}": m for m, n in seen}
    print(f"pass 2: hasattr for {len(dotted_names)} names on {versions}", flush=True)
    checked = dict(zip(
        versions,
        pool_map(lambda v: run_probe(v, "check", {"names": dotted_names})["present"], versions),
        strict=True,
    ))

    floor, newest = versions[0], versions[-1]
    names: dict[str, dict] = {}
    stable: dict[str, list[str]] = {}
    for dotted in dotted_names:
        states = {v: checked[v][dotted] for v in versions}
        yes = [v for v in versions if states[v] == "yes"]
        no = [v for v in versions if states[v] == "no"]
        if not yes:
            continue
        mod = owner[dotted]
        if not no:
            stable.setdefault(mod, []).append(dotted[len(mod) + 1:])
            continue
        lo, hi = yes[0], yes[-1]
        if lo == floor:
            start = _module_start(mod, typeshed)
            lo = start if start and _vkey(start) < _vkey(floor) else lo
        entry = {"present": [lo, None if hi == newest else hi]}
        gaps = [v for v in no if _vkey(yes[0]) < _vkey(v) < _vkey(hi)]
        if gaps:
            entry["gaps"] = gaps
        names[dotted] = entry

    modules = {m: {"present": span} for m, span in typeshed.items()}
    for m, span in EXTRA_TOPS.items():
        modules.setdefault(m, {"present": span})
    for m, span in _swept_modules(typeshed, module_state, versions).items():
        modules.setdefault(m, span)
    for m in PY2_STDLIB:
        modules.setdefault(m, {"present": ["2.7", "2.7"]})
    disagreements = _disagreements(typeshed, module_state, versions)
    for line in disagreements:
        print("typeshed vs sweep:", line, flush=True)

    return {
        "schema": "pymolt.knowledge/1",
        "source": "typeshed stdlib/VERSIONS (vendored, bench/stdlib_sweep/typeshed_VERSIONS.txt) for modules; "
                  "hasattr sweep of real interpreters (bench/stdlib_sweep) for names; Python 2 table "
                  "from pymolt.core.stdlib",
        "date": date,
        "pythons": [exact[v] for v in versions],
        "modules": dict(sorted(modules.items())),
        "names": dict(sorted(names.items())),
        "submodules": sorted(
            m for m, st in module_state.items() if m not in modules and "ok" in st.values()
        ),
        "stable": {m: sorted(set(v)) for m, v in sorted(stable.items())},
    }


def _swept_modules(typeshed, module_state, versions) -> dict[str, dict]:
    """Lifetimes for modules typeshed does not list (it drops what died before its oldest
    supported Python: ``formatter``, ``parser``, ...) or lists only through a parent whose
    lifetime differs from the submodule's real one. First version = oldest swept when seen there."""
    floor, newest = versions[0], versions[-1]
    out = {}
    for mod, state in sorted(module_state.items()):
        ok = [v for v in versions if state.get(v) == "ok"]
        if not ok:
            continue
        span = [ok[0], None if ok[-1] == newest else ok[-1]]
        parent = _typeshed_span(mod, typeshed)
        if parent is not None:
            same = all(
                (state.get(v) == "ok") == (_vkey(v) >= _vkey(parent[0]) and (parent[1] is None or _vkey(v) <= _vkey(parent[1])))
                for v in versions if state.get(v) in ("ok", "absent")
            )
            if same:
                continue
        entry = {"present": span}
        if ok[0] == floor:
            entry["since_unknown"] = True
        out[mod] = entry
    return out


def _typeshed_span(module: str, typeshed: dict[str, list]):
    parts = module.split(".")
    while parts:
        span = typeshed.get(".".join(parts))
        if span:
            return span
        parts.pop()
    return None


def _module_start(module: str, typeshed: dict[str, list]) -> str | None:
    parts = module.split(".")
    while parts:
        span = typeshed.get(".".join(parts))
        if span:
            return span[0]
        parts.pop()
    return None


def _disagreements(typeshed, module_state, versions) -> list[str]:
    out = []
    for mod, (lo, hi) in sorted(typeshed.items()):
        state = module_state.get(mod)
        if not state:
            continue
        for v in versions:
            expect = _vkey(v) >= _vkey(lo) and (hi is None or _vkey(v) <= _vkey(hi))
            got = state.get(v)
            if got == "ok" and not expect:
                out.append(f"{mod} importable on {v} but typeshed says {lo}-{hi or ''}")
            elif got == "absent" and expect:
                out.append(f"{mod} absent on {v} but typeshed says {lo}-{hi or ''}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=datetime.date.today().isoformat())
    parser.add_argument("--only", nargs="*", help="sweep just these versions (dev; output is NOT written unless --write)")
    parser.add_argument("--write", action="store_true", help="write even with --only")
    parser.add_argument("--native", action="store_true", help="run 3.8+ through the host's uv, not a linux container")
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()
    global NATIVE
    NATIVE = args.native
    versions = sorted(args.only or ALL, key=_vkey)
    data = sweep(versions, args.date)
    if args.only and not args.write:
        print(json.dumps({k: len(v) if isinstance(v, dict) else v for k, v in data.items()}, indent=1))
        return
    Path(args.out).write_text(json.dumps(data, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    print(f"wrote {args.out} ({Path(args.out).stat().st_size} bytes)")


if __name__ == "__main__":
    main()

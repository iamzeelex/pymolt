"""The compile check: the target interpreter ``compile()``s every project ``.py`` file.

A small stdlib script runs on the target interpreter as a subprocess (nothing is imported or
executed: ``compile`` only). It reports each ``SyntaxError`` and ``SyntaxWarning`` as
``{file, line, kind, message, python, base}`` (``pymolt.compile/1``). ``base`` says whether the
file fails the same way on the base: True (also fails there: pre-existing, D13), False
(compiles there), None (not observed). Without a base interpreter a SyntaxError is checked with
``ast.parse(feature_version=<base>)``: it failing there too proves ``base`` True; passing proves
nothing (None).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

SCHEMA = "pymolt.compile/1"
SKIP_DIRS = (".venv", "venv", ".git", ".pymolt", ".pymolt_cache", ".tox", "__pycache__",
             "node_modules", "site-packages")
_MAX_FINDINGS = 500
_TIMEOUT = 300.0

# Runs on the TARGET interpreter: stdlib only, no pymolt.
_SCRIPT = r'''
import ast, json, os, sys, warnings
request = json.loads(sys.stdin.read())
root, skip, base = request["root"], set(request["skip"]), request.get("base")
out = {"python": "%d.%d.%d" % sys.version_info[:3], "files": 0, "findings": []}
feature = tuple(int(p) for p in base.split(".")[:2]) if base else None
for dirpath, dirnames, filenames in os.walk(root):
    dirnames[:] = sorted(d for d in dirnames if d not in skip)
    for name in sorted(filenames):
        if not name.endswith(".py"):
            continue
        path = os.path.join(dirpath, name)
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        try:
            with open(path, "rb") as stream:
                source = stream.read()
        except (OSError, IOError):
            continue
        out["files"] += 1
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                compile(source, rel, "exec", dont_inherit=True)
            except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
                line = getattr(exc, "lineno", None)
                item = {"file": rel, "line": line, "kind": "SyntaxError",
                        "message": (getattr(exc, "msg", None) or str(exc))[:300], "base": None}
                if feature is not None and hasattr(ast, "parse"):
                    try:
                        ast.parse(source, rel, feature_version=feature)
                    except SyntaxError:
                        item["base"] = True
                    except Exception:
                        pass
                out["findings"].append(item)
        for w in caught:
            if issubclass(w.category, SyntaxWarning):
                out["findings"].append({"file": rel, "line": w.lineno, "kind": "SyntaxWarning",
                                        "message": str(w.message)[:300], "base": None})
print("@@pymolt-compile@@ " + json.dumps(out))
'''


def compile_check(project_dir: str | Path, executable: str | Path, *,
                  base_python: str | None = None, base_executable: str | Path | None = None,
                  timeout: float = _TIMEOUT) -> dict[str, Any]:
    """Compile every project ``.py`` on ``executable``.

    Returns ``{"schema", "python", "files", "findings": [...], "failure": str | None}``.
    ``base_executable`` (a local interpreter for the base) fills each finding's ``base``;
    never raises."""
    root = Path(project_dir).resolve()
    result: dict[str, Any] = {"schema": SCHEMA, "python": None, "files": 0, "findings": [],
                              "failure": None}
    run = _run(executable, root, base_python if base_executable is None else None, timeout)
    if "failure" in run:
        result["failure"] = run["failure"]
        return result
    python = run["python"]
    result.update(python=python, files=run["files"])
    findings = run["findings"][:_MAX_FINDINGS]
    if len(run["findings"]) > _MAX_FINDINGS:
        result["truncated"] = len(run["findings"]) - _MAX_FINDINGS
    base_run = None
    if base_executable is not None:
        base_run = _run(base_executable, root, None, timeout)
    for item in findings:
        item["python"] = python
        if base_run is not None and "failure" not in base_run:
            same = any(b["file"] == item["file"] and b["kind"] == item["kind"]
                       and b["line"] == item["line"] for b in base_run["findings"])
            item["base"] = same
    result["findings"] = findings
    return result


def _run(executable: str | Path, root: Path, base: str | None, timeout: float) -> dict[str, Any]:
    request = {"root": str(root), "skip": list(SKIP_DIRS), "base": base}
    try:
        proc = subprocess.run(
            [str(executable), "-B", "-c", _SCRIPT], input=json.dumps(request), cwd=str(root),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"failure": f"compile check timed out after {timeout:g}s"}
    except OSError as exc:
        return {"failure": f"could not start {executable}: {exc}"}
    for line in proc.stdout.splitlines():
        if line.startswith("@@pymolt-compile@@ "):
            try:
                return json.loads(line[len("@@pymolt-compile@@ "):])
            except ValueError:
                break
    tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
    return {"failure": f"compile check failed (exit {proc.returncode}) {tail[0][:200]}"}

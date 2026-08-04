"""Re-invoke a captured contact under a *different* dependency version (what-if).

Version isolation is achieved by running the call in a **subprocess that uses the
target venv's interpreter** (so the new dependency version is the one imported);
process isolation comes for free with the subprocess. A self-contained driver
resolves the dependency symbol, calls it with the captured inputs, and normalizes
the result with the *same* normalizer the boundary tracer uses (embedded inline),
so the new result is directly comparable to the recorded contract.

Inputs come from either JSON (for reconstructible values) or a pickle/dill blob
(for opaque live objects — functions, framework state — captured via a snapshot).
The driver prefers ``dill`` when available in the target venv and falls back to
stdlib ``pickle``.

This is the lighter of the two sandbox modes. The heavier one — forking from a
live process state (CoW) to push execution into a blind branch with the real
in-scope objects — is the planned **fork-server** (see ``sandbox.py``).
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel

_NORMALIZE_SRC = (Path(__file__).with_name("_normalize.py")).read_text(encoding="utf-8")

_DRIVER_MAIN = '''

import importlib as _importlib
import json as _json
import sys as _sys


def _pmolt_resolve(qualname):
    parts = qualname.split(".")
    for i in range(len(parts), 0, -1):
        mod = ".".join(parts[:i])
        try:
            obj = _importlib.import_module(mod)
        except Exception:
            continue
        try:
            for attr in parts[i:]:
                obj = getattr(obj, attr)
        except AttributeError:
            continue
        return obj
    raise ImportError("could not resolve " + qualname)


def _pmolt_main():
    spec = _json.loads(_sys.stdin.read())
    qualname = spec["qualname"]
    if spec.get("pickle"):
        try:
            import dill as _pk
        except ImportError:
            import pickle as _pk
        with open(spec["pickle"], "rb") as _f:
            payload = _pk.load(_f)
        args, kwargs = payload.get("args", []), payload.get("kwargs", {})
    else:
        args, kwargs = spec.get("args", []), spec.get("kwargs", {})
    fn = _pmolt_resolve(qualname)
    try:
        result = fn(*args, **kwargs)
        out = {"outcome": "returned", "value": normalize(result)}
    except Exception as exc:
        out = {"outcome": "raised", "error": type(exc).__name__,
               "value": normalize(getattr(exc, "args", None))}
    _sys.stdout.write(_json.dumps(out))


_pmolt_main()
'''


class ReinvokeResult(BaseModel):
    outcome: str                 # returned | raised | error | timeout
    value: Any = None            # normalized return value (or exception args)
    error: str | None = None     # exception type (raised) or message (error/timeout)


def reinvoke(
    python_exe: str | Path,
    qualname: str,
    *,
    args: list | None = None,
    kwargs: dict | None = None,
    args_pickle: str | Path | None = None,
    timeout: float = 30.0,
) -> ReinvokeResult:
    """Call ``qualname(*args, **kwargs)`` under ``python_exe`` and return its normalized result.

    Provide inputs either as JSON-reconstructible ``args``/``kwargs`` or as a
    pickle/dill blob path (``args_pickle`` -> ``{"args": [...], "kwargs": {...}}``).
    """
    spec: dict = {"qualname": qualname}
    if args_pickle is not None:
        spec["pickle"] = str(args_pickle)
    else:
        spec["args"] = args or []
        spec["kwargs"] = kwargs or {}

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(_NORMALIZE_SRC + _DRIVER_MAIN)
        driver_path = f.name
    try:
        proc = subprocess.run(
            [str(python_exe), driver_path],
            input=json.dumps(spec), capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ReinvokeResult(outcome="timeout", error=f"exceeded {timeout}s")
    except OSError as e:
        return ReinvokeResult(outcome="error", error=str(e))
    finally:
        try:
            os.unlink(driver_path)
        except OSError:
            pass

    if proc.returncode != 0:
        return ReinvokeResult(outcome="error", error=(proc.stderr.strip()[-300:] or "driver failed"))
    try:
        data = json.loads(proc.stdout)
        return ReinvokeResult(outcome=data["outcome"], value=data.get("value"), error=data.get("error"))
    except (ValueError, KeyError):
        return ReinvokeResult(outcome="error", error="unparseable driver output")

"""Export the injected runtime as a self-contained bundle for use *outside* pymolt.

The whole point of Mode B is that the tool lives outside the target project: nothing is added
to the project's codebase, and the target environment needs neither pymolt nor pydantic. This
writes the pure-stdlib injected runtime as a standalone ``pymolt_trace`` package plus a
top-level ``sitecustomize.py``. Drop the resulting directory onto ``PYTHONPATH`` (in a venv, a
Docker image, the 3.6 devcontainer, …) and the watcher auto-activates from the
``PYMOLT_TRACE_*`` environment — zero edits to the target app.

    bundle/
      sitecustomize.py            # auto-imported at interpreter startup -> activate()
      pymolt_trace/
        __init__.py
        _normalize.py _backend.py _sinks.py boundary_tracer.py   # verbatim, stdlib-only

CLI: ``pymolt verify export-watcher <dir>``
Standalone: ``python -m pymolt.verify.export_watcher <dir>``
"""
import shutil
from pathlib import Path

from pymolt.verify import _backend, _normalize, _sinks, _wrap, boundary_tracer

# The injected runtime modules, copied verbatim. Their intra-package relative imports
# (``from ._backend import ...``) resolve inside the ``pymolt_trace`` package unchanged.
_RUNTIME_MODULES = (_normalize, _backend, _sinks, _wrap, boundary_tracer)

_SITECUSTOMIZE = '''\
"""Auto-activate the pymolt boundary watcher at interpreter startup.

Python imports a top-level ``sitecustomize`` automatically when it is on the path. This calls
``pymolt_trace.boundary_tracer.activate()``, which is itself a no-op unless PYMOLT_TRACE_TARGET
is set — so the bundle is safe to leave on PYTHONPATH permanently.
"""
try:
    from pymolt_trace.boundary_tracer import activate

    activate()
except Exception:
    # Startup hooks must never break the interpreter; run untraced on any failure.
    pass
'''


def export_watcher(dest: str | Path, package_name: str = "pymolt_trace") -> Path:
    """Write the standalone watcher bundle into ``dest`` and return the bundle root.

    Pure-stdlib output: the bundle imports nothing beyond the standard library, so it loads in
    any interpreter from 3.6 up without installing pymolt or pydantic.
    """
    dest = Path(dest)
    pkg = dest / package_name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(
        '"""Standalone pymolt boundary-trace runtime (pure stdlib, Python 3.6+)."""\n'
    )
    for module in _RUNTIME_MODULES:
        src = Path(module.__file__)
        shutil.copyfile(src, pkg / src.name)
    (dest / "sitecustomize.py").write_text(_SITECUSTOMIZE)
    return dest


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        sys.exit("usage: python -m pymolt.verify.export_watcher <dest-dir>")
    out = export_watcher(sys.argv[1])
    print("watcher bundle written to:", out)
    print("use it with:")
    print(f"  PYTHONPATH={out} PYMOLT_TRACE_TARGET=<dep> "
          f"PYMOLT_TRACE_OUT=/tmp/trace-{{pid}}.jsonl  <your command>")

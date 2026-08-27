"""
axiom_graph/analyzers/ghost_stubs.py

Ghost Stub Synthesizer:
Generates lightweight .pyi type stubs for opaque C++/Rust binary extensions
using ephemeral sandboxed introspection when static AST parsing cannot discover symbols.
"""

from __future__ import annotations

import inspect
import json
import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# Introspection helper script executed in an ephemeral isolated child process
_INTROSPECTION_SCRIPT = """
import sys, inspect, json

module_name = sys.argv[1]
search_path = sys.argv[2]
if search_path:
    sys.path.insert(0, search_path)

try:
    mod = __import__(module_name, fromlist=["*"])
except Exception as e:
    print(json.dumps({"error": str(e), "symbols": []}))
    sys.exit(0)

symbols = []
for name, obj in inspect.getmembers(mod):
    if name.startswith("__") and not name.startswith("__init__"):
        continue
    kind = "attribute"
    sig = ""
    doc = ""
    try:
        if inspect.isclass(obj):
            kind = "class"
        elif inspect.isfunction(obj) or inspect.isbuiltin(obj) or inspect.isroutine(obj):
            kind = "function"
            try:
                sig = str(inspect.signature(obj))
            except Exception:
                sig = "(*args, **kwargs)"
        doc = inspect.getdoc(obj) or ""
    except Exception:
        pass

    symbols.append({
        "name": name,
        "kind": kind,
        "signature": sig,
        "doc": doc[:200],
    })

print(json.dumps({"error": None, "symbols": symbols}))
"""


def synthesize_ghost_stubs(
    package_dir: Path,
    module_name: str,
    output_dir: Path | None = None,
    timeout: float = 5.0,
) -> Path | None:
    """
    Synthesize a .pyi type stub for a module by running an ephemeral introspection process.

    Args:
        package_dir: Directory containing the package / wheel.
        module_name: Python module name to introspect (e.g. "tensorflow" or "torch._C").
        output_dir: Destination directory for .pyi stub. Defaults to package_dir / ".stubs".
        timeout: Subprocess execution timeout in seconds.

    Returns:
        Path to the generated .pyi stub, or None on failure.
    """
    if output_dir is None:
        output_dir = package_dir / ".stubs"
    output_dir.mkdir(parents=True, exist_ok=True)

    stub_file = output_dir / f"{module_name}.pyi"
    if stub_file.exists() and stub_file.stat().st_size > 0:
        return stub_file

    try:
        proc = subprocess.run(
            [sys.executable, "-c", _INTROSPECTION_SCRIPT, module_name, str(package_dir)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            log.debug("Ghost stub introspection failed for %s: %s", module_name, proc.stderr)
            return None

        data = json.loads(proc.stdout.strip())
        if data.get("error"):
            log.debug("Introspection import error for %s: %s", module_name, data["error"])
            return None

        symbols = data.get("symbols", [])
        if not symbols:
            return None

        lines = [f"# Ghost stub synthesized by Axiom Graph for {module_name}", "from typing import Any", ""]
        for sym in symbols:
            name = sym["name"]
            kind = sym["kind"]
            sig = sym["signature"]
            if kind == "class":
                lines.append(f"class {name}: ...")
            elif kind == "function":
                sig_str = sig if sig else "(*args: Any, **kwargs: Any) -> Any"
                lines.append(f"def {name}{sig_str}: ...")
            else:
                lines.append(f"{name}: Any = ...")

        stub_file.write_text("\n".join(lines), encoding="utf-8")
        log.info("Synthesized ghost stub for %s with %d symbols -> %s", module_name, len(symbols), stub_file)
        return stub_file

    except Exception as exc:
        log.debug("Ghost stub synthesis exception for %s: %s", module_name, exc)
        return None

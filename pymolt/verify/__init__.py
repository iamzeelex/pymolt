"""verify/ — Component 6: behavioral truth.

Answers the one question static analysis cannot: does the code still *behave* the same after a
dependency version changes? Observes actual behavior at the user-code <-> dependency boundary
and reports where it changed — as typed, honesty-marked facts. It decides nothing.

Two runtimes live here:
  - Injected runtime (pure stdlib, 3.6+): ``boundary_tracer`` + the ``_*`` modules, loaded
    *inside the target app* (e.g. the 3.6 flasgger devcontainer) via ``sitecustomize``/``.pth``.
  - Host core (Pydantic, >=3.12): ``cascade``, ``coverage``, ``golden_master``, ``diff``,
    ``models`` — consume the JSON/JSONL artifacts and fold them into typed reports.

IMPORTANT — this package is imported *inside the target interpreter* when the watcher loads
``pymolt.verify.boundary_tracer``. So importing the package must NOT eagerly pull in the host
stack (pydantic; 3.8+). Host convenience names below are resolved lazily via ``__getattr__``
(PEP 562); on 3.6 ``__getattr__`` simply never fires, and ``boundary_tracer`` + the ``_*``
modules stay pure stdlib. Rendering is intentionally absent (it belongs in ``interfaces/``);
graph integration is a marked extension point. See ``docs/components/verify.md``.
"""

# Lazy host-API surface: name -> "submodule:attribute". Resolved on first access only, so a
# bare ``import pymolt.verify`` (or the container loading boundary_tracer) never imports pydantic.
_LAZY = {
    "record": "pymolt.verify.boundary_tracer:record",
    "Recorder": "pymolt.verify.boundary_tracer:Recorder",
    "run_cascade": "pymolt.verify.cascade:run_cascade",
    "verify_node": "pymolt.verify.cascade:verify_node",
    "CascadeDeps": "pymolt.verify.cascade:CascadeDeps",
    "build_boundary_diff": "pymolt.verify.diff:build_boundary_diff",
    "snapshot": "pymolt.verify.golden_master:snapshot",
    "diff_snapshots": "pymolt.verify.golden_master:diff_snapshots",
    "build_coverage_map": "pymolt.verify.coverage:build_coverage_map",
    "blind_spots": "pymolt.verify.coverage:blind_spots",
    "CoverageMap": "pymolt.verify.coverage:CoverageMap",
    "BoundaryDiff": "pymolt.verify.models:BoundaryDiff",
    "GoldenDiff": "pymolt.verify.models:GoldenDiff",
    "NodeRef": "pymolt.verify.models:NodeRef",
    "NodeVerifyResult": "pymolt.verify.models:NodeVerifyResult",
    "TestOutcome": "pymolt.verify.models:TestOutcome",
    "VerifyReport": "pymolt.verify.models:VerifyReport",
    "Verdict": "pymolt.core.enums:Verdict",
    "EvidenceLevel": "pymolt.core.enums:EvidenceLevel",
    "TraceScope": "pymolt.core.enums:TraceScope",
    "TestStatus": "pymolt.core.enums:TestStatus",
}

__all__ = list(_LAZY)


def __getattr__(name):  # PEP 562 (3.7+); never fires on 3.6's plain-stdlib import path
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError("module {0!r} has no attribute {1!r}".format(__name__, name))
    import importlib

    module_name, _, attr = target.partition(":")
    value = getattr(importlib.import_module(module_name), attr)
    globals()[name] = value  # cache for subsequent access
    return value


def __dir__():
    return sorted(list(globals()) + __all__)

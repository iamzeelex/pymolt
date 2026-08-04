"""Contract trace at the dependency boundary — the injected-runtime entry point.

INJECTED RUNTIME — pure stdlib, Python 3.6+ (see ``_normalize`` header for the rules).
This is the heart of L3: record every call crossing from user code into a target
dependency, under a given version, to a Sink. Two activation modes, zero edits to user code:

  Mode A — ``record(prefix)`` : a context manager wrapping a test run; aggregates in a
                                MemorySink; ``dump()`` writes the aggregated JSON artifact.
  Mode B — ``activate()``     : a background watcher driven entirely by environment, wired
                                in via ``sitecustomize``/``.pth`` (see ``sitecustomize.py`` /
                                ``_install_hook.py``). Streams JSONL; survives child
                                processes. Required for apps (like flasgger) whose contract
                                only manifests during request handling.

Folding two recordings into a structured ``BoundaryDiff`` is the *host* side's job
(``pymolt.verify.diff``) — that runs under pymolt's own interpreter and is NOT imported here,
keeping this module loadable inside an old (3.6) target interpreter without pydantic.

Watcher environment:
  PYMOLT_TRACE_TARGET   required — what counts as a dependency. Absent => no-op. One of:
                          "flask"          a single import prefix
                          "flask,werkzeug" several prefixes (comma-separated)
                          "all" / "*"      ALL installed dependencies (everything in
                                           site-packages; stdlib and source/editable app excluded)
  PYMOLT_TRACE_EXCLUDE  comma-separated top-levels to drop (e.g. your own package), esp. with "all"
  PYMOLT_TRACE_SOURCE   comma-separated near side = YOUR code (caller). Default: auto-detect
                        (anything not an installed dependency and not stdlib). Only the boundary
                        "our code -> dependency" is recorded; internal dep<->dep traffic is not.
  PYMOLT_TRACE_INTERNAL "1" to also record internal dep<->dep calls (full audit; default off)
  PYMOLT_TRACE_OUT      output path template; "{pid}" -> PID (default ./pymolt-trace-{pid}.jsonl)
  PYMOLT_TRACE_BACKEND  auto | setprofile | monitoring (default auto)
"""
import atexit
import os

from ._backend import select_backend
from ._sinks import JsonlSink, MemorySink

ENV_TARGET = "PYMOLT_TRACE_TARGET"
ENV_OUT = "PYMOLT_TRACE_OUT"
ENV_BACKEND = "PYMOLT_TRACE_BACKEND"
ENV_EXCLUDE = "PYMOLT_TRACE_EXCLUDE"
ENV_SOURCE = "PYMOLT_TRACE_SOURCE"
ENV_INTERNAL = "PYMOLT_TRACE_INTERNAL"
DEFAULT_OUT = "./pymolt-trace-{pid}.jsonl"


def _split_excludes(raw):
    return tuple(p.strip() for p in (raw or "").split(",") if p.strip())


class Recorder(object):
    """Mode A context manager: aggregate in memory, ``dump()`` to aggregated JSON."""

    def __init__(self, target, backend="auto", exclude=(), source=None, include_internal=False):
        self.target = target
        self._sink = MemorySink()
        self._backend = select_backend(target, self._sink, prefer=backend, exclude=exclude,
                                       source=source, include_internal=include_internal)

    def __enter__(self):
        self._backend.start()
        return self

    def __exit__(self, *exc):
        self._backend.stop()
        return False  # never swallow exceptions from the wrapped block

    @property
    def records(self):
        return self._sink.records

    def dump(self, path):
        self._sink.dump(path, target=self.target)


def record(target, backend="auto", exclude=(), source=None, include_internal=False):
    """Mode A: wrap a test run. ``target`` is a prefix, "all"/"*", or a comma list.

    By default records only the boundary *our code -> dependency*; ``source`` names the near
    side (else auto-detected), ``include_internal=True`` also records dep<->dep traffic.
    ``with record("flask") as rec: ...; rec.dump("old.json")``
    """
    return Recorder(target, backend=backend, exclude=exclude,
                    source=source, include_internal=include_internal)


_active = None


def activate():
    """Mode B: start the watcher from the process environment. Idempotent; a no-op when
    PYMOLT_TRACE_TARGET is unset, so the hook is safe to leave installed."""
    global _active
    if _active is not None:
        return
    target = os.environ.get(ENV_TARGET)
    if not target:
        return  # tracing not requested
    out = os.environ.get(ENV_OUT, DEFAULT_OUT)
    backend_pref = os.environ.get(ENV_BACKEND, "auto")
    exclude = _split_excludes(os.environ.get(ENV_EXCLUDE))
    source = _split_excludes(os.environ.get(ENV_SOURCE)) or None
    include_internal = os.environ.get(ENV_INTERNAL, "") not in ("", "0", "false", "False")

    sink = JsonlSink(out)
    backend = select_backend(target, sink, prefer=backend_pref, exclude=exclude,
                             source=source, include_internal=include_internal)
    backend.start()
    _active = (backend, sink)

    def _shutdown():
        try:
            backend.stop()
        finally:
            sink.close()

    atexit.register(_shutdown)

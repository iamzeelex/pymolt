"""Boundary-event receivers (Sink implementations).

INJECTED RUNTIME — pure stdlib, Python 3.6+ (see ``_normalize`` header for the rules).

JsonlSink   streaming, append-only: one JSON object per line (NDJSON). Memory does not
            grow in a long-lived process — everything is flushed to disk as it arrives.
            MANDATORY for Mode B (watcher).
MemorySink  aggregates in a dict keyed by (qualname + inputs); for Mode A ``record()`` +
            ``dump()`` to the aggregated JSON shape.

Both detect non-determinism the same way: a second observation of the same (qualname,
inputs) with a different result collapses the stored result to
``{"__nondeterministic__": True}`` — surfaced, not smoothed (the diff excludes it from
strict comparison and reports it as a typed honesty marker).
"""
import json
import os
import threading
import time

from ._normalize import normalize

_NONDET = {"__nondeterministic__": True}


def _result_repr(result_obj):
    return normalize(result_obj)


class JsonlSink(object):
    """Streaming NDJSON writer. One event per line:
    ``{"t": "return"|"raise", "q": qualname, "in": inputs, ...}``.

    Thread-safe via a lock around the write. Opened in append mode with a ``{pid}``-stamped
    name, so a parent and its children write to *separate* files without interleaving.
    """

    def __init__(self, path_template):
        self.path = path_template.replace("{pid}", str(os.getpid()))
        self._lock = threading.Lock()
        self._fh = open(self.path, "a", buffering=1)  # line-buffered

    def _write(self, obj):
        line = json.dumps(obj, sort_keys=True, default=str)
        with self._lock:
            self._fh.write(line + "\n")

    def on_call(self, qualname, inputs, site=None):
        # No need to persist 'call' separately — return/raise carry the inputs.
        pass

    def on_return(self, qualname, inputs, result_obj, site=None):
        ev = {"t": "return", "q": qualname, "in": inputs,
              "result": _result_repr(result_obj), "ts": time.time()}
        if site is not None:
            ev["where"] = site  # call site in our code: {file, line, func}
        self._write(ev)

    def on_raise(self, qualname, inputs, exc_typename, site=None):
        ev = {"t": "raise", "q": qualname, "in": inputs,
              "raised": exc_typename, "ts": time.time()}
        if site is not None:
            ev["where"] = site
        self._write(ev)

    def close(self):
        with self._lock:
            try:
                self._fh.close()
            except Exception:
                pass


class MemorySink(object):
    """Aggregates in memory by (qualname + inputs). For ``record()`` / ``dump()``."""

    def __init__(self):
        self.records = {}  # key -> dict
        self._lock = threading.Lock()

    @staticmethod
    def _key(qualname, inputs):
        return json.dumps({"q": qualname, "in": inputs}, sort_keys=True, default=str)

    def on_call(self, qualname, inputs, site=None):
        pass

    def on_return(self, qualname, inputs, result_obj, site=None):
        self._store(qualname, inputs, result=_result_repr(result_obj), raised=None, site=site)

    def on_raise(self, qualname, inputs, exc_typename, site=None):
        self._store(qualname, inputs, result=None, raised=exc_typename, site=site)

    def _store(self, qualname, inputs, result, raised, site=None):
        k = self._key(qualname, inputs)
        with self._lock:
            if k in self.records:
                rec = self.records[k]
                rec["count"] += 1
                if rec["result"] != result or rec["raised"] != raised:
                    rec["result"] = _NONDET
            else:
                rec = {"qualname": qualname, "inputs": inputs,
                       "result": result, "raised": raised, "count": 1}
                if site is not None:
                    rec["where"] = site  # representative call site (first seen)
                self.records[k] = rec

    def dump(self, path, target):
        with open(path, "w") as f:
            json.dump({"target": target, "records": list(self.records.values())},
                      f, indent=2, sort_keys=True)

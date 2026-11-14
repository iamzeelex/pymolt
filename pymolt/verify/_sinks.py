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
import io
import json
import os
import queue
import random
import threading
import time

from ._normalize import normalize

_NONDET = {"__nondeterministic__": True}

DEFAULT_QUEUE_SIZE = 65536
_WRITE_BATCH = 1024
DEFAULT_CLOSE_TIMEOUT = 2.0


def _result_repr(result_obj):
    return normalize(result_obj)


# Event schema version (docs/design/tier1-fact-model-v2.md): v2 adds the canonical ``sym``
# (+ ``symq`` when not exact, ``own`` for a receiver of another class) and ``in.defaulted``;
# every v1 field keeps its meaning.
EVENT_VERSION = 2


def _identity_fields(ev, sym, symq, own):
    if sym is not None:
        ev["sym"] = sym
        if symq is not None:
            ev["symq"] = symq
    if own is not None:
        ev["own"] = own


class JsonlSink(object):
    """Streaming NDJSON writer. One event per line:
    ``{"v": 2, "t": "return"|"raise", "q": qualname, "sym": ..., "in": inputs, ...}``.

    Producers only enqueue with ``put_nowait``.  A daemon writer owns the file,
    so a slow filesystem, full pipe, or wedged sink cannot hold application
    traffic indefinitely.  A bounded queue turns overload into an observable
    ``backpressure_dropped`` counter instead of latency in the target process.

    Runtime metadata is written to ``<trace>.meta.json`` on close.  Keeping it
    out of the JSONL preserves compatibility with old trace readers.
    """

    _STOP = object()

    def __init__(self, path_template, metadata=None, queue_size=None, close_timeout=None):
        self.path = path_template.replace("{pid}", str(os.getpid()))
        self.metadata_path = self.path + ".meta.json"
        self._metrics_lock = threading.Lock()
        self._metadata = dict(metadata or {})
        self._started_wall = time.time()
        self._started_monotonic = time.monotonic()
        self._closed = False
        self._closing = threading.Event()
        try:
            self._sample_rate = float(os.environ.get("PYMOLT_TRACE_SAMPLE_RATE", "1"))
        except ValueError:
            self._sample_rate = 1.0
        self._sample_rate = min(1.0, max(0.0, self._sample_rate))
        # The writer thread is not slow (~200k events/s) but it is GIL-starved while a
        # CPU-bound target runs: a 1024-slot queue dropped 84-99% of events in such runs
        # while saving the producer nothing. A deep queue costs memory only while the writer
        # is starved; the close timeout gives it time to drain at exit.
        if queue_size is None:
            try:
                queue_size = int(os.environ.get("PYMOLT_TRACE_QUEUE_SIZE", str(DEFAULT_QUEUE_SIZE)))
            except ValueError:
                queue_size = DEFAULT_QUEUE_SIZE
        if close_timeout is None:
            try:
                close_timeout = float(os.environ.get("PYMOLT_TRACE_CLOSE_TIMEOUT",
                                                     str(DEFAULT_CLOSE_TIMEOUT)))
            except ValueError:
                close_timeout = DEFAULT_CLOSE_TIMEOUT
        self._close_timeout = max(0.0, close_timeout)
        self._queue = queue.Queue(maxsize=max(1, queue_size))
        self._metrics = {
            "events_seen": 0,
            "events_written": 0,
            "sampling_dropped": 0,
            "backpressure_dropped": 0,
            "write_failures": 0,
            "sink_failures": 0,
            "shutdown_dropped": 0,
        }
        self._writer = threading.Thread(
            target=self._writer_loop,
            name="pymolt-trace-writer-{0}".format(os.getpid()),
        )
        self._writer.daemon = True
        self._writer.start()

    def set_metadata(self, **values):
        """Add runtime facts discovered after sink construction (for example backend name)."""
        with self._metrics_lock:
            for key, value in values.items():
                if value is not None:
                    self._metadata[key] = value

    def note_failure(self, kind="sink_failures", count=1):
        """Best-effort failure accounting for wrappers/backends using this sink."""
        with self._metrics_lock:
            key = kind if kind in self._metrics else "sink_failures"
            self._metrics[key] += count

    @property
    def metrics(self):
        with self._metrics_lock:
            counters = dict(self._metrics)
        counters["dropped_events"] = (
            counters["sampling_dropped"]
            + counters["backpressure_dropped"]
            + counters["write_failures"]
            + counters["shutdown_dropped"]
        )
        return counters

    def _sampled(self):
        return self._sample_rate >= 1.0 or random.random() < self._sample_rate

    def admit(self, force=False):
        """Count one boundary call as seen and decide whether to record it.

        Backends call this BEFORE any input normalization, so an unsampled call costs a
        counter bump instead of a full ``normalize`` of its arguments (sampling used to be
        decided at write time, after the expensive work was already done). ``force`` keeps
        a call regardless of the sample rate — used for raises, which are always retained.
        Pass ``admitted=True`` to ``on_return``/``on_raise`` for an admitted call so it is not
        counted or sampled twice.
        """
        self.note_failure("events_seen")
        if force or self._sampled():
            return True
        self.note_failure("sampling_dropped")
        return False

    def _write(self, obj):
        if self._closed:
            self.note_failure("shutdown_dropped")
            return
        try:
            self._queue.put_nowait(obj)
        except queue.Full:
            self.note_failure("backpressure_dropped")

    def _write_line(self, fh, obj):
        fh.write(json.dumps(obj, sort_keys=True, default=str) + "\n")

    def _writer_loop(self):
        # Batched writes, one flush per batch. The file used to be line-buffered: one
        # write syscall per event, each releasing and re-acquiring the GIL, so a writer
        # competing with busy request threads lost the GIL on every line and fell behind.
        # Each drained batch is still flushed promptly, so a killed process loses at most
        # the batch in flight.
        fh = None
        try:
            fh = open(self.path, "a")
        except Exception:
            self.note_failure("sink_failures")
        stop = False
        try:
            while not stop:
                try:
                    item = self._queue.get(timeout=0.05)
                except queue.Empty:
                    if self._closing.is_set():
                        break
                    continue
                batch = [item]
                while len(batch) < _WRITE_BATCH:
                    try:
                        batch.append(self._queue.get_nowait())
                    except queue.Empty:
                        break
                try:
                    buf = io.StringIO()
                    serialized = 0
                    for obj in batch:
                        if obj is self._STOP:
                            stop = True
                            continue
                        try:
                            self._write_line(buf, obj)
                        except Exception:
                            self.note_failure("write_failures")
                        else:
                            serialized += 1
                    if serialized:
                        if fh is None:
                            self.note_failure("write_failures", serialized)
                        else:
                            try:
                                fh.write(buf.getvalue())
                                fh.flush()
                            except Exception:
                                self.note_failure("write_failures", serialized)
                            else:
                                self.note_failure("events_written", serialized)
                finally:
                    for _ in batch:
                        self._queue.task_done()
        finally:
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    self.note_failure("sink_failures")

    def on_call(self, qualname, inputs, site=None):
        # No need to persist 'call' separately — return/raise carry the inputs.
        pass

    def on_return(self, qualname, inputs, result_obj, site=None, admitted=False, sym=None,
                  symq=None, own=None):
        if not admitted and not self.admit():
            return
        ev = {"v": EVENT_VERSION, "t": "return", "q": qualname, "in": inputs,
              "result": _result_repr(result_obj), "ts": time.time()}
        _identity_fields(ev, sym, symq, own)
        if site is not None:
            ev["where"] = site  # call site in our code: {file, line, func}
        self._write(ev)

    def on_raise(self, qualname, inputs, exc_typename, site=None, admitted=False, sym=None,
                 symq=None, own=None):
        if not admitted and not self.admit(force=True):
            return
        ev = {"v": EVENT_VERSION, "t": "raise", "q": qualname, "in": inputs,
              "raised": exc_typename, "ts": time.time()}
        _identity_fields(ev, sym, symq, own)
        if site is not None:
            ev["where"] = site
        self._write(ev)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._closing.set()
        try:
            self._queue.put(self._STOP, timeout=self._close_timeout)
        except queue.Full:
            # The writer is slow or stuck.  Do not wait indefinitely at process
            # shutdown; account for the items that are still observably queued.
            pass
        self._writer.join(self._close_timeout)
        if self._writer.is_alive():
            self.note_failure("sink_failures")
            pending = self._queue.qsize()
            if pending:
                self.note_failure("shutdown_dropped", pending)
        self._write_metadata()

    def _write_metadata(self):
        metrics = self.metrics
        payload = {
            "schema_version": 1,
            "pid": os.getpid(),
            "started_at": self._started_wall,
            "ended_at": time.time(),
            "duration_seconds": max(0.0, time.monotonic() - self._started_monotonic),
            "sample_rate": self._sample_rate,
            "counters": metrics,
        }
        payload.update(self._metadata)
        tmp = self.metadata_path + ".tmp-{0}-{1}".format(os.getpid(), threading.get_ident())
        try:
            with open(tmp, "w") as fh:
                json.dump(payload, fh, sort_keys=True)
                fh.flush()
            os.replace(tmp, self.metadata_path)
        except Exception:
            self.note_failure("sink_failures")
            try:
                os.unlink(tmp)
            except Exception:
                pass


class MemorySink(object):
    """Aggregates in memory by (qualname + inputs). For ``record()`` / ``dump()``."""

    def __init__(self):
        self.records = {}  # key -> dict
        self._lock = threading.Lock()

    @staticmethod
    def _key(qualname, inputs, sym=None, own=None):
        return json.dumps({"q": qualname, "sym": sym, "own": own, "in": inputs},
                          sort_keys=True, default=str)

    def on_call(self, qualname, inputs, site=None):
        pass

    def admit(self, force=False):
        """Tests aggregate everything: every call is admitted (see ``JsonlSink.admit``)."""
        return True

    def on_return(self, qualname, inputs, result_obj, site=None, admitted=False, sym=None,
                  symq=None, own=None):
        self._store(qualname, inputs, result=_result_repr(result_obj), raised=None, site=site,
                    sym=sym, symq=symq, own=own)

    def on_raise(self, qualname, inputs, exc_typename, site=None, admitted=False, sym=None,
                 symq=None, own=None):
        self._store(qualname, inputs, result=None, raised=exc_typename, site=site,
                    sym=sym, symq=symq, own=own)

    def _store(self, qualname, inputs, result, raised, site=None, sym=None, symq=None,
               own=None):
        # Keyed by identity too: two ``__init__`` of one module share ``q`` but are different
        # calls, and so is one inherited method called on two receiver classes.
        k = self._key(qualname, inputs, sym, own)
        with self._lock:
            if k in self.records:
                rec = self.records[k]
                rec["count"] += 1
                if rec["result"] != result or rec["raised"] != raised:
                    rec["result"] = _NONDET
            else:
                rec = {"qualname": qualname, "inputs": inputs,
                       "result": result, "raised": raised, "count": 1}
                _identity_fields(rec, sym, symq, own)
                if site is not None:
                    rec["where"] = site  # representative call site (first seen)
                self.records[k] = rec

    def dump(self, path, target):
        with open(path, "w") as f:
            json.dump({"target": target, "records": list(self.records.values())},
                      f, indent=2, sort_keys=True)

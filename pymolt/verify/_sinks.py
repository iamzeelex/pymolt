"""Boundary-event receivers (Sink implementations).

INJECTED RUNTIME — pure stdlib, Python 3.6+ (see ``_normalize`` header for the rules).

JsonlSink   streaming, append-only: one JSON object per line (NDJSON). Memory does not
            grow in a long-lived process — everything is flushed to disk as it arrives.
            MANDATORY for Mode B (watcher).
MemorySink  aggregates in a dict keyed by (qualname + inputs); for Mode A ``record()`` +
            ``dump()`` to the aggregated JSON shape.
FactSink    in-process aggregation for long runs (``PYMOLT_TRACE_SINK=facts``): bounded fact
            and tally tables, rewritten atomically as ``trace-{pid}.facts.json`` on a flush
            interval and at close. No event queue, so nothing is dropped for backpressure;
            memory is bounded by the caps, not by the run length.

JsonlSink and MemorySink detect non-determinism the same way: a second observation of the
same (qualname, inputs) with a different result collapses the stored result to
``{"__nondeterministic__": True}`` — surfaced, not smoothed (the diff excludes it from
strict comparison and reports it as a typed honesty marker). FactSink keeps the outcome
distribution instead (at most ``MAX_OUTCOMES`` distinct outcomes per fact, then "other").

Sink API (all keyword-compatible with phase-0/1 callers): ``admit(force=False)`` before any
normalization; ``on_return(..., dur=None, tid=None, warnings=None)`` /
``on_raise(..., dur=None, tid=None, warnings=None)`` for admitted calls;
``tally(qualname, sym, site, kind, raised=None, dur=None, warnings=None)`` for a call that was
not sampled but completed (counted, its values never normalized). ``warnings`` is the call's
deprecation-family warnings, ``[{"category", "message", "count"}]``: on the JSONL event, on
the snapshot fact (sampled calls) or tally (unsampled calls), in the JSONL sidecar's
``tally_warnings`` (unsampled calls).
"""
import collections
import hashlib
import heapq
import io
import itertools
import json
import math
import os
import queue
import random
import threading
import time
import weakref

from ._normalize import normalize

_NONDET = {"__nondeterministic__": True}

DEFAULT_QUEUE_SIZE = 65536
_WRITE_BATCH = 1024
DEFAULT_CLOSE_TIMEOUT = 2.0


def _privacy():
    """The privacy profile, read ONCE per sink: ``normalize``'s own per-value environment
    lookup was about two thirds of the cost of normalizing a scalar."""
    return os.environ.get("PYMOLT_TRACE_PRIVACY", "values").strip().lower()


DEFAULT_SAMPLE_FIRST = 32      # calls per key always admitted when sample_rate < 1
DEFAULT_SAMPLE_KEYS = 65536    # keys the novelty table remembers; past it: the rate alone




class NoveltySampler(object):
    """Novelty-first sampling: per key (the backends pass ``(sym or qualname, site_file,
    site_line)``), the first ``PYMOLT_TRACE_SAMPLE_FIRST`` calls are admitted, later ones at
    ``PYMOLT_TRACE_SAMPLE_RATE``. A call site's first behaviour is always recorded however
    rare it is; its repetitions are what sampling thins out.

    The key table is bounded (``PYMOLT_TRACE_SAMPLE_KEYS``); a key first seen past the cap
    goes straight to the rate. Lock-free: a lost increment between threads only admits one
    extra call, it never loses accounting (that is the sinks' counters' job)."""

    def __init__(self, rate=None, first=None, max_keys=None):
        if rate is None:
            rate = _env_number("PYMOLT_TRACE_SAMPLE_RATE", 1.0, float, 0.0)
        self.rate = min(1.0, max(0.0, rate))
        self.all = self.rate >= 1.0
        if first is None:
            first = _env_number("PYMOLT_TRACE_SAMPLE_FIRST",
                                DEFAULT_SAMPLE_FIRST if not self.all else 0, int, 0)
        self.first = 0 if self.all else first
        if max_keys is None:
            max_keys = _env_number("PYMOLT_TRACE_SAMPLE_KEYS", DEFAULT_SAMPLE_KEYS, int, 0)
        self.max_keys = max_keys
        self._seen = {}

    def sampled(self, key=None):
        if self.all:
            return True
        if key is not None and self.first:
            seen = self._seen
            n = seen.get(key)
            if n is None:
                if len(seen) < self.max_keys:
                    seen[key] = 1
                    return True
            elif n < self.first:
                seen[key] = n + 1
                return True
        return random.random() < self.rate

    def reset(self):
        self._seen = {}

    def policy(self):
        return {"rate": self.rate, "first": self.first, "max_keys": self.max_keys}


def _result_repr(result_obj, privacy=None):
    return normalize(result_obj, _privacy=privacy if privacy is not None else _privacy())


# Event schema version (docs/design/tier1-fact-model-v2.md): v2 adds the canonical ``sym``
# (+ ``symq`` when not exact, ``own`` for a receiver of another class) and ``in.defaulted``;
# every v1 field keeps its meaning.
EVENT_VERSION = 2


def _timing_fields(ev, dur, tid):
    if dur is not None:
        ev["dur"] = dur  # seconds, call -> return/raise (monotonic clock)
    if tid is not None:
        ev["tid"] = tid  # threading.get_ident() of the calling thread


def _identity_fields(ev, sym, symq, own):
    if sym is not None:
        ev["sym"] = sym
        if symq is not None:
            ev["symq"] = symq
    if own is not None:
        ev["own"] = own


# ── runtime warnings carried on a call ────────────────────────────────────────
# A recorded call's deprecation-family warnings (``_backend`` attributes them) travel as
# ``[{"category", "message", "count"}]``. Every holder (a call, a fact, a tally) keeps at
# most ``MAX_WARNINGS`` distinct (category, message) pairs; past that a pair's calls fold
# into ``(category, WARNING_MORE)``, so the counts stay whole while the size stays bounded.
MAX_WARNINGS = 8
WARNING_MORE = "<more warnings>"


def add_warning(acc, key, n=1):
    """Count ``n`` occurrences of ``key = (category, message)`` into ``acc`` (a dict)."""
    cur = acc.get(key)
    if cur is not None:
        acc[key] = cur + n
    elif key[1] != WARNING_MORE and sum(1 for k in acc if k[1] != WARNING_MORE) >= MAX_WARNINGS:
        more = (key[0], WARNING_MORE)
        acc[more] = acc.get(more, 0) + n
    else:
        acc[key] = n


def merge_warnings(acc, items):
    """Fold a ``[{"category", "message", "count"}]`` list into ``acc``; malformed items are
    skipped. Returns ``acc``."""
    for item in items or ():
        try:
            category = item.get("category")
            message = item.get("message")
            count = item.get("count", 1)
        except AttributeError:
            continue
        if not isinstance(category, str) or not isinstance(message, str):
            continue
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            count = 1
        add_warning(acc, (category, message), count)
    return acc


def warning_list(acc):
    """``acc`` as the sorted ``[{"category", "message", "count"}]`` wire form."""
    return [{"category": c, "message": m, "count": n} for (c, m), n in sorted(acc.items())]


_TALLY_WARNING_SITES = 4096  # JsonlSink: (symbol, site) slots kept for unsampled calls


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
        self._privacy = _privacy()
        self._sampler = NoveltySampler()
        self._sample_rate = self._sampler.rate
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
            "tallied": 0,
        }
        # Warnings of unsampled calls: the JSONL keeps no event for them, so their warnings
        # go to the sidecar (``tally_warnings``), per (symbol, site), bounded.
        self._tally_warnings = {}
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

    def _sampled(self, key=None):
        return self._sampler.sampled(key)

    def admit(self, key=None, force=False):
        """Count one boundary call as seen and decide whether to record it.

        Backends call this BEFORE any input normalization, so an unsampled call costs a
        counter bump instead of a full ``normalize`` of its arguments (sampling used to be
        decided at write time, after the expensive work was already done). ``force`` keeps
        a call regardless of the sample rate — used for raises, which are always retained.
        ``key`` (``(sym or qualname, site_file, site_line)``) turns on novelty-first sampling
        (``NoveltySampler``); ``None`` samples at the rate alone.
        Pass ``admitted=True`` to ``on_return``/``on_raise`` for an admitted call so it is not
        counted or sampled twice.
        """
        self.note_failure("events_seen")
        if force or self._sampled(key):
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
                  symq=None, own=None, dur=None, tid=None, warnings=None):
        if not admitted and not self.admit():
            self.tally(qualname, sym, site, "return", dur=dur, warnings=warnings)
            return
        ev = {"v": EVENT_VERSION, "t": "return", "q": qualname, "in": inputs,
              "result": _result_repr(result_obj, self._privacy), "ts": time.time()}
        _identity_fields(ev, sym, symq, own)
        _timing_fields(ev, dur, tid)
        if site is not None:
            ev["where"] = site  # call site in our code: {file, line, func}
        if warnings:
            ev["warnings"] = warnings  # deprecation-family warnings this call issued
        self._write(ev)

    def on_raise(self, qualname, inputs, exc_typename, site=None, admitted=False, sym=None,
                 symq=None, own=None, dur=None, tid=None, warnings=None, binding=False):
        if not admitted and not self.admit(force=True):
            self.tally(qualname, sym, site, "raise", raised=exc_typename, dur=dur,
                       warnings=warnings)
            return
        ev = {"v": EVENT_VERSION, "t": "raise", "q": qualname, "in": inputs,
              "raised": exc_typename, "ts": time.time()}
        if binding:
            ev["binding"] = True    # failed while binding its arguments: the callee never ran
        _identity_fields(ev, sym, symq, own)
        _timing_fields(ev, dur, tid)
        if site is not None:
            ev["where"] = site
        if warnings:
            ev["warnings"] = warnings
        self._write(ev)

    def tally(self, qualname, sym, site, kind, raised=None, dur=None, warnings=None):
        """An unsampled call that completed: the JSONL keeps no event for it, only the count
        (``counters.tallied``), so the sidecar still says how many calls there were. Its
        warnings, if any, are kept per (symbol, site) for the sidecar's ``tally_warnings``."""
        self.note_failure("tallied")
        if warnings:
            try:
                self._tally_warning(qualname, sym, site, warnings)
            except Exception:
                self.note_failure("sink_failures")

    def _tally_warning(self, qualname, sym, site, warnings):
        site = site if isinstance(site, dict) else {}
        key = (sym or qualname, site.get("file"), site.get("line"), site.get("func"))
        with self._metrics_lock:
            slot = self._tally_warnings.get(key)
            if slot is None:
                head = {"q": qualname, "sym": sym, "where": dict(site) or None}
                if len(self._tally_warnings) >= _TALLY_WARNING_SITES:
                    key = (OVERFLOW_SITE, None, None, None)
                    head = {"q": OVERFLOW_SITE, "sym": None, "where": None}
                    slot = self._tally_warnings.get(key)
                if slot is None:
                    slot = self._tally_warnings[key] = (head, {})
            merge_warnings(slot[1], warnings)

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
            "sampling": self._sampler.policy(),
            "counters": metrics,
        }
        payload.update(self._metadata)
        with self._metrics_lock:
            tally_warnings = [dict(head, warnings=warning_list(acc))
                              for head, acc in self._tally_warnings.values()]
        if tally_warnings:
            payload["tally_warnings"] = tally_warnings
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
        self.tallied = 0
        self._lock = threading.Lock()
        self._privacy = _privacy()

    @staticmethod
    def _key(qualname, inputs, sym=None, own=None):
        return json.dumps({"q": qualname, "sym": sym, "own": own, "in": inputs},
                          sort_keys=True, default=str)

    def on_call(self, qualname, inputs, site=None):
        pass

    def admit(self, key=None, force=False):
        """Tests aggregate everything: every call is admitted (see ``JsonlSink.admit``)."""
        return True

    def on_return(self, qualname, inputs, result_obj, site=None, admitted=False, sym=None,
                  symq=None, own=None, dur=None, tid=None, warnings=None):
        self._store(qualname, inputs, result=_result_repr(result_obj, self._privacy),
                    raised=None, site=site, sym=sym, symq=symq, own=own, warnings=warnings)

    def on_raise(self, qualname, inputs, exc_typename, site=None, admitted=False, sym=None,
                 symq=None, own=None, dur=None, tid=None, warnings=None, binding=False):
        self._store(qualname, inputs, result=None, raised=exc_typename, site=site,
                    sym=sym, symq=symq, own=own, warnings=warnings)
        if binding:
            with self._lock:
                for rec in self.records.values():
                    if rec["qualname"] == qualname and rec["raised"] == exc_typename \
                            and rec["inputs"] == inputs:
                        rec["binding"] = True

    def tally(self, qualname, sym, site, kind, raised=None, dur=None, warnings=None):
        """MemorySink admits every call, so a tally can only come from a foreign backend:
        it is counted, never stored as a record."""
        with self._lock:
            self.tallied += 1

    def _store(self, qualname, inputs, result, raised, site=None, sym=None, symq=None,
               own=None, warnings=None):
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
            if warnings:
                acc = merge_warnings({}, rec.get("warnings"))
                rec["warnings"] = warning_list(merge_warnings(acc, warnings))

    def dump(self, path, target):
        with open(path, "w") as f:
            json.dump({"target": target, "records": list(self.records.values())},
                      f, indent=2, sort_keys=True)


# ── aggregating sink ───────────────────────────────────────────────────────────────────────

SNAPSHOT_SCHEMA = "pymolt.facts-snapshot/1"
DEFAULT_FLUSH_SECONDS = 10.0
DEFAULT_FACTS_PER_SITE = 32
DEFAULT_MAX_FACTS = 50000
DEFAULT_MAX_TALLIES = 100000
DEFAULT_MAX_FACT_BYTES = 128 * 1024 * 1024  # canonical JSON held by the fact table
HEAVY_RESULT_BYTES = 4096                    # a symbol's object results past this go type-level
DEFAULT_VALUE_BYTES = 65536                  # one input / result rendering
MAX_OUTCOMES = 8          # distinct outcomes per fact; the rest fold into "other"
MAX_OTHER_TYPES = 8       # distinct types kept inside "other"
MAX_RAISE_TYPES = 16      # distinct raised types per tally
WIDENED_INPUT = '{"__widened__":true}'
OVERFLOW_SITE = "<tally-overflow>"
# Duration histogram: bucket b holds durations in [2**(b + _HIST_MIN_EXP - 1),
# 2**(b + _HIST_MIN_EXP)) seconds; bucket 0 also takes everything below ~1 us and the last
# bucket everything above ~64 s.
_HIST_MIN_EXP = -19
HIST_BUCKETS = 27

# The host's canonical form (facts._canon): the fact key must be the same string there.
_canon = json.JSONEncoder(sort_keys=True, separators=(",", ":"), default=str).encode
_TYPE_ONLY = ("__opaque__", "__shape__", "__cycle__", "__nondeterministic__")
_NAMED_TAGS = ("__opaque__", "__shape__", "__cycle__", "__structure__", "__dataclass__")
_TAG_LABELS = (
    ("__bytes__", "bytes"), ("__decimal__", "decimal.Decimal"), ("__uuid__", "uuid.UUID"),
    ("__timedelta__", "datetime.timedelta"), ("__type__", "type"),
    ("__callable__", "function"), ("__list__", "list"), ("__set__", "set"),
    ("__tuple__", "tuple"), ("__dict__", "dict"),
)


def type_label(value):
    """Top-level type name of a normalized value — the host's ``_type_label(value_type(v))``
    (``facts.py``), so a value reduced here and one reduced on the host agree."""
    if value is None:
        return "NoneType"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if not isinstance(value, dict):
        return type(value).__name__
    for key in _NAMED_TAGS:
        if key in value:
            label = str(value[key])
            return "float" if label == "float-nonfinite" else label
    for key, label in _TAG_LABELS:
        if key in value:
            return label
    if "__datetime__" in value:
        iso = str(value["__datetime__"])
        if "T" in iso:
            return "datetime.datetime"
        return "datetime.time" if ":" in iso else "datetime.date"
    if "__enum__" in value:
        return str(value["__enum__"]).rpartition(".")[0]
    return "dict"


def widen_value(value):
    """A normalized value reduced to its type: ``{"__opaque__": type_label}``. A rendering
    that is already type-only (opaque, shape, cycle, ambiguous) is kept as it is."""
    if isinstance(value, dict):
        for key in _TYPE_ONLY:
            if key in value:
                return value
    return {"__opaque__": type_label(value)}


_WIDE_PAYLOADS = {}  # type label -> canonical {"__opaque__": label} (type names: few)


def _widened_payload(value):
    """``_canon(widen_value(value))``, cached per type label (the widened hot path)."""
    if isinstance(value, dict):
        for key in _TYPE_ONLY:
            if key in value:
                return _canon(value)
    label = type_label(value)
    payload = _WIDE_PAYLOADS.get(label)
    if payload is None:
        payload = _canon({"__opaque__": label})
        if len(_WIDE_PAYLOADS) < 1024:
            _WIDE_PAYLOADS[label] = payload
    return payload


def facts_path_for(path_template):
    """``trace-{pid}.jsonl`` -> ``trace-{pid}.facts.json`` (idempotent)."""
    if path_template.endswith(".facts.json"):
        return path_template
    if path_template.endswith(".jsonl"):
        return path_template[:-len(".jsonl")] + ".facts.json"
    return path_template + ".facts.json"


def _trace_path_for(path_template):
    """The per-process trace path a snapshot stands in for (its sidecar's base name)."""
    if path_template.endswith(".facts.json"):
        return path_template[:-len(".facts.json")] + ".jsonl"
    return path_template


def _env_number(name, default, cast, minimum):
    try:
        value = cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _digest(text):
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:20]


def _hist_bucket(dur):
    if dur <= 0:
        return 0
    b = math.frexp(dur)[1] - _HIST_MIN_EXP
    if b < 0:
        return 0
    return b if b < HIST_BUCKETS else HIST_BUCKETS - 1


def write_snapshot_file(path, header, fact_lines, tally_lines):
    """Write a snapshot atomically (tmp + ``os.replace``) in the line layout: one JSON
    document whose header keys sit on the first line and each fact / tally on its own line,
    so a reader can stream it fact by fact (``facts.iter_snapshot``) or ``json.load`` it.

    ``fact_lines`` / ``tally_lines`` are iterables of serialized JSON objects (no newline).
    Returns the number of facts written."""
    tmp = "{0}.tmp-{1}-{2}".format(path, os.getpid(), threading.get_ident())
    written = 0
    try:
        with open(tmp, "w") as fh:
            head = json.dumps(header, separators=(",", ":"), default=str)
            fh.write(head[:-1] + ',\n"facts":[\n')
            first = True
            for line in fact_lines:
                fh.write(line if first else ",\n" + line)
                first = False
                written += 1
            fh.write('\n],\n"tallies":[\n')
            first = True
            for line in tally_lines:
                fh.write(line if first else ",\n" + line)
                first = False
            fh.write("\n]}\n")
            fh.flush()
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise
    return written


def _dur_json(n, total, lo, hi, hist=None):
    out = {"n": n, "sum": total, "min": lo, "max": hi}
    if hist is not None:
        out["hist"] = hist
    return out


class _Fact(object):
    """One fact: a symbol called from one site with one input, and its outcomes."""

    __slots__ = ("skey", "q", "sym", "symq", "own", "where", "in_json", "widened",
                 "outcomes", "other", "other_types", "other_lost", "count", "bytes",
                 "dur_n", "dur_sum", "dur_min", "dur_max", "first_ts", "last_ts", "seq",
                 "warnings")

    def __init__(self, skey, q, sym, symq, own, where, in_json, widened, now, seq):
        self.skey = skey
        self.q = q
        self.sym = sym
        self.symq = symq
        self.own = own
        self.where = where
        self.in_json = in_json
        self.widened = widened
        self.outcomes = {}       # ("r", result json) | ("x", raised type) -> count
        self.other = 0           # calls past MAX_OUTCOMES distinct outcomes
        self.other_types = {}    # ("r", type label) | ("x", raised type) -> count
        self.other_lost = 0      # "other" calls whose type did not fit either
        self.count = 0
        self.bytes = len(in_json)
        self.dur_n = 0
        self.dur_sum = 0.0
        self.dur_min = None
        self.dur_max = None
        self.first_ts = now
        self.last_ts = now
        self.seq = seq
        self.warnings = None     # (category, message) -> count, for its sampled calls

    def to_json(self):
        outs = []
        for (kind, payload), n in list(self.outcomes.items()):
            if kind == "r":
                outs.append('{"t":"return","result":' + payload + ',"count":' + str(n) + "}")
            else:
                outs.append('{"t":"raise","raised":' + _canon(payload)
                            + (',"binding":true' if kind == "b" else "")
                            + ',"count":' + str(n) + "}")
        if self.other:
            types = []
            for (kind, label), n in sorted(self.other_types.items()):
                if kind == "r":
                    types.append({"t": "return", "type": label, "count": n})
                else:
                    types.append({"t": "raise", "raised": label, "count": n,
                                  **({"binding": True} if kind == "b" else {})})
            outs.append(_canon({"t": "other", "count": self.other, "types": types,
                                "lost": self.other_lost}))
        head = {"q": self.q, "sym": self.sym, "symq": self.symq, "own": self.own,
                "where": self.where, "count": self.count,
                "first_ts": self.first_ts, "last_ts": self.last_ts}
        if self.widened:
            head["widened"] = True
        if self.dur_n:
            head["dur"] = _dur_json(self.dur_n, self.dur_sum, self.dur_min, self.dur_max)
        if self.warnings:
            head["warnings"] = warning_list(self.warnings)
        return '{"in":' + self.in_json + ',"outcomes":[' + ",".join(outs) + "]," + \
            _canon(head)[1:]


class _Tally(object):
    """Every call at one (symbol, site), sampled or not: completeness and latency. Its
    ``warnings`` are those of its UNSAMPLED calls only (a sampled call's are on its fact),
    so a site's warnings are its facts' plus its tally's, never counted twice."""

    __slots__ = ("q", "sym", "where", "count", "returns", "raises", "raises_other",
                 "dur_n", "dur_sum", "dur_min", "dur_max", "hist", "warnings")

    def __init__(self, q, sym, where):
        self.q = q
        self.sym = sym
        self.where = where
        self.count = 0
        self.returns = 0
        self.raises = {}
        self.raises_other = 0
        self.dur_n = 0
        self.dur_sum = 0.0
        self.dur_min = None
        self.dur_max = None
        self.hist = None
        self.warnings = None

    def to_json(self):
        out = {"q": self.q, "sym": self.sym, "where": self.where, "count": self.count,
               "returns": self.returns, "raises": dict(self.raises)}
        if self.raises_other:
            out["raises_other"] = self.raises_other
        if self.warnings:
            out["warnings"] = warning_list(self.warnings)
        if self.dur_n:
            out["dur"] = _dur_json(self.dur_n, self.dur_sum, self.dur_min, self.dur_max,
                                   list(self.hist) if self.hist is not None else None)
        return _canon(out)


_LIVE_FACT_SINKS = weakref.WeakSet()
_FORK_HOOK = []


def _register_fork_hook():
    """Reset every live FactSink in a forked child (once per process; 3.7+ only)."""
    if _FORK_HOOK or not hasattr(os, "register_at_fork"):
        return
    _FORK_HOOK.append(True)
    os.register_at_fork(after_in_child=_after_fork_child)


def _after_fork_child():
    for sink in list(_LIVE_FACT_SINKS or ()):
        try:
            sink.after_fork_child()
        except Exception:
            pass


def _count_value(counter):
    """The next value of an ``itertools.count`` without consuming it (``count(N)``)."""
    return int(repr(counter)[6:-1])


class FactSink(object):
    """In-process aggregation into bounded fact / tally tables (``PYMOLT_TRACE_SINK=facts``).

    * A **fact** is keyed by ``(sym or q, where.file, where.line, where.func, json(in))``
      and holds at most ``MAX_OUTCOMES`` distinct outcomes (``("return", result)`` /
      ``("raise", type)``); further distinct outcomes fold into one ``other`` bucket that
      keeps their types (at most ``MAX_OTHER_TYPES``) and counts the rest as ``lost``.
    * Per (symbol, site) at most ``PYMOLT_TRACE_FACTS_PER_SITE`` distinct inputs (ever, so
      evictions do not reopen the site); past that,
      new inputs go to one **widened** fact (``"in": {"__widened__": true}``) whose outcomes
      are reduced to their type (``widen_value``), and ``counters.widened`` counts its calls.
    * At most ``PYMOLT_TRACE_MAX_FACTS`` facts and ``PYMOLT_TRACE_MAX_FACT_BYTES`` of
      canonical JSON: past either, the lowest-count facts are evicted in a batch (Space-Saving
      style, ``max_facts // 16`` at a time so the scan is amortized), their calls summed into
      ``counters.evicted``. The host treats an evicting snapshot as incomplete.
    * A **tally** per (symbol, site) counts every call, sampled or not, with outcome kinds
      and a duration summary + log2 histogram (at most ``PYMOLT_TRACE_MAX_TALLIES``; past
      that, calls go to one overflow tally and ``counters.tally_overflow``).

    Inputs/results longer than ``PYMOLT_TRACE_FACT_VALUE_BYTES`` are kept as a digest
    (inputs) or reduced to their type plus a digest (results), so one fact is bounded too.

    Hot path (8+ request threads): normalization and JSON encoding run on the calling thread;
    the call is then handed off with ``deque.append`` (one C call, atomic under the GIL) and
    the tables are updated in batches by whichever thread finds ``_DRAIN_AT`` calls waiting
    and gets the table lock **without blocking** (``acquire(False)``). A lock taken on every
    call convoyed under the GIL (8 threads ran 2.5x slower than one); nobody waits for the
    lock here unless ``_DRAIN_HARD`` calls are waiting, which bounds the hand-off queue — an
    overloaded sink costs latency, never a dropped call. Counters bumped outside the lock are
    ``itertools.count`` objects (``next()`` is atomic under the GIL).

    A daemon thread rewrites the snapshot every ``PYMOLT_TRACE_FLUSH_SECONDS`` (atomically:
    tmp + ``os.replace``); ``close()`` flushes a final time and writes the runtime sidecar
    ``<trace>.meta.json`` (``<trace>`` = the per-process trace path the snapshot stands in
    for, e.g. ``trace-{pid}.jsonl``) with the counter names the host's JSONL accounting reads.
    """

    _DRAIN_AT = 256      # waiting calls before a producer tries to apply them
    _DRAIN_MAX = 2048    # calls one opportunistic drain applies before letting go
    _DRAIN_HARD = 16384  # waiting calls before a producer waits for the lock

    def __init__(self, path_template, metadata=None, flush_seconds=None, facts_per_site=None,
                 max_facts=None, max_tallies=None, max_fact_bytes=None, value_bytes=None):
        pid = str(os.getpid())
        self.path = facts_path_for(path_template).replace("{pid}", pid)
        self.trace_path = _trace_path_for(path_template).replace("{pid}", pid)
        self.metadata_path = self.trace_path + ".meta.json"
        self._lock = threading.Lock()        # the tables, counters dict and metadata
        self._flush_lock = threading.Lock()  # one snapshot writer at a time
        self._metadata = dict(metadata or {})
        self._started_wall = time.time()
        self._started_monotonic = time.monotonic()
        self._closed = False
        self._stop = threading.Event()
        self._sampler = NoveltySampler()
        self._sample_rate = self._sampler.rate
        self._privacy = _privacy()
        self._heavy = set()     # symbols whose object results were too large to keep rendering
        if flush_seconds is None:
            flush_seconds = _env_number("PYMOLT_TRACE_FLUSH_SECONDS", DEFAULT_FLUSH_SECONDS,
                                        float, 0.0)
        self._flush_seconds = flush_seconds
        self._per_site = facts_per_site if facts_per_site is not None else _env_number(
            "PYMOLT_TRACE_FACTS_PER_SITE", DEFAULT_FACTS_PER_SITE, int, 1)
        self._max_facts = max_facts if max_facts is not None else _env_number(
            "PYMOLT_TRACE_MAX_FACTS", DEFAULT_MAX_FACTS, int, 1)
        self._max_tallies = max_tallies if max_tallies is not None else _env_number(
            "PYMOLT_TRACE_MAX_TALLIES", DEFAULT_MAX_TALLIES, int, 1)
        self._max_bytes = max_fact_bytes if max_fact_bytes is not None else _env_number(
            "PYMOLT_TRACE_MAX_FACT_BYTES", DEFAULT_MAX_FACT_BYTES, int, 1024)
        self._value_bytes = value_bytes if value_bytes is not None else _env_number(
            "PYMOLT_TRACE_FACT_VALUE_BYTES", DEFAULT_VALUE_BYTES, int, 64)
        self._evict_batch = max(1, self._max_facts // 16)
        self._pending = collections.deque()  # calls handed off, not yet in the tables
        self._facts = {}        # (skey, in_json) -> _Fact
        self._site_inputs = {}  # skey -> exact facts ever created at that (symbol, site)
        self._tallies = {}      # skey -> _Tally
        self._bytes = 0
        self._seq = 0
        # Lock-free counters (bumped on the calling thread).
        self._n_seen = itertools.count()
        self._n_unsampled = itertools.count()
        self._n_oversize = itertools.count()
        self._n_shutdown = itertools.count()
        self._c = {               # updated under self._lock
            "calls": 0,           # completed calls: sampled + tallied
            "sampled": 0,         # admitted calls folded into facts
            "tallied": 0,         # tally() calls
            "widened": 0,         # calls folded into a widened fact
            "widened_facts": 0,
            "evicted": 0,         # calls of evicted facts (values lost; tallies keep them)
            "evicted_facts": 0,
            "outcomes_other": 0,  # calls past MAX_OUTCOMES distinct outcomes of their fact
            "tally_overflow": 0,
            "flushes": 0,
            "flush_failures": 0,
            "drain_waits": 0,     # producers that had to wait for the table lock
            "backpressure_dropped": 0,  # always 0: nothing is ever dropped (kept for the host)
            "write_failures": 0,
            "sink_failures": 0,
        }
        self._template = path_template
        self._flusher = None
        self._start_flusher()
        _LIVE_FACT_SINKS.add(self)
        _register_fork_hook()

    def _start_flusher(self):
        self._flusher = None
        if self._flush_seconds > 0:
            self._flusher = threading.Thread(
                target=self._flush_loop, name="pymolt-facts-flush-{0}".format(os.getpid()))
            self._flusher.daemon = True
            self._flusher.start()

    def after_fork_child(self):
        """Fork (a pre-fork server's workers): the tables, locks and flusher belong to the
        parent, which writes its own snapshot. The child starts empty under its own pid; a
        template without ``{pid}`` gets ``.<pid>`` before ``.facts.json`` so a worker never
        overwrites the parent's file. Run automatically via ``os.register_at_fork``."""
        pid = str(os.getpid())
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._stop = threading.Event()
        template = self._template
        if "{pid}" not in template:
            base = facts_path_for(template)[:-len(".facts.json")]
            template = base + ".{pid}.facts.json"
        self.path = facts_path_for(template).replace("{pid}", pid)
        self.trace_path = _trace_path_for(template).replace("{pid}", pid)
        self.metadata_path = self.trace_path + ".meta.json"
        self._metadata = dict(self._metadata)
        self._started_wall = time.time()
        self._started_monotonic = time.monotonic()
        self._pending = collections.deque()
        self._facts = {}
        self._site_inputs = {}
        self._tallies = {}
        self._bytes = 0
        self._seq = 0
        self._sampler.reset()
        for name in ("_n_seen", "_n_unsampled", "_n_oversize", "_n_shutdown"):
            setattr(self, name, itertools.count())
        for key in self._c:
            self._c[key] = 0
        if not self._closed:
            self._start_flusher()

    # ── JsonlSink-compatible surface ──────────────────────────────────────────────────
    def set_metadata(self, **values):
        with self._lock:
            for key, value in values.items():
                if value is not None:
                    self._metadata[key] = value

    def note_failure(self, kind="sink_failures", count=1):
        if kind == "events_seen":
            for _ in range(count):
                next(self._n_seen)
            return
        with self._lock:
            key = kind if kind in self._c else "sink_failures"
            self._c[key] += count

    @property
    def metrics(self):
        with self._lock:
            c = dict(self._c)
            c["facts"] = len(self._facts)
            c["tallies"] = len(self._tallies)
        c["pending"] = len(self._pending)
        c["events_seen"] = _count_value(self._n_seen)
        c["unsampled"] = _count_value(self._n_unsampled)
        c["oversize_values"] = _count_value(self._n_oversize)
        c["shutdown_dropped"] = _count_value(self._n_shutdown)
        # A call the sampler declined is only lost if nobody tallied it on completion.
        c["sampling_dropped"] = max(0, c["unsampled"] - c["tallied"])
        c["events_written"] = c["sampled"]
        c["dropped_events"] = (c["sampling_dropped"] + c["backpressure_dropped"]
                               + c["write_failures"] + c["shutdown_dropped"])
        return c

    def admit(self, key=None, force=False):
        """Count one boundary call and decide whether its values are recorded (see
        ``JsonlSink.admit``; ``key`` = novelty-first sampling). A declined call is expected to
        come back through ``tally``."""
        next(self._n_seen)
        if force or self._sampler.sampled(key):
            return True
        next(self._n_unsampled)
        return False

    def on_call(self, qualname, inputs, site=None):
        pass

    def on_return(self, qualname, inputs, result_obj, site=None, admitted=False, sym=None,
                  symq=None, own=None, dur=None, tid=None, warnings=None):
        if not admitted and not self.admit():
            self.tally(qualname, sym, site, "return", dur=dur, warnings=warnings)
            return
        try:
            key = sym or qualname
            heavy = key in self._heavy
            result = normalize(result_obj, _privacy=self._privacy, _objects=not heavy)
            payload = _canon(result)
            if not heavy and len(payload) > HEAVY_RESULT_BYTES:
                # A result this large is rendered per call, and grows the snapshot, for no
                # extra evidence: from now on this symbol's generic objects stay type-level.
                if len(self._heavy) < 4096:
                    self._heavy.add(key)
            if len(payload) > self._value_bytes:
                next(self._n_oversize)
                result = {"__opaque__": type_label(result), "__digest__": _digest(payload)}
                payload = _canon(result)
            in_json = self._input_json(inputs)
        except Exception:
            self.note_failure("write_failures")
            self.tally(qualname, sym, site, "return", dur=dur, warnings=warnings)
            return
        self._push(("r", qualname, sym, symq, own, site, in_json, payload, result, dur,
                    time.time(), warnings or None))

    def on_raise(self, qualname, inputs, exc_typename, site=None, admitted=False, sym=None,
                 symq=None, own=None, dur=None, tid=None, warnings=None, binding=False):
        if not admitted and not self.admit(force=True):
            self.tally(qualname, sym, site, "raise", raised=exc_typename, dur=dur,
                       warnings=warnings)
            return
        try:
            in_json = self._input_json(inputs)
        except Exception:
            self.note_failure("write_failures")
            self.tally(qualname, sym, site, "raise", raised=exc_typename, dur=dur,
                       warnings=warnings)
            return
        self._push(("b" if binding else "x", qualname, sym, symq, own, site, in_json,
                    str(exc_typename), None, dur, time.time(), warnings or None))

    def tally(self, qualname, sym, site, kind, raised=None, dur=None, warnings=None):
        """A completed call whose values were not sampled: counted at its (symbol, site),
        its warnings (if any) kept on the tally."""
        self._push(("t", qualname, sym, None, None, site, None,
                    raised or "<unknown>" if kind == "raise" else None, None, dur, None,
                    warnings or None))

    def _input_json(self, inputs):
        in_json = _canon(inputs)
        if len(in_json) > self._value_bytes:
            next(self._n_oversize)
            in_json = _canon({"__oversize__": len(in_json), "__digest__": _digest(in_json)})
        return in_json

    def _push(self, item):
        if self._closed:
            next(self._n_shutdown)
            return
        pending = self._pending
        pending.append(item)
        n = len(pending)
        if n < self._DRAIN_AT:
            return
        if n < self._DRAIN_HARD:
            if self._lock.acquire(False):
                try:
                    self._drain_locked(self._DRAIN_MAX)
                finally:
                    self._lock.release()
            return
        with self._lock:  # bounded queue: wait rather than drop
            self._c["drain_waits"] += 1
            self._drain_locked(None)

    # ── table updates (callers hold self._lock) ───────────────────────────────────────
    def _drain_locked(self, limit):
        pop = self._pending.popleft
        done = 0
        while limit is None or done < limit:
            try:
                item = pop()
            except IndexError:
                break
            done += 1
            try:
                self._apply_locked(item)
            except Exception:
                self._c["write_failures"] += 1

    def _apply_locked(self, item):
        okind, q, sym, symq, own, site, in_json, payload, result, dur, now, warns = item
        if site is not None:
            skey = (sym or q, site.get("file"), site.get("line"), site.get("func"))
        else:
            skey = (sym or q, None, None, None)
        c = self._c
        c["calls"] += 1
        # ── tally: every call ──
        t = self._tallies.get(skey)
        if t is None:
            if len(self._tallies) >= self._max_tallies:
                c["tally_overflow"] += 1
                t = self._tallies.get(OVERFLOW_SITE)
                if t is None:
                    t = self._tallies[OVERFLOW_SITE] = _Tally(OVERFLOW_SITE, None, None)
            else:
                t = self._tallies[skey] = _Tally(q, sym, dict(site) if site else None)
        t.count += 1
        if okind == "r" or (okind == "t" and payload is None):
            t.returns += 1
        else:
            n = t.raises.get(payload)
            if n is not None:
                t.raises[payload] = n + 1
            elif len(t.raises) < MAX_RAISE_TYPES:
                t.raises[payload] = 1
            else:
                t.raises_other += 1
        if dur is not None:
            t.dur_n += 1
            t.dur_sum += dur
            if t.dur_min is None or dur < t.dur_min:
                t.dur_min = dur
            if t.dur_max is None or dur > t.dur_max:
                t.dur_max = dur
            if t.hist is None:
                t.hist = [0] * HIST_BUCKETS
            t.hist[_hist_bucket(dur)] += 1
        if okind == "t":
            c["tallied"] += 1
            if warns:
                if t.warnings is None:
                    t.warnings = {}
                merge_warnings(t.warnings, warns)
            return
        # ── fact: sampled calls ──
        c["sampled"] += 1
        f = self._facts.get((skey, in_json))
        if f is None:
            f = self._new_fact_locked(skey, q, sym, symq, own, site, in_json, now)
        f.count += 1
        f.last_ts = now
        if warns:
            if f.warnings is None:
                f.warnings = {}
            held = len(f.warnings)
            merge_warnings(f.warnings, warns)
            if len(f.warnings) != held:   # a new (category, message): it is held bytes too
                grown = sum(len(k[1]) + len(k[0]) + 40 for k in list(f.warnings)[held:])
                f.bytes += grown
                self._bytes += grown
        if dur is not None:
            f.dur_n += 1
            f.dur_sum += dur
            if f.dur_min is None or dur < f.dur_min:
                f.dur_min = dur
            if f.dur_max is None or dur > f.dur_max:
                f.dur_max = dur
        if f.widened:
            c["widened"] += 1
            if okind == "r":
                payload = _widened_payload(result)
        okey = (okind, payload)
        outcomes = f.outcomes
        n = outcomes.get(okey)
        if n is not None:
            outcomes[okey] = n + 1
        elif len(outcomes) < MAX_OUTCOMES:
            outcomes[okey] = 1
            f.bytes += len(payload)
            self._bytes += len(payload)
        else:
            c["outcomes_other"] += 1
            f.other += 1
            tkey = (okind, type_label(result) if okind == "r" else payload)
            n = f.other_types.get(tkey)
            if n is not None:
                f.other_types[tkey] = n + 1
            elif len(f.other_types) < MAX_OTHER_TYPES:
                f.other_types[tkey] = 1
            else:
                f.other_lost += 1

    def _new_fact_locked(self, skey, q, sym, symq, own, site, in_json, now):
        held = self._site_inputs.get(skey, 0)
        widened = held >= self._per_site
        fkey = (skey, in_json)
        if widened:
            fkey = (skey, WIDENED_INPUT)
            f = self._facts.get(fkey)
            if f is not None:
                return f
            in_json = WIDENED_INPUT
        self._make_room_locked()
        self._seq += 1
        f = _Fact(skey, q, sym, symq, own, dict(site) if site else None, in_json, widened,
                  now, self._seq)
        self._facts[fkey] = f
        self._bytes += f.bytes
        if widened:
            self._c["widened_facts"] += 1
        else:
            self._site_inputs[skey] = self._site_inputs.get(skey, 0) + 1
        return f

    def _make_room_locked(self):
        rounds = 0
        while ((len(self._facts) >= self._max_facts or self._bytes > self._max_bytes)
               and self._facts and rounds < 64):
            rounds += 1
            victims = heapq.nsmallest(self._evict_batch, self._facts.items(),
                                      key=lambda kv: kv[1].count)
            for fkey, f in victims:
                del self._facts[fkey]
                self._bytes -= f.bytes
                self._c["evicted"] += f.count
                self._c["evicted_facts"] += 1
                # The site's input count is NOT given back: a site that keeps producing new
                # inputs must widen, not churn through evictions on every call.

    # ── persistence ───────────────────────────────────────────────────────────────────
    def _flush_loop(self):
        while not self._stop.wait(self._flush_seconds):
            self.flush()

    def flush(self):
        """Apply every waiting call and rewrite the snapshot atomically. The table lock is
        held to drain and to copy the table references; serialization runs outside it
        (records only grow, and dict/list copies are single C calls under the GIL)."""
        with self._flush_lock:
            with self._lock:
                self._drain_locked(None)
                facts = list(self._facts.values())
                tallies = list(self._tallies.values())
                metadata = dict(self._metadata)
            for key in ("facts", "tallies"):
                metadata.pop(key, None)
            header = {"schema": SNAPSHOT_SCHEMA}
            header.update(metadata)
            header.update({
                "schema": SNAPSHOT_SCHEMA,
                "pid": os.getpid(),
                "started_at": self._started_wall,
                "ended_at": time.time(),
                "sample_rate": self._sample_rate,
                "sampling": self._sampler.policy(),
                "limits": {"facts_per_site": self._per_site, "max_facts": self._max_facts,
                           "max_outcomes": MAX_OUTCOMES, "max_tallies": self._max_tallies,
                           "max_fact_bytes": self._max_bytes,
                           "value_bytes": self._value_bytes},
                "counters": self.metrics,
            })
            facts.sort(key=lambda f: (f.first_ts, f.seq))
            try:
                write_snapshot_file(self.path, header, (f.to_json() for f in facts),
                                    (t.to_json() for t in tallies))
            except Exception:
                self.note_failure("flush_failures")
                return False
            self.note_failure("flushes")
            return True

    def close(self):
        """Idempotent: stop the flusher, refuse further calls (counted as
        ``shutdown_dropped``), write the final snapshot, then the sidecar."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._stop.set()
        if self._flusher is not None and self._flusher is not threading.current_thread():
            self._flusher.join(DEFAULT_CLOSE_TIMEOUT)
        if not self.flush():
            self.note_failure("sink_failures")
        # A producer that passed the closed check just before close() may still have handed
        # off a call after the final drain: it is not in the snapshot, so say so.
        for _ in range(len(self._pending)):
            next(self._n_shutdown)
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
            "sampling": self._sampler.policy(),
            "counters": metrics,
        }
        with self._lock:
            payload.update(self._metadata)
        payload["sink"] = "facts"
        payload["facts_path"] = self.path
        tmp = self.metadata_path + ".tmp-{0}-{1}".format(os.getpid(), threading.get_ident())
        try:
            with open(tmp, "w") as fh:
                json.dump(payload, fh, sort_keys=True, default=str)
                fh.flush()
            os.replace(tmp, self.metadata_path)
        except Exception:
            self.note_failure("sink_failures")
            try:
                os.unlink(tmp)
            except Exception:
                pass

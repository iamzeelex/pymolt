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
  PYMOLT_TRACE_BACKEND  auto | setprofile | monitoring | wrap | hybrid (default auto: monitoring on
                        3.12+, setprofile below; monitoring below 3.12 falls back to setprofile)
  PYMOLT_TRACE_MODE     discover | watch | auto | incremental. discover: wide for the whole run.
                        watch: narrow from the start, on PYMOLT_TRACE_IMPACT_TARGETS. auto, 3.12+:
                        incremental (the watch set grows as our code calls new dependency code,
                        no discover window; recorded as mode "incremental"). auto below 3.12 (or
                        when incremental cannot start): discover until the observed symbols
                        saturate, then watch on observed ∪ impact targets (the switch is recorded
                        in the metadata). Unset: auto, except that impact targets alone keep the
                        targeted capture they always meant (wrappers for exact symbols, no
                        process-wide hook).
  PYMOLT_TRACE_STDLIB   comma list of stdlib top-level modules (``asyncio,collections,datetime``)
                        recorded like dependency targets: calls into them from the project's
                        code become facts (metadata ``stdlib_targets``). Unset: the stdlib is not
                        recorded. Test-runner packages (``_pytest``, ``pluggy`` ...) are never
                        targets unless PYMOLT_TRACE_TARGET names them.
  PYMOLT_TRACE_INCREMENTAL  "0" makes 3.12+ auto use discover -> watch instead (default "1")
  PYMOLT_TRACE_INCREMENTAL_SETTLE  seconds without a newly armed code after which incremental
                        mode frees its second tool (the global PY_START for dunders, properties
                        and generators); default 30, 0 = never. Contacts seen so far stay
                        watched and plain calls are still found later, but a protocol, dunder or
                        property contact FIRST used after the settle is missed (metadata:
                        ``settled_at``, ``settle_reason``).
  PYMOLT_TRACE_DISCOVER_QUIET  seconds without a new symbol that count as saturated (default 5;
                        the clock starts at the first observed symbol)
  PYMOLT_TRACE_DISCOVER_MAX    seconds after which discover -> watch switches anyway (default 60)
  PYMOLT_TRACE_SINK     jsonl | facts (default jsonl). facts: the aggregating FactSink, writing
                        PYMOLT_TRACE_OUT with its ``.jsonl`` suffix replaced by ``.facts.json``
  PYMOLT_TRACE_QUEUE_SIZE  jsonl: events queued for the writer thread (default 65536)
  PYMOLT_TRACE_BACKPRESSURE  jsonl, queue full: block (default; the caller waits for the writer,
                        counted as ``backpressure_wait_ms``) | drop (``backpressure_dropped``)
  PYMOLT_TRACE_STALL_SECONDS  jsonl: a writer that processes nothing this long is given up on
                        until it moves again (default 10; its losses are counted)
  PYMOLT_TRACE_CLOSE_TIMEOUT  jsonl: at exit, seconds without writer progress before the rest
                        of the queue is counted as ``shutdown_dropped`` (default 2)
  PYMOLT_TRACE_PRIVACY  values | shape (default values)
  PYMOLT_TRACE_WARNINGS "0" to leave ``warnings.warn`` alone (default on: Deprecation/
                        PendingDeprecation/FutureWarning issued during a recorded call are
                        attributed to it, as ``warnings`` on the event / fact; the original
                        ``warn`` still runs, so filters and output are unchanged)
  PYMOLT_TRACE_SAMPLE_RATE  fraction in (0, 1], default 1
  PYMOLT_TRACE_IMPACT_TARGETS  JSON array (preferred) or comma-list of exact symbols and/or
                               ``module.*`` prefixes. Exact-only sets use wrappers and do not
                               install a global profile hook.
  PYMOLT_TRACE_DEPLOYMENT_ID   optional deployment/release identifier
  PYMOLT_TRACE_REQUEST_ID      optional request identifier for a scoped capture
  PYMOLT_TRACE_CORRELATION_ID  optional distributed correlation identifier
"""
import atexit
import json
import os
import sys
import time

from . import _normalize as _nz
from . import _sinks
from ._backend import (
    _C_SITE_MAX,
    HAS_MONITORING,
    AnyMatcher,
    PrefixMatcher,
    SetProfileBackend,
    _warnings_disabled,
    build_matcher,
    loaded_distributions,
    select_backend,
    stdlib_targets,
)
from ._rawthreads import RawEvent, allocate_lock, get_ident, spawn
from ._sinks import JsonlSink, MemorySink

ENV_TARGET = "PYMOLT_TRACE_TARGET"
ENV_OUT = "PYMOLT_TRACE_OUT"
ENV_BACKEND = "PYMOLT_TRACE_BACKEND"
ENV_EXCLUDE = "PYMOLT_TRACE_EXCLUDE"
ENV_STDLIB = "PYMOLT_TRACE_STDLIB"
ENV_SOURCE = "PYMOLT_TRACE_SOURCE"
ENV_INTERNAL = "PYMOLT_TRACE_INTERNAL"
ENV_IMPACT_TARGETS = "PYMOLT_TRACE_IMPACT_TARGETS"
ENV_DEPLOYMENT_ID = "PYMOLT_TRACE_DEPLOYMENT_ID"
ENV_REQUEST_ID = "PYMOLT_TRACE_REQUEST_ID"
ENV_CORRELATION_ID = "PYMOLT_TRACE_CORRELATION_ID"
ENV_MODE = "PYMOLT_TRACE_MODE"
ENV_QUIET = "PYMOLT_TRACE_DISCOVER_QUIET"
ENV_MAX = "PYMOLT_TRACE_DISCOVER_MAX"
ENV_SINK = "PYMOLT_TRACE_SINK"
ENV_INCREMENTAL = "PYMOLT_TRACE_INCREMENTAL"
ENV_SETTLE = "PYMOLT_TRACE_INCREMENTAL_SETTLE"
DEFAULT_OUT = "./pymolt-trace-{pid}.jsonl"
MODES = ("discover", "watch", "auto", "incremental")
DEFAULT_MODE = "auto"
DEFAULT_QUIET = 5.0
DEFAULT_MAX = 60.0
REFRESH_SECONDS = 2.0       # incremental: how often the running discovered count is noted
DEFAULT_SETTLE = 30.0       # incremental: seconds without a newly armed code -> free tool B


def _split_excludes(raw):
    return tuple(p.strip() for p in (raw or "").split(",") if p.strip())


def _parse_impact_targets(raw):
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, list):
        values = parsed
    else:
        values = raw.split(",")
    result = []
    seen = set()
    for value in values:
        if not isinstance(value, str):
            continue
        value = value.strip()
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return tuple(result)


def _profile_prefixes(impact_targets):
    prefixes = []
    for value in impact_targets:
        if value.endswith(".*"):
            prefix = value[:-2].rstrip(".")
        elif value.endswith("."):
            prefix = value.rstrip(".")
        else:
            prefix = value.rsplit(".", 1)[0] if "." in value else value
        if prefix and prefix not in prefixes:
            prefixes.append(prefix)
    return prefixes


def _select_runtime_backend(target, impact_targets, sink, prefer, exclude, source,
                            include_internal):
    """Choose the narrowest backend that honors the supplied impact set."""
    if not impact_targets:
        return select_backend(
            target, sink, prefer=prefer, exclude=exclude,
            source=source, include_internal=include_internal,
        )
    if prefer in ("auto", "wrap", "hybrid"):
        from ._wrap import TargetedBackend
        return TargetedBackend(
            impact_targets, sink, source=source, include_internal=include_internal,
        )
    # An explicit profiling/monitoring request is respected, but its matcher is
    # narrowed from the broad dependency target to affected module prefixes.
    return select_backend(
        _profile_prefixes(impact_targets), sink, prefer=prefer, exclude=exclude,
        source=source, include_internal=include_internal,
    )


def _start_or_fallback(backend, target, sink, exclude, source, include_internal):
    # ``target`` is what a setprofile fallback observes (a spec or a matcher).
    """Start ``backend``; a monitoring backend that cannot start (no free tool id) is replaced
    by setprofile, with a note. Returns the running backend."""
    try:
        backend.start()
        return backend
    except RuntimeError as exc:
        if getattr(backend, "name", None) != "monitoring":
            raise
        fallback = SetProfileBackend(target, sink, exclude, source, include_internal)
        fallback.note = "monitoring-failed: " + str(exc)
        fallback.start()
        return fallback


def _partition(impact_targets):
    from ._wrap import _partition_impact_targets
    return _partition_impact_targets(impact_targets)


def _empty_c_cap():
    return {"max": _C_SITE_MAX, "muted": 0, "exact": True, "sites": [], "sites_omitted": 0,
            "unsited": 0}


class Controller(object):
    """Mode B backend driver: ``discover``, ``watch``, ``incremental`` or ``auto``.

    ``auto`` on 3.12+ is ``incremental``: one ``MonitoringBackend`` whose watch set grows from
    the call sites in our code (``_monitoring``), so there is no discover window and no
    switch; the metadata says ``mode="incremental"`` with ``switched_at`` = its start (so a
    tool waiting for a switch never hangs) and a running ``discovered`` count. When it cannot
    start (two free tool ids are needed) or ``PYMOLT_TRACE_INCREMENTAL=0``, ``auto`` is
    discover -> watch:

    3.12+: discover and watch are one ``MonitoringBackend`` switched in place
    (``enter_watch``). Below 3.12 (or ``PYMOLT_TRACE_BACKEND=setprofile``): discover is
    ``SetProfileBackend`` and watch is a ``WrapBackend`` on the observed symbols ∪ exact impact
    targets (aliases patched too), the setprofile hook narrowed to the ``module.*`` targets or
    retired.

    The switch runs on the controller's daemon thread, once. No call is lost or recorded twice
    across it: the watch instrumentation is in place before the discover instrumentation stops
    taking new calls, a call already in flight completes through the instrumentation that
    started it, and a wrapped call is skipped by a still-active setprofile hook
    (``skip_wrapped``). Known recall limit: a symbol first called after the switch is not seen
    (the metadata records the switch so the host can say so)."""

    def __init__(self, target, sink, mode=DEFAULT_MODE, prefer="auto", exclude=(), source=None,
                 include_internal=False, impact_targets=(), quiet=DEFAULT_QUIET,
                 max_seconds=DEFAULT_MAX, legacy_watch=False, incremental=True,
                 settle=DEFAULT_SETTLE):
        self.target = target
        self.sink = sink
        self.requested_mode = mode
        self.mode = mode if mode in MODES else DEFAULT_MODE
        self.prefer = prefer
        self.exclude = exclude
        self.source = source
        self.include_internal = include_internal
        self.impact_targets = tuple(impact_targets or ())
        self.quiet = quiet
        self.max_seconds = max_seconds
        self.legacy_watch = legacy_watch    # watch through today's targeted backend selection
        self.incremental = incremental      # 3.12+ auto -> incremental
        self.settle = settle                # incremental: free tool B after this quiet (0: never)
        self.notes = []
        if mode not in MODES:
            self.notes.append("unknown-mode: " + str(mode))
        self.discover = None
        self.watch = None
        self.switched_at = None
        self.switch_reason = None
        self._switched = False
        self._stopped = False
        self._lock = allocate_lock()
        self._wake = RawEvent()
        self._thread = None
        self._t0 = None

    # ── state for the metadata ────────────────────────────────────────────────
    @property
    def active(self):
        return self.watch if self.watch is not None else self.discover

    @property
    def name(self):
        first = self.discover if self.discover is not None else self.watch
        return first.name if first is not None else "none"

    @property
    def uses_profiling(self):
        be = self.active
        if be is None:
            return False
        if self.watch is not None and self.discover is not None and self.watch is not self.discover:
            # setprofile family after the switch: narrowed (still hooked) or retired
            return bool(getattr(self.discover, "_retired", True) is False)
        return bool(getattr(be, "uses_profiling", be.name not in ("wrap",)))

    @property
    def skipped(self):
        return list(getattr(self.watch, "skipped", None) or ())

    @property
    def discovered(self):
        d = self.discover
        return len(d.observed_syms) if d is not None and hasattr(d, "observed_syms") else None

    def _metadata(self):
        meta = {"mode": self.mode, "requested_mode": self.requested_mode,
                "backend": self.name, "profiling_enabled": self.uses_profiling}
        if self.mode == "auto":
            meta["discover_quiet"] = self.quiet
            meta["discover_max"] = self.max_seconds
        elif self.mode == "incremental":
            be = self.discover
            meta["switched_at"] = self.switched_at
            meta["discovered"] = self.discovered
            meta["armed"] = getattr(be, "armed", None)
            meta["incremental_settle"] = self.settle
            meta["settled_at"] = getattr(be, "settled_at", None)
            meta["settle_reason"] = getattr(be, "settle_reason", None)
            if getattr(be, "settled_at", None) is not None:
                meta["handed_over"] = getattr(be, "handed_over", None)
        notes = list(self.notes)
        for be in (self.discover, self.watch):
            note = getattr(be, "note", None)
            if note and note not in notes:
                notes.append(note)
        if notes:
            meta["backend_notes"] = notes
        cap = self._c_cap_account()
        if cap is not None:
            meta["c_site_cap"] = cap
        return meta

    def _frame_backends(self):
        """The frame backends (setprofile / sys.monitoring) this controller runs, once each,
        including the profile half of a hybrid / targeted backend."""
        out = []
        for be in (self.discover, self.watch):
            for b in (be, getattr(be, "sp", None), getattr(be, "profile", None)):
                if b is not None and hasattr(b, "c_cap_account") \
                        and not any(b is o for o in out):
                    out.append(b)
        return out

    def _c_cap_account(self):
        """The compiled-call cap's account over this process's frame backends (``c_site_cap``
        in the sidecar): always written by a runtime that has one, so its absence means an
        older runtime (unknown), and ``muted == 0`` means nothing went unrecorded. None when
        no frame backend runs (a wrap backend records no compiled call by its own hook)."""
        backends = self._frame_backends()
        if not backends:
            return None
        merged = _empty_c_cap()
        for be in backends:
            try:
                acct = be.c_cap_account()
            except Exception:
                merged["exact"] = False     # an account we could not read: not exact
                continue
            if not acct:
                continue
            merged["muted"] += acct["muted"]
            merged["exact"] = merged["exact"] and acct["exact"]
            merged["sites"].extend(acct["sites"])
            merged["sites_omitted"] += acct["sites_omitted"]
            merged["unsited"] += acct["unsited"]
        return merged

    # ── lifecycle ──────────────────────────────────────────────────────────────
    def _monitoring_family(self):
        if self.prefer not in ("auto", "monitoring"):
            return False
        if not HAS_MONITORING:
            if self.prefer == "monitoring":
                self.notes.append("monitoring-unavailable: setprofile")
            return False
        return True

    def start(self):
        args = (self.sink, self.exclude, self.source, self.include_internal)
        if self.prefer in ("wrap", "hybrid"):
            # Explicit targeted wrapping: a fixed backend, no discovery.
            self.mode = "fixed"
            self.watch = _select_runtime_backend(
                self.target, self.impact_targets, self.sink, self.prefer, self.exclude,
                self.source, self.include_internal)
            self.watch.start()
            return
        monitoring = self._monitoring_family()
        mode = self.mode
        if mode == "watch" and not self.impact_targets:
            self.notes.append("watch-without-impact-targets: discover")
            mode = self.mode = "discover"
        if mode == "watch":
            if monitoring and not self.legacy_watch:
                from ._monitoring import MonitoringBackend
                be = MonitoringBackend(self.target, *args, mode="watch",
                                       watch_targets=self.impact_targets)
                try:
                    be.start()
                    self.watch = be
                    return
                except RuntimeError as exc:
                    self.notes.append("monitoring-failed: " + str(exc))
            # Explicit watch below 3.12 is the targeted backend (wrappers for exact symbols,
            # setprofile only for ``module.*``); unset mode keeps today's selection exactly.
            prefer = self.prefer if self.legacy_watch else "auto"
            self.watch = _select_runtime_backend(
                self.target, self.impact_targets, self.sink, prefer, self.exclude,
                self.source, self.include_internal)
            self.watch.start()
            return
        if mode == "incremental" and not (monitoring and self.incremental):
            self.notes.append("incremental-unavailable: discover -> watch")
            mode = self.mode = "auto"
        if mode in ("auto", "incremental") and monitoring and self.incremental:
            from ._monitoring import MonitoringBackend
            be = MonitoringBackend(self.target, *args, mode="incremental",
                                   watch_targets=self.impact_targets)
            try:
                be.start()
            except RuntimeError as exc:
                self.notes.append("incremental-unavailable: " + str(exc))
                mode = self.mode = "auto"
            else:
                self.discover = be
                self.mode = "incremental"
                self._switched = True       # nothing to switch: watching from the start
                self.switched_at = time.time()
                self._t0 = time.monotonic()
                self._start_thread()        # keeps the running discovered count noted
                return
        # Impact targets are observed in discover too (one outside the target would otherwise
        # be invisible until the switch): exact local events on 3.12+, their top-level
        # packages added to the setprofile matcher below.
        profile_target = self._discover_matcher()
        if monitoring:
            from ._monitoring import MonitoringBackend
            be = MonitoringBackend(self.target, *args, watch_targets=self.impact_targets)
        else:
            be = SetProfileBackend(profile_target, *args)
        self.discover = _start_or_fallback(be, profile_target, *args)
        self._t0 = time.monotonic()
        if mode == "auto":
            self._start_thread()

    def _discover_matcher(self):
        tops = sorted({t.split(".", 1)[0] for t in self.impact_targets if t})
        if not tops:
            return self.target
        return AnyMatcher(build_matcher(self.target, self.exclude),
                          PrefixMatcher(tops, self.exclude))

    def _start_thread(self):
        self._thread = spawn(self._run, "pymolt-trace-controller")

    def _interval(self):
        return max(0.005, min(1.0, self.quiet / 4.0, self.max_seconds / 4.0))

    def saturated(self, now=None):
        """"max" / "quiet" when discover should end, else None."""
        d = self.discover
        if d is None:
            return None
        now = time.monotonic() if now is None else now
        if self._t0 is not None and now - self._t0 >= self.max_seconds:
            return "max"
        last = getattr(d, "last_new_at", None)
        if last is not None and now - last >= self.quiet:
            return "quiet"
        return None

    def settled(self, now=None):
        """"quiet" when incremental mode should free its second tool, else None. The clock
        starts at the first armed code (an idle start does not settle)."""
        be = self.discover
        if (self.mode != "incremental" or self.settle <= 0 or be is None
                or getattr(be, "settled_at", None) is not None
                or getattr(be, "_tool2", None) is None):
            return None
        stamps = [t for t in (getattr(be, "last_armed_at", None),
                              getattr(be, "last_new_at", None)) if t is not None]
        if not stamps:
            return None
        now = time.monotonic() if now is None else now
        return "quiet" if now - max(stamps) >= self.settle else None

    def _run(self):
        try:
            if self.mode == "incremental":
                tick = REFRESH_SECONDS
                if self.settle > 0:
                    tick = max(0.005, min(tick, self.settle / 4.0))
                while not self._wake.wait(tick):
                    reason = self.settled()
                    if reason is not None:
                        self.discover.settle(reason)
                    self._note_metadata()
                return
            interval = self._interval()
            while not self._wake.wait(interval):
                reason = self.saturated()
                if reason is not None:
                    self.switch(reason)
                    return
        except Exception:
            pass  # the controller must never break the traced program

    def after_fork_child(self):
        """Threads do not survive fork: restart the controller in the child if still discovering.
        The child's backends start a fresh compiled-call cap account (its own trace, sidecar
        and per-site budget)."""
        self._lock = allocate_lock()
        self._wake = RawEvent()
        for be in self._frame_backends():
            try:
                be.after_fork_child()
            except Exception:
                pass
        self._note_c_cap()  # the sink's copied metadata still holds the parent's account
        if self._stopped:
            return
        if self.mode == "incremental" or (self.mode == "auto" and not self._switched):
            try:
                self._start_thread()
            except Exception:
                pass

    def switch(self, reason="manual"):
        """discover -> watch, once. Returns True if this call switched."""
        with self._lock:
            if self._switched or self._stopped or self.discover is None or self.mode != "auto":
                return False
            self._switched = True
            d = self.discover
            try:
                if hasattr(d, "enter_watch"):
                    d.enter_watch()         # impact targets were armed at start
                    self.watch = d
                else:
                    self.watch = self._setprofile_to_wrap(d)
            except Exception as exc:
                self.notes.append("switch-failed: " + type(exc).__name__ + ": " + str(exc))
                self._note_metadata(switch_failed=True)
                return False
            self.switched_at = time.time()
            self.switch_reason = reason
            self._note_metadata(switched_at=self.switched_at, switch_reason=reason,
                                discovered=self.discovered, watch_backend=self.watch.name)
            return True

    def _setprofile_to_wrap(self, d):
        from ._wrap import WrapBackend
        symbols, prefixes = _partition(self.impact_targets)
        observed = sorted(s for s in list(d.observed_syms) if s)
        # A stdlib function wrapped in place is visible to the program (logging's findCaller
        # reports the wrapper as the call site, a traceback runs through it, ``inspect.stack``
        # lists it): stdlib targets stay observed by the profile hook, narrowed to them.
        stdlib = stdlib_targets(os.environ.get(ENV_STDLIB, ""))
        in_stdlib = [s for s in observed if s.split(".", 1)[0] in stdlib]
        stdlib_set = set(in_stdlib)
        observed = [s for s in observed if s not in stdlib_set]
        wrappable = [s for s in observed if "<" not in s]
        seen = set(wrappable)
        wrap = WrapBackend(wrappable + [s for s in symbols if s not in seen], self.sink,
                           source=self.source, include_internal=self.include_internal,
                           patch_aliases=True)
        for s in observed:
            if "<" in s:
                wrap._mark_skip(s, "unaddressable")
        d.skip_wrapped = True       # a still-hooked thread skips what a wrapper records
        wrap.start()                # watch in place first ...
        keep = list(prefixes) + sorted({s.split(".", 1)[0] for s in in_stdlib})
        if keep:
            d.narrow(keep)          # ... then discover narrows to the module.* / stdlib targets
        else:
            d.retire()              # ... or stops taking new calls (in-flight ones complete)
        return wrap

    def _note_metadata(self, **values):
        try:
            values.update(self._metadata())
            self.sink.set_metadata(**values)
        except Exception:
            pass

    def stop(self):
        with self._lock:
            self._stopped = True
        self._wake.set()
        t = self._thread
        if t is not None and t.ident != get_ident():
            t.join(2.0)
        seen = []
        for be in (self.watch, self.discover):
            if be is None or any(be is s for s in seen):
                continue
            seen.append(be)
            try:
                be.stop()
            except Exception:
                pass
        if not self._switched or self.mode == "incremental":
            self._note_metadata(discovered=self.discovered)
        else:
            self._note_c_cap()  # the switch noted the rest; the cap kept counting

    def _note_c_cap(self):
        try:
            cap = self._c_cap_account()
            if cap is not None:
                self.sink.set_metadata(c_site_cap=cap)
        except Exception:
            pass


class Recorder(object):
    """Mode A context manager: aggregate in memory, ``dump()`` to aggregated JSON."""

    def __init__(self, target, backend="auto", exclude=(), source=None, include_internal=False,
                 impact_targets=None):
        self.target = target
        self._sink = MemorySink()
        self._args = (exclude, source, include_internal)
        self._backend = _select_runtime_backend(
            target, tuple(impact_targets or ()), self._sink, backend, exclude,
            source, include_internal,
        )

    def __enter__(self):
        exclude, source, include_internal = self._args
        self._backend = _start_or_fallback(self._backend, self.target, self._sink, exclude,
                                           source, include_internal)
        return self

    def __exit__(self, *exc):
        self._backend.stop()
        return False  # never swallow exceptions from the wrapped block

    @property
    def records(self):
        return self._sink.records

    def dump(self, path):
        self._sink.dump(path, target=self.target)


def record(target, backend="auto", exclude=(), source=None, include_internal=False,
           impact_targets=None):
    """Mode A: wrap a test run. ``target`` is a prefix, "all"/"*", or a comma list.

    By default records only the boundary *our code -> dependency*; ``source`` names the near
    side (else auto-detected), ``include_internal=True`` also records dep<->dep traffic.
    ``with record("flask") as rec: ...; rec.dump("old.json")``
    """
    return Recorder(target, backend=backend, exclude=exclude,
                    source=source, include_internal=include_internal,
                    impact_targets=impact_targets)


_active = None


ENVIRONMENT_SCHEMA = 1
_RLIMITS = ("NOFILE", "NPROC", "STACK", "CORE", "AS", "DATA", "FSIZE", "MEMLOCK", "CPU")
_CONTAINER_WORDS = (("docker", "docker"), ("kubepods", "kubernetes"), ("containerd", "containerd"),
                    ("lxc", "lxc"), ("libpod", "podman"))


def _read_text(path, limit=65536):
    with open(path) as fh:
        return fh.read(limit)


def _container_hint():
    """``docker`` / ``podman`` / ``kubernetes`` / ``lxc`` / ... or None (the host, or unknown)."""
    try:
        if os.path.exists("/.dockerenv"):
            return "docker"
    except Exception:
        pass
    try:
        if os.path.exists("/run/.containerenv"):
            return "podman"
    except Exception:
        pass
    name = os.environ.get("container")
    if name:
        return str(name)[:40]
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return "kubernetes"
    try:
        text = _read_text("/proc/1/cgroup")
    except Exception:
        return None
    for word, label in _CONTAINER_WORDS:
        if word in text:
            return label
    return None


def _environment():
    """The runtime environment this process starts in, recorded before any tracing so the
    tracer's own reads are never facts and the limits are the starting limits (before the
    app's own ``setrlimit``). Every field is read on its own and is None when unreadable. No
    hostname (private, and a container's is its id). Pure stdlib, never imports ``tempfile``
    (it writes a probe file and imports ``random`` before gevent can patch)."""
    env = {"schema": ENVIRONMENT_SCHEMA}
    env["platform"] = sys.platform
    try:
        u = os.uname()
        env["system"], env["release"], env["machine"] = u.sysname, u.release, u.machine
    except Exception:
        env["system"] = "Windows" if os.name == "nt" else None
        env["release"] = None
        env["machine"] = os.environ.get("PROCESSOR_ARCHITECTURE")
    try:
        env["impl"] = sys.implementation.name
    except Exception:
        env["impl"] = None
    try:
        env["container"] = _container_hint()
    except Exception:
        env["container"] = None
    for key, name in (("uid", "getuid"), ("euid", "geteuid")):
        try:
            env[key] = getattr(os, name)()
        except Exception:
            env[key] = None
    env["caps"] = None
    try:
        for line in _read_text("/proc/self/status").splitlines():
            if line.startswith("CapEff:"):
                env["caps"] = line.split(":", 1)[1].strip()
                break
    except Exception:
        pass
    try:
        env["cpus"] = os.cpu_count()
    except Exception:
        env["cpus"] = None
    try:
        env["affinity"] = len(os.sched_getaffinity(0))
    except Exception:
        env["affinity"] = None
    rlimits = None
    try:
        import resource
        rlimits = {}
        inf = resource.RLIM_INFINITY
        for name in _RLIMITS:
            try:
                soft, hard = resource.getrlimit(getattr(resource, "RLIMIT_" + name))
            except Exception:
                continue
            rlimits[name] = ["inf" if soft == inf else soft, "inf" if hard == inf else hard]
    except Exception:
        pass
    env["rlimits"] = rlimits
    env["tmpdir"] = None
    for name in ("TMPDIR", "TEMP", "TMP"):
        value = os.environ.get(name)
        if value:
            env["tmpdir"] = value
            break
    try:
        env["utc_offset"] = time.localtime().tm_gmtoff
    except Exception:
        env["utc_offset"] = None
    enc = {}
    try:
        enc["fs"] = sys.getfilesystemencoding()
    except Exception:
        enc["fs"] = None
    enc["utf8_mode"] = getattr(sys.flags, "utf8_mode", None)
    enc["locale"] = (os.environ.get("LC_ALL") or os.environ.get("LC_CTYPE")
                     or os.environ.get("LANG") or None)
    env["encoding"] = enc
    seed = None
    try:
        raw = os.environ.get("PYTHONHASHSEED")
        if raw and raw.strip().isdigit() and not sys.flags.ignore_environment:
            seed = int(raw.strip())
    except Exception:
        seed = None
    try:
        algorithm = sys.hash_info.algorithm
    except Exception:
        algorithm = None
    # A fixed PYTHONHASHSEED (0 included) makes str hash order the same in every run.
    env["hash"] = {"seed": seed, "randomized": seed is None and bool(
        getattr(sys.flags, "hash_randomization", 1)), "algorithm": algorithm}
    return env


def _safe_environment():
    try:
        return _environment()
    except Exception:
        return None


def _float_env(name, default):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _facts_path(out):
    """The facts snapshot path for an output template: ``.jsonl`` -> ``.facts.json``."""
    if out.endswith(".jsonl"):
        return out[:-len(".jsonl")] + ".facts.json"
    return out


def _make_sink(kind, out, metadata):
    """(sink, kind used). ``facts`` -> ``_sinks.FactSink`` when this runtime has it."""
    if kind == "facts":
        cls = getattr(_sinks, "FactSink", None)
        if cls is not None:
            try:
                return cls(_facts_path(out), metadata=dict(metadata, trace_sink="facts")), "facts"
            except Exception:
                metadata = dict(metadata, sink_note="facts-sink-failed")
        else:
            metadata = dict(metadata, sink_note="facts-sink-unavailable")
    return JsonlSink(out, metadata=dict(metadata, trace_sink="jsonl")), "jsonl"


def _after_fork_child():
    if _active is not None:
        try:
            _active[0].after_fork_child()
        except Exception:
            pass


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
    impact_targets = _parse_impact_targets(os.environ.get(ENV_IMPACT_TARGETS))
    raw_mode = (os.environ.get(ENV_MODE) or "").strip().lower()
    # Unset: impact targets alone keep meaning a targeted capture from the start.
    legacy_watch = not raw_mode and bool(impact_targets)
    mode = raw_mode or ("watch" if legacy_watch else DEFAULT_MODE)
    sink_kind = (os.environ.get(ENV_SINK) or "jsonl").strip().lower()

    sink, _kind = _make_sink(sink_kind, out, {
        "target": target,
        "impact_targets": list(impact_targets),
        "deployment_id": os.environ.get(ENV_DEPLOYMENT_ID),
        "request_id": os.environ.get(ENV_REQUEST_ID),
        "correlation_id": os.environ.get(ENV_CORRELATION_ID),
        "requested_backend": backend_pref,
        "requested_sink": sink_kind,
        # The interpreter that ran the capture: the host resolves static symbols in it.
        "python": "{0}.{1}.{2}".format(*sys.version_info[:3]),
        "executable": sys.executable,
        # Whether deprecation-family warnings are attributed to calls: "no warnings on a
        # fact" means "none issued" only when this is "on".
        "warnings_capture": "off" if _warnings_disabled() else "on",
        # Stdlib top-level modules recorded like dependencies for this capture (empty: none).
        "stdlib_targets": sorted(stdlib_targets(os.environ.get(ENV_STDLIB, ""))),
        # What the values carry (``dict_order``: dict key insertion order), so the host
        # compares order only between captures that recorded it.
        "value_format": _nz.value_format(),
        # Where this process ran (OS, limits, privilege, ...), read before tracing starts.
        "environment": _safe_environment(),
    })
    backend = Controller(
        target, sink, mode=mode, prefer=backend_pref, exclude=exclude, source=source,
        include_internal=include_internal, impact_targets=impact_targets,
        quiet=_float_env(ENV_QUIET, DEFAULT_QUIET), max_seconds=_float_env(ENV_MAX, DEFAULT_MAX),
        legacy_watch=legacy_watch,
        incremental=os.environ.get(ENV_INCREMENTAL, "1").strip() not in ("0", "false", "False"),
        settle=_float_env(ENV_SETTLE, DEFAULT_SETTLE),
    )
    backend.requested_mode = raw_mode or None
    try:
        backend.start()
    except BaseException:
        sink.note_failure("sink_failures")
        sink.close()
        return
    backend._note_metadata()
    _active = (backend, sink)
    if hasattr(os, "register_at_fork"):
        try:
            os.register_at_fork(after_in_child=_after_fork_child)
        except Exception:
            pass

    def _shutdown():
        try:
            backend.stop()
        finally:
            skipped = getattr(backend, "skipped", ())
            if skipped:
                sink.set_metadata(instrumentation_skipped=[
                    {"target": item[0], "reason": item[1]} for item in skipped
                ])
            try:
                # What this process ran against: the host takes a migration's from-side
                # versions from the baseline's record, not from re-resolving a manifest.
                sink.set_metadata(distributions=loaded_distributions())
            except Exception:
                pass
            sink.close()

    atexit.register(_shutdown)

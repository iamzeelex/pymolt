"""Thread primitives the injected runtime keeps working with after the program monkeypatches
threading (gevent ``patch_all``, eventlet ``monkey_patch``).

INJECTED RUNTIME: pure stdlib, Python 3.6+ syntax (no f-strings, walrus or annotations).

A patched ``threading.Lock`` / ``Event`` / ``Thread`` / ``queue`` / ``time.sleep`` is a
cooperative one that parks the caller in the gevent hub. The sink's background thread would
then spin up a hub of its own and wait there while the main thread, waiting on the import lock
it holds, never runs: a capture of a gevent program deadlocked (the pilot's pytest ran 56
minutes, 28 s of CPU). So everything the runtime does concurrently is built on the ``_thread``
and ``time`` functions captured when this module is first imported (at interpreter start, before
the program runs), or, when it is imported late, on the originals the patching library kept.

Never import ``threading`` / ``queue`` primitives back into the runtime for a lock, an event,
a queue, a sleep or a thread: use what is here. A thread started by ``spawn`` is a raw OS
thread (daemon-like: it never keeps the interpreter alive) with no ``threading.Thread`` object.
"""
import _thread
import collections
import queue as _queue
import sys
import time


def _original(module, name, current):
    """The unpatched ``module.name``: gevent / eventlet remember it when patched late."""
    gm = sys.modules.get("gevent.monkey")
    if gm is not None:
        try:
            return gm.get_original(module, name)
        except Exception:
            pass
    ep = sys.modules.get("eventlet.patcher")
    if ep is not None:
        try:
            return getattr(ep.original(module), name)
        except Exception:
            pass
    return current


allocate_lock = _original("_thread", "allocate_lock", _thread.allocate_lock)
start_new_thread = _original("_thread", "start_new_thread", _thread.start_new_thread)
get_ident = _original("_thread", "get_ident", _thread.get_ident)
sleep = _original("time", "sleep", time.sleep)
monotonic = time.monotonic
time_now = time.time


_greenlet = [None]      # greenlet.getcurrent, once greenlet is loaded (never imported by us)


def stack_key():
    """What per-execution-context call stacks are keyed by: the current greenlet when greenlet
    is loaded (gevent / eventlet run many greenlets on one OS thread, and a switch in the middle
    of a call must not cross their stacks), else the OS thread."""
    getcurrent = _greenlet[0]
    if getcurrent is None:
        module = sys.modules.get("greenlet")
        if module is None:
            return get_ident()
        getcurrent = _greenlet[0] = module.getcurrent
    return id(getcurrent())


# The exception types only (the queue module's primitives are not used).
Full = _queue.Full
Empty = _queue.Empty


class RawEvent:
    """``threading.Event`` for one waiter at a time, on a raw lock (held while clear)."""

    def __init__(self):
        self._lock = allocate_lock()
        self._lock.acquire()
        self._flag = False

    def is_set(self):
        return self._flag

    def set(self):
        if not self._flag:
            self._flag = True
            try:
                self._lock.release()
            except Exception:
                pass

    def clear(self):
        if self._flag:
            self._flag = False
            self._lock.acquire(False)

    def wait(self, timeout=None):
        if self._flag:
            return True
        got = self._lock.acquire(True, -1 if timeout is None else max(0.0, timeout))
        if got:
            try:
                self._lock.release()        # stays set for any other waiter
            except Exception:
                pass
        return self._flag


class RawQueue:
    """A bounded FIFO, many producers and one consumer, with the ``queue.Queue`` calls the sinks
    use (``put_nowait`` / ``put(timeout)`` / ``get(timeout)`` / ``get_nowait`` / ``qsize``)."""

    def __init__(self, maxsize):
        self._items = collections.deque()
        self._max = self.maxsize = maxsize
        self._ready = RawEvent()

    def qsize(self):
        return len(self._items)

    def put_nowait(self, item):
        if len(self._items) >= self._max:
            raise Full()
        self._items.append(item)
        self._ready.set()

    def put(self, item, timeout=None):
        deadline = None if timeout is None else monotonic() + timeout
        while True:
            try:
                self.put_nowait(item)
                return
            except Full:
                if deadline is not None and monotonic() >= deadline:
                    raise
                sleep(0.005)

    def get_nowait(self):
        try:
            return self._items.popleft()
        except IndexError:
            raise Empty()

    def get_many(self, limit):
        """Up to ``limit`` items, oldest first, without waiting (possibly none)."""
        pop = self._items.popleft
        out = []
        try:
            while len(out) < limit:
                out.append(pop())
        except IndexError:
            pass
        return out

    def get(self, timeout=None):
        deadline = None if timeout is None else monotonic() + timeout
        while True:
            try:
                return self._items.popleft()
            except IndexError:
                pass
            self._ready.clear()
            if self._items:                 # an item arrived between the pop and the clear
                continue
            wait = None if deadline is None else deadline - monotonic()
            if wait is not None and wait <= 0:
                raise Empty()
            self._ready.wait(wait)

    def task_done(self):
        pass


class RawThread:
    """A started raw OS thread; ``join`` waits on a raw event, never on ``threading``."""

    def __init__(self, target, name=None):
        self.name = name
        self._target = target
        self._done = RawEvent()
        self.ident = None

    def _run(self):
        self.ident = get_ident()
        try:
            self._target()
        except BaseException:
            pass
        finally:
            self._done.set()

    def start(self):
        start_new_thread(self._run, ())
        return self

    def is_alive(self):
        return not self._done.is_set()

    def join(self, timeout=None):
        self._done.wait(timeout)


def spawn(target, name=None):
    """Start ``target()`` in a raw OS thread and return its ``RawThread``."""
    return RawThread(target, name).start()

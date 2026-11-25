"""The tracer never keeps a class the program made alive.

A class defined in Python can be made at run time and dropped again: Flask's debug helper
makes one per request (its ``__getitem__`` closes over the request), ``unittest.mock`` one per
mock, any ``class`` statement in a function. The runtime's per-class caches held such classes
strongly (the renderer's kinds and container modes, with the class's ``__dict__`` slot, member
descriptors and MRO; the receiver labels; the constructor, inherited-method and bound-builtin
decisions; the incremental call sites), so everything the class's namespace reaches stayed
alive. In locust's suite under capture on 3.12 that was a Flask request holding a JSON provider
that holds a ``weakref.proxy`` of a dead app: its ``isinstance`` over ``gc.get_objects()``
raised ``ReferenceError`` in 23 tests that pass untraced.

Each case makes classes that close over a request holding such a proxy, has our code use them
(call them, call their methods, pass their instances to a dependency, which renders them), drops
everything, and checks what the program sees while the tracer runs: the request died, the
classes died, and no dead proxy is left for ``isinstance`` to meet.

Runtime-layer test: pure stdlib + pytest, 3.6-compatible syntax.
"""
import functools
import gc
import itertools
import json
import os
import shutil
import subprocess
import sys
import textwrap
import types
import weakref

import pytest

from pymolt.verify import _identity, _normalize
from pymolt.verify._backend import HAS_MONITORING, PrefixMatcher, SetProfileBackend
from pymolt.verify._sinks import MemorySink

try:
    import dataclasses
except ImportError:             # 3.6
    dataclasses = None

if HAS_MONITORING:
    from pymolt.verify._monitoring import MonitoringBackend

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MOD = "vfakeclasslife"
SRC = '''
class Base(object):
    def __init__(self, payload=None):
        self.payload = payload

    def method(self):
        return 1


class MyDict(dict):
    pass


def take(x):
    return x
'''

BACKENDS = [("setprofile", None)]
if HAS_MONITORING:
    BACKENDS += [("monitoring", "discover"), ("monitoring", "incremental")]


class Tracked(object):
    """Its finalizer leaves a mark: the program sees when it dies."""

    died = []

    def __init__(self, name):
        self.name = name

    def __del__(self):
        Tracked.died.append(self.name)


class App(object):
    pass


class Provider(object):
    """Flask's JSON provider: a weak proxy of its app."""

    def __init__(self, app):
        self._app = weakref.proxy(app)


class Request(object):
    def __init__(self, provider, mark):
        self.json_module = provider
        self.mark = mark


_SEQ = itertools.count()


def _unique(name):
    return name + "#" + str(next(_SEQ))


@pytest.fixture
def dep():
    mod = types.ModuleType(MOD)
    exec(compile(SRC, "<" + MOD + ">", "exec"), mod.__dict__)
    sys.modules[MOD] = mod
    yield mod
    sys.modules.pop(MOD, None)


def _one_request(dep, name):
    """A request whose app dies at once, and the classes our code makes for it."""
    app = App()
    request = Request(Provider(app), Tracked(name))
    del app                                 # the provider's proxy is dead from here on

    class Files(dict):                      # flask.debughelpers: one class per request
        def __getitem__(self, key):
            return request

    class Item(object):
        status_code = 200                   # a class-level default the renderer reads

        def owner(self):
            return request

    class Slotted(object):
        __slots__ = ("a",)

        def owner(self):
            return request

    class Sub(dep.Base):
        def mine(self):
            return request

    classes = [Files, Item, Slotted, Sub]
    files = Files(a=1)                      # a class called from our code
    dep.take(files)                         # rendered: a dict subclass's read mode
    files.get("a")                          # a builtin method bound to its instance
    dict.items(files)                       # a descriptor a dependency class inherits
    item = Item()
    item.a = 1
    dep.take(item)                          # rendered: an object with a status_code default
    slotted = Slotted()
    slotted.a = 2
    dep.take(slotted)                       # rendered: an object with a member descriptor
    sub = Sub(2)                            # a dependency's __init__, through our class
    sub.method()                            # a dependency method on our class's instance
    dep.take(sub)
    if dataclasses is not None:
        Point = dataclasses.make_dataclass(
            "Point", ["x"], namespace={"owner": lambda self: request})
        dep.take(Point(1))                  # rendered: a dataclass made at run time
        classes.append(Point)
    return [weakref.ref(c) for c in classes]


def _dead_proxies():
    """What locust's teardown met: ``isinstance`` reads ``__class__``, a dead proxy raises."""
    found = 0
    for obj in gc.get_objects():
        if type(obj) is Provider:
            try:
                isinstance(obj._app, functools.partial)
            except ReferenceError:
                found += 1
    return found


def _trace(backend, mode, body, targets=(MOD,)):
    sink = MemorySink()
    if backend == "setprofile":
        be = SetProfileBackend(PrefixMatcher(list(targets)), sink, source=__name__)
    else:
        be = MonitoringBackend(PrefixMatcher(list(targets)), sink, source=__name__, mode=mode)
    be.start()
    try:
        body()
    finally:
        be.stop()
    return sink


@pytest.mark.parametrize("backend, mode", BACKENDS, ids=[b + "-" + str(m) for b, m in BACKENDS])
def test_classes_the_program_makes_die_under_the_tracer(dep, backend, mode):
    name = _unique("request")
    seen = {}

    def body():
        refs = []
        for _ in range(3):
            refs.extend(_one_request(dep, name))
        gc.collect()
        seen["died"] = Tracked.died.count(name)
        seen["alive"] = [r().__qualname__ for r in refs if r() is not None]
        seen["dead_proxies"] = _dead_proxies()

    sink = _trace(backend, mode, body, (MOD, "collections"))
    assert seen == {"died": 3, "alive": [], "dead_proxies": 0}, seen
    syms = set(r.get("sym") for r in sink.records.values())
    assert {MOD + ".take", MOD + ".Base.method"} <= syms, syms


def test_rendered_values_of_a_class_made_at_run_time_are_unchanged():
    """The renderer reads a class's ``__dict__`` slot and member descriptors from the live
    class now, not from its cache: the same values render the same."""
    def make():
        class Item(object):
            status_code = 201

            def __init__(self):
                self.a = 1
                self._hidden = 2

        class Slotted(object):
            __slots__ = ("a", "b")

        class Status(object):
            def __init__(self):
                self.status_code = 404

        s = Slotted()
        s.a = 3
        return Item(), s, Status()

    item, slotted, status = make()
    label = __name__ + ".test_rendered_values_of_a_class_made_at_run_time_are_unchanged."
    assert _normalize.normalize(item) == {
        "__structure__": label + "<locals>.make.<locals>.Item", "status_code": 201}
    assert _normalize.normalize(slotted) == {
        "__object__": label + "<locals>.make.<locals>.Slotted", "attrs": {"a": 3}}
    assert _normalize.normalize(status) == {
        "__structure__": label + "<locals>.make.<locals>.Status", "status_code": 404}
    for _ in range(2):                      # the cached kinds render the same again
        assert _normalize.normalize(slotted)["attrs"] == {"a": 3}
        assert _normalize.normalize(item)["status_code"] == 201


@pytest.mark.skipif(dataclasses is None, reason="dataclasses are 3.7+")
def test_a_dataclass_made_at_run_time_renders_and_dies():
    holder = Tracked(_unique("dc"))
    Point = dataclasses.make_dataclass("Point", ["x", ("y", int, 5)],
                                       namespace={"owner": lambda self: holder})
    out = _normalize.normalize(Point(1))
    assert out["fields"] == [["x", 1], ["y", 5]], out
    ref = weakref.ref(Point)
    del Point
    gc.collect()
    assert ref() is None


def test_a_per_class_cache_never_takes_a_new_class_for_a_dead_one():
    """An entry knows its class by a weak reference: a dead class's id, reused by a new class,
    is a miss, never the dead class's kind."""
    def make(base, name):
        return type(name, (base,), {"__len__": lambda self: 0})

    Old = make(dict, "Old")
    assert _normalize._container_mode(Old, dict) == _normalize._AS_OBJECT
    assert _normalize._kind_of(Old)[0] == "status_attr"     # an object with a __dict__
    kinds_ent = _normalize._KINDS[id(Old)]
    modes_ent = _normalize._MODES[id(Old)]
    owners_ent = (_identity.type_label(Old), _identity._OWNERS[id(Old)])[1]
    del Old
    gc.collect()
    assert kinds_ent[0]() is None and modes_ent[0]() is None and owners_ent[0]() is None
    New = type("New", (dict,), {})          # a plain dict subclass: read raw
    _normalize._KINDS[id(New)] = kinds_ent  # as if New had taken Old's id
    _normalize._MODES[id(New)] = modes_ent
    _identity._OWNERS[id(New)] = owners_ent
    assert _normalize._container_mode(New, dict) == _normalize._RAW
    assert _normalize._kind_of(New)[0] == "status_attr"
    assert _normalize._KINDS[id(New)][0]() is New
    assert _normalize._MODES[id(New)][0]() is New
    assert _identity.type_label(New).endswith(".New")


def test_static_types_are_held_and_python_classes_weakly():
    assert _identity.held(dict) is dict
    assert _normalize._held_type(dict) is dict

    class Mine(object):
        pass

    for ref in (_identity.held(Mine), _normalize._held_type(Mine)):
        assert type(ref) is weakref.ref and ref() is Mine


# ── under capture: the program's own check, in a traced child process ─────────

PROGRAM = textwrap.dedent('''
    import functools
    import gc
    import sys
    import weakref

    import {dep}


    class App(object):
        pass


    class Provider(object):
        def __init__(self, app):
            self._app = weakref.proxy(app)


    class Request(object):
        def __init__(self, provider):
            self.json_module = provider


    def one_request():
        app = App()
        request = Request(Provider(app))

        class Files(dict):
            def __getitem__(self, key):
                return request

        class Sub({dep}.Base):
            def mine(self):
                return request

        files = Files(a=1)
        {dep}.take(files)
        files.get("a")
        dict.items(files)
        Sub().method()
        return [weakref.ref(Files), weakref.ref(Sub)]


    refs = []
    for _ in range(3):
        refs.extend(one_request())
    gc.collect()
    try:
        [a for a in gc.get_objects() if isinstance(a, functools._lru_cache_wrapper)]
    except ReferenceError as exc:
        print("ReferenceError:", exc)
        sys.exit(3)
    alive = [r().__qualname__ for r in refs if r() is not None]
    print("alive:", alive)
    sys.exit(1 if alive else 0)
''')


def _interpreter(minor):
    if sys.version_info[:2] == tuple(int(p) for p in minor.split(".")):
        return sys.executable
    uv = shutil.which("uv")
    if uv is None:
        return None
    proc = subprocess.run([uv, "python", "find", "--no-project", minor], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True, timeout=30, cwd="/")
    found = proc.stdout.strip()
    return found if proc.returncode == 0 and found and os.path.isfile(found) else None


UNDER_CAPTURE = [("current", "setprofile")]
if HAS_MONITORING:
    UNDER_CAPTURE += [("current", "monitoring")]
UNDER_CAPTURE += [("3.8", "auto"), ("3.11", "auto")]


@pytest.mark.parametrize("python, backend", UNDER_CAPTURE,
                         ids=[p + "-" + b for p, b in UNDER_CAPTURE])
def test_isinstance_over_gc_objects_does_not_raise_under_capture(tmp_path, python, backend):
    exe = sys.executable if python == "current" else _interpreter(python)
    if exe is None:
        pytest.skip("needs a uv-managed Python " + python)
    dep = "vfakeclassdep"
    (tmp_path / (dep + ".py")).write_text(SRC)
    (tmp_path / "program.py").write_text(PROGRAM.format(dep=dep))
    env = dict((k, v) for k, v in os.environ.items() if not k.startswith(("PYMOLT_", "PYTHON")))
    env.update(PYTHONPATH=os.pathsep.join([ROOT, str(tmp_path)]), PYMOLT_TRACE_TARGET=dep,
               PYMOLT_TRACE_SOURCE="program", PYMOLT_TRACE_BACKEND=backend,
               PYMOLT_TRACE_OUT=str(tmp_path / "t-{pid}.jsonl"))
    activate = ("from pymolt.verify import boundary_tracer\n"
                "boundary_tracer.activate()\n"
                "import runpy\n"
                "runpy.run_path('program.py', run_name='program')\n")
    proc = subprocess.run([exe, "-c", activate], env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True, timeout=120,
                          cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (trace,) = tmp_path.glob("t-*.jsonl")
    events = [json.loads(line) for line in trace.read_text().splitlines() if line.strip()]
    assert len([e for e in events if e.get("sym") == dep + ".take"]) == 3, events
    (meta,) = tmp_path.glob("t-*.jsonl.meta.json")
    used = json.loads(meta.read_text())["backend"]
    if backend != "auto":
        assert used == backend

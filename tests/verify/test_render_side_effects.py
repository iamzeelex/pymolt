"""Rendering a value must not change it.

Under the tracer, Flask's own suite failed ``test_session`` and ``test_session_vary_cookie``:
recording Flask's session as a call input or inside a result read it through
``SecureCookieSession.__getitem__``, which marks the session accessed, and Flask then added
``Vary: Cookie`` to every response. The renderer now reads containers through the builtin
base's C slots, tests kinds on ``type()`` (``isinstance`` reads ``__class__``, which a proxy
answers by evaluating what it stands for) and runs no descriptor of an object it renders.
"""
import collections
import dataclasses
import sys
import types

import pytest

from pymolt.verify import _normalize as nz
from pymolt.verify._backend import HAS_MONITORING, SetProfileBackend
from pymolt.verify._sinks import MemorySink


class Tracking(dict):
    """A mapping that records every read through its own methods (like Flask's session). It
    keeps its items in the dict storage, so it does not define ``__len__`` (a container
    subclass that does is read as an object: see the virtual-container tests)."""

    def __init__(self, *a, **kw):
        dict.__init__(self, *a, **kw)
        self.touched = []

    def _touch(self, name):
        self.touched.append(name)

    def __getitem__(self, key):
        self._touch("__getitem__")
        return dict.__getitem__(self, key)

    def get(self, key, default=None):
        self._touch("get")
        return dict.get(self, key, default)

    def keys(self):
        self._touch("keys")
        return dict.keys(self)

    def items(self):
        self._touch("items")
        return dict.items(self)

    def values(self):
        self._touch("values")
        return dict.values(self)

    def __iter__(self):
        self._touch("__iter__")
        return dict.__iter__(self)

    def __contains__(self, key):
        self._touch("__contains__")
        return dict.__contains__(self, key)


def _pairs(out):
    return [tuple(p) for p in out["__dict__"]]


@pytest.mark.parametrize("render", [nz.normalize, nz.normalize_input])
def test_a_mapping_that_tracks_access_is_read_without_its_methods(render):
    m = Tracking([("b", 2), ("a", 1)])
    out = render(m)
    assert _pairs(out) == [("a", 1), ("b", 2)]
    assert out["__order__"] == [1, 0]
    assert m.touched == []


def test_a_tracking_mapping_inside_a_result_object_is_not_touched():
    class Context:                      # Flask's RequestContext holds the session in __dict__
        def __init__(self, session):
            self.session = session
            self.stack = [session]

    s = Tracking(user="x")
    out = nz.normalize(Context(s))
    assert out["attrs"]["session"] == {"__dict__": [["user", "x"]]}
    assert out["attrs"]["stack"] == {"__list__": [{"__dict__": [["user", "x"]]}]}
    assert s.touched == []


def test_sequence_and_set_subclasses_are_read_through_the_builtin_slots():
    calls = []

    class L(list):
        def __iter__(self):
            calls.append("list.__iter__")
            return iter(())

        def __getitem__(self, i):
            calls.append("list.__getitem__")
            raise IndexError(i)

    class T(tuple):
        def __iter__(self):
            calls.append("tuple.__iter__")
            return iter(())

        def __getitem__(self, i):
            calls.append("tuple.__getitem__")
            raise IndexError(i)

    class S(set):
        def __iter__(self):
            calls.append("set.__iter__")
            return iter(())

    class F(frozenset):
        def __iter__(self):
            calls.append("frozenset.__iter__")
            return iter(())

    assert nz.normalize(L([1, 2])) == {"__list__": [1, 2]}
    assert nz.normalize(T((1, 2))) == {"__tuple__": [1, 2]}
    assert nz.normalize(S({2, 1})) == {"__set__": [1, 2]}
    assert nz.normalize(F({2, 1})) == {"__set__": [1, 2]}
    long = nz.normalize(L(range(nz.MAX_SEQ + 5)))
    assert len(long["__list__"]) == nz.MAX_SEQ and long["__truncated__"] is True
    assert calls == []


class _Proxy:
    """Stands for an object it resolves on demand, like werkzeug's ``LocalProxy`` or Django's
    ``SimpleLazyObject``: both answer ``__class__`` (what ``isinstance`` reads) by resolving."""

    def __init__(self, resolve):
        object.__setattr__(self, "_resolve", resolve)

    @property
    def __class__(self):
        return type(object.__getattribute__(self, "_resolve")())

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_resolve")(), name)

    def __getitem__(self, key):
        return object.__getattribute__(self, "_resolve")()[key]

    def __iter__(self):
        return iter(object.__getattribute__(self, "_resolve")())

    def __len__(self):
        return len(object.__getattribute__(self, "_resolve")())


def test_a_proxy_is_never_resolved_and_renders_as_its_own_type():
    resolved = []
    target = Tracking(k="v")

    def resolve():
        resolved.append(1)
        return target

    p = _Proxy(resolve)
    assert isinstance(p, dict) and resolved     # what the old ``isinstance`` probes did
    del resolved[:]
    name = nz.typename(p)
    assert name.endswith("._Proxy")
    for render in (nz.normalize, nz.normalize_input):
        assert render(p) == {"__opaque__": name}
        assert render([p]) == {"__list__": [{"__opaque__": name}]}
        assert render({p: 1})["__dict__"] == [[{"__opaque__": name}, 1]]

    Pair = collections.namedtuple("Pair", "proxy")
    deep = nz.normalize([[[[[Pair(p)]]]]])      # at the depth cap: the truncation probe
    assert deep["__list__"][0]["__list__"][0]["__list__"][0]["__list__"][0]["__list__"][0] == {
        "__object__": nz.typename(Pair(p)), "attrs": {"proxy": {"__opaque__": name}},
        "__truncated__": True}
    assert resolved == [] and target.touched == []


def test_scalar_subclasses_become_exact_scalars_without_their_methods():
    calls = []

    class S(str):
        def __str__(self):
            calls.append("str")
            return "other"

        def __eq__(self, other):
            calls.append("eq")
            return False

        __hash__ = str.__hash__

    class Int(int):
        def __int__(self):
            calls.append("int")
            return 0

        def __index__(self):
            calls.append("index")
            return 0

    class Fl(float):
        def __float__(self):
            calls.append("float")
            return 0.0

        def __ne__(self, other):
            calls.append("ne")
            return True

    class B(bytes):
        def __bytes__(self):
            calls.append("bytes")
            return b""

    out = nz.normalize([S("text"), Int(7), Fl(1.5), B(b"\x01")])
    assert out == {"__list__": ["text", 7, 1.5, {"__bytes__": "01"}]}
    assert [type(v) for v in out["__list__"][:3]] == [str, int, float]
    assert nz.normalize({S("k"): Int(1)}) == {"__dict__": [["k", 1]]}
    assert nz.normalize(Fl("nan")) == {"__opaque__": "float-nonfinite"}
    assert calls == []


def test_int_enum_members_keep_their_json():
    import enum
    import json

    class Code(enum.IntEnum):
        OK = 200

    assert json.dumps(nz.normalize([Code.OK, Code(200)])) == '{"__list__": [200, 200]}'


def test_ordered_dict_keeps_its_linked_order():
    od = collections.OrderedDict([("a", 1), ("b", 2), ("c", 3)])
    od.move_to_end("a")                 # linked order b, c, a; the dict storage keeps a, b, c
    assert nz.normalize(od)["__order__"] == [1, 2, 0]

    class Sub(collections.OrderedDict):
        def __getitem__(self, key):
            raise AssertionError("subclass __getitem__ ran")

        def items(self):
            raise AssertionError("subclass items ran")

    sub = Sub([("y", 1), ("x", 2)])
    assert nz.normalize(sub) == {"__dict__": [["x", 2], ["y", 1]], "__order__": [1, 0]}


def test_a_multidict_like_subclass_renders_what_it_stores():
    class MultiDict(dict):              # werkzeug / Django: a list per key, first or last shown
        def __getitem__(self, key):
            return dict.__getitem__(self, key)[0]

    md = MultiDict(a=["1", "2"])
    assert nz.normalize(md) == {"__dict__": [["a", {"__list__": ["1", "2"]}]]}


def test_userlist_and_userdict_hybrids_render_their_data():
    class ErrorList(collections.UserList, list):    # Django's forms.utils.ErrorList
        pass

    class Hybrid(collections.UserDict, dict):
        pass

    errs = ErrorList(["required", "too short"])
    assert list.__len__(errs) == 0                  # the builtin storage stays empty
    assert nz.normalize(errs) == {"__list__": ["required", "too short"]}
    assert nz.normalize(Hybrid(b=1, a=2))["__dict__"] == [["a", 2], ["b", 1]]


def test_a_dataclass_field_behind_a_property_is_not_run():
    calls = []

    @dataclasses.dataclass
    class D:
        a: int
        b: int = 0
        c: int = dataclasses.field(default=5, init=False)

    D.b = property(lambda self: calls.append("b") or 1, lambda self, v: None)
    del D.c                             # c: unset on the instance, no class default either
    d = D.__new__(D)
    d.__dict__["a"] = 1
    out = nz.normalize(d)
    assert out["fields"] == [["a", 1], ["b", {"__opaque__": "property"}],
                             ["c", {"__opaque__": "unset"}]]
    assert calls == []


def test_a_slots_dataclass_and_a_class_default_still_render():
    @dataclasses.dataclass
    class P:
        __slots__ = ("x", "y")
        x: int
        y: int

    p = P(1, 2)
    assert nz.normalize(p)["fields"] == [["x", 1], ["y", 2]]
    object.__delattr__(p, "y")
    assert nz.normalize(p)["fields"] == [["x", 1], ["y", {"__opaque__": "unset"}]]

    @dataclasses.dataclass
    class Q:
        z: int = 3

    q = Q.__new__(Q)                    # never initialised: ``z`` is the class default
    assert nz.normalize(q)["fields"] == [["z", 3]]


def test_a_bound_method_of_an_arbitrary_callable_is_not_asked_for_its_names():
    calls = []

    class Callable:
        def __call__(self, *a):
            return None

        def __getattr__(self, name):
            calls.append(name)
            raise AttributeError(name)

    m = types.MethodType(Callable(), object())
    assert nz.normalize(m) == {"__opaque__": "method"}

    def f():
        pass

    bound = types.MethodType(f, object())
    assert nz.normalize(bound) == {"__callable__": f.__module__ + "." + f.__qualname__}
    assert calls == []


def test_typename_reads_the_module_through_the_metatype():
    class Meta(type):                   # like Cython 3's metatype: its own ``__module__``
        @property
        def __module__(cls):
            return "pkg.mod"

    class K(metaclass=Meta):
        pass

    assert nz.typename(K()) == "pkg.mod." + K.__qualname__


class _View(set):
    """Keeps no items in the set storage: iterates something it holds (urllib3's header item
    view, werkzeug's ``CombinedMultiDict``)."""

    calls = []

    def __init__(self, source):
        set.__init__(self)
        self.source = source

    def __len__(self):
        self.calls.append("__len__")
        return len(self.source)

    def __iter__(self):
        self.calls.append("__iter__")
        return iter(self.source)


def test_a_container_subclass_with_its_own_len_renders_as_an_object():
    class Combined(dict):
        def __init__(self, dicts):
            dict.__init__(self)
            self.dicts = dicts

        def __len__(self):
            raise AssertionError("ran")

    class Lazy(list):
        def __init__(self):
            list.__init__(self)
            self._source = [1]

        def __len__(self):
            raise AssertionError("ran")

    del _View.calls[:]
    view = _View([("a", "1")])
    assert view.source and set.__len__(view) == 0
    out = nz.normalize(view)
    assert out == {"__object__": nz.typename(view),
                   "attrs": {"source": {"__list__": [{"__tuple__": ["a", "1"]}]}}}
    assert nz.normalize_input(view) == {"__opaque__": nz.typename(view)}
    assert nz.normalize(Combined([{"k": 1}]))["attrs"] == {
        "dicts": {"__list__": [{"__dict__": [["k", 1]]}]}}
    assert nz.normalize(Lazy()) == {"__opaque__": nz.typename(Lazy())}   # private state only
    assert _View.calls == []


def test_an_allow_listed_view_is_iterated_through_its_own_iter_only():
    ns = {"_View": _View}
    exec("class HTTPHeaderDictItemView(_View):\n    pass\n", ns)
    view_cls = ns["HTTPHeaderDictItemView"]
    view_cls.__module__ = "urllib3._collections"
    view_cls.__len__ = _View.__len__        # the allow-listed class defines ``__len__`` itself
    view_cls.__iter__ = _View.__iter__

    class Sub(view_cls):
        def __iter__(self):
            raise AssertionError("a subclass __iter__ ran")

    del _View.calls[:]
    pairs = [("Content-Type", "json"), ("X-Id", "1")]
    expected = {"__set__": [{"__tuple__": ["Content-Type", "json"]},
                            {"__tuple__": ["X-Id", "1"]}]}
    assert nz.normalize(view_cls(pairs)) == expected
    assert nz.normalize_input(Sub(pairs)) == expected
    assert _View.calls == ["__iter__", "__iter__"]


def test_a_type_is_never_hashed_so_an_unhashable_metaclass_still_renders():
    class Meta(type):
        def __eq__(cls, other):         # ``__hash__`` becomes None: ``hash(K)`` raises
            return type.__eq__(cls, other)

    class K(metaclass=Meta):
        def __init__(self):
            self.x = 1

    class S(str, metaclass=type("SMeta", (Meta,), {})):
        pass

    with pytest.raises(TypeError):
        hash(K)
    assert nz.normalize(K())["attrs"] == {"x": 1}
    assert nz.normalize(S("s")) == "s"
    assert nz.normalize_input(K()) == {"__opaque__": nz.typename(K())}


# ── end to end: the tracer leaves an access-tracking session untouched ──────────

PREFIX = "vfakesession"

DEP_SRC = '''
class CallbackDict(dict):
    """A dependency's dict with an update hook, like werkzeug's ``CallbackDict``."""

    def __init__(self, initial=None, on_update=None):
        dict.__init__(self, initial or ())
        self.on_update = on_update

    def store(self, key, value):
        dict.__setitem__(self, key, value)
        if self.on_update is not None:
            self.on_update(self)
        return len(self)


class Context(object):
    def __init__(self, session):
        self.session = session


def describe(mapping):
    return "mapping"


def push(session):
    return Context(session)
'''


@pytest.fixture
def dep():
    mod = types.ModuleType(PREFIX)
    exec(compile(DEP_SRC, f"<{PREFIX}>", "exec"), mod.__dict__)
    sys.modules[PREFIX] = mod

    class SecureSession(mod.CallbackDict):       # the project's subclass, like Flask's
        accessed = False

        def __getitem__(self, key):
            self.accessed = True
            return mod.CallbackDict.__getitem__(self, key)

        def get(self, key, default=None):
            self.accessed = True
            return dict.get(self, key, default)

    yield mod, SecureSession
    sys.modules.pop(PREFIX, None)


FAMILIES = ["monitoring", "setprofile"] if HAS_MONITORING else ["setprofile"]


def _backend(kind, sink):
    if kind == "monitoring":
        from pymolt.verify._monitoring import MonitoringBackend
        return MonitoringBackend(PREFIX, sink, source=__name__)
    return SetProfileBackend(PREFIX, sink, source=__name__)


@pytest.mark.parametrize("kind", FAMILIES)
def test_recording_a_session_does_not_mark_it_accessed(dep, kind):
    mod, SecureSession = dep
    sink = MemorySink()
    be = _backend(kind, sink)
    be.start()
    try:
        session = SecureSession({"user": "u1"})     # CallbackDict.__init__(self=session, ...)
        session.store("theme", "dark")              # self input: the session, with content
        mod.describe(session)                       # plain input
        ctx = mod.push(session)                     # result: an object holding the session
    finally:
        be.stop()
    assert session.accessed is False
    assert ctx.session is session
    recs = {r["qualname"].rsplit(".", 1)[-1]: r for r in sink.records.values()}
    assert {"store", "describe", "push"} <= set(recs)
    content = {"__dict__": [["theme", "dark"], ["user", "u1"]], "__order__": [1, 0]}
    assert recs["describe"]["inputs"]["bound"]["mapping"] == content
    assert recs["push"]["result"]["attrs"]["session"] == content


def test_flask_session_is_not_marked_accessed_under_the_tracer():
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    app.secret_key = "test"

    @app.route("/set")
    def set_session():
        flask.session["k"] = "v"
        return ""

    @app.route("/plain")
    def plain():
        return ""

    client = app.test_client()
    sink = MemorySink()
    for kind in FAMILIES:
        if kind == "monitoring":
            from pymolt.verify._monitoring import MonitoringBackend as Backend
        else:
            Backend = SetProfileBackend
        be = Backend("werkzeug", sink, source="flask")
        be.start()
        try:
            client.get("/set")
            rv = client.get("/plain")
        finally:
            be.stop()
        assert "Vary" not in rv.headers, kind
    assert sink.records

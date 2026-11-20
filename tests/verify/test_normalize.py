"""Normalization: cycles, opaque, nondeterminism markers, non-finite floats, truncation."""
from pymolt.verify import _normalize as nz


def test_safe_scalars_pass_through():
    assert nz.normalize(1) == 1
    assert nz.normalize("x") == "x"
    assert nz.normalize(True) is True
    assert nz.normalize(None) is None


def test_bytes_are_hex_encoded_stably():
    assert nz.normalize(b"\x00\xff") == {"__bytes__": "00ff"}


def test_non_finite_floats_are_opaque():
    assert nz.normalize(float("inf")) == {"__opaque__": "float-nonfinite"}
    assert nz.normalize(float("nan")) == {"__opaque__": "float-nonfinite"}
    assert nz.normalize(1.5) == 1.5  # finite floats pass through


def test_cycle_is_broken_not_infinite():
    d = {}
    d["self"] = d
    out = nz.normalize(d)
    assert out == {"__dict__": [["self", {"__cycle__": "dict"}]]}


def test_opaque_object_uses_typename_not_repr():
    class Widget:
        pass

    out = nz.normalize(Widget())
    assert out == {"__opaque__": out["__opaque__"]}
    assert out["__opaque__"].endswith(".Widget")


def test_depth_limit_collapses_to_opaque():
    deep = cur = {}
    for _ in range(nz.MAX_DEPTH + 2):
        cur["k"] = {}
        cur = cur["k"]
    # somewhere down the chain the value collapses to an opaque marker, not infinite nesting
    assert "__opaque__" in repr(nz.normalize(deep))


def test_sequence_truncation_is_marked():
    out = nz.normalize(list(range(nz.MAX_SEQ + 50)))
    assert out["__truncated__"] is True
    assert len(out["__list__"]) == nz.MAX_SEQ


def test_dicts_keep_sorted_pairs_and_record_insertion_order_beside_them():
    a = nz.normalize({"x": 1, "y": 2})
    b = nz.normalize({"y": 2, "x": 1})
    assert a == {"__dict__": [["x", 1], ["y", 2]]}  # sorted order: the fact is unchanged
    # items stay sorted by a stable key; the insertion order rides beside them
    assert b == {"__dict__": [["x", 1], ["y", 2]], "__order__": [1, 0]}


def test_dict_order_is_the_sorted_slot_of_each_inserted_key():
    out = nz.normalize({"c": 3, "a": 1, "b": 2})
    assert [p[0] for p in out["__dict__"]] == ["a", "b", "c"]
    assert out["__order__"] == [2, 0, 1]
    keys = [out["__dict__"][i][0] for i in out["__order__"]]
    assert keys == ["c", "a", "b"]


def test_dict_order_covers_dict_subclasses_and_nested_dicts():
    import collections
    od = collections.OrderedDict([("b", 1), ("a", 2)])
    assert nz.normalize(od)["__order__"] == [1, 0]
    dd = collections.defaultdict(int, [("z", 1), ("y", 2)])
    assert nz.normalize(dd)["__order__"] == [1, 0]
    nested = nz.normalize([{"q": 1, "p": 2}])
    assert nested["__list__"][0]["__order__"] == [1, 0]


def test_dict_order_only_for_two_to_sixty_four_keys():
    assert "__order__" not in nz.normalize({"a": 1})
    wide = dict((str(99 - i), i) for i in range(nz.MAX_ORDER_KEYS + 1))
    assert "__order__" not in nz.normalize(wide)
    edge = dict((str(99 - i), i) for i in range(nz.MAX_ORDER_KEYS))
    assert len(nz.normalize(edge)["__order__"]) == nz.MAX_ORDER_KEYS


def test_dict_order_can_be_turned_off_and_then_is_not_declared():
    saved = nz.DICT_ORDER[0]
    try:
        nz.DICT_ORDER[0] = False
        assert nz.normalize({"b": 1, "a": 2}) == {"__dict__": [["a", 2], ["b", 1]]}
        assert nz.value_format() == {}
        nz.DICT_ORDER[0] = True
        assert nz.value_format() == {"dict_order": 1}
    finally:
        nz.DICT_ORDER[0] = saved


def test_dict_order_is_never_recorded_under_shape_privacy():
    assert nz.normalize({"b": 1, "a": 2}, _privacy="shape") == {
        "__dict__": [[{"__shape__": "str"}, {"__shape__": "int"}],
                     [{"__shape__": "str"}, {"__shape__": "int"}]]}


def test_sets_are_normalized_and_sorted():
    assert nz.normalize({3, 1, 2}) == {"__set__": [1, 2, 3]}


def test_shape_privacy_never_persists_scalar_values(monkeypatch):
    monkeypatch.setenv("PYMOLT_TRACE_PRIVACY", "shape")

    out = nz.normalize({"token": "super-secret", "user_id": 42})

    rendered = repr(out)
    assert "super-secret" not in rendered
    assert "42" not in rendered
    assert "__shape__" in rendered


# ── value renderers (fact model v2) ───────────────────────────────────────────
import datetime  # noqa: E402
import decimal  # noqa: E402
import enum  # noqa: E402
import uuid  # noqa: E402

import pytest  # noqa: E402


def _both(value):
    return nz.normalize(value, _privacy="values"), nz.normalize(value, _privacy="shape")


def test_datetime_family_renders_iso_and_hides_it_under_shape():
    cases = [
        (datetime.datetime(2020, 1, 2, 3, 4, 5), "2020-01-02T03:04:05", "datetime.datetime"),
        (datetime.date(2020, 1, 2), "2020-01-02", "datetime.date"),
        (datetime.time(3, 4), "03:04:00", "datetime.time"),
    ]
    for value, iso, tname in cases:
        assert _both(value) == ({"__datetime__": iso}, {"__shape__": tname})


def test_datetime_subclass_uses_the_base_formatter():
    class Stamp(datetime.datetime):
        def isoformat(self, *a, **kw):   # a subclass override never runs
            raise AssertionError("user code ran")

    assert nz.normalize(Stamp(2020, 1, 1)) == {"__datetime__": "2020-01-01T00:00:00"}


def test_timedelta_decimal_uuid():
    u = uuid.UUID(int=7)
    assert _both(datetime.timedelta(minutes=1, microseconds=500000)) == (
        {"__timedelta__": 60.5}, {"__shape__": "datetime.timedelta"})
    assert _both(decimal.Decimal("1.10")) == (
        {"__decimal__": "1.10"}, {"__shape__": "decimal.Decimal"})
    assert _both(u) == ({"__uuid__": str(u)}, {"__shape__": "uuid.UUID"})


class Color(enum.Enum):
    RED = 1


class Perm(enum.Flag if hasattr(enum, "Flag") else enum.Enum):
    R = 1
    W = 2


def test_enum_members_are_structural():
    expected = {"__enum__": __name__ + ".Color.RED"}
    assert _both(Color.RED) == (expected, expected)


@pytest.mark.skipif(not hasattr(enum, "Flag"), reason="enum.Flag is 3.6+")
def test_composite_flag_without_a_name_is_named_by_value():
    out = nz.normalize(Perm.R | Perm.W)
    assert out["__enum__"].startswith(__name__ + ".Perm.")


def test_classes_and_callables_are_named():
    assert _both(Color) == ({"__type__": __name__ + ".Color"},) * 2
    assert nz.normalize(int) == {"__type__": "int"}
    assert nz.normalize(len) == {"__callable__": "len"}
    assert nz.normalize(str.upper) == {"__callable__": "str.upper"}
    assert nz.normalize(nz.normalize) == {
        "__callable__": "pymolt.verify._normalize.normalize"}
    assert nz.normalize(Response(1).__init__) == {
        "__callable__": __name__ + ".Response.__init__"}     # bound method


def test_dataclass_fields_render_in_order_and_at_shape():
    dataclasses = pytest.importorskip("dataclasses")
    ns = {"dataclasses": dataclasses, "__name__": "dcmod"}
    exec(
        "import typing\n"
        "@dataclasses.dataclass\n"
        "class Point:\n"
        "    x: int\n"
        "    y: str = 'a'\n"
        "    tag: typing.ClassVar[str] = 'skip'\n",
        ns,
    )
    values, shape = _both(ns["Point"](1, "b"))
    assert values == {"__dataclass__": "dcmod.Point", "fields": [["x", 1], ["y", "b"]]}
    assert shape == {"__dataclass__": "dcmod.Point",
                     "fields": [["x", {"__shape__": "int"}], ["y", {"__shape__": "str"}]]}


# Fakes of the allow-listed structures: recognised by defining module + class name only.
class ndarray:  # noqa: N801
    __module__ = "numpy"

    def __init__(self, data, dtype="int64"):
        self._data = list(data)
        self.dtype = dtype

    @property
    def shape(self):
        return (len(self._data),)

    def tolist(self):
        return list(self._data)


class _Seq:
    def __init__(self, items):
        self._items = list(items)

    def tolist(self):
        return list(self._items)


class DataFrame:
    __module__ = "pandas.core.frame"

    def __init__(self, columns):
        self._cols = columns

    @property
    def shape(self):
        rows = len(next(iter(self._cols.values()))) if self._cols else 0
        return (rows, len(self._cols))

    @property
    def dtypes(self):
        return _Seq("int64" for _ in self._cols)

    @property
    def columns(self):
        return _Seq(self._cols)

    def to_dict(self, orient):
        assert orient == "list"
        return {k: list(v) for k, v in self._cols.items()}


class Index:
    __module__ = "pandas.core.indexes.base"

    def __init__(self, data):
        self._data = list(data)
        self.dtype = "int64"

    @property
    def shape(self):
        return (len(self._data),)

    def tolist(self):
        return list(self._data)


class RangeIndex(Index):
    __module__ = "pandas.core.indexes.range"


def test_allow_listed_array_renders_shape_dtype_and_small_values():
    values, shape = _both(ndarray([1, 2, 3]))
    assert values == {"__structure__": "numpy.ndarray", "shape": [3], "dtype": "int64",
                      "values": {"__list__": [1, 2, 3]}}
    assert shape == {"__structure__": "numpy.ndarray", "shape": [3], "dtype": "int64"}


def test_large_structures_keep_shape_but_drop_values():
    out = nz.normalize(ndarray(range(nz.MAX_STRUCTURE_VALUES + 1)))
    assert out["shape"] == [nz.MAX_STRUCTURE_VALUES + 1] and "values" not in out


def test_frame_renders_dtypes_columns_and_values():
    values, shape = _both(DataFrame({"a": [1, 2], "b": [3, 4]}))
    assert values == {
        "__structure__": "pandas.core.frame.DataFrame", "shape": [2, 2],
        "dtypes": ["int64", "int64"], "columns": ["a", "b"],
        "values": {"__dict__": [["a", {"__list__": [1, 2]}], ["b", {"__list__": [3, 4]}]]},
    }
    assert "values" not in shape and shape["shape"] == [2, 2]
    # column names are values too: under shape only their types survive
    assert shape["columns"] == [{"__shape__": "str"}, {"__shape__": "str"}]


def test_structure_subclasses_are_recognised_through_the_mro():
    out = nz.normalize(RangeIndex([0, 1]))
    assert out["__structure__"] == "pandas.core.indexes.range.RangeIndex"
    assert out["values"] == {"__list__": [0, 1]}


def test_a_failing_structure_accessor_falls_back_to_opaque():
    class Broken(ndarray):
        @property
        def shape(self):
            raise RuntimeError("boom")

    out = nz.normalize(Broken([1]))
    assert out == {"__opaque__": out["__opaque__"]} and out["__opaque__"].endswith("Broken")


def test_real_numpy_and_pandas_when_available():
    np = pytest.importorskip("numpy")
    pd = pytest.importorskip("pandas")
    arr = nz.normalize(np.arange(3))
    assert arr["shape"] == [3] and arr["values"] == {"__list__": [0, 1, 2]}
    frame = nz.normalize(pd.DataFrame({"a": [1, 2]}))
    assert frame["shape"] == [2, 1] and frame["columns"] == ["a"]
    assert frame["values"] == {"__dict__": [["a", {"__list__": [1, 2]}]]}
    series = nz.normalize(pd.Series([1.5]), _privacy="shape")
    assert series["dtype"] == "float64" and "values" not in series


class Response:
    def __init__(self, code):
        self.status_code = code


def test_status_code_objects_render_their_code_even_under_shape():
    expected = {"__structure__": __name__ + ".Response", "status_code": 404}
    assert _both(Response(404)) == (expected, expected)
    # a non-int status_code is not the structure marker; the generic renderer shows it as an attr
    assert nz.normalize(Response("404")) == {
        "__object__": __name__ + ".Response", "attrs": {"status_code": "404"}}
    assert nz.normalize(Response(True)) == {
        "__object__": __name__ + ".Response", "attrs": {"status_code": True}}


def test_werkzeug_status_property_is_read_but_other_properties_are_not():
    ns = {"__name__": "werkzeug.sansio.response"}
    exec(
        "class Response:\n"
        "    def __init__(self):\n"
        "        self._status_code = 201\n"
        "    @property\n"
        "    def status_code(self):\n"
        "        return self._status_code\n",
        ns,
    )
    assert nz.normalize(ns["Response"]())["status_code"] == 201

    calls = []

    class Lazy:
        @property
        def status_code(self):          # arbitrary user code: must not run
            calls.append(1)
            return 200

    assert nz.normalize(Lazy())["__opaque__"].endswith(".Lazy")
    assert calls == []


def test_rendering_never_runs_attribute_hooks():
    touched = []

    class Spy:
        def __getattribute__(self, name):
            touched.append(name)
            return object.__getattribute__(self, name)

        def __getattr__(self, name):
            touched.append("getattr:" + name)
            raise AttributeError(name)

    class Slotted:
        __slots__ = ("status_code",)

    out = nz.normalize(Spy())
    assert list(out) == ["__opaque__"] and out["__opaque__"].endswith(".Spy")
    assert touched == []    # not even ``isinstance``'s ``__class__`` probe (a proxy resolves there)
    assert nz.normalize(Slotted())["__opaque__"].endswith(".Slotted")


# ── generic structural object renderer ───────────────────────────────────────

def _attrs_of(out):
    assert set(out) - {"__truncated__"} == {"__object__", "attrs"}, out
    return out["attrs"]


def test_plain_object_renders_public_instance_state():
    class Result:
        def __init__(self):
            self.exit_code = 2
            self.output = "boom"
            self._private = "hidden"

    out = nz.normalize(Result())
    assert out["__object__"].endswith("Result")
    assert _attrs_of(out) == {"exit_code": 2, "output": "boom"}
    assert "__truncated__" not in out


def test_object_values_are_normalized_recursively():
    class Box:
        def __init__(self):
            self.items = [1, 2]
            self.when = None

    assert _attrs_of(nz.normalize(Box())) == {"items": {"__list__": [1, 2]}, "when": None}


def test_object_without_state_stays_opaque():
    class Empty:
        pass

    assert "__opaque__" in nz.normalize(Empty())


def test_slots_object_reads_member_descriptors_and_skips_unset():
    class Slotted:
        __slots__ = ("a", "b", "_hidden")

        def __init__(self):
            self.a = 1
            self._hidden = 3          # b stays unset

    assert _attrs_of(nz.normalize(Slotted())) == {"a": 1}


def test_property_and_getattr_are_never_executed():
    calls = []

    class Trap:
        def __init__(self):
            self.real = 1

        @property
        def boom(self):
            calls.append("property")
            raise RuntimeError("property ran")

        def __getattr__(self, name):
            calls.append("getattr:" + name)
            raise RuntimeError("getattr ran")

        def __repr__(self):
            calls.append("repr")
            raise RuntimeError("repr ran")

        def __eq__(self, other):
            calls.append("eq")
            raise RuntimeError("eq ran")

        __hash__ = object.__hash__

    out = nz.normalize(Trap())
    assert _attrs_of(out) == {"real": 1}
    assert calls == []


def test_class_level_descriptor_is_not_run_for_slots_class():
    calls = []

    class Desc(object):
        def __get__(self, obj, owner):
            calls.append("get")
            raise RuntimeError("descriptor ran")

    class WithDesc:
        __slots__ = ("x", "__dict__")
        d = Desc()

        def __init__(self):
            self.x = 5
            self.y = 6

    assert _attrs_of(nz.normalize(WithDesc())) == {"x": 5, "y": 6}
    assert calls == []


def test_attrs_like_class_uses_declared_field_names():
    class Attribute(object):
        def __init__(self, name):
            self.name = name

    class Point:
        __attrs_attrs__ = (Attribute("x"), Attribute("_y"))

        def __init__(self):
            self.x = 1
            self._y = 2
            self.extra = 3

    assert _attrs_of(nz.normalize(Point())) == {"x": 1, "_y": 2}


def test_pydantic_v2_like_fields_from_dict():
    class Model:
        __pydantic_fields__ = {"name": object(), "age": object()}

        def __init__(self):
            self.name = "a"
            self.age = 3
            self.__dict__["__pydantic_extra__"] = None

    out = nz.normalize(Model())
    assert _attrs_of(out) == {"name": "a", "age": 3}


def test_pydantic_v1_like_fields_from_dict():
    class Model:
        __fields__ = {"name": object(), "age": object()}

        def __init__(self):
            self.name = "a"
            self.age = 4

        @property
        def dict(self):
            raise RuntimeError("must not run")

    assert _attrs_of(nz.normalize(Model())) == {"name": "a", "age": 4}


def test_namedtuple_renders_as_object_with_field_names():
    from collections import namedtuple

    P = namedtuple("P", "x y")
    out = nz.normalize(P(1, "z"))
    assert out["__object__"].endswith("P")
    assert out["attrs"] == {"x": 1, "y": "z"}
    # a plain tuple is unchanged
    assert nz.normalize((1, 2)) == {"__tuple__": [1, 2]}


def test_object_cycle_uses_cycle_marker():
    class Node:
        pass

    n = Node()
    n.me = n
    n.val = 1
    out = nz.normalize(n)
    assert out["attrs"]["me"]["__cycle__"].endswith("Node")
    assert out["attrs"]["val"] == 1


def test_object_attr_count_cap_marks_truncated():
    class Wide:
        pass

    w = Wide()
    for i in range(nz.MAX_SEQ + 5):
        setattr(w, "a%04d" % i, i)
    out = nz.normalize(w)
    assert out["__truncated__"] is True
    assert len(out["attrs"]) == nz.MAX_SEQ


def test_object_depth_cap_marks_truncated():
    class Link:
        pass

    head = cur = Link()
    for _ in range(nz.MAX_DEPTH + 2):
        cur.next = Link()
        cur = cur.next
    out = nz.normalize(head)
    inner = out
    while "next" in inner["attrs"] and "__object__" in inner["attrs"]["next"]:
        inner = inner["attrs"]["next"]
    assert inner["__truncated__"] is True
    assert "__opaque__" in inner["attrs"]["next"]


def test_object_budget_bounds_wide_graph():
    class N:
        pass

    def build(d):
        n = N()
        if d:
            for i in range(30):
                setattr(n, "c%d" % i, build(d - 1))
        else:
            n.leaf = 1
        return n

    out = nz.normalize(build(3))
    assert out["__object__"].endswith("N")
    rendered, cut = [], []

    def walk(v):
        if isinstance(v, dict):
            if "__object__" in v:
                (rendered if v.get("attrs") else cut).append(v)
            for x in v.values():
                walk(x)

    walk(out)
    # 27,931 objects in the graph: exactly the budget is rendered, the rest are type-level
    assert len(rendered) == nz.MAX_OBJECTS
    assert cut and all(c["__truncated__"] is True and c["attrs"] == {} for c in cut)


def test_status_code_attr_still_renders_structure():
    class Resp:
        status_code = 200

    assert nz.normalize(Resp()) == {"__structure__": nz.typename(Resp()), "status_code": 200}


def test_status_code_non_int_falls_back_to_object():
    class Resp:
        status_code = "weird"

        def __init__(self):
            self.body = "x"

    assert _attrs_of(nz.normalize(Resp())) == {"body": "x"}


def test_object_shape_privacy_hides_values():
    class R:
        def __init__(self):
            self.code = 2

    out = nz.normalize(R(), _privacy="shape")
    assert out["attrs"] == {"code": {"__shape__": "int"}}


def test_normalize_input_keeps_objects_opaque_but_results_render():
    from collections import namedtuple

    class Plain:
        def __init__(self):
            self.a = 1

    out = nz.normalize_input(Plain())
    assert out == {"__opaque__": nz.typename(Plain())}
    assert "__object__" in nz.normalize(Plain())
    # nested generic objects stay opaque too; builtins are unchanged
    assert nz.normalize_input([Plain(), 1]) == {"__list__": [out, 1]}
    P = namedtuple("P", "x")
    assert nz.normalize_input(P(1)) == {"__tuple__": [1]}
    assert nz.normalize_input(Response(404)) == nz.normalize(Response(404))

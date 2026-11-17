"""Normalize runtime values into a stable, comparable, JSON-safe form.

INJECTED RUNTIME — pure stdlib, Python 3.6+. This module is loaded *inside the
target application's interpreter* (which may be Python 3.6 in the flasgger
devcontainer), so it must import nothing beyond the standard library: no pydantic,
no ``pymolt.*``. Do not add ``from __future__ import annotations`` (PEP 563 is 3.7+),
walrus, or PEP 604 ``X | Y`` unions in runtime positions.

Principle: capture what is observable and deterministic. Opaque or unstable values
collapse to a *typed honesty marker* rather than a raw ``repr`` (which would carry
memory addresses and cause false-positive diffs). This same normalization is shared
by the boundary tracer (L3) and the golden-master snapshots (L2) so a value compared
in one is represented identically in the other.

Markers emitted:
    {"__dict__": [[k, v], ...]}       a dict, its pairs sorted by key (+ "__order__": the
                                      sorted slot of each key in insertion order, only when
                                      that order is not the sorted one, for 2..64 keys)
    {"__bytes__": "<hex>"}            bytes, stably encoded
    {"__opaque__": "<typename>"}      non-serializable / too-deep / non-finite float
    {"__cycle__": "<typename>"}       reference cycle broken here
    {"__nondeterministic__": true}    set by the sinks when identical inputs differ
    {"__opaque__": "<label>"}         an ``OutcomeMarker`` passed where a value is expected
                                      (e.g. "ambiguous:return-or-raise" on <=3.7 interpreters)

Value renderers (fact model v2), tried before the opaque fallback — values / privacy=shape:
    datetime/date/time  {"__datetime__": iso}           / {"__shape__": type}
    timedelta           {"__timedelta__": seconds}      / {"__shape__": type}
    Decimal             {"__decimal__": str}            / {"__shape__": type}
    UUID                {"__uuid__": str}               / {"__shape__": type}
    Enum member         {"__enum__": "mod.Cls.NAME"}    (structural: same under shape)
    class / function    {"__type__": ..} / {"__callable__": ..}   (same under shape)
    dataclass instance  {"__dataclass__": type, "fields": [[name, value], ...]}
    numpy ndarray, pandas Series/DataFrame/Index (allow-listed by package + class name)
                        {"__structure__": type, "shape", "dtype"|"dtypes"+"columns",
                         "values" (only when size <= 200; never under shape)}
    int ``status_code`` {"__structure__": type, "status_code": n}  (same under shape)
    generic object      {"__object__": "mod.Qual", "attrs": {name: value, ...}}
                        (+ "__truncated__"): public instance ``__dict__`` entries, ``__slots__``
                        member descriptors, attrs / pydantic v1+v2 fields and namedtuple
                        fields; reads state only, never a property, ``__getattr__``,
                        ``__repr__`` or ``__eq__``
Rendering is duck-typed and never imports anything: a stdlib type is recognised only if its
module is already in ``sys.modules`` (no value of it can exist otherwise). It never runs user
code beyond the allow-listed structures' cheap accessors (``shape``, ``dtype(s)``, ``columns``,
``tolist()``/``to_dict("list")``) and werkzeug's ``status_code`` property: types are classified
once per type through raw class namespaces, instance state is read through the type's own
``__dict__`` slot, stdlib values are formatted through the BASE class methods, and every
renderer is guarded (a failure falls back to the opaque marker).
"""
import binascii
import json
import os
import sys
import types


class OutcomeMarker(object):
    """A typed honesty marker handed to a sink in place of a real value.

    It normalizes to ``{"__opaque__": label}``, which the host-side diff already treats as
    undecidable (compared by neither value nor shape) — so an outcome the runtime cannot
    classify is surfaced instead of being guessed as ``None``.
    """

    __slots__ = ("label",)

    def __init__(self, label):
        self.label = label


# A profile ``return`` event with ``arg is None`` whose frame stopped on a try/finally or
# ``with`` cleanup opcode: on <=3.7 a normal ``return None`` and an exception unwinding
# through the same block end on the same opcode.
AMBIGUOUS_OUTCOME = OutcomeMarker("ambiguous:return-or-raise")

_SAFE_SCALARS = (type(None), bool, int, float, str, bytes)

# Generous limits: the environment is CI/staging where overhead is tolerable. These
# are a guard against infinite / cyclic structures, not a means of saving cycles.
MAX_DEPTH = 6
MAX_SEQ = 200
# Generic objects rendered per top-level ``normalize`` call: depth x width alone would let a
# deep object graph (an ORM session, an app) explode; past this the rest are type-level.
MAX_OBJECTS = 64            # generic objects rendered per value: a result is a witness, not a dump
MAX_OBJECT_DEPTH = 3        # nested generic objects: deeper ones stay type-level
_BUDGET = [MAX_OBJECTS]
# Dict key insertion order (value format ``dict_order`` 1): a dict of 2..MAX_ORDER_KEYS keys
# whose insertion order is not its sorted order carries ``"__order__": [sorted slot of the
# j-th inserted key, ...]`` next to its (still sorted) ``__dict__``. A dict in sorted order
# keeps its fact byte for byte; a wider dict records no order ("unobserved" on the host).
# ``PYMOLT_TRACE_DICT_ORDER=0`` turns it off, and the capture then does not declare it.
MAX_ORDER_KEYS = 64
DICT_ORDER = [os.environ.get("PYMOLT_TRACE_DICT_ORDER", "1").strip().lower()
              not in ("0", "false", "off", "no")]
VALUE_FORMAT_DICT_ORDER = 1


def value_format():
    """The value format this runtime writes, for the capture metadata."""
    return {"dict_order": VALUE_FORMAT_DICT_ORDER} if DICT_ORDER[0] else {}


def typename(value):
    """Stable type label, e.g. ``flask.app.Flask`` or ``int``."""
    t = type(value)
    mod = getattr(t, "__module__", "")
    if mod and mod != "builtins":
        return "{0}.{1}".format(mod, t.__qualname__)
    return t.__qualname__


def normalize(value, depth=0, _seen=frozenset(), _privacy=None, _objects=True):
    # type: (Any, int, frozenset) -> Any
    """Reduce ``value`` to a deterministic JSON-compatible structure.

    Cycles are broken via ``id()`` tracking in ``_seen``. Anything not safely
    representable becomes ``{"__opaque__": typename}`` — never a raw repr.
    ``_objects=False`` turns the generic object / namedtuple renderer off (see ``normalize_input``).
    """
    if depth == 0:
        _BUDGET[0] = MAX_OBJECTS
    if _privacy is None:
        _privacy = os.environ.get("PYMOLT_TRACE_PRIVACY", "values").strip().lower()

    if isinstance(value, _SAFE_SCALARS):
        if _privacy == "shape":
            return {"__shape__": typename(value)}
        if isinstance(value, bytes):
            # binascii, not bytes.hex(): the latter is 3.5+ and the runtime must load on 3.4.
            return {"__bytes__": binascii.hexlify(value).decode("ascii")}
        if isinstance(value, float):
            # NaN / +-inf are not JSON-stable and not meaningfully comparable.
            if value != value or value in (float("inf"), float("-inf")):
                return {"__opaque__": "float-nonfinite"}
        return value

    if isinstance(value, OutcomeMarker):
        return {"__opaque__": value.label}

    if depth >= MAX_DEPTH:
        return {"__opaque__": typename(value)}

    vid = id(value)
    if vid in _seen:
        return {"__cycle__": typename(value)}
    seen = _seen | {vid}

    if (_objects and isinstance(value, tuple) and type(value) is not tuple
            and _kind_of(type(value))[0] == "namedtuple"):
        try:
            return _render_namedtuple(value, _kind_of(type(value)), depth, seen, _privacy, _objects)
        except Exception:
            pass

    if isinstance(value, (list, tuple)):
        seq = [
            normalize(v, depth + 1, seen, _privacy, _objects)
            for v in list(value)[:MAX_SEQ]
        ]
        tag = "tuple" if isinstance(value, tuple) else "list"
        out = {"__{0}__".format(tag): seq}
        if len(value) > MAX_SEQ:
            out["__truncated__"] = True
        return out

    if isinstance(value, dict):
        items = []
        for k in list(value.keys())[:MAX_SEQ]:
            if isinstance(k, _SAFE_SCALARS):
                nk = normalize(k, depth + 1, seen, _privacy, _objects)
            else:
                nk = {"__opaque__": typename(k)}
            items.append([nk, normalize(value[k], depth + 1, seen, _privacy, _objects)])
        n = len(items)
        if n < 2 or n > MAX_ORDER_KEYS or not DICT_ORDER[0] or _privacy == "shape":
            items.sort(key=_stable_key)
            return {"__dict__": items}
        # Stable sort of insertion positions: equal renderings keep their insertion order,
        # exactly as ``items.sort`` does, so the sorted pairs are today's.
        skeys = [_stable_key(it) for it in items]
        idx = sorted(range(n), key=skeys.__getitem__)
        out = {"__dict__": [items[i] for i in idx]}
        if idx != list(range(n)):
            pos = [0] * n
            for s, j in enumerate(idx):
                pos[j] = s
            out["__order__"] = pos      # sorted slot of the j-th inserted key
        return out

    if isinstance(value, (set, frozenset)):
        norm = sorted(
            (
                normalize(v, depth + 1, seen, _privacy, _objects)
                for v in list(value)[:MAX_SEQ]
            ),
            key=_stable_key,
        )
        return {"__set__": norm}

    try:
        rendered = _render(value, depth, seen, _privacy, _objects)
    except Exception:
        rendered = None
    if rendered is not None:
        return rendered
    return {"__opaque__": typename(value)}


def normalize_input(value, depth=0, _seen=frozenset(), _privacy=None):
    """``normalize`` for recorded call INPUTS: generic library objects stay type-level
    (``__opaque__`` / ``status_code``) — no structural object or namedtuple rendering, which
    costs time per call and ties fact keys to library internals. Everything else is identical."""
    return normalize(value, depth, _seen, _privacy, False)


# ── value renderers ──────────────────────────────────────────────────────────

# Structures are rendered with values only up to this many elements (rows x columns).
MAX_STRUCTURE_VALUES = 200

# C-level accessors: never intercepted by a metaclass ``__getattribute__``.
_TYPE_DICT = type.__dict__["__dict__"]
_TYPE_MODULE = type.__dict__["__module__"]
_TYPE_NAME = type.__dict__["__name__"]
_TYPE_QUALNAME = type.__dict__["__qualname__"]
_TYPE_MRO = type.__dict__["__mro__"]

_CALLABLE_TYPES = frozenset((
    types.FunctionType, types.MethodType, types.BuiltinFunctionType,
    type(str.upper),            # method_descriptor
    type(object.__init__),      # wrapper_descriptor
    type((1).__add__),          # method-wrapper
    type(dict.__dict__["fromkeys"]),  # classmethod_descriptor
))

# (top-level package, class name) -> structure kind, matched against every class in the MRO so
# subclasses (RangeIndex, DatetimeIndex, np.matrix, GeoDataFrame, ...) are covered. The top-level
# package, not the defining module: pandas moved these classes between modules over the years
# and recent releases re-home ``__module__`` to plain ``pandas``.
_STRUCTURES = {
    ("numpy", "ndarray"): "array",
    ("pandas", "Series"): "series",
    ("pandas", "DataFrame"): "frame",
    ("pandas", "Index"): "index",
}

# Packages whose ``status_code`` PROPERTY may be evaluated (a trivial attribute read there).
# Any other property is user code and is not called.
_STATUS_PROPERTY_PACKAGES = frozenset(("werkzeug",))

_MISSING = object()
_OPAQUE = ("opaque",)
_KINDS = {}            # id(type) -> (type, kind): classification is once per type
_KINDS_MAX = 4096


def _class_dict(cls):
    return _TYPE_DICT.__get__(cls, type)


def _class_label(cls):
    """``mod.Qual`` of a class (bare qualname for builtins), via C accessors only."""
    mod = _TYPE_MODULE.__get__(cls, type)
    qual = _TYPE_QUALNAME.__get__(cls, type)
    if isinstance(mod, str) and mod and mod != "builtins":
        return mod + "." + qual
    return qual


def _callable_label(fn):
    mod = getattr(fn, "__module__", None)
    if mod is None:
        owner = getattr(fn, "__objclass__", None)
        if owner is not None:
            mod = _TYPE_MODULE.__get__(owner, type)
    qual = getattr(fn, "__qualname__", None) or getattr(fn, "__name__", None)
    if not isinstance(qual, str) or not qual:
        return None
    if isinstance(mod, str) and mod and mod != "builtins":
        return mod + "." + qual
    return qual


def _stdlib_class(modname, attr):
    mod = sys.modules.get(modname)
    return getattr(mod, attr, None) if mod is not None else None


def _classify(t):
    """The renderer kind for type ``t`` (computed once per type)."""
    if issubclass(t, type):
        return ("class",)
    if t in _CALLABLE_TYPES:
        return ("callable",)
    dt = sys.modules.get("datetime")
    if dt is not None:
        # Base-class formatters: a subclass override (or pandas' own) never runs.
        if issubclass(t, dt.datetime):
            return ("datetime", dt.datetime.isoformat)
        if issubclass(t, dt.date):
            return ("datetime", dt.date.isoformat)
        if issubclass(t, dt.time):
            return ("datetime", dt.time.isoformat)
        if issubclass(t, dt.timedelta):
            return ("timedelta", dt.timedelta.total_seconds)
    dec = _stdlib_class("decimal", "Decimal")
    if dec is not None and issubclass(t, dec):
        return ("decimal", dec.__str__)
    uuid_cls = _stdlib_class("uuid", "UUID")
    if uuid_cls is not None and issubclass(t, uuid_cls):
        return ("uuid", uuid_cls.__str__)
    enum_cls = _stdlib_class("enum", "Enum")
    if enum_cls is not None and issubclass(t, enum_cls):
        return ("enum",)
    mro = _TYPE_MRO.__get__(t, type)
    if issubclass(t, tuple) and t is not tuple:
        for cls in mro:
            names = _class_dict(cls).get("_fields")
            if type(names) is tuple and all(type(n) is str for n in names):
                return ("namedtuple", names[:MAX_SEQ])
    for cls in mro:
        fields = _class_dict(cls).get("__dataclass_fields__")
        if fields is not None:
            names = []
            for name, field in list(fields.items()):
                ftype = getattr(field, "_field_type", None)
                if getattr(ftype, "name", "_FIELD") in ("_FIELD_CLASSVAR", "_FIELD_INITVAR"):
                    continue
                names.append(name)
            return ("dataclass", tuple(names[:MAX_SEQ]), _dict_slot(mro))
    for cls in mro:
        owner = _TYPE_MODULE.__get__(cls, type)
        top = owner.split(".", 1)[0] if isinstance(owner, str) else None
        kind = _STRUCTURES.get((top, _TYPE_NAME.__get__(cls, type)))
        if kind is not None:
            return ("structure", kind)
    for cls in mro:
        ns = _class_dict(cls)
        if "status_code" in ns:
            attr = ns["status_code"]
            if issubclass(type(attr), property):
                fget = attr.fget
                owner = getattr(fget, "__module__", "") or ""
                if (type(fget) is types.FunctionType
                        and owner.split(".", 1)[0] in _STATUS_PROPERTY_PACKAGES):
                    return ("status_prop", fget)
                return _OPAQUE
            slot = _dict_slot(mro)
            if slot is not None:
                return ("status_attr", attr, slot, _object_info(t, mro))
            break
    info = _object_info(t, mro)
    if info is None:
        return _OPAQUE
    slot = info[1]
    return ("status_attr", _MISSING, slot, info) if slot is not None else info


def _field_names(ns):
    """Field names declared by a class namespace for attrs / pydantic v2 / pydantic v1, or None."""
    attrs = ns.get("__attrs_attrs__")
    if type(attrs) is tuple:
        names = [getattr(a, "name", None) for a in attrs]
        if all(type(n) is str for n in names):
            return names
    for key in ("__pydantic_fields__", "__fields__"):
        fields = ns.get(key)
        if type(fields) is dict:
            names = [n for n in fields if type(n) is str]
            return names
    return None


def _object_info(t, mro):
    """``("object", dict_slot, fields, members)`` for a generic instance, or None.

    ``fields`` is the declared field list (attrs / pydantic) or None; ``members`` maps public
    ``__slots__`` names to their C member descriptors."""
    if not (t.__flags__ & 512):    # Py_TPFLAGS_HEAPTYPE: Python-defined classes only; a static
        return None                # C type (generator, code, frame, ...) stays opaque
    fields = None
    members = {}
    for cls in mro:
        ns = _class_dict(cls)
        if fields is None:
            fields = _field_names(ns)
        for name, desc in list(ns.items()):
            if (type(desc) is types.MemberDescriptorType and type(name) is str
                    and name not in members):
                members[name] = desc
    slot = _dict_slot(mro)
    if slot is None and not members:
        return None
    if fields is not None:
        fields = tuple(fields[:MAX_SEQ])
    return ("object", slot, fields, members)


def _dict_slot(mro):
    """The C ``__dict__`` getset of instances of this MRO, or None (slots-only, or a class
    that redefines ``__dict__`` in Python — reading it would run user code)."""
    for cls in mro:
        ns = _class_dict(cls)
        if "__dict__" in ns:
            desc = ns["__dict__"]
            if _TYPE_NAME.__get__(type(desc), type) == "getset_descriptor":
                return desc
            return None
    return None


def _kind_of(t):
    ent = _KINDS.get(id(t))
    if ent is not None and ent[0] is t:
        return ent[1]
    try:
        kind = _classify(t)
    except Exception:
        kind = _OPAQUE
    if len(_KINDS) >= _KINDS_MAX:
        _KINDS.clear()
    _KINDS[id(t)] = (t, kind)   # holds the type: its id cannot be recycled while cached
    return kind


def _render(value, depth, seen, privacy, objects=True):
    """Typed rendering of a known non-container value, or None (-> opaque)."""
    kind = _kind_of(type(value))
    k = kind[0]
    if k == "opaque":
        return None
    shape = privacy == "shape"
    if k == "class":
        return {"__type__": _class_label(value)}
    if k == "callable":
        label = _callable_label(value)
        return {"__callable__": label} if label else None
    if k == "datetime":
        return {"__shape__": typename(value)} if shape else {"__datetime__": kind[1](value)}
    if k == "timedelta":
        return {"__shape__": typename(value)} if shape else {"__timedelta__": kind[1](value)}
    if k == "decimal":
        return {"__shape__": typename(value)} if shape else {"__decimal__": kind[1](value)}
    if k == "uuid":
        return {"__shape__": typename(value)} if shape else {"__uuid__": kind[1](value)}
    if k == "enum":
        return _render_enum(value)
    if k == "dataclass":
        return _render_dataclass(value, kind, depth, seen, privacy, objects)
    if k == "structure":
        return _render_structure(value, kind[1], depth, seen, privacy, objects)
    if k == "object":
        if not objects or depth >= MAX_OBJECT_DEPTH:
            return None
        return _render_object(value, kind, depth, seen, privacy, objects)
    if k == "status_attr" or k == "status_prop":
        out = _render_status(value, kind)
        if out is None and objects and k == "status_attr" and kind[3] is not None:
            return _render_object(value, kind[3], depth, seen, privacy, objects)
        return out
    return None


def _render_enum(value):
    state = object.__getattribute__(value, "__dict__")
    name = state.get("_name_")
    if not isinstance(name, str):
        member = state.get("_value_")      # an unnamed composite Flag: name it by value
        if type(member) is not int:
            return None
        name = "(" + str(member) + ")"
    return {"__enum__": typename(value) + "." + name}


def _render_dataclass(value, kind, depth, seen, privacy, objects=True):
    slot = kind[2]
    state = slot.__get__(value, type(value)) if slot is not None else None
    fields = []
    for name in kind[1]:
        if type(state) is dict and name in state:
            item = state[name]
        else:
            try:
                item = object.__getattribute__(value, name)   # __slots__ dataclasses
            except Exception:
                fields.append([name, {"__opaque__": "unset"}])
                continue
        fields.append([name, normalize(item, depth + 1, seen, privacy, objects)])
    return {"__dataclass__": typename(value), "fields": fields}


def _render_structure(value, kind, depth, seen, privacy, objects=True):
    dims = [int(n) for n in value.shape]
    out = {"__structure__": typename(value), "shape": dims}
    size = 1
    for n in dims:
        size *= n
    with_values = privacy != "shape" and size <= MAX_STRUCTURE_VALUES
    if kind == "frame":
        out["dtypes"] = [str(d) for d in value.dtypes.tolist()[:MAX_SEQ]]
        out["columns"] = [normalize(c, depth + 1, seen, privacy, objects)
                          for c in value.columns.tolist()[:MAX_SEQ]]
        if with_values:
            values = normalize(value.to_dict("list"), depth + 1, seen, privacy, objects)
            if isinstance(values, dict):
                values.pop("__order__", None)   # ``columns`` already holds the column order
            out["values"] = values
    else:
        out["dtype"] = str(value.dtype)
        if with_values:
            out["values"] = normalize(value.tolist(), depth + 1, seen, privacy, objects)
    return out


def _render_object(value, kind, depth, seen, privacy, objects=True):
    """Structural rendering of an instance from its raw state — no user code runs."""
    t = type(value)
    out = {"__object__": _class_label(t), "attrs": {}}
    if _BUDGET[0] <= 0:
        out["__truncated__"] = True
        return out
    _BUDGET[0] -= 1
    slot, fields, members = kind[1], kind[2], kind[3]
    state = slot.__get__(value, t) if slot is not None else None
    if type(state) is not dict:
        state = {}
    found = []
    if fields is not None:
        names = list(fields)
    else:
        names = [n for n in state if type(n) is str and not n.startswith("_")]
        names.extend(n for n in members if not n.startswith("_") and n not in state)
        names.sort()
    if len(names) > MAX_SEQ:
        names = names[:MAX_SEQ]
        out["__truncated__"] = True
    for name in names:
        if name in state:
            item = state[name]
        else:
            desc = members.get(name)
            if desc is None:
                continue
            try:
                item = desc.__get__(value, t)
            except AttributeError:
                continue            # unset slot
        found.append((name, item))
    if fields is None and not found:
        return None                 # nothing observable: stays an opaque, type-level value
    attrs = out["attrs"]
    for name, item in found:
        attrs[name] = normalize(item, depth + 1, seen, privacy, objects)
        if (depth + 1 >= MAX_DEPTH and not isinstance(item, _SAFE_SCALARS)):
            out["__truncated__"] = True
    return out


def _render_namedtuple(value, kind, depth, seen, privacy, objects=True):
    out = {"__object__": _class_label(type(value)), "attrs": {}}
    names = kind[1]
    if len(value) > MAX_SEQ:
        out["__truncated__"] = True
    for i, name in enumerate(names[:len(value)]):
        item = tuple.__getitem__(value, i)
        out["attrs"][name] = normalize(item, depth + 1, seen, privacy, objects)
        if depth + 1 >= MAX_DEPTH and not isinstance(item, _SAFE_SCALARS):
            out["__truncated__"] = True
    return out


def _render_status(value, kind):
    if kind[0] == "status_prop":
        code = kind[1](value)
    else:
        state = kind[2].__get__(value, type(value))
        code = state.get("status_code", _MISSING) if type(state) is dict else _MISSING
        if code is _MISSING:
            code = kind[1]
    t = type(code)
    if t is bool or not issubclass(t, int):
        return None
    return {"__structure__": typename(value), "status_code": int(code)}


def _stable_key(obj):
    return json.dumps(obj, sort_keys=True, default=str)

"""Successors: naming them from the library's own text and data.

HOST side, pure over probe answers. The probe (``delta/probe.py``) returns raw facts: warn,
raise and log texts (a message variable followed one step), the warning helpers a body calls
one level down, the decorator calls with their literal arguments, and the closure cells of each
wrapper. This module reads them:

- :func:`candidates_from` turns texts into candidate names, each with a role: ``successor``
  ("use X instead", "renamed to X"), ``alternative`` ("X or Y", two quoted callables) or
  ``param`` ("use ``errorbar=`` ...", "the parameter ... renamed to 'download_name'"). It never
  picks: the engine keeps a single successor only when the candidates that pass the filters
  name one object; several are an agent's or a person's choice;
- :func:`helper_facts` reads the decorator calls and closure cells against the registered
  deprecation helpers (``knowledge/deprecation_helpers.json``): a rename helper's data
  (``deprecate_renamed_parameter("periods", "n")``) is the library's own statement of a rename,
  a structured hint (``_api.deprecated(alternative=...)``) names a successor;
- :func:`attributed` is the attribution rule: a warning recorded at a call site is evidence
  about the symbol only when its text names the symbol (or one of its parameters) or matches a
  warning template found in the symbol's own code, decorators or helpers. Any other warning is
  a pitfall of the site (seaborn's ``factorplot`` call warns about numpy's ``result_type``).

Prose and text only *name* a successor; nothing here makes a rewrite.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

# ── candidates from text ─────────────────────────────────────────────────────────────────

_NAME = r"[A-Za-z_][\w\.]*"
_QUOTES = "`'\"*"
_OPEN = r"(?::\w+:)?[`'\"*]*~?@?\.?"
_ARGS = r"(?:\([^()]{0,60}\))?"

# Successor prose, most specific first. Each captures the name once (group "n").
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"[Ii]mport\s+" + _OPEN + r"(?P<n>" + _NAME + r")[`'\"*]*\s+instead"),
     "successor"),
    (re.compile(r"\b[Uu]se\s+(?:the\s+)?(?:new\s+)?(?:command\s+|function\s+|method\s+)?"
                + _OPEN + r"(?P<n>" + _NAME + r")" + _ARGS),
     "successor"),
    (re.compile(r"[Ii]mport (?:them|it) directly from\s+" + _OPEN + r"(?P<n>" + _NAME + r")"),
     "successor"),
    (re.compile(r"\b[Ii]nstead,?\s+(?:access|import|use)\s+" + _OPEN + r"(?P<n>" + _NAME
                + r")"), "successor"),
    (re.compile(r"[Ss]ubclass(?:ing)?\s+" + _OPEN + r"(?P<n>" + _NAME + r")"), "successor"),
    (re.compile(r"\brenamed\s+to\s+" + _OPEN + r"(?P<n>" + _NAME + r")"), "successor"),
    (re.compile(r"\bmoved\s+to\s+(?:the\s+)?" + _OPEN + r"(?P<n>[A-Za-z_][\w\.\-]*)"),
     "successor"),
    (re.compile(r"\bin\s+favou?r\s+of\s+" + _OPEN + r"(?P<n>" + _NAME + r")"), "successor"),
    (re.compile(r"\b(?:replaced|superseded)\s+(?:by|with)\s+" + _OPEN + r"(?P<n>" + _NAME
                + r")"), "successor"),
    (re.compile(r"\b[Mm]igrate\s+to\s+(?:[\w ]+?style\s+)?" + _OPEN + r"(?P<n>" + _NAME
                + r")"), "successor"),
    (re.compile(r"\b[Pp]refer\s+" + _OPEN + r"(?P<n>" + _NAME + r")"), "successor"),
]

# Words the patterns can capture that are English, not a successor.
_STOPWORDS = frozenset("""
a an the this that these those it its them they their your you we our one ones any some each
either neither both other another same new old more less most least only also just now then
instead rather please note see here there which what when where while who whom whose why how
and or but nor not no yes if else of to in on at by for from with without as is are was were
be been being will would can could should may might must do does did has have had
version versions release releases python python2 python3 module function method class object
type value values name names argument arguments parameter parameters option options feature
api code docs documentation following above below example later earlier future
command commands keyword keywords attribute attributes property properties package
""".split())

# Generic verbs and bare method names a "use X to <do something>" sentence puts in X's slot:
# never a successor unless the text quotes them as code, qualifies them, or they are a member
# of the receiver's class (the engine checks that last one).
_DENYLIST = frozenset("""
operate apply dispatch call calls invoke execute run runs process handle perform evaluate
resolve fire emit trigger supply provide produce create build make get set pass wrap
convert transform generate compute construct implement override extend inherit define
declare register configure specify describe indicate represent return returns yield
fit predict score update count copy map filter keys values items load dump read write open
close show plot draw save start stop add remove append insert pop clear sort reset init
""".split())

_PARAM_WORDS = re.compile(r"\b(?:parameter|argument|keyword|kwarg|option)s?\b", re.IGNORECASE)
_ALT_SEP = re.compile(r"\s*(?:,\s*(?:or\s+)?|\s+or\s+|\s*/\s*)(?:the\s+)?(?P<q>[`'\"*]*)~?\.?(?P<n>"
                      + _NAME + r")")
_CAVEATS = re.compile(
    r"\b(?:note that|default (?:behaviou?r |value )?(?:has )?changed|has changed|"
    r"(?:is|are) encouraged to review|review|different(?:ly)?|no longer (?:returns|accepts)|"
    r"returns? (?:a|an) [\w ]+ instead|keep (?:the )?(?:existing|old|past) behaviou?r)\b",
    re.IGNORECASE)
_EQUIVALENT = re.compile(r"\b(?:for the same effect|same behaviou?r|to keep the past "
                         r"behaviou?r|has the same behaviou?r|is equivalent|equivalent to)\b",
                         re.IGNORECASE)

ROLES = ("successor", "alternative", "param")

# A text that offers a choice: two quoted names joined by "or" ("`st.cache_data` or
# `st.cache_resource`", "either `displot` (...) or `histplot` (...)").
_QN = r"[`'\"*]~?\.?(?P<{0}>" + _NAME + r")(?:\(\))?[`'\"*]"
_CHOICE = [
    re.compile(_QN.format("a") + r"\s*(?:\([^()]{0,80}\))?,?\s+or\s+(?:the\s+)?"
               + _QN.format("b")),
    re.compile(r"\beither\s+(?:the\s+)?" + _QN.format("a") + r".{0,160}?\bor\s+(?:the\s+)?"
               + _QN.format("b")),
]


@dataclass(frozen=True)
class Candidate:
    """A name a text gives as what to use instead, with how it was given."""

    name: str
    role: str = "successor"      # successor | alternative | param
    source: str = "prose"        # warning | prose | docstring | error | helper | knowledge | ...
    quoted: bool = False
    generic: bool = False        # a bare generic word, unquoted and unqualified
    text: str = ""


def _clean(raw: str) -> str:
    name = raw.strip(_QUOTES + "~@").rstrip(".,;:")
    name = name.lstrip(".")
    while name.endswith("()"):
        name = name[:-2]
    return name.rstrip("=")


def _codey(name: str) -> bool:
    return "_" in name or "." in name or any(c.isupper() for c in name[1:])


def candidates_from(messages: Iterable[str], *, source: str = "prose") -> list[Candidate]:
    """Every name ``messages`` give as a successor, in message and text order, each once per
    message. ``role`` is ``alternative`` when the text offers a choice ("``histplot`` or
    ``displot``", two quoted names joined by "or") and ``param`` when it names a keyword
    (``errorbar=...``, "the 'attachment_filename' parameter has been renamed to
    'download_name'"). Stopwords never count; a bare generic word is kept with ``generic``."""
    out: list[Candidate] = []
    for msg in messages:
        text = " ".join(str(msg or "").split())
        if not text:
            continue
        hits: list[tuple[int, int, str, bool, str]] = []
        for rx, role in _PATTERNS:
            for m in rx.finditer(text):
                raw = m.group("n")
                start, end = m.start("n"), m.end("n")
                quoted = start > 0 and text[start - 1] in _QUOTES or \
                    (start > 1 and text[start - 2:start] in ("`~", "`.", "'."))
                hits.append((start, end, raw, quoted, role))
        seen: set[tuple[str, int]] = set()
        found: list[Candidate] = []
        for start, end, raw, quoted, role in sorted(hits, key=lambda h: h[0]):
            name = _clean(raw)
            if not _acceptable(name, quoted or text[end:end + 1] == "="):
                continue
            if (name, start) in seen or any(c.name == name for c in found):
                continue
            seen.add((name, start))
            tail = text[end:end + 2]
            sentence = text[max(0, text.rfind(".", 0, start) + 1):start]
            is_param = tail.startswith("=") or raw.endswith("=") or (
                "(" not in tail and "." not in name and bool(_PARAM_WORDS.search(sentence)))
            generic = not quoted and not _codey(name) and name.lower() in _DENYLIST
            found.append(Candidate(name=name, role="param" if is_param else role,
                                   source=source, quoted=quoted, generic=generic,
                                   text=text[:300]))
            for alt in _ALT_SEP.finditer(text, _after_quote(text, end)):
                if alt.start() != _after_quote(text, end):
                    break
                other = _clean(alt.group("n"))
                if not alt.group("q") and not (_codey(other) and found[-1].name.count(".")
                                               == other.count(".")):
                    break                         # "use X or the old one": prose, not a choice
                if _acceptable(other, True) and not any(c.name == other for c in found):
                    found[-1] = _as_alternative(found[-1])
                    found.append(Candidate(name=other, role="alternative", source=source,
                                           quoted=True, text=text[:300]))
                end = alt.end()
        for rx in _CHOICE:
            for m in rx.finditer(text):
                for key in ("a", "b"):
                    name = _clean(m.group(key))
                    if not _acceptable(name, True):
                        continue
                    known = next((i for i, c in enumerate(found) if c.name == name), None)
                    if known is None:
                        found.append(Candidate(name=name, role="alternative", source=source,
                                               quoted=True, text=text[:300]))
                    else:
                        found[known] = _as_alternative(found[known])
        out.extend(found)
    return out


def _after_quote(text: str, end: int) -> int:
    while end < len(text) and text[end] in _QUOTES + "()":
        if text[end] == "(":
            close = text.find(")", end)
            if close < 0 or close - end > 60:
                break
            end = close + 1
            continue
        end += 1
    return end


def _as_alternative(c: Candidate) -> Candidate:
    return Candidate(name=c.name, role="alternative", source=c.source, quoted=c.quoted,
                     generic=c.generic, text=c.text)


def _acceptable(name: str, code: bool = False) -> bool:
    """A name worth keeping: an identifier path that is no English stopword (a quoted name, or
    one followed by ``=``, is code and may be one: ``method=``)."""
    if not name or not re.match(r"^[A-Za-z_][\w\.\-]*$", name):
        return False
    low = name.lower()
    if low in _STOPWORDS and not code:
        return False
    if not _codey(name) and name.islower() and len(name) < 3:
        return False
    return True


def caveats(messages: Iterable[str]) -> list[str]:
    """The sentences of ``messages`` that qualify a successor ("Note that the default ... has
    changed", "encouraged to review"), capped. A caveat keeps text alone from rewriting code."""
    out: list[str] = []
    for msg in messages:
        for sentence in re.split(r"(?<=[.!?])\s+", " ".join(str(msg or "").split())):
            if _CAVEATS.search(sentence) and not _EQUIVALENT.search(sentence):
                item = sentence[:200]
                if item not in out:
                    out.append(item)
    return out[:4]


def asserts_equivalence(messages: Iterable[str]) -> bool:
    """A text that says the successor does the same ("for the same effect", "which has the
    same behavior")."""
    return any(_EQUIVALENT.search(str(m or "")) for m in messages)


def first_successor(messages: Iterable[str]) -> str | None:
    """The one successor ``messages`` name, for callers that take a single name (the
    intermediate-release fetch): the first message whose successor-role candidates name exactly
    one non-generic name. Several, or only alternatives, name none."""
    for msg in messages:
        cands = [c for c in candidates_from([msg]) if not c.generic]
        if not cands:
            continue
        names = {c.name for c in cands if c.role == "successor"}
        if any(c.role == "alternative" for c in cands) or len(names) != 1:
            return None
        return names.pop()
    return None


# ── attribution ──────────────────────────────────────────────────────────────────────────


def mentions(msg: str, word: str) -> bool:
    w = word.strip("_")
    return bool(w) and re.search(r"(?<![A-Za-z0-9_])" + re.escape(w) + r"(?![A-Za-z0-9_])",
                                 msg or "") is not None


def _template(text: str) -> re.Pattern | None:
    parts = [re.escape(p) for p in " ".join(text.split()).split("{...}")]
    if not any(len(p) >= 12 for p in parts):
        return None
    return re.compile(".*?".join(parts), re.DOTALL)


def templates_of(step: Mapping[str, Any] | None) -> list[str]:
    """The warning texts a step's own code can emit: its warn and log calls, the PEP 562 branch
    serving it, the warning helpers it calls, and the messages its wrappers keep in their
    closure cells."""
    if not step:
        return []
    out = [w.get("message", "") for w in step.get("warns") or ()]
    out += [w.get("message", "") for w in step.get("getattr_warns") or ()]
    for h in step.get("helper_warns") or ():
        out += list(h.get("messages") or ())
    for c in step.get("cells") or ():
        for value in (c.get("cells") or {}).values():
            if isinstance(value, str) and " " in value:
                out.append(value)
    if step.get("deprecated_attr"):
        out.append(step["deprecated_attr"])
    return [t for t in out if t]


def attributed(msg: str, names: Iterable[str], params: Iterable[str] = (),
               templates: Iterable[str] = ()) -> bool:
    """The attribution rule: a recorded warning is evidence about a symbol when its text names
    the symbol (one of ``names``) or one of its ``params``, or matches one of the warning
    ``templates`` found in its own code (``{...}`` holes match anything)."""
    text = " ".join(str(msg or "").split())
    if any(mentions(text, n) for n in names if n):
        return True
    if any(mentions(text, p) for p in params if p and len(p.strip("_")) > 1):
        return True
    for t in templates:
        rx = _template(t)
        if rx is not None and (rx.fullmatch(text) or rx.match(text)):
            return True
    return False


# ── registered deprecation helpers ───────────────────────────────────────────────────────

_HELPERS_FILE = Path(__file__).resolve().parent.parent / "knowledge" / "deprecation_helpers.json"


@lru_cache(maxsize=1)
def registered_helpers() -> tuple[dict, ...]:
    """The entries of ``knowledge/deprecation_helpers.json``."""
    data = json.loads(_HELPERS_FILE.read_text(encoding="utf-8"))
    return tuple(data.get("helpers") or ())


def _registry_for(qualname: str | None, written: str | None = None) -> dict | None:
    for entry in registered_helpers():
        names = [entry["qualname"], *(entry.get("aliases") or ())]
        if qualname and qualname in names:
            return entry
    return None


def _registry_for_code(module: str | None, code: str | None) -> dict | None:
    """The helper whose wrapper a closure belongs to: the wrapper's code qualname starts with
    the helper's own name (``deprecate_renamed_parameter.<locals>...``,
    ``deprecated.__call__.<locals>.wrapper``) in its module, or is one it lists (``codes``)."""
    if not module or not code:
        return None
    for entry in registered_helpers():
        for q in [entry["qualname"], *(entry.get("aliases") or ())]:
            mod, _, leaf = q.rpartition(".")
            if mod != module:
                continue
            if code == leaf or code.split(".")[0] == leaf or code in (entry.get("codes") or ()):
                return entry
    return None


# Decorators that make no wrapper and change no call (a builtin descriptor, a typing marker).
SAFE_DECORATORS = frozenset((
    "builtins.property", "builtins.classmethod", "builtins.staticmethod", "abc.abstractmethod",
    "functools.cached_property", "typing.overload", "typing.final",
    "typing_extensions.overload", "typing_extensions.final", "property", "classmethod",
    "staticmethod", "abstractmethod", "cached_property", "overload", "final"))
#: What a proof may pass through: a forward (or a twin) only wrappers that warn or dispatch;
#: a keyword rename also wrappers that pass keywords through or rename another keyword.
FORWARD_WRAPPERS = ("warn", "dispatch")
KEYWORD_WRAPPERS = ("warn", "dispatch", "keywords", "rename")


def _wrapper_kind(layer: Mapping[str, Any]) -> str | None:
    if layer.get("type"):
        entry = next((e for e in registered_helpers()
                      if layer["type"] in (e.get("types") or ())), None)
    else:
        entry = _registry_for_code(layer.get("module"), layer.get("code"))
    return entry.get("wrapper") if entry else None


def unknown_wrapper(body: Mapping[str, Any] | None, allowed: Iterable[str]) -> str | None:
    """Why a body summary does not account for every wrapper around the callable (None: it
    does): each layer on its ``__wrapped__`` chain, and each decorator, must be a registered
    helper whose wrapper only does what ``allowed`` names, or a builtin descriptor. A summary
    made before wrappers were read accounts for none."""
    if not body or "layers" not in body or "decorators" not in body:
        return "the probe did not read its wrappers"
    allowed = tuple(allowed)
    for layer in body.get("layers") or ():
        kind = _wrapper_kind(layer)
        if kind not in allowed:
            what = layer.get("type") or f"{layer.get('module')}.{layer.get('code')}"
            return f"it is wrapped by {what}, which pymolt does not know to leave calls alone"
    for dec in body.get("decorators") or ():
        if dec in SAFE_DECORATORS:
            continue
        entry = _registry_for(dec)
        if entry is None or entry.get("wrapper") not in allowed:
            return f"its decorator {dec} may change what it does"
    return None


@dataclass
class HelperFact:
    """What a deprecation helper attached to a callable says, read from its data."""

    kind: str                    # rename-parameter | rename-object | prescription | prose
    helper: str                  # the helper's qualname (or the wrapper's code name)
    via: str                     # decorator | cells | call
    registered: bool = True
    old: str | None = None
    new: str | None = None
    since: str | None = None
    alternative: str | None = None
    message: str | None = None
    values: bool = False         # the helper also maps the values (pandas ``mapping=``)

    def label(self) -> str:
        what = {"rename-parameter": f"renames {self.old} to {self.new}"
                + (" and maps its values" if self.values else ""),
                "rename-object": f"names {self.new}",
                "prescription": f"prescribes {self.alternative}",
                "prose": "says so"}.get(self.kind, self.kind)
        since = f" since {self.since}" if self.since else ""
        how = "" if self.registered else " (generic reading)"
        return f"{self.helper} {what}{since}{how}"


_GENERIC_OLD = ("old_name", "old", "old_arg_name", "old_param", "deprecated_name")
_GENERIC_NEW = ("new_name", "new", "new_arg_name", "new_param")


def _roles(entry: dict, args: list, kwargs: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for i, role in enumerate(entry.get("args") or ()):
        if role and i < len(args):
            out[role] = args[i]
    for key, role in (entry.get("kwargs") or {}).items():
        if key in kwargs:
            out[role] = kwargs[key]
    return out


def _fact_from_roles(entry: dict, roles: Mapping[str, Any], via: str) -> HelperFact | None:
    def text(key: str) -> str | None:
        value = roles.get(key)
        return value if isinstance(value, str) and value else None

    kind = entry.get("kind")
    if kind == "transparent":
        return None
    fact = HelperFact(kind=kind, helper=entry["qualname"], via=via, since=text("since"),
                      old=text("old"), new=text("new"), alternative=text("alternative"),
                      message=text("message"), values=roles.get("values") is not None)
    if kind == "rename-parameter" and not (fact.old and fact.new):
        return None
    if kind == "rename-object" and not fact.new:
        return None
    if kind == "prescription" and not (fact.alternative or fact.message):
        return None
    if kind == "prose" and not fact.message:
        return None
    return fact


def helper_facts(step: Mapping[str, Any] | None) -> list[HelperFact]:
    """What the registered helpers attached to a step say: its decorator calls, its wrappers'
    closure cells, and the helper calls in its body. A wrapper whose cells hold an old and a new
    name but no registered helper is read generically (stamped ``registered=False``)."""
    if not step:
        return []
    out: list[HelperFact] = []
    for call in step.get("decorator_calls") or ():
        entry = _registry_for(call.get("qualname"))
        by_name = False
        if entry is None and call.get("qualname") is None:
            # read statically: the decorator is known only by the name it is written with
            leaf = str(call.get("name") or "").rsplit(".", 1)[-1]
            named = [e for e in registered_helpers()
                     if e["qualname"].rsplit(".", 1)[-1] == leaf]
            entry, by_name = (named[0], True) if len(named) == 1 else (None, False)
        if entry is None:
            continue
        fact = _fact_from_roles(entry, _roles(entry, call.get("args") or [],
                                              call.get("kwargs") or {}), "decorator")
        if fact is not None:
            if by_name:
                fact.registered = False
            out.append(fact)
    for wrapper in step.get("cells") or ():
        cells = wrapper.get("cells") or {}
        entry = _registry_for_code(wrapper.get("module"), wrapper.get("code"))
        if entry is not None:
            roles = {role: cells.get(name) for name, role in (entry.get("cells") or {}).items()
                     if name in cells}
            fact = _fact_from_roles(entry, roles, "cells")
        else:
            old = next((cells[k] for k in _GENERIC_OLD if isinstance(cells.get(k), str)), None)
            new = next((cells[k] for k in _GENERIC_NEW if isinstance(cells.get(k), str)), None)
            fact = None
            if old and new and old.isidentifier() and new.isidentifier():
                fact = HelperFact(kind="rename-parameter", via="cells", registered=False,
                                  helper=f"{wrapper.get('module')}.{wrapper.get('code')}",
                                  old=old, new=new)
        if fact is not None and not any(_same(fact, f) for f in out):
            out.append(fact)
    for w in step.get("warns") or ():
        if not w.get("callee"):
            continue
        entry = next((e for e in registered_helpers()
                      if e.get("call") and e["qualname"].rsplit(".", 1)[-1]
                      == w["callee"].rsplit(".", 1)[-1]), None)
        if entry is None:
            continue
        fact = _fact_from_roles(entry, _roles(entry, w.get("args") or [], w.get("kwargs") or {}),
                                "call")
        if fact is not None and not any(_same(fact, f) for f in out):
            out.append(fact)
    return out


def _same(a: HelperFact, b: HelperFact) -> bool:
    return (a.kind, a.old, a.new, a.alternative) == (b.kind, b.old, b.new, b.alternative)


def param_renames(step: Mapping[str, Any] | None) -> dict[str, HelperFact]:
    """``{old: fact}``: the parameters a step accepts under an old name, by its rename
    helpers."""
    return {f.old: f for f in helper_facts(step) if f.kind == "rename-parameter" and f.old}


def prescription_text(fact: HelperFact) -> str | None:
    """A structured hint as a sentence ``candidates_from`` reads: "Use X instead"."""
    if fact.kind == "rename-object" and fact.new:
        return f"Use `{fact.new}` instead."
    if fact.kind == "prescription" and fact.alternative:
        return f"Use {fact.alternative} instead." if fact.alternative.strip(_QUOTES)[:1] in \
            _QUOTES else f"Use `{fact.alternative.strip(_QUOTES)}` instead."
    return fact.message


# ── the succession record ────────────────────────────────────────────────────────────────

TRUST_ORDER = ("identity", "proof", "curated", "prescription", "named", "hint")
_SOURCE_TRUST = {"identity": "identity", "helper": "prescription", "decorator": "prescription",
                 "knowledge": "curated"}


@dataclass
class Succession:
    """How a fact's successor was named: the ``succession`` addition of ``pymolt.delta/1``."""

    scope: str = "object"                       # object | member | param
    trust: str | None = None
    sources: list[str] = field(default_factory=list)
    params: dict[str, str] = field(default_factory=dict)
    proof: dict[str, Any] | None = None
    caveats: list[str] = field(default_factory=list)
    candidates: list[dict[str, str]] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)
    equivalent: bool = False

    def as_dict(self) -> dict[str, Any] | None:
        if not (self.trust or self.candidates or self.rejected):
            return None
        out: dict[str, Any] = {"scope": self.scope}
        if self.trust:
            out["trust"] = self.trust
        for key in ("sources", "params", "caveats", "candidates", "rejected"):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.proof:
            out["proof"] = self.proof
        if self.equivalent:
            out["equivalent"] = True
        return out


def trust_of(source: str) -> str:
    return _SOURCE_TRUST.get(source.split(":", 1)[0], "named")


__all__ = ["Candidate", "HelperFact", "Succession", "asserts_equivalence", "attributed",
           "candidates_from", "caveats", "first_successor", "helper_facts", "mentions",
           "param_renames", "prescription_text", "registered_helpers", "templates_of",
           "trust_of"]

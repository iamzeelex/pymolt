"""Run-to-run noise measured by a baseline replicate.

The baseline's command run twice on the same code in the same environment (``pymolt capture
baseline --replicate``) differs only by run-to-run noise. ``build_noise_profile`` diffs the two
runs with every rule of the fact diff and records, per call site, the EVIDENCE CLASS of each
difference the baseline itself shows there:

* ``value@complete``: a value differs at equal, fully observed input;
* ``value@hidden``: a value differs at equal input that is not fully observed (a receiver or
  argument rendered by type only);
* ``value@masked``: a value differs where the inputs matched only once volatile values were
  masked (a timestamp or id in the input);
* ``input``: the input changed; ``kind``: an outcome kind changed; ``presence``: a call on one
  side only; ``group``: a volatile site judged as a whole;
* ``order``: a dict key order differs (a returned dict's, or the input's of a call whose
  output then differs);

and the classes of the pairs it found EQUAL there (``equal@complete`` and so on).

Order evidence is kept per (site, dict key set), never per site alone (one call site may
return a deterministic dict for one input and a hash-ordered one for another):
``order_varying`` names the key sets whose order differed between the runs there, and
``order_equal`` the key sets whose orders agreed where hash-seeded orders would agree only by
a chance of at most 1/100 (1/k!: 5 or more keys), in two runs that both hashed with a random
seed (a pinned seed cannot measure hash order; an unknown seed is no evidence). ``order`` and
``equal@order`` in the site lists are summaries of those.

An entry may carry several classes (a value that changed because the input's key order did:
``value@complete`` and ``order``); it is noise only when EVERY class it carries varies at its
site.

``apply_noise_profile`` then judges the migration's diff (baseline vs post-migration):

* a difference is run noise, measured, only when the replicate shows a difference of the SAME
  class (every class of the entry) at that site (``FactEntry.noise``, explained
  ``replicate``): a value change at equal complete input is explained only by one in the
  replicate, never by inputs that vary there;
* otherwise, when the replicate's pairs of that class were equal there (``equal@complete`` for
  a value change at complete input, any equal pair for a changed input, kind or presence), a
  failure is stamped "confirmed deterministic", and the reviews that only existed because
  noise was not measured (unmeasured noise, masked input, input changed) are judged strictly;
* an order difference is noise only when every key set it changed at varies at its site in
  the replicate; an order review (``order changed``, ``input order changed``) is judged
  strictly only when every such key set is in ``order_equal`` at its site (FAIL, confirmed
  deterministic); otherwise it stays a review that says why the replicate cannot tell;
* a replicate that lost events (queue drops) is no evidence about presence: a call missing
  from one of its runs may just be lost, so presence differences are neither explained nor
  confirmed by it.

The profile is cached in ``.pymolt/noise_profile.json`` under the identity of both traces, the
profile schema and the pymolt version. Host side only (never imported by the injected runtime).
"""
from __future__ import annotations

import json
import logging
import math
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from pymolt.verify import dict_order as _order
from pymolt.verify import environment as _env
from pymolt.verify.facts import (
    _MASKED_INPUT_REVIEW,
    INPUT_CHANGED,
    ORDER_REVIEWS,
    UNMEASURED_NOISE,
    Comparison,
    FactCategory,
    FactDiff,
    FactEntry,
    FactSet,
    _kinds_match,
    diff_facts,
    input_hidden,
)

logger = logging.getLogger(__name__)

NOISE_PROFILE_SCHEMA = "pymolt.noise-profile/3"   # /3: dict key order evidence
ORDER = "order"
EQUAL_ORDER = "equal@order"
ORDER_CHANCE_MAX = 1 / 100
NOISE_PROFILE_PATH = Path(".pymolt") / "noise_profile.json"
# Reviews that exist only because no replicate measured the noise: strict once confirmed.
_STRICT_REVIEWS = (UNMEASURED_NOISE, INPUT_CHANGED, _MASKED_INPUT_REVIEW.split(":")[0])
_GROUP_NOTES = ("volatile-site", "unpaired-site")


def site_key(e: FactEntry) -> str:
    """A call site as both diffs name it: symbol, file, function and line text (the line
    number when the text is unknown). The old side's site (the baseline) when there is one."""
    site = e.old_site or e.new_site
    assert site is not None
    return json.dumps([e.symbol, site.file, site.func,
                       site.text if site.text is not None else site.line])


def _input_class(e: FactEntry) -> str:
    if "input-changed" in e.notes or str(e.review or "").startswith(INPUT_CHANGED):
        return "changed"
    if "matched-modulo-masked-input" in e.notes:
        return "masked"
    if input_hidden(e.input) or (e.new_input is not None and input_hidden(e.new_input)):
        return "hidden"
    return "complete"


def evidence(e: FactEntry) -> tuple[set[str], set[str]]:
    """``(difference classes, equal classes)`` of one entry (see the module doc); empty where
    the entry is no evidence of that kind (an uncomparable fact, a difference for the second).
    ``equal@order`` is a site's (``build_noise_profile``), from the entries' agreed orders."""
    if e.category in (FactCategory.APPEARED, FactCategory.DISAPPEARED):
        return {"presence"}, set()
    if e.category is FactCategory.UNCOMPARABLE:
        return set(), set()
    changed = e.category in (FactCategory.CHANGED, FactCategory.REWRITTEN)
    if changed and not _kinds_match(e.old, e.new):
        return {"kind"}, set()  # a new raise is never the same class as a varying value or input
    cls = _input_class(e)
    if cls == "changed":
        return {"input"}, set()
    if e.comparison is Comparison.EQUAL or not changed:
        return set(), {f"equal@{cls}"}
    if any(n.startswith(_GROUP_NOTES) for n in e.notes):
        return {"group"}, set()
    if "order-changed" in e.notes:
        return {ORDER}, set()                 # same keys and values: only the order differs
    classes = {f"value@{cls}"}
    if "input-order-changed" in e.notes:
        classes.add(ORDER)    # the output is a permutation: it changed as the input's order did
    return classes, set()


def noise_class(e: FactEntry) -> str | None:
    """One evidence class of an entry's difference (None: no difference): the value-like one
    when it carries several."""
    classes = evidence(e)[0]
    if not classes:
        return None
    rest = sorted(classes - {ORDER})
    return rest[0] if rest else ORDER


def build_noise_profile(first: FactSet, second: FactSet) -> dict[str, Any]:
    """The noise two runs of the same baseline show, per call site (see the module doc)."""
    diff = diff_facts(first, second)
    varying: dict[str, set[str]] = defaultdict(set)
    equal: dict[str, set[str]] = defaultdict(set)
    order_varying: dict[str, set[str]] = defaultdict(set)
    agreed: dict[str, set[str]] = defaultdict(set)
    for e in diff.entries:
        key = site_key(e)
        differs, same = evidence(e)
        varying[key].update(differs)
        equal[key].update(same)
        if ORDER in differs:
            order_varying[key].update(e.order_keysets or ())
        for ks in e.order_agreed or ():
            agreed[key].add(_order.keyset_id(ks))
    hashing = (_env.hash_randomized(first.environment),
               _env.hash_randomized(second.environment))
    order_evidence = hashing == (True, True) and first.dict_order and second.dict_order
    order_equal: dict[str, set[str]] = defaultdict(set)
    if order_evidence:
        for key, ids in agreed.items():
            for ident in ids - order_varying.get(key, set()):
                k = _order.keyset_size(ident)
                if k and 1 / math.factorial(k) <= ORDER_CHANCE_MAX:
                    order_equal[key].add(ident)
                    equal[key].add(EQUAL_ORDER)
    varying = defaultdict(set, {k: v for k, v in varying.items() if v})
    equal = defaultdict(set, {k: v for k, v in equal.items() if v})
    return {
        "varying": {k: sorted(v) for k, v in sorted(varying.items())},
        "equal": {k: sorted(v) for k, v in sorted(equal.items())},
        "order_varying": {k: sorted(v) for k, v in sorted(order_varying.items()) if v},
        "order_equal": {k: sorted(v) for k, v in sorted(order_equal.items()) if v},
        "presence_evidence": not (first.dropped_events or second.dropped_events),
        "order_evidence": order_evidence,
        "order_note": None if order_evidence else _order_note(first, second, hashing),
        "warnings": [w for w in diff.warnings if w.startswith(("run-variation", "volatile"))],
    }


def _order_note(first: FactSet, second: FactSet, hashing: tuple) -> str:
    if not (first.dict_order and second.dict_order):
        return "its runs did not record dict key order"
    if None in hashing:
        return "its runs did not record whether str hashing was randomized"
    return "a run hashed with a pinned seed (PYTHONHASHSEED), which cannot measure hash order"


def _confirms(cls: str, equal: set[str]) -> bool:
    """Whether the replicate's equal pairs at a site show that a difference of class ``cls``
    there is no run noise: the same input class for a value change, ``equal@order`` for an
    order change, any for the rest."""
    if cls.startswith("value@"):
        return "equal@" + cls.split("@", 1)[1] in equal
    if cls == ORDER:
        return EQUAL_ORDER in equal
    return bool(equal - {EQUAL_ORDER}) or EQUAL_ORDER in equal


def apply_noise_profile(diff: FactDiff, profile: dict[str, Any], label: str) -> dict[str, int]:
    """Judge ``diff`` (baseline vs post-migration) with the replicate's ``profile``; sets
    ``diff.noise_measured`` and returns the counts (explained, confirmed, strict)."""
    varying = {k: set(v) for k, v in (profile.get("varying") or {}).items()}
    equal = {k: set(v) for k, v in (profile.get("equal") or {}).items()}
    order_varying = {k: set(v) for k, v in (profile.get("order_varying") or {}).items()}
    order_equal = {k: set(v) for k, v in (profile.get("order_equal") or {}).items()}
    presence_ok = bool(profile.get("presence_evidence"))
    counts: Counter = Counter()
    for e in diff.entries:
        classes = evidence(e)[0]
        if not classes or ("presence" in classes and not presence_ok):
            continue
        key = site_key(e)
        shown = "+".join(sorted(classes))
        keysets = set(e.order_keysets or ())
        order_noise = ORDER not in classes or (
            bool(keysets) and keysets <= order_varying.get(key, set()))
        order_stable = bool(keysets) and keysets <= order_equal.get(key, set())
        if (classes - {ORDER}) <= varying.get(key, set()) and order_noise:
            if e.fails or e.inconclusive:
                e.noise = (f"replicate: the baseline's own two runs differ here the same way "
                           f"({shown})")
                e.notes.append("noise: measured by the baseline replicate")
                e.explained = f"replicate: the baseline replicate differs here too ({shown})"
                counts["explained"] += 1
            continue
        if e.review and str(e.review).startswith(ORDER_REVIEWS):
            if order_stable:
                e.notes.append(f"strict: the baseline replicate is equal here ({e.review[:40]})")
                e.notes.append("confirmed deterministic: equal key orders in the baseline "
                               "replicate (random hash seeds)")
                e.review = None
                counts["strict"] += 1
                counts["confirmed"] += 1
            else:
                why = profile.get("order_note") or (
                    "its equal pairs here compared too few keys to rule out a chance agreement "
                    "of hash-seeded orders (fewer than 5), or none")
                e.review = f"{e.review}; the baseline replicate cannot tell: {why}"
            continue
        if ORDER in classes and not (classes - {ORDER}):
            if e.fails and order_stable:   # a cited or pinned-seed order failure
                e.notes.append("confirmed deterministic: equal key orders in the baseline "
                               "replicate (random hash seeds)")
                counts["confirmed"] += 1
            continue
        cls = noise_class(e)
        if cls is None or not _confirms(cls, equal.get(key, set())):
            continue
        if e.review and str(e.review).startswith(_STRICT_REVIEWS):
            e.notes.append(f"strict: the baseline replicate is equal here ({e.review[:40]})")
            e.review = None
            counts["strict"] += 1
        if e.fails:
            e.notes.append("confirmed deterministic: equal in the baseline replicate")
            counts["confirmed"] += 1
    stable = len(equal.keys() - varying.keys())
    diff.noise_measured = {
        "replicate": label,
        "sites_varying": len(varying),
        "sites_stable": stable,
        "presence_evidence": presence_ok,
        **{k: counts[k] for k in ("explained", "confirmed", "strict")},
    }
    diff.warnings = [w for w in diff.warnings if not w.startswith("run-variation")]
    diff.warnings.append(
        f"replicate: {label} measured run-to-run noise at {len(varying)} site(s) "
        f"({stable} equal in both runs); {counts['explained']} difference(s) explained as "
        f"noise (same evidence class at the same site), {counts['confirmed']} failure(s) "
        f"confirmed deterministic, {counts['strict']} review(s) judged strictly"
        + ("" if presence_ok else "; the replicate lost events, so it is no evidence about "
           "calls present on one side only"))
    return dict(counts)


def _identity(path: str | Path, captured_at: str | None) -> dict[str, Any]:
    st = os.stat(path)
    return {"trace": os.path.basename(str(path)), "captured_at": captured_at,
            "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def load_or_build_profile(
    project_dir: str | Path, baseline_trace: str | Path, baseline_at: str | None,
    replicate_trace: str | Path, replicate_at: str | None,
) -> dict[str, Any]:
    """The cached profile when both traces and the pymolt version are the ones it was built
    from, else a fresh one (written to the cache; a cache that cannot be written is logged)."""
    from pymolt import __version__
    from pymolt.verify.facts import load_facts

    root = Path(os.path.abspath(project_dir))
    baseline_trace = Path(baseline_trace) if Path(baseline_trace).is_absolute() \
        else root / baseline_trace
    replicate_trace = Path(replicate_trace) if Path(replicate_trace).is_absolute() \
        else root / replicate_trace
    identity = {"baseline": _identity(baseline_trace, baseline_at),
                "replicate": _identity(replicate_trace, replicate_at), "pymolt": __version__,
                "schema": NOISE_PROFILE_SCHEMA}
    cache = root / NOISE_PROFILE_PATH
    try:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        if cached.get("schema") == NOISE_PROFILE_SCHEMA and cached.get("identity") == identity:
            return cached["profile"]
    except (OSError, ValueError, AttributeError, KeyError):
        pass
    profile = build_noise_profile(load_facts(baseline_trace, root=root),
                                  load_facts(replicate_trace, root=root))
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"schema": NOISE_PROFILE_SCHEMA, "identity": identity,
                                     "profile": profile}), encoding="utf-8")
    except OSError as exc:
        logger.warning("could not cache the noise profile: %s", exc)
    return profile

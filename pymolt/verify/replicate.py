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

and the classes of the pairs it found EQUAL there (``equal@complete`` and so on).

``apply_noise_profile`` then judges the migration's diff (baseline vs post-migration):

* a difference is run noise, measured, only when the replicate shows a difference of the SAME
  class at that site (``FactEntry.noise``, explained ``replicate``): a value change at equal
  complete input is explained only by one in the replicate, never by inputs that vary there;
* otherwise, when the replicate's pairs of that class were equal there (``equal@complete`` for
  a value change at complete input, any equal pair for a changed input, kind or presence), a
  failure is stamped "confirmed deterministic", and the reviews that only existed because
  noise was not measured (unmeasured noise, masked input, input changed) are judged strictly;
* a replicate that lost events (queue drops) is no evidence about presence: a call missing
  from one of its runs may just be lost, so presence differences are neither explained nor
  confirmed by it.

The profile is cached in ``.pymolt/noise_profile.json`` under the identity of both traces, the
profile schema and the pymolt version. Host side only (never imported by the injected runtime).
"""
from __future__ import annotations

import json
import logging
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from pymolt.verify.facts import (
    _MASKED_INPUT_REVIEW,
    INPUT_CHANGED,
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

NOISE_PROFILE_SCHEMA = "pymolt.noise-profile/2"
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


def evidence(e: FactEntry) -> tuple[str | None, str | None]:
    """``(difference class, equal class)`` of one entry (see the module doc); None where the
    entry is no evidence of that kind (an uncomparable fact, a difference for the second)."""
    if e.category in (FactCategory.APPEARED, FactCategory.DISAPPEARED):
        return "presence", None
    if e.category is FactCategory.UNCOMPARABLE:
        return None, None
    changed = e.category in (FactCategory.CHANGED, FactCategory.REWRITTEN)
    if changed and not _kinds_match(e.old, e.new):
        return "kind", None  # a new raise is never the same class as a varying value or input
    cls = _input_class(e)
    if cls == "changed":
        return "input", None
    if e.comparison is Comparison.EQUAL or not changed:
        return None, f"equal@{cls}"
    if any(n.startswith(_GROUP_NOTES) for n in e.notes):
        return "group", None
    return f"value@{cls}", None


def noise_class(e: FactEntry) -> str | None:
    """The evidence class of an entry's difference (None: no difference)."""
    return evidence(e)[0]


def build_noise_profile(first: FactSet, second: FactSet) -> dict[str, Any]:
    """The noise two runs of the same baseline show, per call site (see the module doc)."""
    diff = diff_facts(first, second)
    varying: dict[str, set[str]] = defaultdict(set)
    equal: dict[str, set[str]] = defaultdict(set)
    for e in diff.entries:
        key = site_key(e)
        differs, same = evidence(e)
        if differs is not None:
            varying[key].add(differs)
        if same is not None:
            equal[key].add(same)
    return {
        "varying": {k: sorted(v) for k, v in sorted(varying.items())},
        "equal": {k: sorted(v) for k, v in sorted(equal.items())},
        "presence_evidence": not (first.dropped_events or second.dropped_events),
        "warnings": [w for w in diff.warnings if w.startswith(("run-variation", "volatile"))],
    }


def _confirms(cls: str, equal: set[str]) -> bool:
    """Whether the replicate's equal pairs at a site show that a difference of class ``cls``
    there is no run noise: the same input class for a value change, any for the rest."""
    if cls.startswith("value@"):
        return "equal@" + cls.split("@", 1)[1] in equal
    return bool(equal)


def apply_noise_profile(diff: FactDiff, profile: dict[str, Any], label: str) -> dict[str, int]:
    """Judge ``diff`` (baseline vs post-migration) with the replicate's ``profile``; sets
    ``diff.noise_measured`` and returns the counts (explained, confirmed, strict)."""
    varying = {k: set(v) for k, v in (profile.get("varying") or {}).items()}
    equal = {k: set(v) for k, v in (profile.get("equal") or {}).items()}
    presence_ok = bool(profile.get("presence_evidence"))
    counts: Counter = Counter()
    for e in diff.entries:
        cls = noise_class(e)
        if cls is None or (cls == "presence" and not presence_ok):
            continue
        key = site_key(e)
        if cls in varying.get(key, ()):
            if e.fails or e.inconclusive:
                e.noise = f"replicate: the baseline's own two runs differ here the same way ({cls})"
                e.notes.append("noise: measured by the baseline replicate")
                e.explained = f"replicate: the baseline replicate differs here too ({cls})"
                counts["explained"] += 1
            continue
        if not _confirms(cls, equal.get(key, set())):
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

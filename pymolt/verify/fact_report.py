"""Findings, not entries: the fact diff as a person can act on it and an agent can execute it.

HOST side, presentation only. Nothing here changes a verdict: it reads a ``FactDiff`` (the
semantics live in ``facts.py``) and folds its entries into findings, grouped by (verdict
class, mechanism, symbol, site), each with a count, its comparison levels and one
representative sample (masked values next to the recorded raw ones).

``summarize(diff)`` is the ``summary`` block of the ``pymolt.fact-diff/2`` JSON, in reading
order: the verdict with its reasons, failing findings (kind > presence > type > value change,
then by count, each saying why it is not noise), review findings grouped by reason with the
one action that settles each, the explained table (mechanism, count, example, rule), and the
quality stamps. ``cap_value`` bounds every value a report carries (a 740 KB HTML string
becomes head, tail, length and sha1). ``render_text`` is the CLI's plain-text form.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pymolt.verify.facts import FactDiff, FactEntry

STRING_CAP = 200     # characters a reported string keeps before it is capped
_HEAD, _TAIL = 120, 40
LIST_CAP = 40        # elements a reported list keeps before it is capped
_LIST_HEAD, _LIST_TAIL = 20, 5
FINDINGS_CAP = 50    # findings listed per section (the rest are counted)
_SAMPLED_FINDINGS = 10  # failing findings the text form shows with their before/after sample

# mechanism -> the rule (function) that produced it: an auditable pointer, not prose
RULES = {
    "volatile-site": "facts._group_entries (masked-key pairing)",
    "unpaired-site": "facts._group_entries (unpaired members)",
    "masked-input": "facts._review_masked_input",
    "input-changed": "facts._judge_input_changes",
    "binding": "facts._unexplained_input_leaves (binding-independent input)",
    "input-substitution": "facts._explain_change (input substitution)",
    "source": "facts._Masker (entropy-source tokens)",
    "clock": "facts._Masker._offset (clock offset from the call)",
    "environment": "facts._Masker (project root, Python version)",
    "attribution": "facts._pair_qualname_equivalent / same-call relabel",
    "backend": "facts._soften_backend",
    "possibly-dropped": "facts._soften_dropped / _group_entries (lost producer)",
    "c-site-capped": "facts._soften_capped (compiled calls counted past the per-site cap)",
    "call-shape": "facts._soften_capped (compiled call vs call with inputs)",
    "implicit": "facts._apply_noise",
    "unmeasured-noise": "facts._gate_unmeasured",
    "replicate": "replicate.apply_noise_profile",
    "order": "facts._judge_order",
}

# review reason (the review text's prefix) -> the one action that settles it
ACTIONS = {
    "unmeasured noise": "capture a baseline replicate (pymolt capture baseline --replicate)",
    "masked input": "capture a baseline replicate (pymolt capture baseline --replicate)",
    "input changed": "capture a baseline replicate (pymolt capture baseline --replicate)",
    "unpaired site": "capture a baseline replicate (pymolt capture baseline --replicate)",
    "order changed": "capture a baseline replicate (random hash seeds): pymolt capture "
                     "baseline --replicate",
    "input order changed": "capture a baseline replicate (random hash seeds): pymolt capture "
                           "baseline --replicate",
    "environment differs": "capture both sides in the same environment (the post-migration "
                           "capture in the baseline's container or host, with the target "
                           "Python)",
    "the value differs on a hand-edited line": "review the hand edit at the call site",
    "not reached": "check that the code around the call still runs under the tests",
    "no longer a dependency call here": "check what the name refers to on that line now",
}
_DEFAULT_ACTION = "review the entry"


# ── value capping ─────────────────────────────────────────────────────────────────────────


def cap_value(v: Any) -> Any:
    """``v`` with every long string replaced by ``{"__capped__": "str", head, tail, len,
    sha1}`` and every long list by its head and tail plus a marker with its length. Short
    values pass unchanged, so capped and uncapped values still compare equal when short."""
    if isinstance(v, str):
        if len(v) <= STRING_CAP:
            return v
        return {"__capped__": "str", "head": v[:_HEAD], "tail": v[-_TAIL:], "len": len(v),
                "sha1": hashlib.sha1(v.encode("utf-8", "replace")).hexdigest()[:16]}
    if isinstance(v, list):
        if len(v) <= LIST_CAP:
            return [cap_value(x) for x in v]
        return [*(cap_value(x) for x in v[:_LIST_HEAD]),
                {"__capped__": "list", "len": len(v)},
                *(cap_value(x) for x in v[-_LIST_TAIL:])]
    if isinstance(v, dict):
        return {k: cap_value(x) for k, x in v.items()}
    return v


def _cap_outcomes(outcomes: Any) -> Any:
    if not isinstance(outcomes, list):
        return outcomes
    return [{**o, "value": cap_value(o.get("value"))} if isinstance(o, dict) else o
            for o in outcomes]


def cap_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """One JSON entry with its values capped (inputs, outcomes, type trees)."""
    for key in ("input", "new_input", "old_types", "new_types"):
        if key in entry:
            entry[key] = cap_value(entry[key])
    for key in ("old", "new"):
        if key in entry:
            entry[key] = _cap_outcomes(entry[key])
    return entry


# ── classification ────────────────────────────────────────────────────────────────────────


def verdict_class(e: FactEntry) -> str:
    if e.noise:
        return "noise"
    if e.fails:
        return "failing"
    if e.review:
        return "review"
    if e.inconclusive:
        return "not-comparable"
    if e.explained:
        return "explained"
    return "equal"


def mechanism(e: FactEntry) -> str:
    if e.explained:
        return e.explained.split(":", 1)[0]
    return e.category.value


def _review_reason(text: str) -> str:
    for prefix in ACTIONS:
        if text.startswith(prefix):
            return prefix
    return text.split(":", 1)[0][:60]


def _change_rank(e: FactEntry) -> tuple[int, str]:
    """(rank, label) of a failing entry's change: kind < presence < type < value."""
    from pymolt.verify.facts import (
        FactCategory,
        _cover,
        _distinct_types,
        _kinds_match,
        _outcome_types_match,
    )
    if e.category in (FactCategory.DISAPPEARED, FactCategory.APPEARED):
        return 1, "presence changed"
    if "order-changed" in e.notes:
        return 4, "order changed"
    if not _kinds_match(e.old, e.new):
        return 0, "kind changed"
    if not _cover(_distinct_types(e.old or []), _distinct_types(e.new or []),
                  _outcome_types_match):
        return 2, "type changed"
    if "outcomes-equal" in e.notes:
        return 3, "input changed"      # the call answered the same; what it was given moved
    return 3, "value changed"


def _order_why(e: FactEntry, label: str) -> str | None:
    """The why text of a failing entry whose dict key order differs (``FactEntry.order``)."""
    o = e.order or {}
    if not o:
        return None
    where = f"{o.get('path')} ({o.get('keys')} keys)"
    cause = f"; cause: {o['cause']}" if o.get("cause") else ""
    confirmed = "; stable across baseline runs (confirmed deterministic)" \
        if "confirmed deterministic" in " ".join(e.notes) else ""
    pinned = "; the same pinned hash seed on both sides" if any(
        n.startswith("order-controlled") for n in e.notes) else ""
    if o.get("side") == "output":
        return f"{label}: same keys and values, key order at {where}{cause}{confirmed}{pinned}"
    if "input-order-changed" in e.notes:
        return (f"{label} as the input's dict key order changed (same keys and values) at "
                f"{where}; the output is {o.get('output')}{cause}{confirmed}{pinned}")
    if e.new_input is None:
        return f"{label}; input equal except dict key order at {o.get('path')}{confirmed}"
    return None


def _why_not_noise(e: FactEntry, label: str, order: str | None = None) -> str:
    from pymolt.verify.facts import FactCategory, input_hidden
    why = _order_why(e, label)
    if why is not None:
        return why
    if "confirmed deterministic" in " ".join(e.notes):
        return f"{label}; stable across baseline runs (confirmed deterministic)"
    rare = next((n for n in e.notes if n.startswith("rare outcome:")), None)
    if rare is not None:
        return f"{label}; {rare}"
    if e.category is FactCategory.DISAPPEARED:
        return f"{label}: the call no longer happens at an unedited site"
    if e.category is FactCategory.APPEARED:
        return f"{label}: a new call at an unedited site"
    if "input-changed" in e.notes:
        return f"{label} as the input changed: an outcome kind or type difference always fails"
    if e.provenance in ("ledger",):
        return f"{label} on pymolt's own edit (strict: it claimed equivalence)"
    if e.new_input is None and not input_hidden(e.input):
        if order != "compared" and _order_unrecorded_matters(e.input):
            return f"{label}; same input content (dict key order not recorded by these captures)"
        return f"{label}; same complete input on both sides"
    if label != "value changed":
        return label
    return f"{label}; the input is equal on both sides"


def _order_unrecorded_matters(inputs: Any) -> bool:
    from pymolt.verify.dict_order import has_dict
    return has_dict(inputs)


def _site(e: FactEntry) -> tuple[str, str | None]:
    s = e.old_site or e.new_site
    return (s.label() if s is not None else "?"), (s.text if s is not None else None)


# ── samples ───────────────────────────────────────────────────────────────────────────────


def _outs(outcomes: Any) -> Any:
    if outcomes is None:
        return None
    return [cap_value(o.model_dump(mode="json")) for o in outcomes]


def _size(e: FactEntry) -> int:
    try:
        return len(json.dumps([_outs(e.old), _outs(e.new)], default=str))
    except (TypeError, ValueError):
        return 1 << 30


def _sample(e: FactEntry) -> dict[str, Any]:
    """The masked (compared) values and, where a mask changed them, the recorded ones; the
    input, and the new side's input only when it differs."""
    out: dict[str, Any] = {"input": cap_value(e.input)}
    if e.new_input is not None:
        out["new_input"] = cap_value(e.new_input)
    out["old"], out["new"] = _outs(e.old), _outs(e.new)
    raw = {}
    if e.input_raw is not None:
        raw["input"] = cap_value(e.input_raw)
    if e.new_input_raw is not None:
        raw["new_input"] = cap_value(e.new_input_raw)
    if e.old_raw is not None:
        raw["old"] = _outs(e.old_raw)
    if e.new_raw is not None:
        raw["new"] = _outs(e.new_raw)
    if raw:
        out["raw"] = raw
    if e.order:
        out["order"] = dict(e.order)
    return out


# ── summary ───────────────────────────────────────────────────────────────────────────────


def _finding(group: list[FactEntry], **extra: Any) -> dict[str, Any]:
    rep = min(group, key=_size)
    site, text = _site(rep)
    levels = Counter((e.level.value if e.level else "none") for e in group)
    out: dict[str, Any] = {
        "symbol": rep.symbol, "site": site, "site_text": text,
        "mechanism": mechanism(rep), "entries": len(group),
        "calls": {"old": sum(e.old_count for e in group),
                  "new": sum(e.new_count for e in group)},
        "levels": dict(levels), **extra,
        "explained": rep.explained, "sample": _sample(rep),
    }
    if rep.new_symbol:
        out["new_symbol"] = rep.new_symbol
    return out


def _group(entries: list[FactEntry]) -> dict[tuple, list[FactEntry]]:
    groups: dict[tuple, list[FactEntry]] = defaultdict(list)
    for e in entries:
        groups[(verdict_class(e), mechanism(e), e.symbol, _site(e)[0])].append(e)
    return groups


def _environment_stamp(diff: FactDiff, by_class: dict[str, list[FactEntry]]) -> Any:
    """``quality.environment``: "same", the differing aspects with the reviews and readings
    they explain, or "not recorded (<side>)" (with an inference from the call-site paths)."""
    env = diff.environment
    if env is None:
        return "not recorded (old, new)"
    if isinstance(env, dict) and "differs" in env:
        reviews = sum(1 for e in by_class["review"]
                      if str(e.review or "").startswith("environment differs"))
        explained = sum(1 for e in diff.entries
                        if str(e.explained or "").startswith("environment: the value is"))
        return {**env, "reviews": reviews, "explained": explained}
    return env


def summarize(diff: FactDiff) -> dict[str, Any]:
    entries = list(diff.entries)
    by_class: dict[str, list[FactEntry]] = defaultdict(list)
    for e in entries:
        by_class[verdict_class(e)].append(e)

    failing = []
    for (_cls, _mech, _sym, _site_), group in _group(by_class["failing"]).items():
        rank, label = min(_change_rank(e) for e in group)
        failing.append((rank, -len(group), _finding(
            group, change=label, why=_why_not_noise(group[0], label, diff.dict_order))))
    failing.sort(key=lambda t: (t[0], t[1], t[2]["symbol"], t[2]["site"]))

    reviews: dict[str, list[FactEntry]] = defaultdict(list)
    for e in by_class["review"]:
        reviews[_review_reason(e.review or "")].append(e)
    review = []
    for reason, group in sorted(reviews.items(), key=lambda kv: -len(kv[1])):
        findings = sorted((_finding(g) for g in _group(group).values()),
                          key=lambda f: (-f["entries"], f["symbol"], f["site"]))
        review.append({
            "reason": reason, "entries": len(group), "findings_total": len(findings),
            "action": ACTIONS.get(reason, _DEFAULT_ACTION),
            "findings": findings[:FINDINGS_CAP],
        })

    explained: dict[str, list[FactEntry]] = defaultdict(list)
    for cls in ("explained", "noise", "not-comparable"):
        for e in by_class[cls]:
            explained[mechanism(e)].append(e)
    table = []
    for mech, group in sorted(explained.items(), key=lambda kv: -len(kv[1])):
        ex = min(group, key=_size)
        table.append({
            "mechanism": mech, "entries": len(group), "rule": RULES.get(mech),
            "example": {"symbol": ex.symbol, "site": _site(ex)[0],
                        "explained": (ex.explained or "")[:200]},
        })

    compared = [e for e in entries if e.level is not None]
    levels = Counter(e.level.value for e in compared)
    total = sum(levels.values()) or 1
    quality = {
        "noise": diff.noise_measured or "not measured (no baseline replicate)",
        "dropped": {"old": diff.old_summary.dropped_events,
                    "new": diff.new_summary.dropped_events},
        "backends": {"old": diff.old_summary.backends, "new": diff.new_summary.backends},
        "dict_order": diff.dict_order or "not recorded (old, new)",
        "environment": _environment_stamp(diff, by_class),
        "levels": {k: {"entries": v, "share": round(v / total, 3)}
                   for k, v in sorted(levels.items())},
    }

    n_fail = len(by_class["failing"])
    reasons = []
    if failing:
        reasons.append(f"{n_fail} failing entr{'y' if n_fail == 1 else 'ies'} in "
                       f"{len(failing)} finding(s)")
    if review:
        reasons.append("; ".join(f"{r['entries']} {r['reason']}" for r in review)
                       + " (review)")
    if by_class["not-comparable"]:
        reasons.append(f"{len(by_class['not-comparable'])} not comparable")
    if diff.incomplete:
        reasons.append("a facts snapshot evicted facts: the evidence is incomplete")
    return {
        "verdict": diff.verdict.value,
        "reasons": reasons,
        "ledger": {"failing": n_fail, "failing_findings": len(failing),
                   "review": len(by_class["review"]), "noise": len(by_class["noise"]),
                   "explained": len(by_class["explained"]),
                   "not_comparable": len(by_class["not-comparable"]),
                   "equal": len(by_class["equal"])},
        "failing": [f for _r, _n, f in failing[:FINDINGS_CAP]],
        "failing_total": len(failing),
        "review": review,
        "explained": table,
        "quality": quality,
    }


# ── plain text (CLI) ──────────────────────────────────────────────────────────────────────


def _short(v: Any, n: int = 90) -> str:
    text = json.dumps(v, ensure_ascii=False, default=str, sort_keys=True)
    return text if len(text) <= n else text[:n - 1] + "…"


def _sample_lines(sample: dict[str, Any], indent: str) -> list[str]:
    lines = []
    if "new_input" in sample:
        lines.append(f"{indent}input  {_short(sample.get('input'))}")
        lines.append(f"{indent}     →  {_short(sample.get('new_input'))}")
    lines.append(f"{indent}before {_short(sample.get('old'))}")
    lines.append(f"{indent}after  {_short(sample.get('new'))}")
    raw = sample.get("raw") or {}
    if "old" in raw or "new" in raw:
        lines.append(f"{indent}recorded {_short(raw.get('old'), 60)} → "
                     f"{_short(raw.get('new'), 60)}")
    return lines


def render_text(summary: dict[str, Any], *, explained: bool = False,
                max_lines: int = 50) -> list[str]:
    """Sections 1-3 (verdict, failing findings, review by reason); with ``explained`` also
    the explained table. Quality stamps are one line. At most ``max_lines`` finding lines."""
    out = [f"Behaviour diff: {summary['verdict'].upper()}"
           + (f" ({'; '.join(summary['reasons'])})" if summary.get("reasons") else "")]
    budget = max_lines
    if summary.get("failing"):
        out.append(f"Failing ({summary['failing_total']} finding(s)):")
        printed = 0
        for f in summary["failing"]:
            if budget <= 0:
                break
            out.append(f"  ✗ {f['symbol']} @ {f['site']}  ×{f['entries']}  {f['why']}")
            if printed < _SAMPLED_FINDINGS:
                out.extend(_sample_lines(f["sample"], "      "))
            budget -= 1
            printed += 1
        if summary["failing_total"] > printed:
            out.append(f"  … {summary['failing_total'] - printed} more, "
                       f"{summary['failing_total']} in all (see --json)")
    for r in summary.get("review") or ():
        out.append(f"Review — {r['reason']}: {r['entries']} entr"
                   f"{'y' if r['entries'] == 1 else 'ies'} at {r['findings_total']} site(s). "
                   f"Action: {r['action']}")
        for f in r["findings"][:5]:
            if budget <= 0:
                break
            out.append(f"  ? {f['symbol']} @ {f['site']}  ×{f['entries']}")
            budget -= 1
        if r["findings_total"] > 5:
            out.append(f"  … {r['findings_total'] - 5} more site(s)")
    if explained and summary.get("explained"):
        out.append("Explained (not failing):")
        for row in summary["explained"]:
            ex = row["example"]
            out.append(f"  {row['mechanism']:<20} {row['entries']:>6}  e.g. {ex['symbol']} @ "
                       f"{ex['site']}  [{row.get('rule') or '-'}]")
    q = summary.get("quality") or {}
    noise = q.get("noise")
    noise_text = noise if isinstance(noise, str) else "measured by a baseline replicate"
    dropped = q.get("dropped") or {}
    shares = ", ".join(f"{k} {v['share']:.0%}" for k, v in (q.get("levels") or {}).items())
    env = q.get("environment")
    env_text = env if isinstance(env, str) or env is None else \
        f"differs ({', '.join(env.get('differs') or ())})"
    out.append(f"Quality: noise {noise_text}; dropped events old {dropped.get('old')} / new "
               f"{dropped.get('new')}; backends {q.get('backends')}; levels {shares}; dict "
               f"order {q.get('dict_order')}; environment {env_text}")
    return out

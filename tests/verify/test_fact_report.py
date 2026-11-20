"""Findings, not entries: the fact diff's summary, its capped values and its text form."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_fact_noise import T_NEW, T_OLD, env_fs, fact, fs  # noqa: E402

from pymolt.interfaces.views import _findings_data  # noqa: E402
from pymolt.verify.fact_report import cap_value, render_text, summarize  # noqa: E402
from pymolt.verify.facts import diff_facts  # noqa: E402

MSG = {"o": {"__tuple__": ["stats", {"__dict__": [["name", "/1"]]}]}}


def mixed_diff():
    """A kind change, a type change, three value changes at one site, one input change (a
    review), and a clock reading (explained)."""
    old = env_fs(
        fact("lib.load", {"s": "x"}, (("return", 1),), line=1),
        fact("lib.kind", {"s": "y"}, (("return", "a"),), line=2),
        *[fact("msgpack.packb", {"o": {"__tuple__": ["m", i]}}, (("return", f"aa{i}"),),
               line=3, first=10 + i) for i in range(3)],
        fact("billing.charge", {"amount": 100}, (("return", "ok"),), line=4),
        fact("lib.stamp", {"x": 1}, (("return", "at 2026-11-16 10:26:05"),), line=5),
        ts=T_OLD)
    new = env_fs(
        fact("lib.load", {"s": "x"}, (("raise", "ValueError"),), line=1),
        fact("lib.kind", {"s": "y"}, (("return", 2),), line=2),
        *[fact("msgpack.packb", {"o": {"__tuple__": ["m", i]}}, (("return", f"bb{i}"),),
               line=3, first=10 + i) for i in range(3)],
        fact("billing.charge", {"amount": 999}, (("return", "ok"),), line=4),
        fact("lib.stamp", {"x": 1}, (("return", "at 2026-11-16 10:31:41"),), line=5),
        ts=T_NEW)
    return diff_facts(old, new)


def test_failing_findings_group_by_site_and_sort_kind_type_value():
    s = summarize(mixed_diff())
    order = [(f["symbol"], f["change"], f["entries"]) for f in s["failing"]]
    assert order == [("lib.load", "kind changed", 1), ("lib.kind", "type changed", 1),
                     ("msgpack.packb", "value changed", 3)]
    assert "same complete input" in s["failing"][2]["why"]
    assert s["ledger"]["failing"] == 5 and s["failing_total"] == 3


def test_a_replicate_is_not_asked_for_again_once_one_was_measured():
    from pymolt.verify.fact_report import AFTER_REPLICATE

    diff = mixed_diff()
    diff.noise_measured = {"runs": 2}      # a baseline replicate was measured
    (r,) = summarize(diff)["review"]
    assert r["reason"] == "input changed" and r["action"] == AFTER_REPLICATE
    assert "--replicate" not in r["action"] and "--accept" in r["action"]


def test_review_findings_carry_their_reason_and_action():
    s = summarize(mixed_diff())
    (r,) = s["review"]
    assert r["reason"] == "input changed" and "replicate" in r["action"]
    assert r["findings"][0]["symbol"] == "billing.charge"
    sample = r["findings"][0]["sample"]
    assert sample["input"] == {"bound": {"amount": 100}}
    assert sample["new_input"] == {"bound": {"amount": 999}}


def test_the_explained_table_names_mechanism_count_example_and_rule():
    s = summarize(mixed_diff())
    (row,) = [r for r in s["explained"] if r["mechanism"] == "clock"]
    assert row["entries"] == 1 and row["example"]["symbol"] == "lib.stamp"
    assert row["rule"].startswith("facts._Masker")


def test_masked_samples_show_the_recorded_values_too():
    # A TTL that shrank from 120 s to 30 s: the values compare masked as offsets from the
    # call's own time; the finding's sample carries the recorded epochs beside them.
    old = env_fs(fact("auth.expiry", {"u": 1}, (("return", int(T_OLD) + 120),)), ts=T_OLD)
    new = env_fs(fact("auth.expiry", {"u": 1}, (("return", int(T_NEW) + 30),)), ts=T_NEW)
    (f,) = summarize(diff_facts(old, new))["failing"]
    sample = f["sample"]
    assert sample["old"][0]["value"].startswith("⟨clock:")
    assert sample["new"][0]["value"].startswith("⟨clock:")
    assert sample["raw"]["old"][0]["value"] == int(T_OLD) + 120
    assert sample["raw"]["new"][0]["value"] == int(T_NEW) + 30
    # the entries keep the recorded values off their JSON form: masked only
    d = mixed_diff()
    (e,) = [x for x in d.entries if x.symbol == "lib.stamp"]
    assert e.old_raw is not None and e.old_raw[0].value == "at 2026-11-16 10:26:05"
    assert "raw" not in json.dumps([x for x in d.to_json_dict()["entries"]
                                    if x["symbol"] == "lib.stamp"])


def test_the_quality_stamp_says_noise_was_not_measured_without_a_replicate():
    assert summarize(mixed_diff())["quality"]["noise"].startswith("not measured")


def test_values_are_capped_with_head_tail_length_and_digest():
    big = "<html>" + "x" * 740_000 + "</html>"
    capped = cap_value({"body": big, "items": list(range(100)), "short": "ok"})
    assert capped["short"] == "ok"
    assert capped["body"]["__capped__"] == "str" and capped["body"]["len"] == len(big)
    assert capped["body"]["head"].startswith("<html>") and capped["body"]["tail"].endswith(
        "</html>") and len(capped["body"]["sha1"]) == 16
    assert len(capped["items"]) == 26 and capped["items"][20] == {"__capped__": "list",
                                                                  "len": 100}


def test_report_entries_carry_capped_values():
    big = "y" * 50_000
    old = fs(fact("jinja2.Template.render", {"t": big}, (("return", big + "a"),)))
    new = fs(fact("jinja2.Template.render", {"t": big}, (("return", big + "b"),)))
    doc = diff_facts(old, new).to_json_dict()
    assert len(json.dumps(doc)) < 5_000
    entry = doc["entries"][0]
    assert entry["old"][0]["value"]["__capped__"] == "str"
    assert entry["old"][0]["value"]["sha1"] != entry["new"][0]["value"]["sha1"]


def test_text_shows_sections_one_to_three_and_the_explained_table_on_request():
    s = summarize(mixed_diff())
    lines = render_text(s)
    assert lines[0].startswith("Behaviour diff: FAIL")
    assert lines[1].startswith("Failing (3 finding(s)):")
    assert any(x.startswith("Review — input changed") for x in lines)
    assert not any(x.startswith("Explained") for x in lines)
    assert any(x.startswith("Explained") for x in render_text(s, explained=True))
    assert lines[-1].startswith("Quality: noise not measured")


def test_text_lists_at_most_the_budgeted_findings():
    old = fs(*[fact(f"lib.f{i}", {"x": i}, (("return", 1),), line=i + 1) for i in range(80)])
    new = fs(*[fact(f"lib.f{i}", {"x": i}, (("return", 2),), line=i + 1) for i in range(80)])
    lines = render_text(summarize(diff_facts(old, new)), max_lines=50)
    assert sum(1 for x in lines if x.lstrip().startswith("✗")) == 50
    assert "… 30 more, 80 in all (see --json)" in [x.strip() for x in lines]


def test_the_agent_view_carries_findings_with_actions():
    data = _findings_data(summarize(mixed_diff()))
    assert data["failing"][0]["why"].startswith("kind changed")
    assert data["review"][0]["action"].startswith("capture a baseline replicate")

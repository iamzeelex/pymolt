"""Decision 7: an INCONCLUSIVE verdict says whether it needs a person (a review item) or
evidence (BLIND, an uncomparable fact, a capture, unmeasured noise). Each reason carries its kind
from where it is made; ``verification.json`` keeps ``inconclusive_kind`` (``needs_review`` wins)."""
import json

from pymolt.interfaces import views
from pymolt.verify.models import VerificationVerdict
from pymolt.verify.report import ContractReport, SymbolStatus
from pymolt.verify.service import _stamp_verdict
from pymolt.verify.unified import UnifiedVerificationReport, build_unified_verification

REVIEW = {"category": "rewritten", "comparison": "uncomparable", "symbol": "dep.f",
          "review": "the value differs on a hand-edited line: review"}
CAPPED = {"category": "uncomparable", "comparison": "uncomparable", "symbol": "dep.g",
          "notes": ["c-site-capped: the old capture recorded the first 2000 calls"]}
NOISE = {"category": "changed", "comparison": "changed", "symbol": "dep.h",
         "review": "unmeasured noise: the value differs and its input is not fully observed"}


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def _verdict(tmp_path, *entries, blind=0, clean=True, noise_measured=None):
    """The unified verdict of two valid recordings whose fact diff holds ``entries`` and whose
    static map has ``blind`` symbols no run exercised."""
    event = {"v": 2, "t": "return", "q": "dep.f", "sym": "dep.f", "in": {"bound": {}},
             "result": 1, "where": {"file": str(tmp_path / "app.py"), "line": 1,
                                    "func": "main"}}
    old = _write(tmp_path / "old.jsonl", [event])
    new = _write(tmp_path / "new.jsonl", [event])
    fact_diff = {"verdict": "inconclusive" if entries else "pass",
                 "counts": {"equal": 3, **{e["category"]: 1 for e in entries}},
                 "entries": list(entries), "noise_measured": noise_measured}
    symbols = [SymbolStatus(target=f"dep.b{i}", dep="dep", status="blind",
                            blind_reason="not-run") for i in range(blind)]
    report = ContractReport(root=str(tmp_path), diff={"skipped_opaque": 0}, diff_clean=clean,
                            static_targets=1 + blind, confirmed=1, blind=blind,
                            symbols=symbols, fact_diff=fact_diff)
    _stamp_verdict(report, str(new), str(old), [])
    return build_unified_verification(report)


def _kind(unified):
    return unified.inconclusive_kind.value if unified.inconclusive_kind is not None else None


def test_a_review_item_alone_needs_a_person(tmp_path):
    unified = _verdict(tmp_path, REVIEW)
    assert unified.verdict is VerificationVerdict.INCONCLUSIVE
    assert _kind(unified) == "needs_review"
    (cause,) = unified.inconclusive_causes
    assert (cause.cause, cause.reason) == ("review", unified.reasons[0])
    assert "--accept ID" in cause.settle


def test_blind_alone_needs_evidence(tmp_path):
    unified = _verdict(tmp_path, blind=2)
    assert _kind(unified) == "needs_evidence"
    (cause,) = unified.inconclusive_causes
    assert cause.cause == "not-exercised" and "add a test" in cause.settle


def test_an_uncomparable_fact_alone_needs_evidence(tmp_path):
    unified = _verdict(tmp_path, CAPPED)
    assert _kind(unified) == "needs_evidence"
    (cause,) = unified.inconclusive_causes
    assert cause.cause == "uncomparable"
    assert cause.settle.startswith("capturing again does not settle it")


def test_a_review_item_and_missing_evidence_need_a_person_and_keep_both_reasons(tmp_path):
    unified = _verdict(tmp_path, REVIEW, CAPPED, blind=1)
    assert _kind(unified) == "needs_review"
    assert [c.kind.value for c in unified.inconclusive_causes].count("needs_evidence") == 2
    assert [c.reason for c in unified.inconclusive_causes] == unified.reasons


def test_noise_needs_evidence_until_a_replicate_measured_it(tmp_path):
    unmeasured = _verdict(tmp_path, NOISE)
    assert _kind(unmeasured) == "needs_evidence"
    assert unmeasured.inconclusive_causes[0].cause == "noise"
    measured = _verdict(tmp_path, NOISE, noise_measured={"replicate": "r"})
    assert _kind(measured) == "needs_review"


def test_a_missing_capture_needs_evidence(tmp_path):
    report = ContractReport(root=str(tmp_path))
    _stamp_verdict(report, None, None, [])
    unified = build_unified_verification(report)
    assert _kind(unified) == "needs_evidence"
    assert {c.cause for c in unified.inconclusive_causes} == {"capture"}


def test_pass_and_fail_have_no_kind(tmp_path):
    passed = _verdict(tmp_path)
    assert passed.verdict is VerificationVerdict.PASS and passed.inconclusive_kind is None
    failed = _verdict(tmp_path, REVIEW, clean=False)
    assert failed.verdict is VerificationVerdict.FAIL and failed.inconclusive_kind is None
    assert failed.inconclusive_causes == []


def test_the_kind_is_saved_and_a_schema_1_file_reads_with_none(tmp_path):
    unified = _verdict(tmp_path, CAPPED)
    path = unified.save(tmp_path)
    saved = json.loads(path.read_text())
    assert (saved["schema_version"], saved["inconclusive_kind"]) == (2, "needs_evidence")
    loaded = UnifiedVerificationReport.load(tmp_path)
    assert loaded.inconclusive_kind == unified.inconclusive_kind
    assert loaded.inconclusive_causes == unified.inconclusive_causes

    saved["schema_version"] = 1
    for key in ("inconclusive_kind", "inconclusive_causes"):
        del saved[key]
    for item in saved["evidence"]:
        del item["causes"]
    del saved["contract"]["inconclusive_causes"]
    path.write_text(json.dumps(saved))
    old = UnifiedVerificationReport.load(tmp_path)
    assert old is not None and old.verdict is VerificationVerdict.INCONCLUSIVE
    assert old.inconclusive_kind is None and old.inconclusive_causes == []


def test_the_agent_view_carries_the_kind_and_what_settles_it(tmp_path):
    view = views.verify_view(_verdict(tmp_path, CAPPED, blind=1), tmp_path, saved_path=None)
    data = view["data"]
    assert data["inconclusive_kind"] == "needs_evidence"
    assert [c["cause"] for c in data["inconclusive_causes"]] == ["uncomparable",
                                                                 "not-exercised"]
    assert "\nNeeds evidence: capturing again does not settle it" in view["summary"]
    assert view["hint"].startswith("Needs evidence:")
    review = views.verify_view(_verdict(tmp_path, REVIEW, blind=1), tmp_path, saved_path=None)
    assert review["data"]["inconclusive_kind"] == "needs_review"
    assert "It also needs evidence (1 reason(s))." in review["summary"]
    assert views.verify_view(_verdict(tmp_path), tmp_path,
                             saved_path=None)["data"]["inconclusive_kind"] is None

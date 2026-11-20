"""An operator's forward and reflected methods are alternatives at one statement.

For ``a > b`` the static map names both ``A.__gt__`` (on the left operand) and ``B.__lt__`` (the
reflected method, on the right one). Python calls the reflected one only when the forward one
returns NotImplemented (or the right operand's type is a subclass of the left's), so at one
statement one of the two runs. Either recorded there confirms the pair; the pair is BLIND only
when neither was, and it counts once in coverage and recall.
"""
import json
import sys

import pytest

from pymolt.ingestion.config import EnvConfig, ToolChoice
from pymolt.verify import service
from pymolt.verify.contact_map import build_contact_map
from pymolt.verify.models import CaptureMode, VerificationVerdict
from pymolt.verify.report import NOT_RUN, RECORDED, build_contract_report

CORE = '''import fakedep


def newer(a, b):
    return fakedep.Ver(a) > fakedep.Ver(b)  # @gt


def limit(b):
    return fakedep.LIMIT < fakedep.Ver(b)  # @reflected


def same(a, b):
    return fakedep.Ver(a) == fakedep.Ver(b)  # @eq


def mixed(a, b):
    return fakedep.Ver(a) == fakedep.Other(b)  # @eq-mixed


def local(x, b):
    return x > fakedep.Lone(b)  # @local


def cold(a, b):
    return fakedep.Cold(a) > fakedep.Cold(b)  # @cold
'''


def _line(marker):
    for number, text in enumerate(CORE.splitlines(), 1):
        if text.endswith("# @" + marker):
            return number
    raise AssertionError(marker)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "app").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text("")
    (root / "app" / "core.py").write_text(CORE)
    return root


def _contacts(root):
    return {(c.target, c.line): c for c in build_contact_map(root).contacts
            if c.kind == "protocol"}


def test_the_map_links_an_operators_two_methods(project):
    got = _contacts(project)
    gt, ref, eq = _line("gt"), _line("reflected"), _line("eq")
    assert got[("fakedep.Ver.__gt__", gt)].alternatives == ["fakedep.Ver.__lt__"]
    assert got[("fakedep.Ver.__lt__", gt)].alternatives == ["fakedep.Ver.__gt__"]
    assert got[("fakedep.LIMIT.__lt__", ref)].alternatives == ["fakedep.Ver.__gt__"]
    assert got[("fakedep.Ver.__gt__", ref)].alternatives == ["fakedep.LIMIT.__lt__"]
    # ``==`` maps to the same method on both sides: one contact, its own alternative
    assert got[("fakedep.Ver.__eq__", eq)].alternatives == ["fakedep.Ver.__eq__"]
    assert got[("fakedep.Other.__eq__", _line("eq-mixed"))].alternatives \
        == ["fakedep.Ver.__eq__"]
    # a left operand that is not the dependency's: the reflected method stands alone
    assert got[("fakedep.Lone.__lt__", _line("local"))].alternatives == []


def _trace(root, tmp_path, recorded, ran):
    """A trace recording each ``(symbol, marker)`` call at its line, and an executed record
    listing the ``ran`` markers' lines (plus the module's and defs' lines)."""
    core = str(root / "app" / "core.py")
    records = [{"q": sym, "sym": sym, "t": "return", "in": {"bound": {}}, "result": None,
                "where": {"file": core, "line": _line(marker), "func": "f"}}
               for sym, marker in recorded]
    path = tmp_path / "trace.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records))
    lines = sorted({1, 4, 8, 12, 16, 20, 24} | {_line(m) for m in ran})
    executed = {"v": 1, "granularity": "line", "complete": True, "until": None,
                "files": {core: {"lines": ",".join(map(str, lines))}}}
    (tmp_path / "trace.jsonl.meta.json").write_text(
        json.dumps({"processes": [{"executed": executed}]}))
    return path


def _report(root, trace):
    # exact-string identity: the trace names the static paths themselves
    return build_contract_report(root, trace=trace, resolve_identities=False)


def _at(rep, marker):
    return {c.target: c for c in rep.contacts if c.line == _line(marker) and c.kind == "protocol"}


def _symbol(rep, target):
    return next(s for s in rep.symbols if s.target == target)


CALLS = [("fakedep.Ver", m) for m in ("gt", "reflected", "eq", "eq-mixed")] \
    + [("fakedep.Other", "eq-mixed"), ("fakedep.Lone", "local")]


def test_the_forward_method_recorded_confirms_the_reflected_one(project, tmp_path):
    trace = _trace(project, tmp_path, CALLS + [("fakedep.Ver.__gt__", "gt")], ran=["gt"])
    rep = _report(project, trace)

    at = _at(rep, "gt")
    assert at["fakedep.Ver.__gt__"].status == RECORDED
    assert at["fakedep.Ver.__gt__"].matched_by == "site"
    assert at["fakedep.Ver.__lt__"].status == RECORDED
    assert at["fakedep.Ver.__lt__"].matched_by == "alternative"
    lt = _symbol(rep, "fakedep.Ver.__lt__")
    assert lt.status == "confirmed" and lt.matched_by == "alternative"
    assert lt.observed_as == [] and lt.observed_at == []
    assert "fakedep.Ver.__lt__" not in [s.target for s in rep.symbols if s.status == "blind"]


def test_the_pair_counts_once_in_coverage_and_recall(project, tmp_path):
    trace = _trace(project, tmp_path, CALLS + [("fakedep.Ver.__gt__", "gt")], ran=["gt"])
    rep = _report(project, trace)
    at = _at(rep, "gt")
    counted = [c for c in at.values() if c.counted_with is None]
    assert len(counted) == 1
    others = [c for c in at.values() if c.counted_with is not None]
    assert [c.counted_with for c in others] == [counted[0].target]
    # the statement holds the Ver(a) / Ver(b) call contact and the pair: two counted contacts
    gt_line = [c for c in rep.contacts if c.line == _line("gt") and c.counted_with is None]
    assert {c.status for c in gt_line} == {RECORDED} and len(gt_line) == 2


def test_only_the_reflected_method_ran_confirms_the_forward_one(project, tmp_path):
    """``LIMIT < Ver(b)`` where LIMIT is a plain int: ``int.__lt__`` returns NotImplemented
    and Python calls ``Ver.__gt__``, the reflected method; ``LIMIT.__lt__`` never runs."""
    trace = _trace(project, tmp_path, CALLS + [("fakedep.Ver.__gt__", "reflected")],
                   ran=["reflected"])
    rep = _report(project, trace)

    at = _at(rep, "reflected")
    assert at["fakedep.Ver.__gt__"].status == RECORDED
    assert at["fakedep.LIMIT.__lt__"].status == RECORDED
    assert at["fakedep.LIMIT.__lt__"].matched_by == "alternative"
    assert _symbol(rep, "fakedep.LIMIT.__lt__").status == "confirmed"


def test_a_pair_that_never_ran_stays_blind_and_counts_once(project, tmp_path):
    trace = _trace(project, tmp_path, CALLS, ran=[])
    rep = _report(project, trace)

    at = _at(rep, "cold")
    assert {c.status for c in at.values()} == {NOT_RUN}
    assert len([c for c in at.values() if c.counted_with is None]) == 1
    for target in ("fakedep.Cold.__gt__", "fakedep.Cold.__lt__"):
        sym = _symbol(rep, target)
        assert sym.status == "blind" and sym.blind_reason == NOT_RUN


def test_a_pair_that_ran_unrecorded_is_blind(project, tmp_path):
    trace = _trace(project, tmp_path, CALLS, ran=["gt"])
    rep = _report(project, trace)

    at = _at(rep, "gt")
    assert {c.status for c in at.values()} == {"ran-not-recorded"}
    for target in ("fakedep.Ver.__gt__", "fakedep.Ver.__lt__"):
        assert _symbol(rep, target).status == "blind"
    assert rep.contacts_ran_not_recorded == len(
        [c for c in rep.contacts if c.status == "ran-not-recorded" and c.counted_with is None])


def test_equality_keeps_its_one_method(project, tmp_path):
    recorded = CALLS + [("fakedep.Ver.__eq__", "eq"), ("fakedep.Ver.__eq__", "eq-mixed")]
    rep = _report(project, _trace(project, tmp_path, recorded, ran=["eq", "eq-mixed"]))

    (eq,) = _at(rep, "eq").values()
    assert eq.status == RECORDED and eq.matched_by == "site" and eq.counted_with is None
    mixed = _at(rep, "eq-mixed")
    assert mixed["fakedep.Ver.__eq__"].matched_by == "site"
    assert mixed["fakedep.Other.__eq__"].status == RECORDED
    assert mixed["fakedep.Other.__eq__"].matched_by == "alternative"

    # not recorded where it ran: BLIND, as any call
    rep = _report(project, _trace(project, tmp_path, CALLS, ran=["eq"]))
    (eq,) = _at(rep, "eq").values()
    assert eq.status == "ran-not-recorded"
    assert _symbol(rep, "fakedep.Ver.__eq__").status == "blind"


def test_a_reflected_method_alone_needs_its_own_record(project, tmp_path):
    """``x > Ver(b)`` with ``x`` a local: nothing else at the statement can stand for it."""
    rep = _report(project, _trace(project, tmp_path, CALLS + [("fakedep.Ver.__gt__", "gt")],
                                  ran=["gt", "local"]))
    (alone,) = _at(rep, "local").values()
    assert alone.target == "fakedep.Lone.__lt__" and alone.status == "ran-not-recorded"
    assert alone.counted_with is None


# ── through capture and verify ───────────────────────────────────────────────

def _toy(root, body):
    proj = root / "proj"
    (proj / "app").mkdir(parents=True)
    (proj / "app" / "__init__.py").write_text("")
    (proj / "app" / "core.py").write_text(body)
    (proj / "requirements.txt").write_text("packaging\n")
    EnvConfig(selected_manifest="requirements.txt", selected_tool=ToolChoice.UV,
              base_python="3.12", target_python="3.12").save(
        proj / ".pymolt" / "env_config.json")
    run = root / "run.py"
    run.write_text(f"import sys\nsys.path.insert(0, {str(proj)!r})\n"
                   "from app.core import newer\nprint(newer('2.0', '1.0'))\n")
    for when in ("baseline", "post_migration"):
        service.capture_named_trace(proj, when, CaptureMode.TEST_SUITE,
                                    command=[sys.executable, str(run)], target="packaging")
    return service.build_contract_report_from_state(proj)


def test_an_unchanged_capture_pair_with_a_comparison_passes(tmp_path):
    pytest.importorskip("packaging")
    compared = _toy(tmp_path / "compared", (
        "from packaging.version import Version\n\n\n"
        "def newer(a, b):\n    return Version(a) > Version(b)\n"))
    plain = _toy(tmp_path / "plain", (
        "from packaging.version import Version\n\n\n"
        "def newer(a, b):\n    return str(Version(a))\n"))

    assert [s.target for s in compared.symbols if s.status == "blind"] == []
    assert compared.recall == 1.0
    assert plain.verdict is VerificationVerdict.PASS
    assert compared.verdict is plain.verdict, compared.verdict_reasons

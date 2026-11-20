"""A baseline replicate: the baseline's command run twice measures run-to-run noise.

Unit tests build fact sets directly; service and CLI tests capture with a stand-in tracer that
writes chosen events (no subprocess, no container).
"""
import hashlib
import json
import sys
import uuid
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent))

from test_fact_noise import fact, fs  # noqa: E402

from pymolt.ingestion.config import EnvConfig, ToolChoice  # noqa: E402
from pymolt.interfaces.cli.commands import app  # noqa: E402
from pymolt.status import build_status  # noqa: E402
from pymolt.verify import service  # noqa: E402
from pymolt.verify.fact_report import summarize  # noqa: E402
from pymolt.verify.facts import (  # noqa: E402
    CappedSite,
    FactCategory,
    Outcome,
    SiteTally,
    diff_facts,
)
from pymolt.verify.models import CaptureMode, ContractState  # noqa: E402
from pymolt.verify.replicate import (  # noqa: E402
    NOISE_PROFILE_PATH,
    apply_noise_profile,
    build_noise_profile,
    timing_difference,
)
from pymolt.verify.service import TraceCaptureResult  # noqa: E402

# ── the profile and how it judges a diff ──────────────────────────────────────────────────


def side(*, port, total, extra=None, status="ok", line_g=2):
    facts = [
        fact("http.connect", {"port": port}, (("return", status),), line=1,
             text="c = http.connect(port)"),
        fact("billing.total", {"cart": "c1"}, (("return", total),), line=line_g,
             text="t = billing.total(cart)"),
    ]
    if extra is not None:
        facts.append(fact("cache.warm", {"k": "a"}, (("return", extra),), line=3,
                          text="cache.warm('a')"))
    return fs(*facts)


def test_the_profile_names_the_sites_the_baseline_varies_at():
    profile = build_noise_profile(side(port=40001, total=10, extra=1),
                                  side(port=40002, total=10))
    varying = {json.loads(k)[0]: v for k, v in profile["varying"].items()}
    assert varying == {"http.connect": ["input"], "cache.warm": ["presence"]}
    equal = {json.loads(k)[0]: v for k, v in profile["equal"].items()}
    assert equal == {"billing.total": ["equal@complete"]}
    assert profile["presence_evidence"] is True


def judged(baseline, replicate, post):
    d = diff_facts(baseline, post)
    counts = apply_noise_profile(d, build_noise_profile(baseline, replicate), "test replicate")
    return d, counts, {e.symbol: e for e in d.entries}


def test_a_difference_the_baseline_shows_too_is_measured_noise():
    d, counts, by = judged(side(port=40001, total=10), side(port=40002, total=10),
                           side(port=40003, total=10))
    e = by["http.connect"]
    assert e.noise and not e.fails and not e.inconclusive
    assert e.explained.startswith("replicate")
    assert counts["explained"] == 1 and d.verdict.value == "pass"
    assert d.noise_measured["sites_varying"] == 1
    assert any(w.startswith("replicate:") for w in d.warnings)
    assert not any(w.startswith("run-variation") for w in d.warnings)


def test_a_diff_of_measured_noise_alone_is_no_evidence():
    # Every difference is run-to-run noise and nothing was compared equal: nothing shows the
    # behaviour unchanged, so the verdict is INCONCLUSIVE, never PASS (honesty rule).
    def connect_only(port):
        return fs(fact("http.connect", {"port": port}, (("return", "ok"),), line=1,
                       text="c = http.connect(port)"))

    d, counts, by = judged(connect_only(40001), connect_only(40002), connect_only(40003))
    e = by["http.connect"]
    assert e.noise and not e.fails and not e.inconclusive and not e.compared_equal
    assert counts["explained"] == 1
    assert d.verdict.value == "inconclusive"
    assert d.to_json_dict()["compared_equal"] == 0


def test_measured_noise_beside_a_fact_compared_equal_still_passes():
    d, _, by = judged(side(port=40001, total=10), side(port=40002, total=10),
                      side(port=40003, total=10))
    assert by["http.connect"].noise and by["billing.total"].compared_equal
    assert d.verdict.value == "pass" and d.to_json_dict()["compared_equal"] == 1


def test_a_change_at_a_site_the_baseline_repeats_exactly_is_confirmed():
    d, counts, by = judged(side(port=40001, total=10), side(port=40002, total=10),
                           side(port=40003, total=12))
    e = by["billing.total"]
    assert e.fails and "confirmed deterministic: equal in the baseline replicate" in e.notes
    assert counts["confirmed"] == 1


def test_another_kind_of_difference_at_a_varying_site_still_fails():
    # The replicate shows the port varying; a new raise there is no run noise.
    base = side(port=40001, total=10)
    post = side(port=40003, total=10)
    post.facts[0].outcomes[0].kind, post.facts[0].outcomes[0].value = "raise", "OSError"
    d, _, by = judged(base, side(port=40002, total=10), post)
    assert by["http.connect"].fails


def test_an_input_change_at_a_stable_site_is_judged_strictly():
    # Without a replicate an unexplained input change is a review; the replicate shows the
    # site repeats exactly, so the change is the migration's.
    base, rep = side(port=1, total=10), side(port=1, total=10)
    post = side(port=1, total=10)
    post.facts[1].input = {"bound": {"cart": "c2"}}
    plain = diff_facts(base, post)
    (e,) = [x for x in plain.entries if x.symbol == "billing.total"]
    assert e.review and e.review.startswith("input changed") and not e.fails
    d, counts, by = judged(base, rep, post)
    assert by["billing.total"].fails and counts["strict"] == 1
    # the report labels the failure as the input change it is
    (finding,) = summarize(d)["failing"]
    assert finding["change"] == "input changed"
    assert finding["why"].startswith("input changed; stable across baseline runs")


def test_a_replicate_that_lost_events_is_no_presence_evidence():
    rep = side(port=40002, total=10)
    rep.dropped_events = 5
    base = side(port=40001, total=10, extra=1)
    d, counts, by = judged(base, rep, side(port=40003, total=10))
    e = by["cache.warm"]
    assert e.category is FactCategory.DISAPPEARED and e.fails and not e.noise
    assert d.noise_measured["presence_evidence"] is False


def test_the_report_says_noise_was_measured():
    d, _, _ = judged(side(port=40001, total=10), side(port=40002, total=10),
                     side(port=40003, total=12))
    q = summarize(d)["quality"]
    assert isinstance(q["noise"], dict) and q["noise"]["replicate"] == "test replicate"


# ── capture, validation, verify (stand-in tracer) ─────────────────────────────────────────

APP = "import http, billing\n\nc = http.connect(port)\nt = billing.total(cart)\n"


def _configure(root):
    (root / "requirements.txt").write_text("flask==2.0.3\n", encoding="utf-8")
    EnvConfig(selected_manifest="requirements.txt", selected_tool=ToolChoice.UV,
              base_python="3.11").save(root / ".pymolt" / "env_config.json")
    (root / "app.py").write_text(APP, encoding="utf-8")


def _event(root, sym, line, args, result):
    return {"v": 2, "t": "return", "q": sym, "sym": sym, "in": {"bound": args},
            "result": result, "ts": 1794824765.0, "dur": 0.01,
            "where": {"file": str(root / "app.py"), "line": line, "func": "<module>"}}


class Tracer:
    """``capture_trace`` stand-in: each run writes the next prepared list of events."""

    def __init__(self, root, runs):
        self.root, self.runs, self.commands = root, list(runs), []

    def __call__(self, target, command, out_path, backend, **kw):
        self.commands.append(list(command))
        port, total = self.runs.pop(0)
        events = [_event(self.root, "http.connect", 3, {"port": port}, "ok"),
                  _event(self.root, "billing.total", 4, {"cart": "c1"}, total)]
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text("".join(json.dumps(e) + "\n" for e in events))
        return TraceCaptureResult(out_path=str(out_path), events=len(events), processes=1,
                                  returncode=0, where="local")


def _capture(root, when, tracer, monkeypatch):
    monkeypatch.setattr(service, "capture_trace", tracer)
    return service.capture_named_trace(root, when, CaptureMode.TEST_SUITE,
                                       command=["pytest", "-q"], target="all", stdlib="none")


def test_a_replicate_reruns_the_baseline_and_refuses_other_code(tmp_path, monkeypatch):
    _configure(tmp_path)
    tracer = Tracer(tmp_path, [(40001, 10), (40002, 10)])
    base = _capture(tmp_path, "baseline", tracer, monkeypatch)
    rep = service.capture_baseline_replicate(tmp_path)
    assert rep.replicate_of == base.captured_at and rep.command == base.command
    assert tracer.commands == [["pytest", "-q"], ["pytest", "-q"]]
    state = service.load_contract_state(tmp_path)
    assert state.baseline_replicate is not None
    assert service.usable_replicate(tmp_path, state) == (state.baseline_replicate, None)
    with pytest.raises(ValueError, match="baseline's own command"):
        service.capture_baseline_replicate(tmp_path, command=["tox"])
    (tmp_path / "app.py").write_text(APP + "# edited\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"code changed since the baseline \(app.py\)"):
        service.capture_baseline_replicate(tmp_path)
    assert tracer.commands == [["pytest", "-q"], ["pytest", "-q"]]  # refused before running


def test_replacing_the_baseline_archives_its_replicate(tmp_path, monkeypatch):
    _configure(tmp_path)
    tracer = Tracer(tmp_path, [(1, 10), (2, 10), (3, 10)])
    _capture(tmp_path, "baseline", tracer, monkeypatch)
    service.capture_baseline_replicate(tmp_path)
    _capture(tmp_path, "baseline", tracer, monkeypatch)
    assert service.load_contract_state(tmp_path).baseline_replicate is None
    archived = list((tmp_path / ".pymolt" / "contract_traces" / "archive").glob(
        "baseline_replicate-*.jsonl"))
    assert len(archived) == 1


def test_a_replicate_of_an_earlier_baseline_is_ignored_with_a_note(tmp_path, monkeypatch):
    _configure(tmp_path)
    _capture(tmp_path, "baseline", Tracer(tmp_path, [(1, 10)]), monkeypatch)
    monkeypatch.setattr(service, "capture_trace", Tracer(tmp_path, [(2, 10)]))
    service.capture_baseline_replicate(tmp_path)
    state = service.load_contract_state(tmp_path)
    state.baseline_replicate.replicate_of = "2026-01-01T00:00:00+00:00"
    replicate, note = service.usable_replicate(tmp_path, state)
    assert replicate is None and "earlier baseline" in note
    state.baseline_replicate.replicate_of = state.baseline.captured_at
    state.baseline_replicate.sample_rate = 0.5
    assert "sample rate differs" in service.usable_replicate(tmp_path, state)[1]


def test_verify_judges_the_migration_with_the_measured_noise(tmp_path, monkeypatch):
    _configure(tmp_path)
    tracer = Tracer(tmp_path, [(40001, 10), (40002, 10), (40003, 12)])
    _capture(tmp_path, "baseline", tracer, monkeypatch)
    service.capture_baseline_replicate(tmp_path)
    _capture(tmp_path, "post_migration", tracer, monkeypatch)
    rep = service.build_contract_report_from_state(tmp_path)
    by = {e["symbol"]: e for e in rep.all_fact_diff["entries"]}
    assert by["http.connect"].get("noise") and not by["http.connect"]["fails"]
    assert by["billing.total"]["fails"]
    assert "confirmed deterministic: equal in the baseline replicate" in by[
        "billing.total"]["notes"]
    assert rep.all_fact_diff["noise_measured"]["explained"] == 1
    cache = json.loads((tmp_path / NOISE_PROFILE_PATH).read_text())
    assert cache["schema"] == "pymolt.noise-profile/5" and cache["identity"]["pymolt"]
    # a stale replicate is ignored, and the report says so
    state = service.load_contract_state(tmp_path)
    state.baseline_replicate.replicate_of = "earlier"
    state.save(tmp_path / ".pymolt" / "contract_state.json")
    rep = service.build_contract_report_from_state(tmp_path)
    assert rep.all_fact_diff["noise_measured"] is None
    assert any(n.startswith("baseline replicate ignored") for n in rep.notes)


def test_state_without_a_replicate_still_loads(tmp_path):
    path = tmp_path / ".pymolt" / "contract_state.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 1, "baseline": None}))
    assert ContractState.load(path).baseline_replicate is None


def test_the_cli_captures_a_replicate_and_refuses_one_after_migration(tmp_path, monkeypatch):
    _configure(tmp_path)
    tracer = Tracer(tmp_path, [(1, 10), (2, 10), (3, 10)])
    monkeypatch.setattr(service, "capture_trace", tracer)
    runner = CliRunner()
    res = runner.invoke(app, ["capture", "baseline", "--mode", "tests", "--runs", "2",
                              "--project-dir", str(tmp_path), "--stdlib", "none", "--",
                              "pytest", "-q"])
    assert res.exit_code == 0, res.output
    assert service.load_contract_state(tmp_path).baseline_replicate is not None
    res = runner.invoke(app, ["capture", "post-migration", "--replicate", "--project-dir",
                              str(tmp_path), "--json"])
    assert res.exit_code != 0 and "baseline's run-to-run noise" in res.output
    res = runner.invoke(app, ["capture", "baseline", "--replicate", "--force", "--json",
                              "--project-dir", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["replicate_of"]


def test_status_suggests_a_replicate_when_noise_left_the_verdict_open(tmp_path, monkeypatch):
    from pymolt import status as status_mod

    st = status_mod.FunnelStatus(project_dir=str(tmp_path))

    class V:
        class verdict:  # noqa: N801
            value = "inconclusive"
        reasons = ["3 call(s) whose input changed with no visible cause (x); a baseline "
                   "replicate would tell run noise from a real change"]

    done = status_mod.PhaseState(phase="x", state=status_mod.DONE)
    cfg = EnvConfig(selected_manifest="requirements.txt", selected_tool=ToolChoice.UV,
                    base_python="3.11", target_python="3.12")
    st.phases.append(status_mod.PhaseState(phase="scan", state=status_mod.DONE))
    st.phases.append(status_mod.PhaseState(phase="assess", state=status_mod.DONE))
    for can, expected in ((True, "pymolt capture baseline --replicate"),
                          (False, "pymolt rollback .")):
        status_mod._decide_next(st, cfg, done, done, done, done, done, None, V(), [],
                                has_replicate=False, can_replicate=lambda c=can: c)
        assert st.next_command == expected
    status_mod._decide_next(st, cfg, done, done, done, done, done, None, V(), [],
                            has_replicate=True)
    assert st.next_command is None
    _configure(tmp_path)
    assert build_status(tmp_path).next_command  # the real path runs without a replicate


# ── wiring: an explicit post-migration recording; relative slot paths ─────────────────────

def _baseline_replicate_post(tmp_path, monkeypatch):
    """Baseline and replicate in their slots; the post-migration recording left outside the
    slots (as after a rollback cleared it)."""
    _configure(tmp_path)
    tracer = Tracer(tmp_path, [(40001, 10), (40002, 10), (40003, 12)])
    _capture(tmp_path, "baseline", tracer, monkeypatch)
    service.capture_baseline_replicate(tmp_path)
    post = _capture(tmp_path, "post_migration", tracer, monkeypatch)
    kept = tmp_path / "post.jsonl"
    Path(post.trace_path).rename(kept)
    state = service.load_contract_state(tmp_path)
    state.post_migration = None
    state.save(tmp_path / ".pymolt" / "contract_state.json")
    return kept


def test_an_explicit_post_migration_recording_still_uses_the_replicate(tmp_path, monkeypatch):
    post = _baseline_replicate_post(tmp_path, monkeypatch)
    baseline = service.load_contract_state(tmp_path).baseline.trace_path
    # --baseline naming the baseline slot's own recording (no --baseline is
    # test_an_explicit_post_migration_recording_is_compared_with_the_baseline_slot)
    rep = service.build_contract_report_from_state(
        tmp_path, trace_override=str(post), against_override=baseline)
    assert rep.all_fact_diff["noise_measured"]["explained"] == 1
    # another "before" recording is not the baseline the replicate measured
    other = tmp_path / "other.jsonl"
    other.write_bytes(Path(baseline).read_bytes())
    rep = service.build_contract_report_from_state(
        tmp_path, trace_override=str(post), against_override=str(other))
    assert rep.all_fact_diff["noise_measured"] is None


def test_the_replicate_is_used_from_any_directory(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    post = _baseline_replicate_post(project, monkeypatch)
    raw = json.loads((project / ".pymolt" / "contract_state.json").read_text())
    for name in ("baseline", "baseline_replicate"):  # saved project-relative
        assert not Path(raw[name]["trace_path"]).is_absolute()
        assert not Path(raw[name]["sources_path"]).is_absolute()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    # a state read without anchoring (relative paths as stored) still resolves
    state = ContractState.load(project / ".pymolt" / "contract_state.json")
    replicate, note = service.usable_replicate(project, state)
    assert note is None and Path(replicate.trace_path).is_absolute()
    assert service.sources_changed(project, state.baseline.sources_path) == []
    res = CliRunner().invoke(app, ["verify", str(project), "--post-migration", str(post),
                                   "--json"])
    doc = json.loads(res.stdout)
    measured = doc["contract"]["all_fact_diff"]["noise_measured"]
    assert measured and measured["explained"] == 1, res.output


# ── evidence classes: a value change at equal complete input is explained only by one ──────

PACK, PACK_LINE = "msgpack.packb", "return msgpack.dumps((self.type, self.data, self.node_id))"


def pack_run(node, port, stats_bytes):
    """One run at one msgpack site: a heartbeat carrying this run's uuid4 node id (its bytes
    carry the id: equal once masked, or not), a ping to this run's port (the input changes)
    and a stats message with a fixed, fully observed input."""
    def at(bound, result, first):
        return fact(PACK, {"o": {"__tuple__": bound}}, (("return", {"__bytes__": result}),),
                    file="locust/rpc/protocol.py", func="serialize", line=14, text=PACK_LINE,
                    first=first)

    digest = hashlib.sha1(node.encode()).hexdigest()  # derived from the id, never masked
    return fs(fact("uuid.uuid4", {}, (("return", {"__uuid__": node}),), line=5,
                   text="uuid4()", first=0),
              at(["heartbeat", node.replace("-", "")], digest, 1),
              at(["ping", port], "aa", 2),
              at(["stats", {"__dict__": [["users", 3], ["state", "running"]]}], stats_bytes, 3))


def test_inputs_that_vary_at_a_site_never_explain_a_value_change_at_complete_input():
    n1, n2, n3 = (str(uuid.uuid5(uuid.NAMESPACE_DNS, f"n{i}")) for i in range(3))
    b1, b2 = pack_run(n1, 40001, "93a5"), pack_run(n2, 40002, "93a5")
    profile = build_noise_profile(b1, b2)
    (classes,) = [v for k, v in profile["varying"].items() if json.loads(k)[0] == PACK]
    assert "value@complete" not in classes and set(classes) & {"value@masked", "input"}
    (same,) = [v for k, v in profile["equal"].items() if json.loads(k)[0] == PACK]
    assert "equal@complete" in same
    # the post-migration run packs the stats dict in another key order: a true finding
    d = diff_facts(b1, pack_run(n3, 40003, "93a6"))
    apply_noise_profile(d, profile, "test replicate")
    stats = [e for e in d.entries if e.symbol == PACK and e.input["bound"]["o"][
        "__tuple__"][0] == "stats"]
    (e,) = stats
    assert e.fails and not e.noise
    assert "confirmed deterministic: equal in the baseline replicate" in e.notes
    # ... while the masked-input and changed-input differences there are measured noise
    others = [x for x in d.entries if x.symbol == PACK and x is not e
              and x.category is not FactCategory.EQUAL]
    assert others and all(x.noise for x in others)


def test_a_value_change_at_complete_input_the_replicate_shows_too_is_noise():
    n1, n2, n3 = (str(uuid.uuid5(uuid.NAMESPACE_DNS, f"n{i}")) for i in range(3))
    b1 = pack_run(n1, 40001, "93a5")
    profile = build_noise_profile(b1, pack_run(n2, 40002, "93b5"))
    d = diff_facts(b1, pack_run(n3, 40003, "93c5"))
    apply_noise_profile(d, profile, "test replicate")
    assert not failing_entries(d)


def failing_entries(d):
    return [e for e in d.entries if e.fails]


def test_an_explicit_post_migration_recording_is_compared_with_the_baseline_slot(
        tmp_path, monkeypatch):
    post = _baseline_replicate_post(tmp_path, monkeypatch)
    state = service.load_contract_state(tmp_path)
    state.baseline.dropped_events = 7
    state.save(tmp_path / ".pymolt" / "contract_state.json")
    rep = service.build_contract_report_from_state(tmp_path, trace_override=str(post))
    assert rep.all_fact_diff["noise_measured"]["explained"] == 1
    assert any("baseline capture dropped 7 event(s)" in r for r in rep.verdict_reasons)


# ── evidence classes the locust re-capture exposed (2026-11-16) ───────────────────────────


def _site_run(*ports):
    return fs(*[fact("urlparse", {"url": f"http://127.0.0.1:{p}"},
                     (("return", f"127.0.0.1:{p}"),), line=1, text="u = urlparse(url)")
                for p in ports])


def test_fresh_inputs_grouped_in_the_replicate_explain_an_input_change_paired_in_the_diff():
    # The replicate's runs made 2 and 3 calls with fresh ports: an unpaired-site group. The
    # migration's diff pairs its calls one to one (input changed). Same noise: the class a
    # site gets must not depend on whether its call counts happen to match. Before, the
    # group was its own class and the recurring port made the change "confirmed" -> FAIL.
    base, rep, post = _site_run(50000, 40001), _site_run(50000, 40002, 40004), \
        _site_run(50000, 40003)
    d, counts, by = judged(base, rep, post)
    changed = [e for e in d.entries if e.symbol == "urlparse" and e.new_input is not None]
    assert changed and all(e.noise and not e.fails for e in changed)
    assert d.verdict.value == "pass"


def _calls_run(raises=0, returns=75):
    outs = [Outcome(kind="return", value=True, count=returns)]
    if raises:
        outs.append(Outcome(kind="raise", value="greenlet.GreenletExit", count=raises))
    f = fact("Group.join", {"self": {"__opaque__": "gevent.pool.Group"}}, line=4,
             text="g.join()")
    f.outcomes = outs
    f.count = returns + raises
    return fs(f)


def test_a_rare_new_outcome_is_not_called_deterministic():
    # 1 GreenletExit in 76 calls on the post side; the baseline's two runs (150 calls) would
    # show it about twice as noise: their agreement proves nothing. Kind stays strict.
    d, counts, by = judged(_calls_run(), _calls_run(), _calls_run(raises=1))
    e = by["Group.join"]
    assert e.fails
    assert not any("confirmed deterministic" in n for n in e.notes)
    assert any(n.startswith("rare outcome: 1 of 76") for n in e.notes)
    assert counts["rare"] == 1
    why = summarize(d)["failing"][0]["why"]
    assert "rare outcome" in why and "confirmed" not in why


def test_a_frequent_new_outcome_is_still_confirmed():
    d, counts, by = judged(_calls_run(), _calls_run(), _calls_run(raises=40, returns=36))
    assert any("confirmed deterministic" in n for n in by["Group.join"].notes)
    assert counts["confirmed"] == 1


# ── evidence that facts' counts hide: calls that moved between inputs ──────────────────────

def _logged(**counts):
    """A logging site called with a user count; ``u8=8`` means 8 calls with ``{"U": 8}``."""
    facts = []
    for name, n in counts.items():
        users = int(name[1:])
        f = fact("json.dumps", {"obj": {"__dict__": [["U", users]]}},
                 (("return", f'{{"U": {users}}}'),), line=7, text="s = json.dumps(counts)",
                 count=n)
        f.outcomes[0].count = n
        facts.append(f)
    return fs(*facts)


def test_calls_that_moved_between_inputs_in_the_replicate_are_input_variation():
    # Same inputs in both baseline runs, but some calls logged 4 users in one run and 5 or 6
    # in the other: what reaches the call depends on the run (timing), although every value
    # recurs and each pair is equal.
    profile = build_noise_profile(_logged(u4=6, u5=3, u6=5, u8=8), _logged(u4=2, u5=6, u6=6, u8=8))
    assert profile["varying"] == {}
    (leaves,) = profile["input_shifts"].values()
    assert leaves == ['obj/"U"']


def test_an_input_change_where_the_replicate_moved_calls_between_inputs_is_noise():
    # locust runners.py:1402: both baseline runs logged 8 users eight times, the post side 7.
    base, rep = _logged(u4=6, u5=3, u6=5, u8=8), _logged(u4=2, u5=6, u6=6, u8=8)
    post = _logged(u4=4, u5=4, u6=7, u7=8)
    d, counts, by = judged(base, rep, post)
    moved = [e for e in d.entries if e.new_input is not None and e.input != e.new_input]
    assert moved and all(e.noise and not e.fails for e in moved)
    assert not counts.get("confirmed")


def test_more_calls_of_the_same_input_in_one_run_is_no_input_variation():
    # A retry loop that polled once more in one run: the volume varies, not what reached the
    # call. An input change at that site stays strict.
    base, rep = _logged(u0=5, u3=1), _logged(u0=6, u3=1)
    profile = build_noise_profile(base, rep)
    assert profile["varying"] == {} and profile["input_shifts"] == {}
    d, _, by = judged(base, rep, _logged(u0=5, u2=1))
    assert any(e.fails for e in d.entries)


def test_sampled_runs_infer_nothing_from_counts():
    tally = [SiteTally(symbol="json.dumps", file="app.py", func="main", line=7,
                       text="s = json.dumps(counts)", count=22, returns=22, raises={})]
    base, rep = _logged(u4=6, u5=3, u6=5, u8=8), _logged(u4=2, u5=6, u6=6, u8=8)
    base.site_tallies, rep.site_tallies = tally, tally
    assert build_noise_profile(base, rep)["input_shifts"] == {}


@pytest.mark.parametrize("loss", [
    {"dropped_events": 3}, {"evicted": 2}, {"widened": 4}, {"outcomes_other": 1},
    {"c_capped": [CappedSite(symbol="json.dumps", file="app.py", func="main", line=7,
                             text=None, recorded=2000, muted=5, exact=True)]},
])
def test_counts_from_a_replicate_that_lost_calls_are_no_evidence(loss):
    base, rep = _logged(u4=6, u5=3, u6=5, u8=8), _logged(u4=2, u5=6, u6=6, u8=8)
    for k, v in loss.items():
        setattr(rep, k, v)
    assert build_noise_profile(base, rep)["input_shifts"] == {}
    d, _, _ = judged(base, rep, _logged(u4=4, u5=4, u6=7, u7=8))
    assert any(e.fails for e in d.entries)


def test_only_the_leaves_that_moved_in_the_replicate_explain_an_input_change():
    base, rep = _logged(u4=6, u5=3, u6=5, u8=8), _logged(u4=2, u5=6, u6=6, u8=8)
    post = _logged(u4=4, u5=4, u6=7)
    renamed = fact("json.dumps", {"obj": {"__dict__": [["V", 8]]}}, (("return", '{"V": 8}'),),
                   line=7, text="s = json.dumps(counts)", count=8)
    renamed.outcomes[0].count = 8
    post.facts.append(renamed)          # another key, not another count: not what moved
    d, _, _ = judged(base, rep, post)
    (e,) = [x for x in d.entries if x.new_input is not None and x.input != x.new_input]
    assert e.fails and not e.noise


# ── an input seen on one side only, at a call both sides make ─────────────────────────────

def _polled(*seen, receiver=True, typed=False):
    """``self.assertEqual(3, n)`` retried until ``n == 3``: one fact per value seen, each
    raising until then. ``receiver``: the test case is rendered by type (hidden input)."""
    facts = []
    for n in sorted(set(seen)):
        c = seen.count(n)
        out = (("return", None),) if n == 3 else (
            ("raise", "AssertionError" if typed else "<unknown>"),)
        bound = {"first": 3, "second": n}
        if receiver:
            bound["self"] = {"__opaque__": "tests.T"}
        f = fact("unittest.case.TestCase.assertEqual", bound, out, line=9,
                 text="self.assertEqual(3, len(missing))", count=c)
        f.outcomes[0].count = c
        facts.append(f)
    return fs(*facts)


POLLED = (0, 0, 0, 0, 0, 1, 2, 3)


def test_a_polled_value_missing_on_one_side_is_an_input_change():
    # locust test_runners.py:972: the poll saw 2 on 3.8 only. The call is made on both sides,
    # raising until the value arrives: what reached it changed, not the dependency.
    d = diff_facts(_polled(*POLLED), _polled(0, 0, 0, 0, 0, 1, 3, typed=True))
    (e,) = [x for x in d.entries if x.category is FactCategory.DISAPPEARED]
    assert e.review.startswith("input changed") and "input-variant" in e.notes
    assert not e.fails and d.verdict.value == "inconclusive"


def test_a_replicate_with_hidden_inputs_cannot_confirm_a_missing_polled_value():
    base = rep = _polled(*POLLED)
    d, counts, _ = judged(base, rep, _polled(0, 0, 0, 0, 0, 1, 3, typed=True))
    (e,) = [x for x in d.entries if x.category is FactCategory.DISAPPEARED]
    assert e.review and not e.fails and not any("confirmed" in n for n in e.notes)
    assert not counts.get("confirmed")


def test_a_replicate_with_complete_inputs_judges_a_missing_value_strictly():
    base = rep = _polled(*POLLED, receiver=False)
    post = _polled(0, 0, 0, 0, 0, 1, 3, receiver=False, typed=True)
    d = diff_facts(base, post)
    apply_noise_profile(d, build_noise_profile(base, rep), "r")
    (e,) = [x for x in d.entries if x.category is FactCategory.DISAPPEARED]
    assert e.fails and "confirmed deterministic: equal in the baseline replicate" in e.notes
    # Across Python versions or tracers the replicate ran at the baseline's timing only.
    d = diff_facts(base, post)
    counts = apply_noise_profile(d, build_noise_profile(base, rep), "r",
                                 timing="the sides ran Python 3.8, setprofile vs Python 3.12")
    (e,) = [x for x in d.entries if x.category is FactCategory.DISAPPEARED]
    assert e.fails and not any(n.startswith("confirmed") for n in e.notes)
    assert any(n.startswith("reproducible in the baseline replicate, not confirmed") for n in
               e.notes)
    assert counts["timing"] == 1 and not counts.get("confirmed")
    (f,) = summarize(d)["failing"]
    assert "reproducible in the baseline replicate" in f["why"] and "Python 3.12" in f["why"]


def test_a_value_change_at_equal_input_keeps_its_determinism_claim_across_pythons():
    base, rep, post = side(port=1, total=10), side(port=1, total=10), side(port=1, total=12)
    d = diff_facts(base, post)
    apply_noise_profile(d, build_noise_profile(base, rep), "r", timing="the sides ran X vs Y")
    (e,) = [x for x in d.entries if x.symbol == "billing.total"]
    assert "confirmed deterministic: equal in the baseline replicate" in e.notes


def _with(base, bound, outcome):
    return fs(*base.facts, fact("unittest.case.TestCase.assertEqual",
                                {"self": {"__opaque__": "tests.T"}, "first": 3, **bound},
                                (outcome,), line=9, text="self.assertEqual(3, len(missing))"))


@pytest.mark.parametrize("base,post", [
    # a new raise type at the site (both sides typed)
    (_polled(0, 1, 3, typed=True),
     _with(_polled(0, 1, 3, typed=True), {"second": 5}, ("raise", "ValueError"))),
    # the site's only raise vanished
    (_polled(0, 3, typed=True), _polled(3, typed=True)),
    # a signature the old side never used there
    (_polled(0, 1, 3, typed=True),
     _with(_polled(0, 1, 3, typed=True), {"second": 4, "msg": "x"},
           ("raise", "AssertionError"))),
], ids=["new-raise-type", "only-raise-vanished", "new-signature"])
def test_a_one_sided_input_that_changes_the_site_still_fails(base, post):
    d = diff_facts(base, post)
    assert any(e.fails for e in d.entries)
    assert not any("input-variant" in e.notes for e in d.entries if e.fails)


def test_a_call_gone_from_its_site_is_still_presence():
    base = rep = fs(*_polled(*POLLED).facts,
                    fact("lib.ping", {"n": 1}, line=12, text="lib.ping(1)"))
    d, counts, by = judged(base, rep, _polled(*POLLED))
    e = by["lib.ping"]
    assert e.fails and "input-variant" not in e.notes
    assert "confirmed deterministic: equal in the baseline replicate" in e.notes


def test_a_value_one_replicate_run_missed_explains_the_same_change_but_not_a_vanished_site():
    base = fs(*_polled(*POLLED).facts, fact("lib.ping", {"n": 1}, line=12, text="lib.ping(1)"))
    rep = fs(*_polled(0, 0, 0, 0, 0, 1, 3).facts, fact("lib.ping", {"n": 1}, line=12,
                                                       text="lib.ping(1)"))
    profile = build_noise_profile(base, rep)
    assert ["input"] in profile["varying"].values()
    d, _, by = judged(base, rep, _polled(0, 0, 0, 0, 0, 1, 3, typed=True))
    (e,) = [x for x in d.entries if x.category is FactCategory.DISAPPEARED
            and x.symbol.endswith("assertEqual")]
    assert e.noise and not e.fails and not e.inconclusive
    assert by["lib.ping"].fails             # the whole call gone is not input variation


def test_a_replicate_that_lost_events_neither_explains_nor_judges_a_missing_value():
    base = _polled(*POLLED, receiver=False)
    rep = _polled(0, 0, 0, 0, 0, 1, 3, receiver=False)
    rep.dropped_events = 4
    profile = build_noise_profile(base, rep)
    assert profile["varying"] == {} and profile["presence_evidence"] is False
    d, _, _ = judged(base, rep, _polled(0, 0, 0, 0, 0, 1, 3, receiver=False, typed=True))
    (e,) = [x for x in d.entries if x.category is FactCategory.DISAPPEARED]
    assert e.review and not e.fails and not e.noise


def test_the_timing_difference_names_the_python_and_the_tracer():
    old, new = fs(backends=("setprofile",)), fs(backends=("monitoring",))
    old.python_versions, new.python_versions = ["3.8.18"], ["3.12.1"]
    assert timing_difference(old, new) == (
        "the sides ran Python 3.8, setprofile vs Python 3.12, monitoring")
    new.python_versions, new.backends = ["3.8.20"], ["setprofile"]
    assert timing_difference(old, new) is None
    new.python_versions = []                      # unknown: no claim either way
    assert timing_difference(old, new) is None

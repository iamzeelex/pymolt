"""Runtime environment records and the rules that read them (E0-E6).

Every rule: the noise case it absorbs (the two captures ran in different environments) and
the adversarial case it must not hide (a real regression while the environments differ).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_fact_noise import fact, fs  # noqa: E402

from pymolt.verify import boundary_tracer as bt  # noqa: E402
from pymolt.verify import environment as env  # noqa: E402
from pymolt.verify.fact_report import summarize  # noqa: E402
from pymolt.verify.facts import (  # noqa: E402
    ComparisonLevel,
    DiffContext,
    FactCategory,
    diff_facts,
)
from pymolt.verify.models import VerificationVerdict  # noqa: E402

DOCKER = {"schema": 1, "platform": "linux", "system": "Linux", "release": "6.10.14-linuxkit",
          "machine": "aarch64", "impl": "cpython", "container": "docker", "uid": 0, "euid": 0,
          "caps": "00000000a80425fb", "cpus": 8, "affinity": 8,
          "rlimits": {"NOFILE": [1048576, 1048576], "CORE": [0, "inf"]},
          "tmpdir": None, "utc_offset": 0,
          "encoding": {"fs": "utf-8", "utf8_mode": 0, "locale": "C.UTF-8"},
          "hash": {"seed": None, "randomized": True, "algorithm": "siphash24"}}
MAC = {"schema": 1, "platform": "darwin", "system": "Darwin", "release": "25.2.0",
       "machine": "arm64", "impl": "cpython", "container": None, "uid": 501, "euid": 501,
       "caps": None, "cpus": 12, "affinity": None,
       "rlimits": {"NOFILE": [1048576, "inf"], "CORE": [0, "inf"]},
       "tmpdir": "/var/folders/hm/xy/T/", "utc_offset": -21600,
       "encoding": {"fs": "utf-8", "utf8_mode": 1, "locale": "C.UTF-8"},
       "hash": {"seed": None, "randomized": True, "algorithm": "siphash13"}}

SETRLIMIT_IN = {"bound": {"resource": 7, "limits": {"__tuple__": [10000, 2 ** 63 - 1]}}}


def sides(old_facts, new_facts, old_env=(DOCKER,), new_env=(MAC,)):
    a, b = fs(*old_facts), fs(*new_facts)
    a.environment = [dict(x) for x in old_env] if old_env is not None else None
    b.environment = [dict(x) for x in new_env] if new_env is not None else None
    return a, b


def setrlimit(outcome):
    return fact("resource.setrlimit", inp=SETRLIMIT_IN, outcomes=(outcome,),
                text="resource.setrlimit(")


def only(d, symbol=None):
    es = [e for e in d.entries if symbol is None or e.symbol == symbol]
    (e,) = es
    return e


# ── E0: aspects ───────────────────────────────────────────────────────────────────────────


def test_aspects_exclude_the_python_version_and_the_hash_seed():
    a = dict(DOCKER, hash={"seed": 0, "randomized": False, "algorithm": "x"})
    cmp = env.compare([DOCKER], [a])
    assert cmp is not None and cmp.same


def test_aspects_name_what_differs():
    cmp = env.compare([DOCKER], [MAC])
    assert {"os", "release", "machine", "container", "privilege", "rlimit:NOFILE", "cpus",
            "tmpdir", "utc_offset", "encoding"} <= set(cmp.differs)
    assert "rlimit:CORE" not in cmp.differs
    assert cmp.differs["privilege"] == ("uid 0 caps 00000000a80425fb", "uid 501")


def test_processes_of_one_side_that_disagree_make_the_aspect_differ():
    cmp = env.compare([DOCKER, dict(DOCKER, cpus=4)], [DOCKER, DOCKER])
    assert list(cmp.differs) == ["cpus"]


def test_a_side_without_a_record_for_every_process_is_unknown():
    assert env.compare([DOCKER], None) is None
    assert env.compare([DOCKER, None], [DOCKER]) is None


# ── E3: the review at a reader ────────────────────────────────────────────────────────────


def test_setrlimit_raising_in_the_container_and_returning_on_the_host_is_a_review():
    old, new = sides([setrlimit(("raise", "<unknown>"))], [setrlimit(("return", None))])
    d = diff_facts(old, new)
    e = only(d)
    assert not e.fails and e.review.startswith("environment differs: ")
    assert "rlimit:NOFILE soft 1048576, hard 1048576 -> soft 1048576, hard inf" in e.review
    assert "privilege" in e.review and "resource.setrlimit reads it" in e.review
    assert d.verdict is VerificationVerdict.INCONCLUSIVE
    s = summarize(d)
    assert s["review"][0]["reason"] == "environment differs"
    assert "same environment" in s["review"][0]["action"]
    q = s["quality"]["environment"]
    assert q["comparable"] is False and q["reviews"] == 1 and "privilege" in q["differs"]


def test_the_same_environment_keeps_everything_strict():
    old, new = sides([setrlimit(("raise", "<unknown>"))], [setrlimit(("return", None))],
                     new_env=(DOCKER,))
    d = diff_facts(old, new)
    assert only(d).fails and summarize(d)["quality"]["environment"] == "same"


def test_an_unknown_environment_keeps_everything_strict():
    old, new = sides([setrlimit(("raise", "<unknown>"))], [setrlimit(("return", None))],
                     old_env=None)
    d = diff_facts(old, new)
    assert only(d).fails
    assert summarize(d)["quality"]["environment"] == "not recorded (old)"


def test_a_call_that_reads_no_environment_still_fails():
    old, new = sides([fact("json.loads", {"s": "{}"}, (("return", 1),))],
                     [fact("json.loads", {"s": "{}"}, (("raise", "ValueError"),))])
    assert only(diff_facts(old, new)).fails


def test_an_aspect_that_does_not_differ_is_no_excuse():
    # only cpus differ: a setrlimit kind change is not about the CPUs
    only_cpus = dict(DOCKER, cpus=12, affinity=12)
    old, new = sides([setrlimit(("raise", "<unknown>"))], [setrlimit(("return", None))],
                     new_env=(only_cpus,))
    assert only(diff_facts(old, new)).fails


def test_a_type_error_at_a_reader_is_never_softened():
    old, new = sides([setrlimit(("return", None))], [setrlimit(("raise", "TypeError"))])
    assert only(diff_facts(old, new)).fails


def test_a_non_platform_type_change_at_a_reader_is_never_softened():
    old, new = sides([fact("platform.release", {}, (("return", "6.1"),))],
                     [fact("platform.release", {}, (("return", 6.1),))])
    assert only(diff_facts(old, new)).fails


def test_a_symbol_the_plan_predicts_a_change_at_is_never_softened():
    old, new = sides([setrlimit(("raise", "<unknown>"))], [setrlimit(("return", None))])
    ctx = DiffContext(predicted_symbols={"resource.setrlimit"})
    assert only(diff_facts(old, new, context=ctx)).fails
    ctx = DiffContext(successors={"resource.setrlimit": "resource.prlimit"})
    assert only(diff_facts(old, new, context=ctx)).fails


def test_an_edited_site_is_never_softened():
    from pymolt.verify.facts import _judge_environment

    old, new = sides([setrlimit(("raise", "<unknown>"))], [setrlimit(("return", None))])
    e = only(diff_facts(old, new))
    e.review, e.provenance = None, "hand"
    assert _judge_environment([e], env.compare([DOCKER], [MAC]), set()) == 0 and e.fails


def test_a_presence_change_at_a_reader_is_a_review():
    old, new = sides([fact("os.sched_getaffinity", {"pid": 0}, (("return", 1),))], [])
    e = only(diff_facts(old, new))
    assert e.category is FactCategory.DISAPPEARED and e.review.startswith("environment differs")


def test_a_platform_tagged_type_name_reads_the_os():
    pm_mac = {"__object__": "psutil._psosx.pmem", "attrs": {"rss": 10, "vms": 20, "pfaults": 1}}
    pm_linux = {"__object__": "psutil._pslinux.pmem", "attrs": {"rss": 11, "vms": 21,
                                                                "shared": 3}}
    old, new = sides([fact("lib.memory", {}, (("return", pm_linux),))],
                     [fact("lib.memory", {}, (("return", pm_mac),))])
    e = only(diff_facts(old, new))
    assert e.review.startswith("environment differs: os Linux -> Darwin")


def test_a_permission_error_only_on_the_less_privileged_side_reads_privilege():
    old, new = sides([fact("lib.bind_low", {"p": 80}, (("return", None),))],
                     [fact("lib.bind_low", {"p": 80}, (("raise", "PermissionError"),))])
    assert only(diff_facts(old, new)).review.startswith("environment differs: privilege")
    # the more privileged side raising it is not explained by privilege
    old, new = sides([fact("lib.bind_low", {"p": 80}, (("return", None),))],
                     [fact("lib.bind_low", {"p": 80}, (("raise", "PermissionError"),))],
                     old_env=(MAC,), new_env=(DOCKER,))
    assert only(diff_facts(old, new)).fails


def test_a_low_port_bind_reads_privilege_a_high_one_does_not():
    low = {"bound": {"self": {"__opaque__": "socket.socket"},
                     "address": {"__tuple__": ["", 80]}}}
    high = {"bound": {"self": {"__opaque__": "socket.socket"},
                      "address": {"__tuple__": ["", 8080]}}}
    for inp, fails in ((low, False), (high, True)):
        old, new = sides([fact("socket.socket.bind", inp=inp, outcomes=(("return", None),))],
                         [fact("socket.socket.bind", inp=inp,
                               outcomes=(("raise", "PermissionError"),))],
                         old_env=(MAC,), new_env=(dict(MAC, uid=502, euid=502),))
        assert only(diff_facts(old, new)).fails is fails


# ── E2: masks and the side's own readings ─────────────────────────────────────────────────


def test_a_value_that_is_each_sides_own_reading_is_explained():
    old, new = sides([fact("os.cpu_count", {}, (("return", 8),))],
                     [fact("os.cpu_count", {}, (("return", 12),))])
    e = only(diff_facts(old, new))
    assert e.category is FactCategory.EQUAL and e.level is ComparisonLevel.MASKED
    assert e.explained.startswith("environment: the value is this side's own cpus")


def test_a_value_that_matches_neither_reading_stays_a_difference():
    old, new = sides([fact("os.cpu_count", {}, (("return", 8),))],
                     [fact("os.cpu_count", {}, (("return", 16),))])
    e = only(diff_facts(old, new))
    assert e.category is FactCategory.CHANGED and e.review.startswith("environment differs")


def test_getrlimit_returns_the_sides_own_recorded_limit():
    lim = {"bound": {"resource": 7}}  # RLIMIT_NOFILE on Linux
    host = dict(DOCKER, container=None, rlimits={"NOFILE": [1024, "inf"]})
    old, new = sides(
        [fact("resource.getrlimit", inp=lim,
              outcomes=(("return", {"__tuple__": [1048576, 1048576]}),))],
        [fact("resource.getrlimit", inp=lim,
              outcomes=(("return", {"__tuple__": [1024, 2 ** 64 - 1]}),))],
        new_env=(host,))
    e = only(diff_facts(old, new))
    assert e.explained.startswith("environment: the value is this side's own rlimit"), e
    assert e.category is FactCategory.EQUAL


def test_getrlimit_across_platforms_names_another_resource_number():
    # RLIMIT_NOFILE is 7 on Linux and 8 on macOS: the input differs, so the input-change rule
    # judges it (a review), not the reading rule
    old, new = sides(
        [fact("resource.getrlimit", inp={"bound": {"resource": 7}},
              outcomes=(("return", {"__tuple__": [1048576, 1048576]}),))],
        [fact("resource.getrlimit", inp={"bound": {"resource": 8}},
              outcomes=(("return", {"__tuple__": [1048576, 2 ** 63 - 1]}),))])
    e = only(diff_facts(old, new))
    assert e.review.startswith("input changed") and not e.fails


def test_a_value_equal_only_once_the_os_names_are_masked_is_a_review_not_equal():
    # a library that merely mentions the platform it ran on: equal once masked, but that is
    # what a real change to another platform's string looks like too (F4)
    old, new = sides([fact("lib.user_agent", {}, (("return", "app/1.0 (Linux; aarch64)"),))],
                     [fact("lib.user_agent", {}, (("return", "app/1.0 (Darwin; arm64)"),))])
    d = diff_facts(old, new)
    e = only(d)
    assert e.category is FactCategory.CHANGED and not e.fails
    assert e.review.startswith("environment differs: os Linux -> Darwin")
    assert d.verdict is VerificationVerdict.INCONCLUSIVE


def test_a_wheel_tag_that_changed_platform_is_inconclusive_across_environments_only():
    def run(o, n, **kw):
        a, b = sides([fact("lib.wheel_tag", outcomes=(("return", o),))],
                     [fact("lib.wheel_tag", outcomes=(("return", n),))], **kw)
        return diff_facts(a, b).verdict
    assert run("linux", "darwin") is VerificationVerdict.INCONCLUSIVE
    assert run("linux", "darwin", old_env=(DOCKER,), new_env=(DOCKER,)) is \
        VerificationVerdict.FAIL
    assert run("linux", "win32") is VerificationVerdict.FAIL


def test_a_curated_os_reader_returning_its_own_os_is_equal():
    old, new = sides([fact("platform.system", {}, (("return", "Linux"),))],
                     [fact("platform.system", {}, (("return", "Darwin"),))])
    e = only(diff_facts(old, new))
    assert e.category is FactCategory.EQUAL and e.level is ComparisonLevel.MASKED
    assert e.explained.startswith("environment: the value is this side's own os")


def test_a_platform_masked_value_at_a_predicted_symbol_fails():
    old, new = sides([fact("lib.user_agent", {}, (("return", "app (Linux)"),))],
                     [fact("lib.user_agent", {}, (("return", "app (Darwin)"),))])
    e = only(diff_facts(old, new, context=DiffContext(predicted_symbols={"lib.user_agent"})))
    assert e.fails


def test_the_tmpdir_is_masked_like_the_project_root():
    old, new = sides([fact("lib.cache_dir", {}, (("return", "/tmp/build/cache"),))],
                     [fact("lib.cache_dir", {}, (("return", "/var/folders/hm/xy/T/cache"),))],
                     old_env=(dict(DOCKER, tmpdir="/tmp/build/"),))
    e = only(diff_facts(old, new))
    assert e.category is FactCategory.EQUAL and e.level is ComparisonLevel.MASKED


def test_os_names_are_not_masked_when_the_os_is_the_same():
    old, new = sides([fact("lib.user_agent", {}, (("return", "app (Linux)"),))],
                     [fact("lib.user_agent", {}, (("return", "app (Linux 2)"),))],
                     new_env=(DOCKER,))
    assert only(diff_facts(old, new)).fails


# ── E5 / E6: replicate and report ─────────────────────────────────────────────────────────


def _trace(tmp: Path, name: str, record) -> Path:
    t = tmp / f"{name}.jsonl"
    t.write_text("")
    (tmp / f"{name}.jsonl.meta.json").write_text(json.dumps(
        {"processes": [{"environment": record}] if record is not None else [{}]}))
    return t


def test_a_replicate_in_another_environment_is_rejected(tmp_path: Path):
    from pymolt.verify.service import _replicate_environment_differs

    base = _trace(tmp_path, "b", DOCKER)
    assert _replicate_environment_differs(base, _trace(tmp_path, "r1", DOCKER)) is None
    why = _replicate_environment_differs(base, _trace(tmp_path, "r2", dict(DOCKER, cpus=4)))
    assert why.startswith("cpus 8")
    assert _replicate_environment_differs(base, _trace(tmp_path, "r3", None)) is None


def test_the_verdict_reason_names_the_differing_aspects():
    from pymolt.verify.service import _fact_diff_reasons

    old, new = sides([setrlimit(("raise", "<unknown>"))], [setrlimit(("return", None))])
    reasons = _fact_diff_reasons(diff_facts(old, new).to_json_dict())
    (line,) = [r for r in reasons if r.startswith("the captures ran in different environments")]
    assert "privilege" in line and "1 difference(s) at calls that read them are reviews" in line


def test_an_unknown_environment_may_infer_from_paths_but_changes_nothing():
    old, new = sides([setrlimit(("raise", "<unknown>"))], [setrlimit(("return", None))],
                     old_env=None, new_env=None)
    old.paths_mapped = 3
    d = diff_facts(old, new)
    assert only(d).fails
    q = summarize(d)["quality"]["environment"]
    assert q.startswith("not recorded (old, new); inferred from call-site paths: the baseline "
                        "ran in a container")


# ── the runtime record ────────────────────────────────────────────────────────────────────


def test_the_runtime_records_its_environment_without_a_hostname():
    import socket

    rec = bt._environment()
    assert rec["schema"] == 1 and rec["platform"] == sys.platform
    assert set(rec) >= {"system", "release", "machine", "container", "uid", "euid", "caps",
                        "cpus", "affinity", "rlimits", "tmpdir", "utc_offset", "encoding",
                        "hash"}
    assert socket.gethostname() not in json.dumps(rec)
    assert rec["hash"]["algorithm"] == sys.hash_info.algorithm
    if rec["rlimits"]:
        assert all(isinstance(v, int) or v == "inf"
                   for pair in rec["rlimits"].values() for v in pair)


def test_the_runtime_reads_a_pinned_hash_seed(monkeypatch):
    monkeypatch.setenv("PYTHONHASHSEED", "0")
    expected = None if sys.flags.ignore_environment else 0   # -E / -I: the variable is unread
    assert bt._environment()["hash"]["seed"] == expected
    monkeypatch.setenv("PYTHONHASHSEED", "random")
    assert bt._environment()["hash"]["seed"] is None


def test_the_python_version_readers_of_platform_are_never_environment_readers():
    assert "os" not in env.reader_aspects("platform.python_version")
    assert "os" in env.reader_aspects("platform.system")
    old, new = sides([fact("platform.python_version", {}, (("return", "3.8.20"),))],
                     [fact("platform.python_version", {}, (("return", "3.12.11"),))])
    assert only(diff_facts(old, new)).fails


def test_a_python_version_dependent_value_at_a_non_reader_fails_across_environments():
    # 3.12 changed mimetypes.guess_type('x.js'): a migration change, never an environment one
    js = (("return", {"__tuple__": ["application/javascript", None]}),)
    js12 = (("return", {"__tuple__": ["text/javascript", None]}),)
    assert env.reader_aspects("mimetypes.guess_type") == set()
    old, new = sides([fact("mimetypes.guess_type", outcomes=js)],
                     [fact("mimetypes.guess_type", outcomes=js12)])
    assert only(diff_facts(old, new)).fails


def test_only_the_process_locale_getters_and_setters_read_the_encoding():
    assert env.reader_aspects("locale.getpreferredencoding") == {"encoding"}
    assert env.reader_aspects("locale.format_string") == set()
    old, new = sides([fact("locale.getpreferredencoding", outcomes=(("return", "UTF-8"),))],
                     [fact("locale.getpreferredencoding",
                           outcomes=(("return", "ANSI_X3.4-1968"),))])
    assert only(diff_facts(old, new)).review.startswith("environment differs: encoding")


def test_identical_multi_process_environments_are_the_same():
    worker = dict(DOCKER, rlimits={"NOFILE": [65536, 1048576], "CORE": [0, "inf"]})
    envs = (DOCKER, worker)
    assert env.compare(list(envs), [worker, DOCKER]).same
    old, new = sides([setrlimit(("return", None))], [setrlimit(("raise", "PermissionError"))],
                     old_env=envs, new_env=envs)
    d = diff_facts(old, new)
    assert only(d).fails and summarize(d)["quality"]["environment"] == "same"
    # a side whose processes disagree, against another mix of the same values: cannot tell
    assert not env.compare(list(envs), [worker, worker, DOCKER]).same
    # more processes of one identical record is the same environment
    assert env.compare([DOCKER], [DOCKER, DOCKER]).same

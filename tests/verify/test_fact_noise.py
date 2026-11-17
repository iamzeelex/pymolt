"""Verify noise rules: what makes two runs differ besides the migration, and what must still fail.

Every rule has two kinds of tests: the noise case it explains and the regression it must not hide.
Facts are built directly (no trace files): ``fact`` is one dependency call from one site.
"""
from pymolt.verify.facts import (
    Comparison,
    ComparisonLevel,
    DiffContext,
    Fact,
    FactCategory,
    FactSet,
    Outcome,
    compare_outcomes,
    diff_facts,
    types_match,
    value_type,
)
from pymolt.verify.models import VerificationVerdict

ROOT = "/proj"


def out(*pairs):
    """``out(("return", 1), ("raise", "ValueError"))`` -> outcomes (count 1 each)."""
    return [Outcome(kind=k, value=v, count=1) for k, v in pairs]


def fact(symbol, bound=None, outcomes=(("return", None),), *, file="app.py", func="main",
         line=10, text=None, count=1, first=0, inp=None):
    leaf = symbol.rsplit(".", 1)[-1]
    return Fact(
        symbol=symbol, file=file, func=func, line=line, lines=[line],
        text=text if text is not None else f"x = obj.{leaf}(a)",
        input=inp if inp is not None else {"bound": dict(bound or {})},
        outcomes=out(*outcomes), count=count, first_seen=first,
    )


def fs(*facts, backends=("monitoring",), **kw):
    return FactSet(facts=list(facts), root=ROOT, backends=list(backends), **kw)


def failing(d):
    return [e for e in d.entries if e.fails]


# ── step 1: an opaque rendering meets a structural rendering of the same type ─────────────

PATTERN_OBJ = {"__object__": "re.Pattern", "attrs": {"flags": 34, "groups": 0,
                                                     "pattern": "^https?://"}}


def test_opaque_and_object_of_the_same_type_match_at_type_level():
    assert types_match({"opaque": "re.Pattern"}, value_type(PATTERN_OBJ))
    assert types_match(value_type(PATTERN_OBJ), {"opaque": "re.Pattern"})
    cmp = compare_outcomes(out(("return", {"__opaque__": "re.Pattern"})),
                           out(("return", PATTERN_OBJ)))
    assert cmp.comparison is Comparison.EQUAL and cmp.level is ComparisonLevel.TYPE


def test_opaque_and_object_of_different_types_still_differ():
    other = {"__object__": "re.Match", "attrs": {"pos": 0}}
    assert not types_match({"opaque": "re.Pattern"}, value_type(other))
    cmp = compare_outcomes(out(("return", {"__opaque__": "re.Pattern"})), out(("return", other)))
    assert cmp.comparison is Comparison.CHANGED


def test_renderer_asymmetry_is_not_a_failure_in_the_diff():
    # 3.8 renders re.Pattern opaque (a static type), 3.12 as an __object__ (a heap type).
    old = fs(fact("re.compile", {"pattern": "^https?://", "flags": 2},
                  (("return", {"__opaque__": "re.Pattern"}),)), backends=("setprofile",))
    new = fs(fact("re.compile", {"pattern": "^https?://", "flags": 2},
                  (("return", PATTERN_OBJ),)))
    d = diff_facts(old, new)
    assert not failing(d) and d.verdict is VerificationVerdict.PASS
    assert d.entries[0].category is FactCategory.EQUAL
    assert d.entries[0].level is ComparisonLevel.TYPE


def test_renderer_asymmetry_does_not_hide_a_new_raise():
    old = fs(fact("re.compile", {"pattern": "("}, (("return", {"__opaque__": "re.Pattern"}),)),
             backends=("setprofile",))
    new = fs(fact("re.compile", {"pattern": "("}, (("raise", "re.error"),)))
    d = diff_facts(old, new)
    assert [e.category for e in failing(d)] == [FactCategory.CHANGED]


# ── step 2: volatile sites (every call a fresh input) compare as a group ──────────────────

PACK = "msgpack.packb"
PACK_LINE = "return msgpack.dumps((self.type, self.data, self.node_id))"


def packs(ids, *, result=lambda i: {"__bytes__": f"93a3{i}"}, first=0, raise_at=None):
    """One fact per call: each carries its own client id, so no input repeats across runs."""
    facts = []
    for k, i in enumerate(ids):
        outcome = (("raise", raise_at),) if raise_at is not None and k == 0 else \
            (("return", result(i)),)
        facts.append(fact(PACK, {"o": {"__tuple__": ["ack", f"node_{i}"]}}, outcome,
                          file="locust/rpc/protocol.py", func="serialize", line=14,
                          text=PACK_LINE, first=first + k))
    return facts


def test_fresh_inputs_at_one_site_compare_as_one_group():
    old = fs(*packs(["a1", "a2", "a3"]))
    new = fs(*packs(["b1", "b2"]))
    d = diff_facts(old, new)
    assert not failing(d) and d.verdict is VerificationVerdict.PASS
    (e,) = d.entries
    assert e.category is FactCategory.EQUAL and e.level is ComparisonLevel.TYPE
    assert (e.old_count, e.new_count) == (3, 2)
    assert e.explained.startswith("volatile-site") and "volatile-site:3/2" in e.notes
    assert any(w.startswith("volatile-sites:1:") for w in d.warnings)


def test_a_new_raise_type_in_a_volatile_group_fails():
    old = fs(*packs(["a1", "a2", "a3"]))
    new = fs(*packs(["b1", "b2"], raise_at="TypeError"))
    d = diff_facts(old, new)
    (e,) = failing(d)
    assert e.category is FactCategory.CHANGED and e.explained.startswith("volatile-site")


def test_a_new_return_type_in_a_volatile_group_fails():
    old = fs(*packs(["a1", "a2"]))
    new = fs(*packs(["b1", "b2", "b3"], result=lambda i: f"93a3{i}"))  # bytes -> str
    assert [e.category for e in failing(diff_facts(old, new))] == [FactCategory.CHANGED]


def test_exact_repeats_at_a_volatile_site_still_compare_exactly():
    # msgpack: the same message (same input) packs to different bytes (dict order changed):
    # the repeat pairs exactly and fails; the fresh-input calls around it form the group.
    stats = {"o": {"__tuple__": ["stats", {"__dict__": [["name", "/1"], ["method", "GET"]]}]}}
    repeat_old = fact(PACK, stats, (("return", {"__bytes__": "93a5aa"}),),
                      file="locust/rpc/protocol.py", func="serialize", line=14, text=PACK_LINE)
    repeat_new = fact(PACK, stats, (("return", {"__bytes__": "93a5bb"}),),
                      file="locust/rpc/protocol.py", func="serialize", line=14, text=PACK_LINE)
    d = diff_facts(fs(repeat_old, *packs(["a1", "a2", "a3"], first=1)),
                   fs(repeat_new, *packs(["b1", "b2"], first=1)))
    (e,) = failing(d)
    assert e.category is FactCategory.CHANGED and e.level is ComparisonLevel.EXACT
    assert e.input == {"bound": stats}
    assert d.counts()["equal"] == 1  # the group


def test_one_sided_site_is_not_a_group():
    d = diff_facts(fs(*packs(["a1", "a2"])), fs())
    assert d.counts()["disappeared"] == 2 and d.verdict is VerificationVerdict.FAIL


def test_a_changed_input_signature_is_not_absorbed_into_a_group():
    # Same symbol and site, but the bound parameter was renamed: structure, not noise.
    old = fs(*[fact("lib.f", {"a": i}, first=i) for i in range(2)])
    new = fs(fact("lib.f", {"b": 9}))
    d = diff_facts(old, new)
    assert d.counts()["disappeared"] == 2 and d.counts()["appeared"] == 1


# ── step 3: environment, clock, entropy sources, input substitution ───────────────────────

WIN_OLD = (1_794_824_533.0, 1_794_824_831.0)   # 2026-11-16 10:22:13 .. 10:27:11 UTC
WIN_NEW = (1_794_824_897.0, 1_794_825_149.0)   # 2026-11-16 10:28:17 .. 10:32:29 UTC


def env_fs(*facts, roots=(), python=None, window=None, **kw):
    s = fs(*facts, **kw)
    s.env_roots = list(roots)
    s.python_versions = [python] if python else []
    s.run_windows = [window] if window else []
    s.host_capture = False
    return s


def test_project_root_as_each_process_saw_it_is_environment():
    tb_old = '  File "/work/locust/test/test_web.py", line 198, in test_exceptions\n'
    tb_new = ('  File "/Users/me/src/locust/locust/test/test_web.py", line 198, in '
              'test_exceptions\n')
    old = env_fs(fact("html.escape", {"s": tb_old}, (("return", tb_old),)), roots=["/work"])
    new = env_fs(fact("html.escape", {"s": tb_new}, (("return", tb_new),)),
                 roots=["/Users/me/src/locust"])
    d = diff_facts(old, new)
    (e,) = d.entries
    assert not failing(d) and e.category is FactCategory.EQUAL
    assert e.level is ComparisonLevel.MASKED and e.explained.startswith("environment")


def test_a_path_that_lost_its_root_still_differs():
    old = env_fs(fact("lib.where", {}, (("return", "/work/locust/data.csv"),)), roots=["/work"])
    new = env_fs(fact("lib.where", {}, (("return", "locust/data.csv"),)),
                 roots=["/Users/me/src/locust"])
    assert [e.category for e in failing(diff_facts(old, new))] == [FactCategory.CHANGED]


def test_the_python_version_is_environment_and_stays_visible():
    old = env_fs(fact("platform.python_version", {}, (("return", "3.8.20"),)), python="3.8.20")
    new = env_fs(fact("platform.python_version", {}, (("return", "3.12.11"),)),
                 python="3.12.11")
    d = diff_facts(old, new)
    (e,) = d.entries
    assert not failing(d) and e.explained.startswith("environment")
    assert e.old[0].value == "⟨python⟩" and "3.8.20" in e.explained


def test_clock_readings_inside_each_run_window_are_masked():
    old = env_fs(fact("lib.render", {"t": "x"},
                      (("return", "During: 2026-11-16 10:26:04 - report_1794824765.0062904"),)),
                 window=WIN_OLD)
    new = env_fs(fact("lib.render", {"t": "x"},
                      (("return", "During: 2026-11-16 10:31:41 - report_1794825101.045572"),)),
                 window=WIN_NEW)
    d = diff_facts(old, new)
    (e,) = d.entries
    assert not failing(d) and e.explained.startswith("clock")
    assert e.level is ComparisonLevel.MASKED


def test_a_timezone_shift_is_outside_the_window_and_still_differs():
    # UTC -> local time (UTC-6): the new value is six hours away from the new run's window.
    old = env_fs(fact("lib.stamp", {}, (("return", "2026-11-16T10:26:04"),)), window=WIN_OLD)
    new = env_fs(fact("lib.stamp", {}, (("return", "2026-11-16T04:31:41"),)), window=WIN_NEW)
    assert [e.category for e in failing(diff_facts(old, new))] == [FactCategory.CHANGED]


def test_a_clock_format_change_still_differs():
    # Seconds precision lost (a float epoch became an int), and an offset appeared.
    old = env_fs(fact("lib.ts", {}, (("return", 1_794_824_765.006),)), window=WIN_OLD)
    new = env_fs(fact("lib.ts", {}, (("return", 1_794_825_101),)), window=WIN_NEW)
    assert failing(diff_facts(old, new))
    old = env_fs(fact("lib.iso", {}, (("return", "2026-11-16T10:26:04"),)), window=WIN_OLD)
    new = env_fs(fact("lib.iso", {}, (("return", "2026-11-16T10:31:41+00:00"),)),
                 window=WIN_NEW)
    assert failing(diff_facts(old, new))


UUID_A = "03c09253-4e55-4474-913d-3ac4c0d94453"
UUID_B = "d2b0d279-25f4-4c89-b5f0-02153bbccd83"


def uuid_side(u, rendered, line_out="node"):
    return [fact("uuid.uuid4", {}, (("return", {"__uuid__": u}),), line=5, text="uuid4()"),
            fact("lib.register", {"name": "w"}, (("return", f"{line_out}_{rendered}"),),
                 line=6, first=1)]


def test_values_from_an_entropy_source_are_masked_everywhere_on_that_side():
    old = fs(*uuid_side(UUID_A, UUID_A.replace("-", "")))
    new = fs(*uuid_side(UUID_B, UUID_B.replace("-", "")))
    d = diff_facts(old, new)
    assert not failing(d), [(e.symbol, e.old, e.new) for e in failing(d)]
    reg = next(e for e in d.entries if e.symbol == "lib.register")
    assert reg.explained.startswith("source") and reg.level is ComparisonLevel.MASKED


def test_a_uuid_format_change_still_differs():
    # The dependency used to render the id with hyphens; now it renders the hex form.
    old = fs(*uuid_side(UUID_A, UUID_A))
    new = fs(*uuid_side(UUID_B, UUID_B.replace("-", "")))
    d = diff_facts(old, new)
    assert [e.symbol for e in failing(d)] == ["lib.register"]


def test_a_random_pick_is_compared_by_structure():
    choice = "random.Random.choice"
    seq = {"self": {"__opaque__": "random.Random"}, "seq": {"__list__": [0.5, 1, 1.5]}}
    d = diff_facts(fs(fact(choice, seq, (("return", 1),))),
                   fs(fact(choice, seq, (("return", 1.5),))))
    (e,) = d.entries
    assert not failing(d) and e.explained.startswith("source")
    # ... unless the program seeded its generator: then the values are deterministic.
    seed = fact("random.seed", {"a": 42}, line=3, text="random.seed(42)")
    d = diff_facts(fs(seed, fact(choice, seq, (("return", 1),))),
                   fs(seed, fact(choice, seq, (("return", 1.5),))))
    assert [e.symbol for e in failing(d)] == [choice]


def test_a_new_raise_from_an_entropy_source_still_fails():
    old = fs(fact("random.Random.choice", {"seq": {"__list__": []}}, (("return", 1),)))
    new = fs(fact("random.Random.choice", {"seq": {"__list__": []}}, (("raise", "IndexError"),)))
    assert failing(diff_facts(old, new))


def url_pair(port_old, port_new, *, scheme_new="http"):
    old = fact("urllib.parse.urlparse", {"url": f"http://127.0.0.1:{port_old}/x"},
               (("return", {"__object__": "urllib.parse.ParseResult", "attrs": {
                   "netloc": f"127.0.0.1:{port_old}", "scheme": "http", "path": "/x"}}),))
    new = fact("urllib.parse.urlparse", {"url": f"http://127.0.0.1:{port_new}/x"},
               (("return", {"__object__": "urllib.parse.ParseResult", "attrs": {
                   "netloc": f"127.0.0.1:{port_new}", "scheme": scheme_new, "path": "/x"}}),))
    return fs(old), fs(new)


def test_output_that_differs_only_as_the_input_does_is_masked():
    d = diff_facts(*url_pair(32925, 57864))
    (e,) = d.entries
    assert not failing(d) and e.level is ComparisonLevel.MASKED
    assert e.explained.startswith("input-substitution") and "32925 -> 57864" in e.explained


def test_a_residual_difference_beyond_the_input_still_fails():
    # The port changed run to run, AND the scheme now differs: the second is not explained.
    d = diff_facts(*url_pair(32925, 57864, scheme_new="https"))
    assert [e.category for e in failing(d)] == [FactCategory.CHANGED]


def test_an_input_structure_change_is_not_substituted():
    old = fs(fact("lib.f", {"url": "a:1111"}, (("return", "a:1111"),)))
    new = fs(fact("lib.f", {"uri": "a:2222"}, (("return", "a:2222"),)))
    d = diff_facts(old, new)
    assert not any((e.explained or "").startswith("input-substitution") for e in d.entries)
    assert failing(d)


def test_a_short_token_only_masks_whole_words():
    # 9095 -> 9096 is the run's port; the output also holds a constant containing 9095.
    old = fs(fact("lib.page", {"port": 9095}, (("return", "port 9095, e=2.5949095"),)))
    new = fs(fact("lib.page", {"port": 9096}, (("return", "port 9096, e=2.5949096"),)))
    d = diff_facts(old, new)
    assert [e.category for e in failing(d)] == [FactCategory.CHANGED]
    new = fs(fact("lib.page", {"port": 9096}, (("return", "port 9096, e=2.5949095"),)))
    assert not failing(diff_facts(old, new))


def test_same_complete_input_value_change_fails():
    # msgpack: the same message packs to different bytes (dict insertion order changed).
    msg = {"o": {"__tuple__": ["stats", {"__dict__": [["name", "/1"], ["method", "GET"]]}]}}
    old = fs(fact("msgpack.packb", msg, (("return", {"__bytes__": "93a5aa"}),)))
    new = fs(fact("msgpack.packb", msg, (("return", {"__bytes__": "93a5bb"}),)))
    assert [e.category for e in failing(diff_facts(old, new))] == [FactCategory.CHANGED]


def test_opaque_receiver_value_change_fails_in_a_quiet_workload():
    # jinja2: the same template and context now render https instead of http.
    inp = {"self": {"__opaque__": "jinja2.environment.Template"}, "t": "www.example.org"}
    old = fs(fact("jinja2.environment.Template.render", inp,
                  (("return", '<a href="http://www.example.org">'),)))
    new = fs(fact("jinja2.environment.Template.render", inp,
                  (("return", '<a href="https://www.example.org">'),)))
    d = diff_facts(old, new)
    assert [e.category for e in failing(d)] == [FactCategory.CHANGED]
    assert d.verdict is VerificationVerdict.FAIL


# ── step 4: attribution and evidence asymmetries ──────────────────────────────────────────

DECORATED = ('import flask\napp = flask.Flask("x")\n\n\n@app.route("/fast")\n'
             'def fast():\n    return "ok"\n')


def ctx_for(src):
    return DiffContext(old_sources={"app.py": src}, new_sources={"app.py": src})


def decorator_call(symbol, line, text, fn="app.fast", result=None):
    return fact(symbol, {"f": {"__callable__": fn}},
                (("return", result or {"__callable__": fn}),), func="<module>", line=line,
                text=text)


def test_a_decorator_named_and_placed_differently_by_the_interpreter_pairs():
    # 3.8: co_name only, call attributed to the def line; 3.12: co_qualname, the @ line.
    old = fs(decorator_call("flask.sansio.scaffold.decorator", 6, "def fast():"),
             backends=("setprofile",))
    new = fs(decorator_call("flask.sansio.scaffold.Scaffold.route.<locals>.decorator", 5,
                            '@app.route("/fast")'))
    d = diff_facts(old, new, context=ctx_for(DECORATED))
    assert not failing(d), [(e.category, e.symbol) for e in d.entries]
    (e,) = d.entries
    assert e.category is FactCategory.EQUAL and e.explained.startswith("attribution")


def test_qualname_pairing_needs_the_same_leaf_and_input():
    old = fs(decorator_call("flask.sansio.scaffold.decorator", 6, "def fast():"),
             backends=("setprofile",))
    new = fs(decorator_call("flask.sansio.scaffold.Scaffold.route.<locals>.wrapper", 5,
                            '@app.route("/fast")'))
    d = diff_facts(old, new, context=ctx_for(DECORATED))
    assert not any((e.explained or "").startswith("attribution") for e in d.entries)
    new = fs(decorator_call("flask.sansio.scaffold.Scaffold.route.<locals>.decorator", 5,
                            '@app.route("/fast")', fn="app.slow"))
    d = diff_facts(old, new, context=ctx_for(DECORATED))
    assert not any((e.explained or "").startswith("attribution") for e in d.entries)


WITH_SRC = "def main(s):\n    with s.get('/') as r:\n        r.read()\n"


def test_a_with_exit_attributed_to_another_unedited_line_is_not_a_rewrite():
    old = fs(fact("lib.Response.__exit__", {"self": {"__opaque__": "lib.Response"}},
                  line=3, text="r.read()"), backends=("setprofile",))
    new = fs(fact("lib.Response.__exit__", {"self": {"__opaque__": "lib.Response"}},
                  line=2, text="with s.get('/') as r:"))
    d = diff_facts(old, new, context=ctx_for(WITH_SRC))
    (e,) = d.entries
    assert e.category is FactCategory.EQUAL and "attribution-moved" in e.notes


def test_a_call_moved_by_an_edit_is_still_a_rewrite():
    new_src = "def main(s):\n    with s.get('/', timeout=1) as r:\n        r.read()\n"
    ctx = DiffContext(old_sources={"app.py": WITH_SRC}, new_sources={"app.py": new_src})
    old = fs(fact("lib.Session.get", {"self": {"__opaque__": "lib.Session"}}, line=2,
                  text="with s.get('/') as r:"))
    new = fs(fact("lib.Session.get", {"self": {"__opaque__": "lib.Session"}}, line=2,
                  text="with s.get('/', timeout=1) as r:"))
    d = diff_facts(old, new, context=ctx)
    assert not any("attribution-moved" in e.notes for e in d.entries)


def c_call(symbol, line=10, outcomes=(("return", {"__opaque__": "unobserved:c-return"}),)):
    return fact(symbol, outcomes=outcomes, inp={"__c_call__": True}, line=line)


def test_a_compiled_call_only_the_wider_backend_sees_is_not_an_appearance():
    old = fs(backends=("setprofile",))
    new = fs(c_call("gevent._gevent_cgreenlet.Greenlet.join"))
    d = diff_facts(old, new)
    (e,) = d.entries
    assert e.category is FactCategory.DRIFT and e.explained.startswith("backend")
    assert d.verdict is VerificationVerdict.PASS
    assert any(w.startswith("backend-only:1:") for w in d.warnings)


def test_a_compiled_call_the_wider_backend_lost_still_disappears():
    old = fs(c_call("collections.defaultdict.items"), backends=("setprofile",))
    new = fs(backends=("monitoring",))
    assert [e.category for e in failing(diff_facts(old, new))] == [FactCategory.DISAPPEARED]


def test_compiled_calls_compare_strictly_under_one_backend_family():
    old = fs(backends=("monitoring",))
    new = fs(c_call("gevent._gevent_cgreenlet.Greenlet.join"))
    assert [e.category for e in failing(diff_facts(old, new))] == [FactCategory.APPEARED]


def test_a_fact_missing_on_a_side_that_dropped_events_is_not_comparable():
    old = fs(fact("json.dumps", {"obj": {"__dict__": [["a", 1]]}}, (("return", '{"a": 1}'),)))
    new = fs(dropped_events=1053468)
    d = diff_facts(old, new)
    (e,) = d.entries
    assert e.category is FactCategory.UNCOMPARABLE and "possibly-dropped:new" in e.notes
    assert d.verdict is VerificationVerdict.INCONCLUSIVE and not failing(d)


def test_drops_on_one_side_do_not_excuse_an_appearance_on_the_other():
    old = fs()
    new = fs(fact("lib.new_call", {"x": 1}), dropped_events=10)
    assert [e.category for e in failing(diff_facts(old, new))] == [FactCategory.APPEARED]


def test_a_volatile_group_missing_a_kind_on_the_dropping_side_is_not_comparable():
    old = fs(*packs(["a1", "a2", "a3"], raise_at="TimeoutError"))
    new = fs(*packs(["b1", "b2"]), dropped_events=5)
    (e,) = diff_facts(old, new).entries
    assert e.category is FactCategory.UNCOMPARABLE and "possibly-dropped:new" in e.notes


def test_a_new_raise_type_on_the_dropping_side_still_fails():
    old = fs(*packs(["a1", "a2", "a3"]))
    new = fs(*packs(["b1", "b2"], raise_at="TypeError"), dropped_events=5)
    assert [e.category for e in failing(diff_facts(old, new))] == [FactCategory.CHANGED]


REQ_SRC = "def test(port):\n    response = requests.get(url)\n    return response\n"


def test_a_property_read_not_named_on_its_line_is_implicit():
    # io finalizers read HTTPResponse.closed; the call lands on the project's nearest line.
    closed = fact("urllib3.response.HTTPResponse.closed",
                  {"self": {"__opaque__": "urllib3.response.HTTPResponse"}},
                  (("return", True),), func="test", line=2,
                  text="response = requests.get(url)")
    other = fact("requests.api.get", {"url": "u"}, func="test", line=2,
                 text="response = requests.get(url)")
    d = diff_facts(fs(other), fs(other, closed), context=ctx_for(REQ_SRC))
    e = next(e for e in d.entries if e.symbol.endswith("closed"))
    assert e.category is FactCategory.DRIFT and e.explained.startswith("implicit")
    assert not failing(d)


def test_a_vanished_call_named_on_its_line_stays_strict():
    src = "def cols(row):\n    return list(row.keys())\n"
    keys = fact("sqlalchemy.engine.result.RowProxy.keys",
                {"self": {"__opaque__": "sqlalchemy.engine.result.RowProxy"}},
                (("return", {"__list__": ["id", "name"]}),), func="cols", line=2,
                text="return list(row.keys())")
    other = fact("lib.other", {"x": 1}, func="cols", line=2, text="return list(row.keys())")
    d = diff_facts(fs(keys, other), fs(other), context=ctx_for(src))
    assert [e.category for e in failing(d)] == [FactCategory.DISAPPEARED]


PATH_SRC = "import pathlib\n\nBASE = pathlib.Path(__file__)\n"


def path_ctor(symbol, outcomes=(("return", None),)):
    return fact(symbol, {"cls": {"__type__": "pathlib.Path"}}, outcomes, func="<module>",
                line=3, text="BASE = pathlib.Path(__file__)")


def test_the_other_half_of_a_paired_constructor_is_the_constructor_protocol():
    new_ = path_ctor("pathlib.Path.__new__", (("return", {"__opaque__": "pathlib.PosixPath"}),))
    init = path_ctor("pathlib.Path.__init__")
    d = diff_facts(fs(new_), fs(new_, init), context=ctx_for(PATH_SRC))
    e = next(e for e in d.entries if e.symbol.endswith("__init__"))
    assert e.category is FactCategory.DRIFT and "constructor protocol" in e.explained


def test_a_constructor_half_that_raises_still_fails():
    new_ = path_ctor("pathlib.Path.__new__", (("return", {"__opaque__": "pathlib.PosixPath"}),))
    init = path_ctor("pathlib.Path.__init__", (("raise", "TypeError"),))
    d = diff_facts(fs(new_), fs(new_, init), context=ctx_for(PATH_SRC))
    assert [e.symbol for e in failing(d)] == ["pathlib.Path.__init__"]


def test_the_tracer_observing_itself_is_not_compared():
    own = fact("collections.deque.popleft", outcomes=(("return", 1),),
               file="/tmp/pymolt-trace-x/bundle/pymolt_trace/_rawthreads.py")
    d = diff_facts(fs(), fs(own))
    assert not d.entries and any(w.startswith("tracer-self:1:") for w in d.warnings)

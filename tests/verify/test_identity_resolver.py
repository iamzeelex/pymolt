"""Static identity: public import paths resolved in a subprocess of the capture's interpreter.

Every test resolves against a fake package written to disk, so nothing here depends on what
happens to be installed — and nothing is ever imported into the test process itself.
"""

from __future__ import annotations

import ast
import json
import os
import sys
import textwrap
import time

import pytest

from pymolt.verify import identity_resolver
from pymolt.verify.identity_resolver import (
    _PROBE_SOURCE,
    InterpreterChoice,
    resolve_identities,
    select_interpreter,
)

_HOST = InterpreterChoice(executable=sys.executable, source="host")


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def _make_project(root):
    """A project whose root holds ``fakepkg`` (importable because the root is on sys.path)."""
    _write(
        root / "fakepkg" / "__init__.py",
        """
        from fakepkg._impl import Thing, Other, Pub, decorated, parse
        thing_alias = Thing
        print("fakepkg says hello on stdout")  # must not corrupt the protocol
        """,
    )
    _write(
        root / "fakepkg" / "_decorators.py",
        """
        import functools

        def logged(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                return fn(*args, **kwargs)
            return wrapper
        """,
    )
    _write(
        root / "fakepkg" / "_impl.py",
        """
        from typing import overload
        from fakepkg._decorators import logged

        class Thing:
            def __init__(self):
                self.x = 1

            def run(self):
                return self.x

        class Other:
            def __init__(self):  # a second __init__: the v1 name is ambiguous
                pass

        class Pub:
            def ping(self):
                return "pong"

        Pub.__module__ = "fakepkg"  # documentation-style rewrite (httpx does this)

        @logged
        def decorated(value):
            return value

        @overload
        def parse(value: int) -> int: ...
        @overload
        def parse(value: str) -> str: ...
        def parse(value):
            return value
        """,
    )
    _write(root / "fakepkg" / "broken.py", "raise RuntimeError('boom at import')\n")
    _write(root / "fakepkg" / "slow.py", "import time\ntime.sleep(30)\n")
    return root


@pytest.fixture
def project(tmp_path):
    return _make_project(tmp_path / "proj")


_PATHS = [
    "fakepkg.Thing",
    "fakepkg.thing_alias",
    "fakepkg.Pub",
    "fakepkg.decorated",
    "fakepkg.parse",
    "fakepkg._impl",
    "fakepkg.Thing.run",
    "fakepkg.Missing",
    "fakepkg.broken.anything",
    "nosuchdist.thing",
]


@pytest.fixture(scope="module")
def resolved(tmp_path_factory):
    """One probe run over every path above (the subprocess is the slow part)."""
    root = _make_project(tmp_path_factory.mktemp("resolved") / "proj")
    return resolve_identities(_PATHS, root, _HOST, use_cache=False)


def test_probe_parses_as_python36():
    ast.parse(_PROBE_SOURCE, feature_version=(3, 6))


@pytest.mark.parametrize(("path", "sym", "kind"), [
    ("fakepkg.Thing", "fakepkg._impl.Thing", "class"),                # a re-export
    ("fakepkg.thing_alias", "fakepkg._impl.Thing", "class"),          # an alias of it
    ("fakepkg.Pub", "fakepkg._impl.Pub", "class"),    # where its code runs, not its __module__
    ("fakepkg.decorated", "fakepkg._impl.decorated", "function"),     # through the wrapper
    ("fakepkg._impl", "fakepkg._impl", "module"),
    ("fakepkg.Thing.run", "fakepkg._impl.Thing.run", "function"),
])
def test_a_public_path_resolves_to_where_its_code_lives(resolved, path, sym, kind):
    r = resolved.results[path]
    assert (r.sym, r.kind) == (sym, kind) and r.error is None


def test_v1_names_are_kept_only_where_they_are_unambiguous(resolved):
    r = resolved.results
    assert "fakepkg._impl.run" in r["fakepkg.Thing"].legacy  # unique code name
    assert "fakepkg._impl.__init__" not in r["fakepkg.Thing"].legacy  # Other.__init__ too
    assert r["fakepkg.Thing.run"].legacy == ["fakepkg._impl.run"]
    # typing.overload stubs are compiled but never run: the implementation stays unique.
    assert r["fakepkg.parse"].legacy == ["fakepkg._impl.parse"]


def test_a_rewritten_module_keeps_the_public_path_as_an_alias(resolved):
    assert "fakepkg.Pub" in resolved.results["fakepkg.Pub"].aliases


def test_a_decorated_function_is_marked_unwrapped(resolved):
    assert resolved.results["fakepkg.decorated"].unwrapped is True


@pytest.mark.parametrize(("path", "fragment"), [
    ("fakepkg.Missing", "AttributeError"),
    ("fakepkg.broken.anything", "boom at import"),
    ("nosuchdist.thing", "nosuchdist"),
])
def test_a_path_that_cannot_resolve_is_a_lasting_error(resolved, path, fragment):
    r = resolved.results[path]
    assert fragment in r.error and not r.transient and r.sym is None


def test_the_batch_counts_what_resolved_and_names_its_python(resolved):
    assert not any(identity.transient for identity in resolved.results.values())
    assert resolved.resolved == 7
    assert resolved.python == ".".join(str(part) for part in sys.version_info[:3])


def test_per_path_timeout_keeps_resolving_the_rest(project):
    res = resolve_identities(
        ["fakepkg.slow.x", "fakepkg.thing_alias"],
        project,
        _HOST,
        per_path_timeout=0.5,
        use_cache=False,
    )
    slow = res.results["fakepkg.slow.x"]
    assert slow.error.startswith("timeout") and slow.transient
    assert res.results["fakepkg.thing_alias"].sym == "fakepkg._impl.Thing"


def test_overall_timeout_never_raises_and_keeps_partial_results(project):
    started = time.monotonic()
    res = resolve_identities(
        ["fakepkg.Thing", "fakepkg.slow.x", "fakepkg.thing_alias"],
        project,
        _HOST,
        timeout=3.0,
        per_path_timeout=0,  # no per-path alarm: only the overall timeout can stop it
        use_cache=False,
    )
    assert time.monotonic() - started < 20
    assert res.results["fakepkg.Thing"].resolved  # answered before the hang
    for path in ("fakepkg.slow.x", "fakepkg.thing_alias"):
        assert res.results[path].transient and "timed out" in res.results[path].error
    assert any("timed out" in note for note in res.notes)


def test_missing_interpreter_degrades_to_errors(project):
    res = resolve_identities(
        ["fakepkg.Thing"],
        project,
        InterpreterChoice(executable=str(project / "no-python"), source="probe-python"),
    )
    assert res.results["fakepkg.Thing"].error.startswith("could not start")
    assert res.resolved == 0


def test_cache_reuses_answers_until_a_manifest_changes(project, monkeypatch):
    (project / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    paths = ["fakepkg.Thing", "fakepkg.slow.x"]
    first = resolve_identities(paths, project, _HOST, per_path_timeout=0.5)
    assert first.from_cache == 0
    cache_files = list((project / ".pymolt" / "cache").glob("identity-*.json"))
    assert len(cache_files) == 1
    cached = json.loads(cache_files[0].read_text(encoding="utf-8"))
    assert "fakepkg.Thing" in cached["results"]
    assert "fakepkg.slow.x" not in cached["results"]  # transient answers are never cached

    calls = []
    real = identity_resolver._run_probe

    def spy(interpreter, root, todo, timeout, per_path_timeout):
        calls.append(list(todo))
        return real(interpreter, root, todo, timeout, per_path_timeout)

    monkeypatch.setattr(identity_resolver, "_run_probe", spy)
    second = resolve_identities(["fakepkg.Thing"], project, _HOST)
    assert second.from_cache == 1 and calls == []
    assert second.results["fakepkg.Thing"].sym == "fakepkg._impl.Thing"

    stat = os.stat(project / "pyproject.toml")
    os.utime(project / "pyproject.toml", ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    third = resolve_identities(["fakepkg.Thing"], project, _HOST)
    assert third.from_cache == 0 and calls == [["fakepkg.Thing"]]


def test_interpreter_choice_order(tmp_path):
    trace = tmp_path / "t.jsonl"
    trace.write_text("", encoding="utf-8")

    host = select_interpreter(None, trace)
    assert host.source == "host" and host.executable == sys.executable
    assert "pymolt's own interpreter" in host.note

    meta = tmp_path / "t.jsonl.meta.json"
    meta.write_text(json.dumps({"processes": [{"executable": sys.executable}]}), encoding="utf-8")
    recorded = select_interpreter(None, trace)
    assert (recorded.source, recorded.executable) == ("trace-metadata", sys.executable)
    assert recorded.note is None

    # A workload that spawns other Pythons: the one that recorded the bulk of the events wins.
    other = tmp_path / "other-python"
    other.write_text("#!/bin/sh\n", encoding="utf-8")
    other.chmod(0o755)
    meta.write_text(json.dumps({"processes": [
        {"executable": str(other), "counters": {"events_written": 3}},
        {"executable": sys.executable, "counters": {"events_written": 5000}},
    ]}), encoding="utf-8")
    busiest = select_interpreter(None, trace)
    assert busiest.executable == sys.executable
    assert "ran 2 interpreters" in busiest.note

    explicit = select_interpreter(sys.executable, trace)
    assert explicit.source == "probe-python"

    meta.write_text(
        json.dumps({"processes": [{"executable": "/in/a/container/python3.6"}]}), encoding="utf-8"
    )
    unrunnable = select_interpreter(None, trace)
    assert unrunnable.source == "host"
    assert "/in/a/container/python3.6" in unrunnable.note

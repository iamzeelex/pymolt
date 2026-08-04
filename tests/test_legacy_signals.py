"""Facts a dead project states about itself that pymolt used to render as a dash.

All four came out of an end-to-end run against the `blaze` fixture — abandoned
around 2016, six manifests, git:// dependencies. Each is a case where pymolt had
the information and threw it away, or asserted a guess as a fact.
"""

from __future__ import annotations

from pymolt.core.legacy_stdlib import describe, is_py2_stdlib, py3_replacement
from pymolt.inventory.edges import build_inventory
from pymolt.setup.service import _pick_default_manifest, gather_setup_options

# ── Python-2 stdlib is evidence, not a dependency ─────────────────────────────

class TestPy2Stdlib:
    def test_known_names_are_not_third_party(self):
        assert is_py2_stdlib("ConfigParser") and is_py2_stdlib("StringIO")
        assert not is_py2_stdlib("flask")

    def test_replacement_is_named_where_one_exists(self):
        assert py3_replacement("ConfigParser") == "configparser"
        assert py3_replacement("urllib2") == "urllib.request"
        assert py3_replacement("sets") == ""      # removed outright
        assert "ConfigParser → configparser" in describe(["ConfigParser"])

    def test_contact_map_reports_them_instead_of_counting_them(self, tmp_path):
        from pymolt.verify.contact_map import build_contact_map

        pkg = tmp_path / "legacyapp"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "core.py").write_text(
            "import ConfigParser\nimport flask\n\n"
            "def go():\n"
            "    ConfigParser.SafeConfigParser()\n"
            "    return flask.Flask(__name__)\n",
            encoding="utf-8",
        )

        cmap = build_contact_map(tmp_path)

        assert "ConfigParser" not in cmap.by_dep     # not a package to migrate
        assert "flask" in cmap.by_dep                # the real one still counts
        assert any("not been ported to Python 3" in n for n in cmap.notes)


# ── a VCS dependency has a name, and git:// no longer works ───────────────────

class TestVcsEdges:
    def _edges(self, tmp_path, line):
        (tmp_path / "requirements.txt").write_text(line + "\n", encoding="utf-8")
        return build_inventory(tmp_path).edges

    def test_name_is_recovered_from_the_url_and_marked_inferred(self, tmp_path):
        (edge,) = self._edges(tmp_path, "git+https://github.com/blaze/odo.git")
        assert edge.name == "odo"
        assert edge.name_inferred is True     # the repo name is a guess, not a declaration

    def test_declared_egg_name_wins_and_is_not_inferred(self, tmp_path):
        (edge,) = self._edges(tmp_path, "git+https://github.com/x/repo.git#egg=realname")
        assert edge.name == "realname"
        assert edge.name_inferred is False

    def test_dead_git_protocol_is_flagged_as_a_blocker(self, tmp_path):
        (edge,) = self._edges(tmp_path, "git+git://github.com/blaze/datashape.git")
        assert edge.name == "datashape"
        assert edge.blocker and "git://" in edge.blocker

    def test_an_ordinary_dependency_carries_no_blocker(self, tmp_path):
        (edge,) = self._edges(tmp_path, "flask==2.0.3")
        assert edge.blocker is None
        assert edge.name_inferred is False


# ── which manifest is "the project"? ──────────────────────────────────────────

class _Src:
    def __init__(self, name, is_lock=False):
        from pathlib import Path
        self.path = Path(name)
        self.is_lock = is_lock


class TestManifestRanking:
    def _pick(self, *names):
        return _pick_default_manifest([_Src(n) for n in names]).path.name

    def test_a_python_matrix_row_never_wins(self):
        """The blaze case: six manifests, and the alphabet elected the 3.10 row."""
        assert self._pick(
            "requirements-310.txt", "requirements-311.txt", "requirements-312.txt",
            "requirements-39.txt", "requirements-rtd.txt", "requirements-strict.txt",
        ) == "requirements-strict.txt"

    def test_dev_never_outranks_the_runtime_manifest(self):
        assert self._pick("requirements-dev.txt", "requirements.txt") == "requirements.txt"

    def test_a_lock_beats_a_manifest(self):
        picked = _pick_default_manifest(
            [_Src("requirements.txt"), _Src("uv.lock", is_lock=True)]
        )
        assert picked.path.name == "uv.lock"

    def test_setup_py_is_a_last_resort(self):
        # pymolt does not classify setup.py's edges yet, so it must not be the
        # assumed source of truth when a real manifest is present.
        assert self._pick("setup.py", "requirements-strict.txt") == "requirements-strict.txt"
        assert self._pick("setup.py", "setup.cfg") == "setup.py"   # nothing better


# ── an assumed interpreter is not a declared one ──────────────────────────────

class TestBasePythonProvenance:
    def test_undeclared_python_is_marked_assumed_and_noted(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("flask\n", encoding="utf-8")
        options = gather_setup_options(tmp_path)
        assert options.base_python_source == "assumed"
        assert any("No Python version is declared" in n for n in options.notes)

    def test_declared_python_is_marked_declared(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("flask\n", encoding="utf-8")
        (tmp_path / ".python-version").write_text("3.8.10\n", encoding="utf-8")
        options = gather_setup_options(tmp_path)
        assert options.base_python_default == "3.8.10"
        assert options.base_python_source == "declared"

    def test_gold_needs_a_real_interpreter_not_a_fallback(self):
        from pymolt.assess.service import BaselineTier, classify_baseline_tier

        common = {"baseline_resolved": True, "source_fixation": "pinned", "tests_passed": False}
        assert classify_baseline_tier(**common) is BaselineTier.GOLD
        # Pins are exact; the environment they were resolved in was a guess.
        assert classify_baseline_tier(
            **common, base_python_assumed=True
        ) is BaselineTier.SILVER

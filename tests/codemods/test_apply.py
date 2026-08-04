"""Tests for pymolt.codemods.apply — the local LibCST applier (offline)."""

from __future__ import annotations

from pymolt.codemods.apply import apply_pattern, apply_to_repo
from pymolt.codemods.models import CodemodPattern


def _rewrite_import():
    return CodemodPattern(
        old_qualname="flask.helpers.safe_join",
        new_qualname="werkzeug.utils.safe_join",
        kind="rewrite-import",
        confidence="high",
    )


def _rename_call():
    return CodemodPattern(
        old_qualname="mypkg.old_api",
        new_qualname="mypkg.new_api",
        kind="rename-call",
        confidence="high",
    )


def _rewrite_module():
    # A whole-namespace move — one pattern for all of keras.* (framework succession).
    return CodemodPattern(
        old_qualname="keras", new_qualname="tensorflow.keras",
        kind="rewrite-module", confidence="high",
    )


def test_rewrite_module_moves_whole_namespace_any_symbol():
    out, n = apply_pattern("from keras.layers import Conv2D, Dense, Input\n", _rewrite_module())
    assert n == 1 and out.strip() == "from tensorflow.keras.layers import Conv2D, Dense, Input"


def test_rewrite_module_aliases_bare_import_to_preserve_binding():
    out, n = apply_pattern("import keras\nx = keras.Model()\n", _rewrite_module())
    assert "import tensorflow.keras as keras" in out and n == 1  # keras.Model() still resolves


def test_rewrite_module_preserves_alias_and_ignores_unrelated():
    out, _ = apply_pattern("import keras.backend as K\nimport numpy as np\n", _rewrite_module())
    assert "import tensorflow.keras.backend as K" in out
    assert "import numpy as np" in out  # untouched


def _rewrite_attr():
    # tf.Session() → tf.compat.v1.Session() — the attribute-path form (TF1→TF2).
    return CodemodPattern(
        old_qualname="tensorflow.Session", new_qualname="tensorflow.compat.v1.Session",
        kind="rewrite-attr", confidence="high",
    )


def test_rewrite_attr_inserts_module_segment_on_aliased_import():
    out, n = apply_pattern("import tensorflow as tf\nsess = tf.Session()\n", _rewrite_attr())
    assert n == 1 and "tf.compat.v1.Session()" in out


def test_rewrite_attr_is_binding_safe():
    # `tf` bound to numpy, not tensorflow → must NOT be rewritten.
    src = "import numpy as tf\ntf.Session()\n"
    out, n = apply_pattern(src, _rewrite_attr())
    assert n == 0 and out == src


def test_rewrite_attr_only_touches_the_matched_symbol():
    out, _ = apply_pattern(
        "import tensorflow as tf\ncfg = tf.ConfigProto()\ns = tf.Session(config=cfg)\n",
        _rewrite_attr(),
    )
    assert "tf.ConfigProto()" in out  # untouched
    assert "tf.compat.v1.Session(config=cfg)" in out


class TestApplyPattern:
    def test_rewrite_import_single(self):
        src = "from flask.helpers import safe_join\nx = safe_join(a, b)\n"
        out, sites = apply_pattern(src, _rewrite_import())
        assert "from werkzeug.utils import safe_join" in out
        assert "safe_join(a, b)" in out
        assert sites == 1

    def test_rewrite_import_multi_name_split(self):
        src = "from flask.helpers import safe_join, url_for\nx = safe_join(a, b)\n"
        out, _ = apply_pattern(src, _rewrite_import())
        assert "from flask.helpers import url_for" in out
        assert "from werkzeug.utils import safe_join" in out
        assert ";" not in out

    def test_rewrite_import_preserves_comment(self):
        src = "from flask.helpers import safe_join  # keep\nx = safe_join(a, b)\n"
        out, _ = apply_pattern(src, _rewrite_import())
        assert "# keep" in out

    def test_rename_call_and_import(self):
        src = "from mypkg import old_api\nr = old_api(1)\n"
        out, sites = apply_pattern(src, _rename_call())
        assert "import new_api" in out
        assert "new_api(1)" in out
        assert "old_api" not in out
        assert sites >= 1

    def test_noop_when_absent(self):
        src = "y = 1 + 2\n"
        out, sites = apply_pattern(src, _rewrite_import())
        assert out == src
        assert sites == 0


class TestRenameIsBindingAware:
    """rename-call uses the scope graph: only the imported symbol is rewritten."""

    P = CodemodPattern(
        old_qualname="mypkg.old_api", new_qualname="mypkg.new_api",
        kind="rename-call", confidence="high",
    )

    def test_unrelated_local_is_left_alone(self):
        # a local function named old_api, never imported from mypkg
        src = "def old_api(x):\n    return x\n\ndef use():\n    return old_api(5)\n"
        out, sites = apply_pattern(src, self.P)
        assert sites == 0
        assert out == src  # untouched

    def test_same_name_from_other_module_left_alone(self):
        src = "from othermod import old_api\ny = old_api(1)\n"
        out, sites = apply_pattern(src, self.P)
        assert sites == 0
        assert out == src

    def test_real_import_is_renamed(self):
        src = "from mypkg import old_api\nz = old_api(2)\n"
        out, _ = apply_pattern(src, self.P)
        assert "from mypkg import new_api" in out
        assert "new_api(2)" in out

    def test_aliased_import_keeps_alias_call(self):
        src = "from mypkg import old_api as foo\nw = foo(3)\n"
        out, _ = apply_pattern(src, self.P)
        # import target renamed, the alias call site stays valid (unchanged)
        assert "from mypkg import new_api as foo" in out
        assert "foo(3)" in out

    def test_attribute_call_via_module_import(self):
        src = "import mypkg\nr = mypkg.old_api(4)\n"
        out, _ = apply_pattern(src, self.P)
        assert "mypkg.new_api(4)" in out

    def test_local_shadow_is_not_rewritten(self):
        # imported AND shadowed by a local def → conservatively left alone
        src = (
            "from mypkg import old_api\n"
            "def old_api(x):\n    return x\n"
            "v = old_api(1)\n"
        )
        out, sites = apply_pattern(src, self.P)
        # the call resolves ambiguously (shadowed) → call not rewritten
        assert "old_api(1)" in out


class TestApplyToRepo:
    def _make_repo(self, tmp_path):
        (tmp_path / "app.py").write_text(
            "from flask.helpers import safe_join\n\n"
            "def serve(b, n):\n    return safe_join(b, n)\n"
        )
        (tmp_path / "unrelated.py").write_text("z = 3\n")
        venv = tmp_path / ".venv"
        venv.mkdir()
        (venv / "lib.py").write_text("from flask.helpers import safe_join\n")
        return tmp_path

    def test_dry_run_does_not_write(self, tmp_path):
        repo = self._make_repo(tmp_path)
        result = apply_to_repo(repo, [_rewrite_import()])
        assert result.dry_run is True
        assert result.files_changed == 1
        # disk unchanged
        assert "flask.helpers" in (repo / "app.py").read_text()

    def test_write_applies(self, tmp_path):
        repo = self._make_repo(tmp_path)
        result = apply_to_repo(repo, [_rewrite_import()], write=True)
        assert result.dry_run is False
        assert "werkzeug.utils" in (repo / "app.py").read_text()

    def test_skips_virtualenv(self, tmp_path):
        repo = self._make_repo(tmp_path)
        result = apply_to_repo(repo, [_rewrite_import()], write=True)
        # the .venv copy must be untouched
        assert "flask.helpers" in (repo / ".venv" / "lib.py").read_text()
        assert all(".venv" not in c.path for c in result.changes)

    def test_skips_unparseable_file(self, tmp_path):
        (tmp_path / "good.py").write_text("from flask.helpers import safe_join\n")
        (tmp_path / "broken.py").write_text("def (:\n")  # syntax error
        result = apply_to_repo(tmp_path, [_rewrite_import()], write=True)
        # good file still processed despite the broken one
        assert result.files_changed == 1

"""
tests/test_upstream_compat.py

Tests for mining official upstream author-provided compatibility and rename dictionaries.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from axiom_graph.analyzers.upstream_compat import mine_upstream_compatibility_tables
from axiom_graph.core.models import ApiState, ChangeRisk


def test_mine_tensorflow_renames_v2_mock() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        compat_dir = root / "tensorflow" / "tools" / "compatibility"
        compat_dir.mkdir(parents=True)

        renames_code = """
# Auto-generated TensorFlow compatibility table
renames = {
    "tf.AUTO_REUSE": "tf.compat.v1.AUTO_REUSE",
    "tf.accumulate_n": "tf.math.accumulate_n",
    "tf.angle": "tf.math.angle",
    "tf.assert_greater": "tf.debugging.assert_greater",
}

symbol_renames = {
    "tf.contrib.layers.flatten": "tf.keras.layers.Flatten",
}
"""
        (compat_dir / "renames_v2.py").write_text(renames_code, encoding="utf-8")

        changes, patterns = mine_upstream_compatibility_tables(root, "tensorflow")

        assert len(changes) == 5
        assert len(patterns) == 5

        qualnames = {p.old_qualname: p.new_qualname for p in patterns}
        assert qualnames["tf.AUTO_REUSE"] == "tf.compat.v1.AUTO_REUSE"
        assert qualnames["tf.accumulate_n"] == "tf.math.accumulate_n"
        assert qualnames["tf.contrib.layers.flatten"] == "tf.keras.layers.Flatten"

        for c in changes:
            assert c.risk == ChangeRisk.MECHANICAL
            assert c.state == ApiState.REMOVED
            assert "Official upstream replacement" in (c.deprecation_hint or "")


def test_mine_jax_deprecations_mock() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        src_dir = root / "jax" / "_src"
        src_dir.mkdir(parents=True)

        deprecations_code = """
_deprecated_function_replacements = {
    "jax.experimental.pjit": "jax.jit",
    "jax.tree_util.tree_multimap": "jax.tree_util.tree_map",
}
"""
        (src_dir / "deprecations.py").write_text(deprecations_code, encoding="utf-8")

        changes, patterns = mine_upstream_compatibility_tables(root, "jax")

        assert len(changes) == 2
        assert len(patterns) == 2

        qualnames = {p.old_qualname: p.new_qualname for p in patterns}
        assert qualnames["jax.experimental.pjit"] == "jax.jit"
        assert qualnames["jax.tree_util.tree_multimap"] == "jax.tree_util.tree_map"

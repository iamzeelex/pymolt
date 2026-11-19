"""Framework succession: detection, curated edges, in-place apply (S2). No network."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from pymolt.codemods.models import CodemodPattern
from pymolt.interfaces.cli import commands
from pymolt.strategic.succession.detect import detect_frameworks
from pymolt.strategic.succession.models import AbstractionMapping, SuccessionEdge, WeightConversion
from pymolt.strategic.succession.service import run_succession

runner = CliRunner()


class _FakeEdges:
    """Stands in for the curated knowledge: returns fixed edges, records the lookups."""

    def __init__(self, edges):
        self._edges = edges
        self.calls = []

    def __call__(self, frameworks):
        self.calls.append(list(frameworks))
        return self._edges


def _keras_shim() -> SuccessionEdge:
    return SuccessionEdge(
        from_framework="keras", to_framework="tensorflow.keras", strategy="in_place",
        summary="Keras absorbed into tf.keras",
        patterns=[CodemodPattern(
            old_qualname="keras", new_qualname="tensorflow.keras",
            kind="rewrite-module", confidence="high",
        )],
    )


def _transplant() -> SuccessionEdge:
    return SuccessionEdge(
        from_framework="keras", to_framework="torch+torchvision", strategy="transplant",
        summary="Re-init from torchvision", scaffold_hint="maskrcnn_resnet50_fpn(...)",
        abstraction_mappings=[
            AbstractionMapping(from_symbol="keras.Model", to_symbol="torch.nn.Module")
        ],
        weight_conversion=WeightConversion(
            from_format=".h5", to_format=".pth", approach="h5py→state_dict"
        ),
    )


# ── detection ─────────────────────────────────────────────────────────────────

def test_detect_frameworks_from_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "tensorflow>=1.3.0\nkeras>=2.0.8\nnumpy\n", encoding="utf-8"
    )
    fw = detect_frameworks(tmp_path)
    assert {"tensorflow", "keras", "numpy"} <= set(fw)


def test_detect_frameworks_empty_when_no_manifest(tmp_path):
    assert detect_frameworks(tmp_path) == []


# ── service: in-place applies, transplant does not ───────────────────────────

def test_run_succession_applies_in_place_shim(tmp_path):
    (tmp_path / "requirements.txt").write_text("keras>=2.0.8\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "from keras.layers import Conv2D\nx = Conv2D(3)\n", encoding="utf-8"
    )
    fake = _FakeEdges([_keras_shim()])

    edges, result = run_succession(tmp_path, edges_for=fake)

    assert fake.calls == [["keras"]]  # detected + looked up
    assert result is not None and result.files_changed == 1
    # dry-run: disk is untouched
    assert "from keras.layers import Conv2D" in (tmp_path / "app.py").read_text()


def test_in_place_previews_are_what_a_plan_holds(tmp_path):
    from pymolt.strategic.succession.service import in_place_previews

    (tmp_path / "app.py").write_text("from keras.layers import Conv2D\n", encoding="utf-8")

    previews = in_place_previews(tmp_path, [_keras_shim(), _transplant()])

    assert [p.new_source for p in previews] == ["from tensorflow.keras.layers import Conv2D\n"]
    assert (tmp_path / "app.py").read_text() == "from keras.layers import Conv2D\n"


def test_cli_succession_plan_is_applied_by_apply_only(tmp_path, monkeypatch):
    from pymolt.ingestion.config import EnvConfig, ToolChoice
    from pymolt.migration_plan import MigrationPlan

    (tmp_path / "requirements.txt").write_text("keras>=2.0.8\n", encoding="utf-8")
    (tmp_path / "requirements-target.txt").write_text("tensorflow==2.15.0\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("from keras.layers import Conv2D\n", encoding="utf-8")
    EnvConfig(
        selected_manifest="requirements.txt", selected_tool=ToolChoice.UV,
        base_python="3.11", target_python="3.12",
    ).save(tmp_path / ".pymolt" / "env_config.json")
    monkeypatch.setattr(
        "pymolt.strategic.succession.service.run_succession",
        lambda *a, **k: ([_keras_shim()], None),
    )

    result = runner.invoke(commands.app, ["succession", str(tmp_path), "--plan", "--json"])

    assert result.exit_code == 0, result.output
    plan = MigrationPlan.load(tmp_path)
    assert plan is not None and [c.path for c in plan.changes] == ["app.py"]
    assert json.loads(result.stdout)["plan_path"]
    assert (tmp_path / "app.py").read_text() == "from keras.layers import Conv2D\n"


def test_cli_succession_write_names_its_replacement(tmp_path):
    result = runner.invoke(commands.app, ["succession", str(tmp_path), "--write"])
    assert result.exit_code == 2
    assert "--plan" in result.output and "pymolt apply" in result.output


def test_run_succession_never_applies_transplant(tmp_path):
    (tmp_path / "requirements.txt").write_text("keras>=2.0.8\n", encoding="utf-8")
    src = "from keras.layers import Conv2D\n"
    (tmp_path / "app.py").write_text(src, encoding="utf-8")

    edges, result = run_succession(tmp_path, edges_for=_FakeEdges([_transplant()]))
    assert edges[0].is_transplant
    assert result is None  # nothing in-place to apply
    assert (tmp_path / "app.py").read_text() == src  # untouched


def test_run_succession_no_frameworks_returns_empty(tmp_path):
    edges, result = run_succession(tmp_path, edges_for=_FakeEdges([_keras_shim()]))
    assert edges == [] and result is None


# ── CLI smoke ─────────────────────────────────────────────────────────────────

def test_cli_succession_json_smoke(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("keras>=2.0.8\n", encoding="utf-8")
    monkeypatch.setattr(
        "pymolt.strategic.succession.service.run_succession",
        lambda *a, **k: ([_keras_shim()], None),
    )
    result = runner.invoke(commands.app, ["succession", str(tmp_path), "--json"])
    assert result.exit_code == 0
    assert "tensorflow.keras" in result.output and "keras" in result.output


# ── transplant plan (S3) ──────────────────────────────────────────────────────

def test_build_transplant_plan_orders_steps_and_ends_in_contract():
    from pymolt.strategic.succession.transplant import build_transplant_plan
    plan = build_transplant_plan(_transplant())
    assert [s.n for s in plan.steps] == list(range(1, len(plan.steps) + 1))  # 1..N
    last = plan.steps[-1]
    assert "verify" in last.title.lower()
    joined = "\n".join(last.commands)
    assert "pymolt capture baseline" in joined
    assert "pymolt capture post-migration" in joined
    assert "pymolt verify" in joined


def test_build_transplant_plan_carries_scaffold_and_weights():
    from pymolt.strategic.succession.transplant import build_transplant_plan
    plan = build_transplant_plan(_transplant())
    assert plan.steps[0].code == "maskrcnn_resnet50_fpn(...)"  # scaffold snippet
    assert any(".h5" in s.title and ".pth" in s.title for s in plan.steps)  # weights step


def test_cli_succession_renders_transplant_plan(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("keras>=2.0.8\n", encoding="utf-8")
    monkeypatch.setattr(
        "pymolt.strategic.succession.service.run_succession",
        lambda *a, **k: ([_transplant()], None),
    )
    result = runner.invoke(commands.app, ["succession", str(tmp_path)])
    assert result.exit_code == 0
    assert "transplant" in result.output
    assert "Scaffold" in result.output and "pymolt capture" in result.output


# ── curated knowledge ─────────────────────────────────────────────────────────

def test_curated_knowledge_holds_the_framework_edges():
    from pymolt.knowledge import succession_edges

    edges = succession_edges(["tensorflow==1.15", "keras>=2.0.8", "numpy"])

    by_target = {e.to_framework: e for e in edges}
    assert set(by_target) == {"tensorflow.keras", "torch+torchvision", "tensorflow>=2"}
    assert by_target["tensorflow.keras"].is_in_place
    assert by_target["tensorflow.keras"].patterns[0].kind == "rewrite-module"
    assert by_target["torch+torchvision"].is_transplant
    assert by_target["torch+torchvision"].weight_conversion.from_format == ".h5"
    assert all(any("pymolt knowledge" in ev for ev in e.evidence) for e in edges)


def test_knowledge_edges_dedupe_across_aliases():
    from pymolt.knowledge import succession_edges

    edges = succession_edges(["tensorflow", "tensorflow<2"])
    assert [e.to_framework for e in edges] == ["tensorflow>=2"]


def test_run_succession_applies_the_curated_shim(tmp_path):
    (tmp_path / "requirements.txt").write_text("keras>=2.0.8\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("from keras.layers import Conv2D\n", encoding="utf-8")

    edges, result = run_succession(tmp_path)

    assert {e.to_framework for e in edges} == {"tensorflow.keras", "torch+torchvision"}
    assert result is not None and result.files_changed == 1
    assert (tmp_path / "app.py").read_text() == "from keras.layers import Conv2D\n"


# ── C8: an honest "not in pymolt's knowledge" ─────────────────────────────────

def test_plausible_frameworks_drops_plain_libraries():
    from pymolt.strategic.succession.detect import plausible_frameworks

    declared = ["theano", "scipy", "six", "numpy", "Keras>=2", "nose"]
    assert plausible_frameworks(declared) == ["Keras>=2", "nose", "theano"]
    assert plausible_frameworks(["scipy", "six"]) == []


def test_knowledge_coverage_names_what_the_curated_catalog_knows():
    from pymolt.strategic.succession.detect import knowledge_coverage

    assert {"keras", "tensorflow"} <= set(knowledge_coverage())


def test_cli_says_a_framework_is_not_in_knowledge(tmp_path):
    from typer.testing import CliRunner

    from pymolt.interfaces.cli.commands import app

    (tmp_path / "requirements.txt").write_text("theano\nscipy\nsix\n", encoding="utf-8")
    out = CliRunner().invoke(app, ["succession", str(tmp_path)]).output
    assert "Frameworks detected: theano" in out and "scipy" not in out
    assert "not in pymolt's succession knowledge" in out and "keras" in out
    assert "No known succession path" not in out


def test_cli_says_it_checked_every_dependency(tmp_path):
    import json

    from typer.testing import CliRunner

    from pymolt.interfaces.cli.commands import app

    (tmp_path / "requirements.txt").write_text("scipy\nsix\n", encoding="utf-8")
    runner = CliRunner()
    out = runner.invoke(app, ["succession", str(tmp_path)]).output
    assert "No framework among the 2 declared dependencies" in out
    assert "Checked all 2 declared dependencies" in out
    payload = json.loads(runner.invoke(app, ["succession", str(tmp_path), "--json"]).stdout)
    assert payload["frameworks"] == [] and payload["checked_dependencies"] == 2
    assert "keras" in payload["knowledge_covers"]

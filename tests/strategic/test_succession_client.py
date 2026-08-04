"""Framework-succession client + detection + in-place apply (S2). No network."""

from __future__ import annotations

from typer.testing import CliRunner

import pymolt.strategic.succession.client as client_mod
from pymolt.codemods.models import CodemodPattern
from pymolt.interfaces.cli import commands
from pymolt.strategic.succession.detect import detect_frameworks
from pymolt.strategic.succession.models import AbstractionMapping, SuccessionEdge, WeightConversion
from pymolt.strategic.succession.service import run_succession

runner = CliRunner()


class _FakeClient:
    def __init__(self, edges):
        self._edges = edges
        self.calls = []

    def fetch(self, frameworks, *, progress=None):
        self.calls.append(list(frameworks))
        if progress:
            progress("fetching")
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
        abstraction_mappings=[AbstractionMapping(from_symbol="keras.Model", to_symbol="torch.nn.Module")],
        weight_conversion=WeightConversion(from_format=".h5", to_format=".pth", approach="h5py→state_dict"),
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


# ── client ────────────────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_client_fetch_parses_edges_and_sends_token(monkeypatch):
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["url"] = url
        captured["frameworks"] = json["frameworks"]
        captured["auth"] = headers.get("Authorization")
        return _FakeResp({"edges": [_keras_shim().model_dump(mode="json")]})

    monkeypatch.setattr(client_mod.httpx, "post", fake_post)
    edges = client_mod.SuccessionClient("http://svc:8000", token="tok").fetch(["keras"])

    assert captured["url"] == "http://svc:8000/succession"
    assert captured["frameworks"] == ["keras"]
    assert captured["auth"] == "Bearer tok"
    assert len(edges) == 1 and edges[0].is_in_place


def test_client_empty_frameworks_no_request(monkeypatch):
    monkeypatch.setattr(
        client_mod.httpx, "post",
        lambda **k: (_ for _ in ()).throw(AssertionError("should not call network")),
    )
    assert client_mod.SuccessionClient("http://x").fetch([]) == []


# ── service: in-place applies, transplant does not ───────────────────────────

def test_run_succession_applies_in_place_shim(tmp_path):
    (tmp_path / "requirements.txt").write_text("keras>=2.0.8\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "from keras.layers import Conv2D\nx = Conv2D(3)\n", encoding="utf-8"
    )
    fake = _FakeClient([_keras_shim()])

    edges, result = run_succession(tmp_path, base_url="http://x", client=fake)

    assert fake.calls == [["keras"]]  # detected + sent
    assert result is not None and result.files_changed == 1
    # dry-run: disk is untouched
    assert "from keras.layers import Conv2D" in (tmp_path / "app.py").read_text()


def test_run_succession_write_applies_to_disk(tmp_path):
    (tmp_path / "requirements.txt").write_text("keras>=2.0.8\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("from keras.layers import Conv2D\n", encoding="utf-8")

    run_succession(tmp_path, base_url="http://x", write=True, client=_FakeClient([_keras_shim()]))
    assert "from tensorflow.keras.layers import Conv2D" in (tmp_path / "app.py").read_text()


def test_run_succession_never_applies_transplant(tmp_path):
    (tmp_path / "requirements.txt").write_text("keras>=2.0.8\n", encoding="utf-8")
    src = "from keras.layers import Conv2D\n"
    (tmp_path / "app.py").write_text(src, encoding="utf-8")

    edges, result = run_succession(
        tmp_path, base_url="http://x", write=True, client=_FakeClient([_transplant()])
    )
    assert edges[0].is_transplant
    assert result is None  # nothing in-place to apply
    assert (tmp_path / "app.py").read_text() == src  # untouched


def test_run_succession_no_frameworks_returns_empty(tmp_path):
    edges, result = run_succession(tmp_path, base_url="http://x", client=_FakeClient([_keras_shim()]))
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
    assert "contract" in last.title.lower()
    joined = "\n".join(last.commands)
    assert "pymolt contract capture --when baseline" in joined
    assert "--when post-migration" in joined
    assert "pymolt contract report" in joined


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
    assert "Scaffold" in result.output and "pymolt contract" in result.output

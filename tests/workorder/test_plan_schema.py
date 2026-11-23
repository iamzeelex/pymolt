"""The plan's work order (schema 2's steps, in schema 3): the committed JSON schemas, a round
trip, and a schema-1 plan read as step v1."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from pymolt.anchors import StatementIndex
from pymolt.codemods.models import CodemodPattern
from pymolt.migration_plan import (
    DEFAULT_PLAN_PATH,
    PLAN_SCHEMA_VERSION,
    MigrationPlan,
    PlannedFileChange,
    inspect_migration_plan,
)
from pymolt.verify.predict import PredictedRisk, PredictionReport, RiskSite, RiskTier
from pymolt.workorder.edits import HunkMismatch, apply_hunks, make_edit, sha256, split_lines
from pymolt.workorder.models import (
    SCHEMA_FILE,
    BeforeAfter,
    Hunk,
    ManifestPin,
    ManualDo,
    PlanStep,
    SignatureChange,
    StepBaseline,
    StepDelta,
    StepDo,
    StepEffort,
    StepRecipe,
    StepSymbol,
    StepVerify,
    StepWhy,
    VerifySite,
    plan_json_schema,
    step_key,
)

OLD = "import pandas as pd\n\n\ndef combine(s1, s2):\n    return s1.append(s2)\n"
NEW = "import pandas as pd\n\n\ndef combine(s1, s2):\n    return pd.concat([s1, s2])\n"


def test_the_committed_schema_is_the_generated_one():
    committed = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    assert SCHEMA_FILE.name == "migration_plan-3.schema.json"
    assert committed == plan_json_schema(), (
        "pymolt/schemas/migration_plan-3.schema.json is stale: regenerate it with "
        "`python -m pymolt.workorder.models`")
    assert committed["$id"] == "pymolt.plan/3"
    assert committed["properties"]["schema_version"]["default"] == PLAN_SCHEMA_VERSION == 3


def test_the_schema_2_file_stays_committed_and_frozen():
    frozen = json.loads((SCHEMA_FILE.parent / "migration_plan-2.schema.json").read_text(
        encoding="utf-8"))
    assert frozen["$id"] == "pymolt.plan/2"
    assert frozen["properties"]["schema_version"]["default"] == 2
    assert not {"name", "label", "lineage", "content_digest"} & set(frozen["properties"])


def _steps() -> list[PlanStep]:
    edit = make_edit("app.py", OLD, NEW, python_before="3.11", python_after="3.12")
    where = StatementIndex.build(OLD).anchor_at(5, file="app.py",
                                                symbol="pandas.Series.append")
    pattern = CodemodPattern(old_qualname="pandas.Series.append", new_qualname="pandas.concat",
                             kind="rename-call", confidence="verified")
    recipe = StepRecipe(kind="pattern", keys=["rename-call:pandas.Series.append→pandas.concat"],
                        patterns=[pattern])
    return [
        PlanStep(id="env-1", key=step_key("manifest-pin", "requirements.txt"), phase="env",
                 type="manifest-pin", title="Pin pandas", executor="pymolt", claim="intended",
                 basis="target-manifest", scope="env",
                 do=StepDo(recipe=StepRecipe(kind="manifest-pin", pins=[ManifestPin(
                     name="pandas", from_version="1.5.3", to_version="2.2.3",
                     old="pandas==1.5.3\n", new="pandas==2.2.3\n", line=1)])),
                 verify=StepVerify(judge="none")),
        PlanStep(id="env-2", key=step_key("target-env", "env"), phase="env", type="target-env",
                 title="Build the target environment", executor="person", scope="env",
                 do=StepDo(manual=ManualDo(summary="build it", commands=["uv venv"])),
                 effort=StepEffort(size="small", why="a build")),
        PlanStep(id="mech-1", key=step_key("codemod", recipe.keys[0]), phase="mechanical",
                 type="codemod", title="`Series.append` → `pd.concat`", executor="pymolt",
                 claim="preserve", basis="verified-rule", package="pandas", from_="1.5.3",
                 to="2.2.3", symbol=StepSymbol(old="pandas.Series.append", new="pandas.concat"),
                 where=[where], why=StepWhy(evidence=["removed in 2.0"], tier="certain",
                                            risk_ids=["7c0e9a41d2b3"]),
                 do=StepDo(recipe=recipe), edits=[edit],
                 verify=StepVerify(judge="strict", sites=[VerifySite(
                     anchor=where.id, file="app.py", func="combine", baseline_calls=1,
                     coverage="recorded")], command=["pytest"]),
                 checks_at_plan={"compile_target": "ok"}),
        PlanStep(id="beh-1", key=step_key("behaviour-change", "pandas|DataFrame.mean|DEFAULT"),
                 phase="behaviour", type="behaviour-change", title="`DataFrame.mean` default",
                 executor="person", claim="preserve", basis="manual",
                 why=StepWhy(tier="high", baseline=StepBaseline(calls=2, sites=1,
                                                               warnings=["FutureWarning: x"]),
                             delta=StepDelta(kind="DEFAULT", param="numeric_only", old="None",
                                             new="False")),
                 do=StepDo(manual=ManualDo(
                     summary="pass numeric_only", successor=None,
                     signature=SignatureChange(old="(numeric_only=None)",
                                               new="(numeric_only=False)", diff=["- None"]),
                     before_after=BeforeAfter(before="df.mean()", after=None),
                     pitfalls=["a call-time default"], consumers=[])),
                 verify=StepVerify(judge="strict", tests=None, note="no test ids", uncovered=1),
                 effort=StepEffort(size="large", sites=1, files=1, why="a behaviour change")),
    ]


def test_a_plan_with_every_kind_of_step_round_trips(tmp_path):
    plan = MigrationPlan(target_python="3.12", source_manifest="requirements.txt",
                         steps=_steps(), changes=[PlannedFileChange(
                             path="app.py", old_sha256=sha256(OLD), new_sha256=sha256(NEW),
                             old_source=OLD, new_source=NEW, sites=1)])
    path = plan.save(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 3
    assert raw["steps"][2]["from"] == "1.5.3" and "from_" not in raw["steps"][2]
    loaded = MigrationPlan.load(tmp_path)
    assert loaded is not None and loaded.adapted_from is None
    assert loaded.model_dump(mode="json") == plan.model_dump(mode="json")
    assert loaded.step("mech-1").edits[0].after_sha256 == sha256(NEW)
    assert inspect_migration_plan(tmp_path, loaded, check_environment=False).reasons == [
        "planned source is missing or unreadable: app.py"]


def test_a_step_does_one_thing_and_has_a_valid_id():
    with pytest.raises(ValidationError):
        StepDo()
    with pytest.raises(ValidationError):
        StepDo(recipe=StepRecipe(kind="rule"), manual=ManualDo(summary="x"))
    with pytest.raises(ValidationError):
        PlanStep(id="step-1", key="0123456789", phase="env", type="target-env", title="t",
                 executor="person", do=StepDo(manual=ManualDo(summary="x")))
    with pytest.raises(ValidationError):
        ManualDo(summary="x", pitfalls=["p"] * 11)
    with pytest.raises(ValidationError):
        StepWhy(evidence=["x" * 301])


def test_hunks_rebuild_the_after_text_byte_for_byte():
    before = "a = 1\r\nb = 2\r\nc = 3"
    after = "a = 1\r\nb = 20\r\nx = 9\r\nc = 3"
    edit = make_edit("m.py", before, after)
    assert apply_hunks(before, edit.hunks) == after
    assert split_lines(before) == ["a = 1\r\n", "b = 2\r\n", "c = 3"]
    with pytest.raises(HunkMismatch):
        apply_hunks("a = 1\n", [Hunk(old_start=1, old=["z = 0\n"], new_start=1, new=[])])


def test_an_effect_names_statements_not_lines():
    before = "import os\n\n\ndef f(x):\n    # old note\n    return os.path.join(x)\n"
    after = ("import os\nimport collections.abc\n\n\ndef f(x):\n    # new note\n"
             "    return os.path.join(x, 'y')\n")
    edit = make_edit("m.py", before, after)
    old_key = StatementIndex.build(before).statement_at(6).key
    new_key = StatementIndex.build(after).statement_at(7).key
    assert edit.effect.replace == [(old_key, new_key)]
    assert edit.effect.imports_inserted == [StatementIndex.build(after).statement_at(2).key]
    assert edit.effect.insert == [] and edit.effect.delete == []
    assert [a.scope for a in edit.anchors_old] == ["f"]
    assert {a.stmt for a in edit.anchors_new} == {"import collections.abc",
                                                  "return os.path.join(x, 'y')"}
    # A comment edited alone changes no statement.
    assert make_edit("m.py", before, before.replace("old", "other")).effect.is_empty()


def _v1_json(tmp_path) -> dict:
    risk = PredictedRisk(id="a" * 12, tier=RiskTier.CERTAIN, symbol="pandas.Series.append",
                         site=RiskSite(file="app.py", func="combine", line=5, text="x"),
                         reason="removed", fixed_by_plan=True)
    other = risk.model_copy(update={"id": "b" * 12, "fixed_by_plan": False})
    data = {
        "schema_version": 1, "plan_id": "f" * 32, "created_at": "2026-11-01T00:00:00+00:00",
        "target_python": "3.12", "source_manifest": "requirements.txt",
        "dependencies": [{"name": "pandas", "from_version": "1.5.3", "to_version": "2.2.3"}],
        "changes": [{"path": "app.py", "old_sha256": sha256(OLD), "new_sha256": sha256(NEW),
                     "old_source": OLD, "new_source": NEW, "sites": 1,
                     "recipes": ["template:$S.append($O)→pd.concat([$S, $O])"],
                     "claims": {"template:$S.append($O)→pd.concat([$S, $O])": "verified-rule"},
                     "runs_on_base": None}],
        "risks": json.loads(PredictionReport(risks=[risk, other]).model_dump_json(
            by_alias=True)),
    }
    for item in data["risks"]["risks"]:
        item.pop("step_id")
    data["risks"]["schema"] = "pymolt.risks/1"
    # A schema-1 plan is a file of the 0.1/0.2 layout: read while there is no pointer.
    (tmp_path / ".pymolt").mkdir(exist_ok=True)
    (tmp_path / DEFAULT_PLAN_PATH).write_text(json.dumps(data), encoding="utf-8")
    return data


def test_a_v1_plan_is_read_as_the_one_step_v1(tmp_path):
    _v1_json(tmp_path)
    plan = MigrationPlan.load(tmp_path)
    assert plan is not None
    assert plan.schema_version == PLAN_SCHEMA_VERSION and plan.adapted_from == 1
    # Its identity is filled as a schema-2 plan's.
    assert plan.name == "20261101T0000Z-ffffffff" and plan.lineage.root == "f" * 32
    assert [s.id for s in plan.steps] == ["v1"]
    step = plan.steps[0]
    assert (step.type, step.executor, step.claim, step.basis) == (
        "codemod", "pymolt", "preserve", "verified-rule")
    assert step.do.recipe.kind == "whole-file" and not step.do.recipe.rerunnable
    assert (step.package, step.from_, step.to) == ("pandas", "1.5.3", "2.2.3")
    (edit,) = step.edits
    assert len(edit.hunks) == 1 and edit.hunks[0].old_start == 1
    assert apply_hunks(OLD, edit.hunks) == NEW
    assert (edit.before_sha256, edit.after_sha256) == (sha256(OLD), sha256(NEW))
    assert [a.stmt for a in edit.anchors_old] == ["return s1.append(s2)"]
    assert [a.stmt for a in edit.anchors_new] == ["return pd.concat([s1, s2])"]
    # Its risk ids stay as saved; the fixed one links to v1.
    assert plan.risks.schema_ == "pymolt.risks/1"
    assert [(r.id, r.step_id) for r in plan.risks.risks] == [("a" * 12, "v1"),
                                                             ("b" * 12, None)]
    # The adapter is deterministic: a frozen copy reads back equal.
    assert MigrationPlan.load(tmp_path).model_dump() == plan.model_dump()
    assert inspect_migration_plan(tmp_path, plan, check_environment=False).valid is False


def test_a_v1_plan_with_mixed_claims_names_no_single_basis(tmp_path):
    data = _v1_json(tmp_path)
    data["changes"][0]["claims"]["rename-call:a→b"] = "same-object"
    (tmp_path / DEFAULT_PLAN_PATH).write_text(json.dumps(data), encoding="utf-8")
    step = MigrationPlan.load(tmp_path).steps[0]
    assert step.claim == "preserve" and step.basis is None
    assert any("different claims" in e for e in step.why.evidence)

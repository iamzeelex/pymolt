"""Type relocations: a baseline type whose public spelling names another class on the new side,
with same-object evidence from both interpreters, never a name guess."""

from __future__ import annotations

from types import SimpleNamespace

from pymolt.delta import engine as engine_mod
from pymolt.delta.engine import compute_type_relocations, relocation_candidates
from pymolt.verify.predict import collect_type_relocations


def _cls(defined_as):
    return {"exists": True, "steps": [{"kind": "class", "defined_as": defined_as}]}


def test_candidates_are_the_own_path_then_each_enclosing_package():
    assert relocation_candidates("sqlalchemy.engine.result.ResultProxy") == [
        "sqlalchemy.engine.result.ResultProxy", "sqlalchemy.engine.ResultProxy",
        "sqlalchemy.ResultProxy"]
    assert relocation_candidates("pkg.Err") == ["pkg.Err"]


def test_public_spelling_naming_another_class_is_a_relocation():
    home = "pydantic.error_wrappers.ValidationError"
    old = {home: _cls(home), "pydantic.ValidationError": _cls(home)}
    new = {"pydantic.error_wrappers.ValidationError": {"exists": False, "steps": []},
           "pydantic.ValidationError": _cls("pydantic_core._pydantic_core.ValidationError")}
    assert compute_type_relocations(old, new, ["pydantic.error_wrappers.ValidationError"]) == [
        {"old": "pydantic.error_wrappers.ValidationError",
         "new": "pydantic_core._pydantic_core.ValidationError", "via": "pydantic.ValidationError"}]


def test_a_spelling_that_was_not_the_same_object_before_is_not_evidence():
    # in the old interpreter the top-level name was a different class: no same-object link
    old = {"pkg.sub.Err": _cls("pkg.sub.Err"), "pkg.Err": _cls("pkg.other.Err")}
    new = {"pkg.sub.Err": {"exists": False, "steps": []}, "pkg.Err": _cls("pkg.new.Err")}
    assert compute_type_relocations(old, new, ["pkg.sub.Err"]) == []


def test_a_type_that_still_resolves_to_itself_is_not_relocated():
    old = {"pkg.sub.Err": _cls("pkg.sub.Err")}
    new = {"pkg.sub.Err": _cls("pkg.sub.Err")}
    assert compute_type_relocations(old, new, ["pkg.sub.Err"]) == []


def test_an_alias_that_is_gone_is_no_relocation():
    old = {"pkg.sub.RowProxy": _cls("pkg.sub.RowProxy"), "pkg.RowProxy": _cls("pkg.sub.RowProxy")}
    new = {"pkg.sub.RowProxy": {"exists": False, "steps": []},
           "pkg.RowProxy": {"exists": False, "steps": []}}
    assert compute_type_relocations(old, new, ["pkg.sub.RowProxy"]) == []


def test_baseline_type_names_come_from_raises_and_markers(monkeypatch, tmp_path):
    def outcome(kind, value):
        return SimpleNamespace(kind=kind, value=value)

    facts = SimpleNamespace(facts=[
        SimpleNamespace(outcomes=[outcome("raise", "pkg.errors.Boom"),
                                  outcome("return", {"__object__": "pkg.core.Thing",
                                                     "attrs": {"x": {"__type__": "pkg.core.Sub"}}}),
                                  outcome("return", {"__opaque__": "dict"}),
                                  outcome("return", {"__opaque__": "some thing"})])])
    monkeypatch.setattr("pymolt.verify.facts.load_facts", lambda *a, **k: facts)
    trace = tmp_path / "t.jsonl"
    trace.write_text("")
    assert engine_mod.baseline_type_names(tmp_path, trace) == [
        "pkg.core.Sub", "pkg.core.Thing", "pkg.errors.Boom"]


def test_plan_collects_the_reports_relocations_once():
    item = {"old": "a.B", "new": "c.B", "via": "a.B"}
    reports = [SimpleNamespace(type_relocations=[item]),
               {"type_relocations": [dict(item), {"old": "a.X", "new": "c.X", "via": "a.X"}]}]
    assert collect_type_relocations(reports) == [item, {"old": "a.X", "new": "c.X", "via": "a.X"}]


def test_the_plan_json_carries_the_field_additively():
    from pymolt.migration_plan import MigrationPlan

    plan = MigrationPlan(target_python="3.12", source_manifest="requirements.txt")
    assert plan.model_dump()["type_relocations"] == []
    # a plan written before the field existed still loads, with no relocations
    older = plan.model_dump(mode="json")
    del older["type_relocations"]
    assert MigrationPlan.model_validate(older).type_relocations == []
    item = {"old": "a.B", "new": "c.B", "via": "a.B"}
    again = MigrationPlan.model_validate(
        MigrationPlan(target_python="3.12", source_manifest="r.txt",
                      type_relocations=[item]).model_dump())
    assert again.type_relocations == [item]

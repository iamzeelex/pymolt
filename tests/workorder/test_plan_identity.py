"""Plan schema 3: the plan's identity (name, label, version, source, baseline, lineage, content
digest), how a schema-2 plan is read into it, the content digest, and the stored form that
approvals hash and frozen copies keep."""
from __future__ import annotations

import json

import pytest
from _stepwise import APP, RECIPES, plan_for, project
from pydantic import ValidationError

from pymolt.machine_paths import PathScrubber
from pymolt.migration_plan import (
    DIGEST_EXCLUDED_FIELDS,
    NAME_RE,
    PLAN_SCHEMA_VERSION,
    CaptureRef,
    MigrationPlan,
    PlanLineage,
    PlanSource,
    content_digest_of,
    create_migration_plan,
    inspect_migration_plan,
    name_of,
    name_time_known,
    plan_name,
)
from pymolt.state_io import StateFileError
from pymolt.verify import service
from pymolt.verify.models import capture_id
from pymolt.verify.predict import DeltaSummary, PredictionReport
from pymolt.workorder.approval import plan_digest
from tests.fixtures.v02_loader import expected, legacy_plan_digest, materialize

LEGACY = ".pymolt/migration_plan.json"
M = "app/m.py"

#: Every field ``content_digest_of`` hashes: with ``DIGEST_EXCLUDED_FIELDS``, the plan's fields.
CONTENT_FIELDS = {
    "target_python", "source_manifest", "target_manifest_path", "dependencies", "impacts",
    "files_scanned", "changes", "advisories", "manual", "risks", "type_relocations", "python",
    "compile_findings", "steps",
}


def _identity(plan_id: str = "ab" * 16) -> dict:
    return {
        "plan_id": plan_id, "created_at": "2026-11-20T06:31:09+00:00",
        "name": f"20261120T0631Z-{plan_id[:8]}-numpy2", "label": "numpy2",
        "pymolt_version": "0.3.0", "content_digest": "c" * 64,
        "source": PlanSource(git_commit="1" * 40, git_dirty=True, git_prefix="sub",
                             quality="git"),
        "baseline": CaptureRef(capture_id="20261120T060620Z-b3e1a2c0",
                               captured_at="2026-11-20T06:06:20+00:00",
                               trace_sha256="b3e1a2c0" + "0" * 56, env_fingerprint="e",
                               env_setup_fingerprint="s", command=["pytest", "-q"]),
        "lineage": PlanLineage(root="cd" * 16, parent="ef" * 16, generation=2),
    }


def _v2_file(root, **changes) -> dict:
    """A schema-2 plan file as pymolt 0.2 wrote it (``model_dump`` of its model)."""
    data = {
        "schema_version": 2, "plan_id": "5" * 32, "created_at": "2026-11-20T06:31:59+00:00",
        "target_python": "3.12", "source_manifest": "requirements.txt",
        "baseline_trace_path": str(root / ".pymolt" / "contract_traces" / "baseline.jsonl"),
        "baseline_captured_at": "2026-11-20T06:06:20+00:00",
        "baseline_trace_sha256": "b3e1a2c0" + "1" * 56, "steps": [], "adapted_from": None,
        **changes,
    }
    path = root / LEGACY
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data


# ── the model ────────────────────────────────────────────────────────────────────────────


def test_a_schema_3_plan_round_trips_with_its_identity(tmp_path):
    plan = MigrationPlan(target_python="3.12", source_manifest="requirements.txt",
                         **_identity())
    path = plan.save(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == PLAN_SCHEMA_VERSION == 3
    assert raw["source"]["quality"] == "git" and raw["lineage"]["generation"] == 2
    assert raw["baseline"]["capture_id"] == "20261120T060620Z-b3e1a2c0"

    loaded = MigrationPlan.load(tmp_path)
    assert loaded == plan and loaded.adapted_from is None
    assert loaded.model_dump(mode="json") == plan.model_dump(mode="json")
    # The stored form is the file as read; a plan made in memory has its model_dump.
    assert loaded.stored_form() == raw and plan.stored_form() == plan.model_dump(mode="json")


def test_the_name_is_time_id8_and_label():
    assert plan_name("2026-11-20T06:31:59+00:00", "f74941c5" + "0" * 24) == \
        "20261120T0631Z-f74941c5"
    assert plan_name("2026-11-20T08:31:59+02:00", "f74941c5" + "0" * 24, "numpy2") == \
        "20261120T0631Z-f74941c5-numpy2"
    assert NAME_RE.match("20261120T0631Z-f74941c5-numpy2.0_x-y")
    for bad in ("20261120T0631Z-f74941c5-", "20261120T0631Z-F74941C5", "20261120T0631-f74941c5",
                "20261120T0631Z-f74941c5-Numpy", "20261120T0631Z-f74941c5-a/b",
                "20261120T0631Z-f74941c5-" + "a" * 33, "../20261120T0631Z-f74941c5"):
        assert not NAME_RE.match(bad), bad
    with pytest.raises(ValueError):
        plan_name("not a time", "f" * 32)
    undated = MigrationPlan(target_python="3.12", source_manifest="r.txt", plan_id="f" * 32,
                            created_at="not a time", label="x")
    assert name_of(undated) == "00000000T0000Z-ffffffff-x"
    assert name_time_known("20261120T0631Z-f74941c5")
    assert not name_time_known(name_of(undated)) and not name_time_known(None)
    with pytest.raises(ValueError, match="label"):
        plan_name("2026-11-20T06:31:59+00:00", "f" * 32, "NumPy")


@pytest.mark.parametrize("changes", [
    {"name": "20261120T0631Z-00000000"},                    # another plan's id8
    {"name": "20261120T0631Z-abababab"},                    # the label is missing
    {"name": "../etc", "label": None},
    {"label": "Bad Label", "name": None},
    {"content_digest": "xyz"},
    {"lineage": {"root": "not an id"}},
    {"lineage": {"root": "a" * 32, "generation": -1}},
    {"source": {"git_commit": "1" * 40}},                   # no quality
])
def test_an_invalid_identity_makes_the_plan_damaged(tmp_path, changes):
    data = {**MigrationPlan(target_python="3.12", source_manifest="r.txt",
                            **_identity()).model_dump(mode="json"), **changes}
    with pytest.raises(ValidationError):
        MigrationPlan.model_validate(data)
    (tmp_path / ".pymolt").mkdir()
    (tmp_path / LEGACY).write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateFileError) as caught:
        MigrationPlan.load(tmp_path)
    assert caught.value.problem == "damaged"


def test_unknown_keys_still_load_as_in_02(tmp_path):
    _v2_file(tmp_path, retired_key={"x": 1})
    assert MigrationPlan.load(tmp_path) is not None


def test_a_schema_4_plan_is_refused_as_newer(tmp_path):
    _v2_file(tmp_path, schema_version=4)
    with pytest.raises(StateFileError) as caught:
        MigrationPlan.load(tmp_path)
    assert caught.value.problem == "newer"
    assert "schema 4; this pymolt reads up to 3" in caught.value.detail


# ── reading schema 2 ─────────────────────────────────────────────────────────────────────


def test_a_schema_2_plan_is_read_with_its_identity_filled(tmp_path):
    data = _v2_file(tmp_path)
    plan = MigrationPlan.load(tmp_path)
    assert plan.schema_version == 3 and plan.adapted_from == 2
    assert plan.name == "20261120T0631Z-55555555" and plan.label is None
    assert plan.lineage == PlanLineage(root="5" * 32, parent=None, generation=0)
    assert plan.baseline == CaptureRef(
        capture_id="20261120T060620Z-b3e1a2c0", captured_at=data["baseline_captured_at"],
        trace_sha256=data["baseline_trace_sha256"])
    assert (plan.pymolt_version, plan.source, plan.content_digest) == (None, None, None)
    # The stored form is the schema-2 file, untouched by the adapter.
    assert plan.stored_form() == data
    # Deterministic: read twice, the same model (``_check_frozen`` compares model_dumps).
    assert MigrationPlan.load(tmp_path).model_dump() == plan.model_dump()
    assert inspect_migration_plan(tmp_path, plan, check_environment=False).valid


def test_a_schema_2_plan_without_a_baseline_has_none(tmp_path):
    _v2_file(tmp_path, baseline_trace_path=None, baseline_captured_at=None,
             baseline_trace_sha256=None)
    assert MigrationPlan.load(tmp_path).baseline is None


def test_a_created_at_that_is_not_a_time_names_every_copy_of_the_plan_alike(tmp_path):
    """Its time is unknown, and said so: the plan file and its run's frozen copy get the same
    name whatever their mtimes, so apply's frozen check holds on an open 0.2 run."""
    import os
    import shutil

    from pymolt.workorder.apply import _check_frozen

    data = _v2_file(tmp_path, created_at="yesterday")
    frozen = tmp_path / ".pymolt" / "migrations" / data["plan_id"] / "plan.json"
    frozen.parent.mkdir(parents=True)
    shutil.copy(tmp_path / LEGACY, frozen)
    os.utime(tmp_path / LEGACY, (1_700_000_000, 1_700_000_000))
    os.utime(frozen, (1_763_620_260, 1_763_620_260))

    plan = MigrationPlan.load(tmp_path)
    copy = MigrationPlan.load(tmp_path, frozen)

    assert plan.name == copy.name == "00000000T0000Z-55555555"
    assert NAME_RE.match(plan.name) and not name_time_known(plan.name)
    assert copy.model_dump() == plan.model_dump()
    _check_frozen(tmp_path.resolve(), data["plan_id"], plan)        # no StepRefusal


def test_a_frozen_copy_of_a_schema_1_plan_keeps_adapted_from_1(tmp_path):
    _v2_file(tmp_path, adapted_from=1)
    assert MigrationPlan.load(tmp_path).adapted_from == 1


# ── the content digest ───────────────────────────────────────────────────────────────────


def test_every_field_is_either_content_or_excluded_from_the_digest():
    fields = set(MigrationPlan.model_fields)
    assert not CONTENT_FIELDS & DIGEST_EXCLUDED_FIELDS
    assert fields == CONTENT_FIELDS | DIGEST_EXCLUDED_FIELDS, (
        "a new MigrationPlan field: say whether it is content (CONTENT_FIELDS) or identity, "
        "provenance or an input binding (DIGEST_EXCLUDED_FIELDS)")


def _with_risks(tmp_path, *, notes: list[str], delta_notes: list[str], sides: dict):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)}, save=False)
    plan.risks = PredictionReport(status="partial", notes=notes, deltas=[DeltaSummary(
        package="a", from_version="1.0", to_version="2.0", quality="exact",
        notes=delta_notes)])
    plan.python = {"from": "3.11", "to": "3.12", "quality": "exact", "sides": sides,
                   "notes": notes}
    return plan


def test_the_digest_ignores_identity_provenance_bindings_and_cache_text(tmp_path):
    one = _with_risks(tmp_path, notes=["old side read statically from a.whl"],
                      delta_notes=["probed"], sides={"old": "/x/3.11"})
    two = MigrationPlan.model_validate({
        **one.model_dump(), **_identity("9" * 32), "schema_version": 3, "adapted_from": 2,
        "baseline_trace_path": "/elsewhere", "baseline_captured_at": "2027-01-01T00:00:00Z",
        "baseline_trace_sha256": "f" * 64, "environment_fingerprint": "other",
        "environment_setup_fingerprint": "other", "target_manifest_sha256": "0" * 64})
    two.risks.notes = ["old side read from the cache"]
    two.risks.deltas[0].notes = []
    two.python = {**two.python, "sides": {"old": "/y/3.11"}, "notes": []}
    assert content_digest_of(one) == content_digest_of(two)
    # A plan and the same plan read back hash alike.
    one.save(tmp_path)
    assert content_digest_of(MigrationPlan.load(tmp_path)) == content_digest_of(one)


def test_the_digest_changes_with_the_content(tmp_path):
    plan = _with_risks(tmp_path, notes=[], delta_notes=[], sides={})
    digest = content_digest_of(plan)
    for change in (
        lambda p: setattr(p.steps[-1], "title", p.steps[-1].title + "!"),
        lambda p: setattr(p, "target_python", "3.13"),
        lambda p: setattr(p.risks, "status", "complete"),
        lambda p: p.python.update({"quality": "inconclusive"}),
        lambda p: setattr(p, "steps", p.steps[:-1]),
    ):
        other = plan.model_copy(deep=True)
        change(other)
        assert content_digest_of(other) != digest


# ── the stored form: approvals and frozen copies ─────────────────────────────────────────


def test_the_02_fixtures_digest_is_02s(tmp_path):
    exp = expected()
    root = materialize(tmp_path / "p", relocate=False)
    plan = MigrationPlan.load(root)
    frozen = MigrationPlan.load(root, root / exp["run"]["frozen_plan"])
    assert plan.adapted_from == 2 and plan.name.endswith(exp["plan"]["plan_id"][:8])
    assert plan_digest(plan) == plan_digest(frozen) == exp["plan"]["plan_digest"]
    # Relocated, the file changes (its paths and the trace's hash): 0.2's formula over it.
    moved = materialize(tmp_path / "q")
    assert plan_digest(MigrationPlan.load(moved)) == legacy_plan_digest(moved / LEGACY) != \
        exp["plan"]["plan_digest"]


def test_freeze_writes_the_stored_form(tmp_path):
    exp = expected()
    root = materialize(tmp_path / "p")
    plan = MigrationPlan.load(root)
    copy = tmp_path / "copy.json"
    plan.freeze(copy)
    assert json.loads(copy.read_text()) == json.loads((root / LEGACY).read_text())
    assert legacy_plan_digest(copy) == legacy_plan_digest(root / LEGACY)
    assert json.loads(copy.read_text())["schema_version"] == 2
    # Its frozen copy reads as the same model, as apply's frozen check needs.
    frozen = MigrationPlan.load(root, root / exp["run"]["frozen_plan"])
    assert MigrationPlan.load(root, copy).model_dump() == plan.model_dump() == \
        frozen.model_dump()
    # A plan made in memory freezes as its model_dump.
    fresh = MigrationPlan(target_python="3.12", source_manifest="r.txt", **_identity())
    fresh.freeze(tmp_path / "fresh.json")
    assert json.loads((tmp_path / "fresh.json").read_text()) == fresh.model_dump(mode="json")


def test_a_schema_1_plans_digest_is_of_the_stored_schema_1_file(tmp_path):
    from test_plan_schema import _v1_json

    data = _v1_json(tmp_path)
    plan = MigrationPlan.load(tmp_path)
    assert plan.stored_form() == data and plan.steps
    adapted = MigrationPlan.model_validate(plan.model_dump())
    # 0.2 hashed the adapted plan: an approval 0.2 gave a schema-1 plan does not hold.
    assert plan_digest(plan) != plan_digest(adapted)


# ── plan time: the baseline binding ──────────────────────────────────────────────────────


def test_a_plan_binds_its_baseline_as_a_capture_ref(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)}, save=False)
    slot = service.load_contract_state(tmp_path).baseline
    assert plan.baseline == CaptureRef(
        capture_id=slot.capture_id, captured_at=slot.captured_at,
        trace_sha256=plan.baseline_trace_sha256, env_fingerprint=slot.env_fingerprint,
        env_setup_fingerprint=slot.env_setup_fingerprint,
        command=PathScrubber(tmp_path)(list(slot.command)))
    # The capture's command names no folder of the machine: its script is the project's.
    assert plan.baseline.command[-1] == "baseline_run.py"
    assert plan.baseline.capture_id == capture_id(slot.captured_at, slot.trace_sha256)
    assert plan.baseline_warning is None


def test_a_recording_changed_since_its_capture_is_bound_as_it_is_and_warned(tmp_path):
    project(tmp_path)
    slot = service.load_contract_state(tmp_path).baseline
    with open(slot.trace_path, "a", encoding="utf-8") as fh:
        fh.write("\n")
    plan = create_migration_plan(tmp_path, [], [], target_python="3.12",
                                 source_manifest="requirements.txt",
                                 target_manifest_path=tmp_path / "requirements-target.txt",
                                 files_scanned=0, predict=False)
    assert plan.baseline.trace_sha256 == plan.baseline_trace_sha256 != slot.trace_sha256
    assert plan.baseline.capture_id != slot.capture_id
    assert "changed since its capture" in plan.baseline_warning
    assert slot.capture_id in plan.baseline_warning
    # The warning is about this plan run: it is never saved.
    assert "baseline_warning" not in json.dumps(plan.model_dump(mode="json"))

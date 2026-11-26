"""The 0.2 project fixture (``v02_loader``): it is text without private data, its recorded
digest is 0.2's formula over the stored plan, it lands consistent wherever it is put, and this
pymolt reads its state."""
from __future__ import annotations

import gzip
import hashlib
import json
import re

import pytest

from tests.fixtures.v02_loader import (
    ENVS_MARK,
    ROOT_MARK,
    TREE,
    expected,
    legacy_plan_digest,
    materialize,
    stored_files,
    stored_path,
)
from tests.test_no_private_data import leaks

PLAN = ".pymolt/migration_plan.json"
TRACE = ".pymolt/contract_traces/baseline.jsonl"


def _texts(root):
    """Every file under ``root`` as text, ``.gz`` ones decompressed."""
    out = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        data = path.read_bytes()
        out[path.relative_to(root).as_posix()] = (
            gzip.decompress(data) if path.suffix == ".gz" else data).decode("utf-8")
    return out


def test_the_fixture_is_text_without_machine_paths():
    found = []
    for stored, _project in stored_files():
        text = (TREE / stored).read_text(encoding="utf-8")
        for n, line in enumerate(text.splitlines(), 1):
            found += [f"{stored}:{n}: {what}" for what in leaks(line)]
            if re.search(r"/Users/|/home/|/private/|/var/folders/", line):
                found.append(f"{stored}:{n}: an absolute machine path")
    assert not found, "\n".join(found[:20])


def test_it_is_a_02_project_with_an_open_run():
    exp = expected()
    names = {project for _stored, project in stored_files()}
    assert {PLAN, ".pymolt/PLAN.md", ".pymolt/.gitignore", ".pymolt/contract_state.json",
            ".pymolt/migration_state.json", ".pymolt/apply_ledger.json", TRACE,
            exp["run"]["frozen_plan"]} <= names
    assert not any(n.startswith((".pymolt/plans/", ".pymolt/cache/")) for n in names)
    plan = json.loads((TREE / stored_path(PLAN)).read_text(encoding="utf-8"))
    assert plan["schema_version"] == 2 == exp["plan"]["schema_version"]
    assert plan["plan_id"] == exp["plan"]["plan_id"] == exp["run"]["run_id"]
    assert exp["made_by"]["pymolt_version"].startswith("0.2")
    receipt = json.loads((TREE / stored_path(".pymolt/migration_state.json")).read_text())
    [run] = receipt["runs"]
    assert run["closed"] is False and exp["run"]["applied"] == ["env-1"]
    assert (TREE / stored_path(".pymolt/PLAN.md")).read_text().startswith(
        f"# Migration plan {plan['plan_id'][:8]}")


def test_the_recorded_plan_digest_is_02s_formula_over_the_stored_plan():
    exp = expected()
    digest = exp["plan"]["plan_digest"]
    assert legacy_plan_digest(TREE / stored_path(PLAN)) == digest
    assert legacy_plan_digest(TREE / stored_path(exp["run"]["frozen_plan"])) == digest


def test_as_stored_it_lands_byte_for_byte(tmp_path):
    root = materialize(tmp_path / "p", relocate=False)
    for stored, project in stored_files():
        data = (root / project).read_bytes()
        if project.endswith(".gz"):
            data = gzip.decompress(data)
        assert data == (TREE / stored).read_bytes(), project
    assert legacy_plan_digest(root / PLAN) == expected()["plan"]["plan_digest"]


def test_relocated_it_is_consistent_where_it_lands(tmp_path):
    envs = tmp_path / "envs"
    root = materialize(tmp_path / "p", envs=envs)
    texts = _texts(root)
    assert not [n for n, t in texts.items() if ROOT_MARK in t or ENVS_MARK in t]
    plan = json.loads(texts[PLAN])
    frozen = json.loads(texts[expected()["run"]["frozen_plan"]])
    assert plan == frozen
    assert plan["baseline_trace_path"] == str(root / TRACE)
    sha = hashlib.sha256((root / TRACE).read_bytes()).hexdigest()
    assert plan["baseline_trace_sha256"] == sha != expected()["baseline"]["trace_sha256"]
    assert f"Z-{sha[:8]})" in texts[".pymolt/PLAN.md"].splitlines()[1]
    assert json.loads(texts[".pymolt/env_config.json"])["target_env_path"] == \
        str(envs.resolve() / "3.12")
    sources = json.loads(texts[".pymolt/contract_traces/baseline.jsonl.sources.json.gz"])
    assert sources["root"] == str(root)
    for line in texts[TRACE].splitlines():
        assert json.loads(line)["where"]["file"].startswith(str(root) + "/")


def test_this_pymolt_reads_its_state(tmp_path):
    from pymolt.apply_ledger import load_ledger
    from pymolt.migration_plan import MigrationPlan
    from pymolt.migration_state import MigrationReceipt
    from pymolt.status import build_status
    from pymolt.verify.models import CaptureValidity
    from pymolt.verify.service import load_contract_state

    exp = expected()
    root = materialize(tmp_path / "p")
    plan = MigrationPlan.load(root)
    assert plan is not None and plan.plan_id == exp["plan"]["plan_id"]
    assert [s.id for s in plan.steps] == exp["plan"]["steps"]
    assert MigrationPlan.load(root, root / exp["run"]["frozen_plan"]).plan_id == plan.plan_id
    assert load_contract_state(root).baseline.validity is CaptureValidity.VALID
    receipt = MigrationReceipt.load(root)
    assert [(r.run_id, r.closed) for r in receipt.runs] == [(exp["run"]["run_id"], False)]
    assert len(load_ledger(root).events) == 1
    assert build_status(root).problems == []


def test_it_lands_only_in_an_empty_folder(tmp_path):
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / "x").write_text("")
    with pytest.raises(FileExistsError):
        materialize(tmp_path / "p")

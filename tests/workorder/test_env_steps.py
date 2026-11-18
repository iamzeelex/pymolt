"""The env phase (design 1.1, decision 1): the manifest pin pymolt writes, the lines it leaves
to a person, and the target environment a person builds."""

from __future__ import annotations

from pymolt.codemods.client import DependencyMigration
from pymolt.migration_plan import create_migration_plan
from pymolt.workorder.edits import apply_hunks, sha256
from pymolt.workorder.env import env_steps, manifest_pins, target_pins

SOURCE = ("pandas==1.5.3\n"
          "requests[socks]==2.0.0 ; python_version >= '3'  # web\n"
          "numpy>=1.20\n"
          "flask\n"
          "-r other.txt\n"
          "# a comment naming pandas==1.0\n"
          "hashed==1.0 \\\n"
          "    --hash=sha256:abc\n"
          "same==1.0\n"
          "absent==0.1\n")
TARGET = ("pandas==2.2.3 \\\n    --hash=sha256:x\n"
          "requests==2.32.3\nnumpy==2.0.0\nflask==3.0.0\nhashed==2.0\nsame==1.0\n")


def _project(tmp_path, source=SOURCE, name="requirements.txt"):
    (tmp_path / name).write_text(source, encoding="utf-8")
    (tmp_path / "requirements-target.txt").write_text(TARGET, encoding="utf-8")
    return tmp_path


def test_the_target_pins_are_read_through_hashes():
    assert target_pins(TARGET) == {"pandas": "2.2.3", "requests": "2.32.3", "numpy": "2.0.0",
                                   "flask": "3.0.0", "hashed": "2.0", "same": "1.0"}


def test_pymolt_pins_plain_lines_and_leaves_the_rest_to_a_person():
    rewrite, other = manifest_pins(SOURCE, target_pins(TARGET))
    assert [(p.name, p.from_version, p.to_version, p.line) for p in rewrite] == [
        ("pandas", "1.5.3", "2.2.3", 1), ("requests", "2.0.0", "2.32.3", 2)]
    assert rewrite[1].new == "requests[socks]==2.32.3 ; python_version >= '3'  # web\n"
    assert other == [(3, "numpy>=1.20", "numpy==2.0.0"), (4, "flask", "flask==3.0.0"),
                     (7, "hashed==1.0 \\", "hashed==2.0")]


def test_the_env_steps(tmp_path):
    root = _project(tmp_path)
    pin, person, build = env_steps(root, source_manifest="requirements.txt",
                                   target_manifest="requirements-target.txt",
                                   target_python="3.12", target_env=".venv-target")
    assert [(s.id, s.type, s.executor, s.claim, s.basis) for s in (pin, person, build)] == [
        ("env-1", "manifest-pin", "pymolt", "intended", "target-manifest"),
        ("env-2", "manifest-pin", "person", "intended", "target-manifest"),
        ("env-3", "target-env", "person", None, None)]
    (edit,) = pin.edits
    after = apply_hunks(SOURCE, edit.hunks)
    assert (edit.before_sha256, edit.after_sha256) == (sha256(SOURCE), sha256(after))
    assert after.startswith("pandas==2.2.3\nrequests[socks]==2.32.3 ;")
    assert after.splitlines()[2:] == SOURCE.splitlines()[2:]
    assert edit.anchors_old == [] and len(edit.effect.replace) == 2
    assert pin.do.recipe.kind == "manifest-pin" and pin.do.recipe.keys == [
        "manifest-pin:pandas==1.5.3→2.2.3", "manifest-pin:requests==2.0.0→2.32.3"]
    assert person.do.manual.lines == ["line 3: `numpy>=1.20` → `numpy==2.0.0`",
                                      "line 4: `flask` → `flask==3.0.0`",
                                      "line 7: `hashed==1.0 \\` → `hashed==2.0`"]
    assert build.do.manual.commands[0] == "uv venv .venv-target --python 3.12"
    assert any(".venv-target" in e for e in build.why.evidence)
    assert all(s.verify.judge == "none" and not s.depends_on for s in (pin, person, build))


def test_nothing_to_pin_gives_only_the_target_environment(tmp_path):
    root = _project(tmp_path, "same==1.0\nabsent==0.1\n")
    steps = env_steps(root, source_manifest="requirements.txt",
                      target_manifest="requirements-target.txt", target_python="3.12")
    assert [s.type for s in steps] == ["target-env"]


def test_another_manifest_kind_is_a_person_step_naming_the_moves(tmp_path):
    root = _project(tmp_path, "[project]\ndependencies = ['pandas==1.5.3']\n", "pyproject.toml")
    (person, _build) = env_steps(
        root, source_manifest="pyproject.toml", target_manifest="requirements-target.txt",
        target_python="3.12", dependencies=[DependencyMigration("pandas", "1.5.3", "2.2.3")])
    assert (person.executor, person.do.manual.lines) == (
        "person", ["`pandas 1.5.3` → `pandas==2.2.3`"])


def test_the_plan_starts_with_the_env_phase_and_its_changes_stay_python(tmp_path):
    root = _project(tmp_path)
    plan = create_migration_plan(root, [], [], target_python="3.12",
                                 source_manifest="requirements.txt",
                                 target_manifest_path=root / "requirements-target.txt",
                                 files_scanned=0, predict=False)
    assert [s.id for s in plan.steps] == ["env-1", "env-2", "env-3"]
    assert plan.changes == []
    assert [s.effort.size for s in plan.steps] == ["none", "small", "small"]

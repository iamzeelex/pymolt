from pymolt.discovery.discover import build_surface_map
from pymolt.discovery.tox_nox import parse_noxfile, parse_tox_ini


def test_tox_envlist_versions_and_factors(tmp_path):
    (tmp_path / "tox.ini").write_text(
        "[tox]\nenvlist = py36, py38, py311-django{32,42}, pypy39, lint\n\n"
        "[testenv]\ndeps =\n    pytest\n    py36: importlib_resources\n",
        encoding="utf-8",
    )
    s = parse_tox_ini(tmp_path / "tox.ini")
    assert s.declared_versions == ["3.6", "3.8", "3.11"]
    assert "pytest" in s.extra_deps
    assert "importlib_resources" in s.extra_deps  # factor guard stripped


def test_noxfile_versions(tmp_path):
    (tmp_path / "noxfile.py").write_text(
        "import nox\n@nox.session(python=['3.9', '3.12'])\ndef tests(session):\n    pass\n",
        encoding="utf-8",
    )
    s = parse_noxfile(tmp_path / "noxfile.py")
    assert s.declared_versions == ["3.9", "3.12"]


def test_monorepo_multiple_roots_and_divergence(tmp_path):
    # service-a: pyproject says >=3.11, Dockerfile uses 3.11 -> consistent
    a = tmp_path / "services" / "a"
    a.mkdir(parents=True)
    (a / "pyproject.toml").write_text(
        "[project]\nname='a'\nrequires-python='>=3.11'\ndependencies=[]\n", encoding="utf-8")
    (a / "Dockerfile").write_text("FROM python:3.11-slim\nRUN pip install .\n", encoding="utf-8")

    # service-b: pyproject says >=3.8 but Dockerfile is 3.6 -> DIVERGENT
    b = tmp_path / "services" / "b"
    b.mkdir(parents=True)
    (b / "requirements.txt").write_text("flask\n", encoding="utf-8")
    (b / "Dockerfile").write_text("FROM python:3.6\nRUN pip install -r requirements.txt\n", encoding="utf-8")
    (b / ".python-version").write_text("3.8\n", encoding="utf-8")

    smap = build_surface_map(tmp_path)
    roots = {r.path: r for r in smap.project_roots}
    assert set(roots) == {"services/a", "services/b"}

    assert roots["services/a"].distinct_versions() == ["3.11"]
    # b: .python-version 3.8 vs Dockerfile 3.6 -> divergent
    assert set(roots["services/b"].distinct_versions()) == {"3.8", "3.6"}
    assert any("monorepo" in n for n in smap.notes)


def test_ignored_dirs_are_pruned(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\ndependencies=[]\n", encoding="utf-8")
    venv = tmp_path / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "pyproject.toml").write_text("[project]\nname='dep'\n", encoding="utf-8")

    smap = build_surface_map(tmp_path)
    assert [r.path for r in smap.project_roots] == ["."]  # .venv pruned


def test_dot_directories_are_pruned(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\ndependencies=[]\n", encoding="utf-8")
    # Arbitrary hidden dirs (not just the known ignore-list) must be skipped.
    for hidden in (".research", ".old"):
        d = tmp_path / hidden / "sub"
        d.mkdir(parents=True)
        (d / "requirements.txt").write_text("requests\n", encoding="utf-8")

    smap = build_surface_map(tmp_path)
    assert [r.path for r in smap.project_roots] == ["."]


def test_dockerfile_py_source_is_not_a_dockerfile(tmp_path):
    # A python module named dockerfile.py must NOT be detected as a Dockerfile.
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "dockerfile.py") .write_text("# parser module\n", encoding="utf-8")
    # A real Dockerfile.prod variant should still be picked up.
    (tmp_path / "Dockerfile.prod").write_text("FROM python:3.11\n", encoding="utf-8")

    smap = build_surface_map(tmp_path)
    paths = {r.path for r in smap.project_roots}
    assert "pkg" not in paths      # dockerfile.py did not create a root
    assert "." in paths            # Dockerfile.prod did


def test_tox_matrix_is_a_range_not_divergence(tmp_path):
    # requires-python 3.12 + a wide tox matrix must NOT read as divergent.
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='x'\nrequires-python='>=3.12'\ndependencies=[]\n", encoding="utf-8")
    (tmp_path / "tox.ini").write_text(
        "[tox]\nenvlist = py36, py37, py38, py39, py310, py311\n", encoding="utf-8")

    root = build_surface_map(tmp_path).project_roots[0]
    assert not root.is_divergent                       # tox range is not a conflict
    assert root.distinct_versions() == ["3.12"]        # only the runtime source
    assert root.tested_versions() == ["3.6", "3.7", "3.8", "3.9", "3.10", "3.11"]
    # tox versions are no longer mixed into version_evidence
    assert all("envlist" not in ev.source for ev in root.version_evidence)


def test_requires_python_floor_satisfied_is_not_divergent(tmp_path):
    # requires-python is a *floor*; a runtime above it is consistent, not divergent.
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='x'\nrequires-python='>=3.8'\ndependencies=[]\n", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text("FROM python:3.11-slim\n", encoding="utf-8")

    root = build_surface_map(tmp_path).project_roots[0]
    assert not root.is_divergent
    assert root.runtime_versions() == ["3.11"]
    assert root.floor_violations() == []
    floor = next(e for e in root.version_evidence if e.kind == "floor")
    assert floor.constraint == ">=3.8"


def test_pymolt_generated_manifests_are_not_inputs(tmp_path):
    # The tool's own outputs (requirements-target.txt, or anything carrying the
    # "# Generated by pymolt" header) must not be re-detected as project manifests.
    (tmp_path / "requirements.txt").write_text("flask\n", encoding="utf-8")
    (tmp_path / "requirements-target.txt").write_text("flask==3.0.0\n", encoding="utf-8")
    (tmp_path / "requirements-pinned.txt").write_text(
        "# Generated by pymolt audit for Python 3.12\nflask==3.0.0\n", encoding="utf-8"
    )

    root = build_surface_map(tmp_path).project_roots[0]
    assert root.manifests == ["requirements.txt"]


def test_dir_with_only_generated_manifest_is_not_a_root(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='x'\ndependencies=[]\n", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    (out / "requirements-target.txt").write_text("flask==3.0.0\n", encoding="utf-8")

    smap = build_surface_map(tmp_path)
    assert [r.path for r in smap.project_roots] == ["."]


def test_runtime_below_floor_is_flagged(tmp_path):
    # Dockerfile pins 3.6 but the project declares it needs >=3.8 -> floor violation.
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='x'\nrequires-python='>=3.8'\ndependencies=[]\n", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text("FROM python:3.6\n", encoding="utf-8")

    root = build_surface_map(tmp_path).project_roots[0]
    assert not root.is_divergent                       # one runtime -> no disagreement
    violations = root.floor_violations()
    assert violations and violations[0][0] == "3.6"
    assert ">=3.8" in violations[0][1]

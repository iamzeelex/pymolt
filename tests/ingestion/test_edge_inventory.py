from pymolt.core.enums import EdgeKind
from pymolt.inventory.edges import build_inventory


def _by_name(inv):
    return {e.name: e for e in inv.edges if e.name}


def test_requirements_edges_and_directives(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "\n".join([
            "requests==2.31.0",
            "internal-lib @ git+https://git.example.com/internal-lib.git@v1.2",
            "wheelpkg @ https://example.com/wheelpkg-1.0-py3-none-any.whl",
            "-e .",
            "-e ../sibling",
            "-r dev-requirements.txt",
            "-c constraints.txt",
            "--index-url https://pypi.internal.example.com/simple",
            "--extra-index-url https://pypi.org/simple",
            "--find-links ./wheels",
            "flask[async]>=2.0 ; python_version >= '3.8'",
        ]),
        encoding="utf-8",
    )
    (tmp_path / "dev-requirements.txt").write_text("pytest==8.0.0\n", encoding="utf-8")

    inv = build_inventory(tmp_path)
    by = _by_name(inv)

    assert by["requests"].kind == EdgeKind.PYPI
    assert by["internal-lib"].kind == EdgeKind.VCS
    assert by["wheelpkg"].kind == EdgeKind.URL
    # flask extras + marker preserved
    assert by["flask"].extras == ["async"]
    assert by["flask"].marker is not None

    # editable local edges
    locals_ = [e for e in inv.edges if e.kind == EdgeKind.LOCAL]
    assert any(e.editable for e in locals_)

    # directives captured
    assert "constraints.txt" in inv.constraints
    kinds = {i.kind for i in inv.indexes}
    assert {"index-url", "extra-index-url", "find-links"} <= kinds

    # -r dev-requirements.txt followed; pytest classified into the 'dev' group
    assert by["pytest"].group == "dev"
    assert by["requests"].group == "main"


def test_pyproject_extras_and_groups(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "p"
dependencies = ["requests>=2.0"]

[project.optional-dependencies]
test = ["pytest>=8"]
docs = ["sphinx"]
""",
        encoding="utf-8",
    )
    inv = build_inventory(tmp_path)
    groups = inv.counts_by_group()
    assert groups.get("main") == 1
    assert groups.get("test") == 1
    assert groups.get("docs") == 1


def test_poetry_git_and_path_deps(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.poetry]
name = "p"

[tool.poetry.dependencies]
python = "^3.12"
requests = "^2.31"
internal = { git = "https://git.example.com/internal.git", branch = "main" }
locallib = { path = "../locallib" }

[tool.poetry.group.dev.dependencies]
pytest = "^8.0"
""",
        encoding="utf-8",
    )
    inv = build_inventory(tmp_path)
    by = _by_name(inv)
    assert by["requests"].kind == EdgeKind.PYPI
    assert by["internal"].kind == EdgeKind.VCS
    assert by["locallib"].kind == EdgeKind.LOCAL
    assert by["pytest"].group == "dev"
    assert "python" not in by  # python constraint excluded

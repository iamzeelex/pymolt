from pymolt.verify.contact_map import build_contact_map


def _project(tmp_path):
    app = tmp_path / "myapp"
    app.mkdir()
    (app / "__init__.py").write_text("", encoding="utf-8")
    (app / "views.py").write_text(
        "import os\n"
        "import flask\n"
        "from flask.cli import with_appcontext\n"
        "from .util import helper\n\n"
        "def handler():\n"
        "    helper()\n"          # first-party — not a contact
        "    os.getcwd()\n"       # stdlib — not a contact
        "    return flask.Flask(__name__)\n\n"
        "class V:\n"
        "    def m(self):\n"
        "        return with_appcontext(self)\n",
        encoding="utf-8",
    )
    (app / "util.py").write_text(
        "import requests\n\ndef helper():\n    return requests.get('http://x')\n",
        encoding="utf-8",
    )
    return tmp_path


def test_contact_map_finds_third_party_only(tmp_path):
    cmap = build_contact_map(_project(tmp_path))

    assert cmap.by_dep == {
        "flask": ["flask.Flask", "flask.cli.with_appcontext"],
        "requests": ["requests.get"],
    }
    pairs = {(c.caller, c.target) for c in cmap.contacts}
    assert ("myapp.views.handler", "flask.Flask") in pairs
    assert ("myapp.views.V.m", "flask.cli.with_appcontext") in pairs
    assert ("myapp.util.helper", "requests.get") in pairs

    # stdlib and first-party calls are excluded.
    targets = {c.target for c in cmap.contacts}
    assert not any(t.startswith("os.") for t in targets)
    assert "myapp.util.helper" not in targets


def test_file_attribution_for_methods(tmp_path):
    cmap = build_contact_map(_project(tmp_path))
    by_caller = {c.caller: c.file for c in cmap.contacts}
    assert by_caller["myapp.views.handler"] == "myapp/views.py"
    assert by_caller["myapp.views.V.m"] == "myapp/views.py"  # longest module prefix
    assert by_caller["myapp.util.helper"] == "myapp/util.py"


def test_dependencies_filter(tmp_path):
    # Restrict to a known dependency set -> requests dropped.
    cmap = build_contact_map(_project(tmp_path), dependencies={"flask"})
    assert set(cmap.by_dep) == {"flask"}


def test_empty_project(tmp_path):
    cmap = build_contact_map(tmp_path)
    assert cmap.contacts == []
    assert any("no Python files" in n for n in cmap.notes)


def test_ignores_pymolt_watcher_bundle(tmp_path):
    # An exported watcher bundle (export-watcher) written into the project must not
    # show up as a 'pymolt_trace' dependency contact.
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "sitecustomize.py").write_text(
        "import pymolt_trace.boundary_tracer\n"
        "def go():\n    return pymolt_trace.boundary_tracer.activate()\n",
        encoding="utf-8",
    )
    cmap = build_contact_map(tmp_path)
    assert "pymolt_trace" not in cmap.by_dep
    assert not any(c.dep == "pymolt_trace" for c in cmap.contacts)

from pymolt.verify.contact_map import Contact, build_contact_map


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
        "    helper()\n"  # first-party — not a contact
        "    os.getcwd()\n"  # stdlib — not a contact
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


def test_old_contact_payload_defaults_to_call_kind():
    contact = Contact.model_validate(
        {
            "caller": "app.run",
            "target": "thirdparty.run",
            "dep": "thirdparty",
        }
    )

    assert contact.kind == "call"


def test_contact_map_covers_non_call_and_protocol_boundaries(tmp_path):
    (tmp_path / "local.py").write_text(
        "class LocalBase:\n    pass\nLOCAL_ITEMS = [1]\n",
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text(
        "import os\n"
        "import flask\n"
        "from flask import DEFAULT_CONFIG, current_app\n"
        "from .local import LOCAL_ITEMS, LocalBase\n\n"
        "@flask.cli.with_appcontext\n"
        "class View(flask.views.MethodView, LocalBase):\n"
        "    config = DEFAULT_CONFIG\n"
        "    home = os.environ\n\n"
        "    def run(self):\n"
        "        flask.Flask()\n"
        "        flask.Flask()\n"
        "        value = current_app.config\n"
        "        with flask.app.app_context():\n"
        "            pass\n"
        "        for item in flask.DEFAULT_ITEMS:\n"
        "            value = flask.DEFAULT_ITEMS[0]\n"
        "        local = LOCAL_ITEMS[0]\n"
        "        return value, local\n\n"
        "@flask.Flask\n"
        "def make(value=flask.Flask()):\n"
        "    return value\n",
        encoding="utf-8",
    )

    cmap = build_contact_map(tmp_path)
    contacts = {(c.caller, c.target, c.kind) for c in cmap.contacts}

    assert ("app.View", "flask.cli.with_appcontext", "decorator") in contacts
    assert ("app.View", "flask.views.MethodView", "class-base") in contacts
    assert ("app.View", "flask.DEFAULT_CONFIG", "imported-constant") in contacts
    assert ("app.View.run", "flask.current_app.config", "attribute-read") in contacts
    assert ("app.View.run", "flask.app.app_context", "context-manager") in contacts
    assert ("app.View.run", "flask.DEFAULT_ITEMS.__iter__", "protocol") in contacts
    assert ("app.View.run", "flask.DEFAULT_ITEMS.__getitem__", "protocol") in contacts
    assert ("app.View.run", "flask.Flask", "call") in contacts
    assert ("app.make", "flask.Flask", "decorator") in contacts
    assert ("app.make", "flask.Flask", "call") in contacts

    # Repeated calls collapse, but a symbol used under two kinds remains two
    # distinct contacts. Local and stdlib expressions never enter the map.
    assert (
        sum(
            c.caller == "app.View.run" and c.target == "flask.Flask" and c.kind == "call"
            for c in cmap.contacts
        )
        == 1
    )
    assert all(c.dep not in {"local", "os"} for c in cmap.contacts)
    assert len(contacts) == len(cmap.contacts)


def test_protocol_operations_are_named_when_import_target_is_resolvable(tmp_path):
    (tmp_path / "app.py").write_text(
        "from toolkit import LEFT, RIGHT, WAITABLE\n\n"
        "async def use():\n"
        "    total = LEFT + RIGHT\n"
        "    present = LEFT in RIGHT\n"
        "    size = len(LEFT)\n"
        "    result = await WAITABLE\n"
        "    return total, present, size, result\n",
        encoding="utf-8",
    )

    cmap = build_contact_map(tmp_path)
    targets = {(contact.target, contact.kind) for contact in cmap.contacts}

    assert ("toolkit.LEFT.__add__", "protocol") in targets
    assert ("toolkit.RIGHT.__radd__", "protocol") in targets
    assert ("toolkit.RIGHT.__contains__", "protocol") in targets
    assert ("toolkit.LEFT.__len__", "protocol") in targets
    assert ("toolkit.WAITABLE.__await__", "protocol") in targets

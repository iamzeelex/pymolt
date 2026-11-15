# ruff: noqa: UP031, UP008  -- runs inside the old environment; super(Cls, self) mirrors flasgger's own code
"""Ground truth by USE: exercise flasgger's touched flask/werkzeug symbols the way flasgger
uses them (argument shapes taken from tests/artifacts/flasgger), inside one environment.

Independent of probe.py's introspection: these are real calls, imports and subclassing.
Each scenario records ("ok", <type>) or ("raise", <exception type>) plus any warnings.

Usage: python -I calls_flask.py   (prints JSON)
"""

import inspect
import json
import sys
import warnings


def scenario(results, symbol, fn):
    with warnings.catch_warnings(record=True) as got:
        warnings.simplefilter("always")
        try:
            value = fn()
            outcome = ["ok", type(value).__name__ if value is not None else "None"]
        except BaseException as exc:  # noqa: BLE001 — SystemExit/HTTPException are outcomes too
            outcome = ["raise", type(exc).__name__]
    warns = sorted(
        {
            "%s: %s" % (w.category.__name__, str(w.message)[:160])
            for w in got
            if "flask" in str(w.filename)
            or "werkzeug" in str(w.filename)
            or w.category in (DeprecationWarning, FutureWarning)
        }
    )
    results[symbol] = {"outcome": outcome, "warnings": warns}


def main():
    import flask
    from flask import Blueprint, Flask, jsonify, redirect, render_template, request, url_for
    from jinja2 import DictLoader

    results = {}
    app = Flask("gt")
    app.jinja_loader = DictLoader({"flasgger/index.html": "{{ title }}"})

    def view():
        return "ok"

    app.add_url_rule("/x", "x_ep", view, methods=["GET", "POST"])

    # --- module-level public paths -------------------------------------------------------
    scenario(results, "flask.Markup", lambda: __import__("flask").Markup("<b>%s</b>" % "x"))
    scenario(results, "flask.__version__", lambda: flask.__version__)
    scenario(results, "flask.Flask", lambda: Flask("gt2", static_folder=None))
    scenario(results, "flask.Flask.route", lambda: Flask("gt3").route("/r")(view))
    scenario(results, "flask.Flask.register_blueprint", lambda: _blueprint_app())
    scenario(
        results,
        "flask.Blueprint",
        lambda: Blueprint("bp", "gt", url_prefix="/p", template_folder="templates"),
    )
    scenario(
        results,
        "flask.Blueprint.add_url_rule",
        lambda: Blueprint("bp2", "gt").add_url_rule("/a", "a", view_func=view, methods=["GET"]),
    )
    scenario(results, "flask.Flask.app_context", lambda: app.app_context())
    scenario(
        results, "flask.Response", lambda: flask.Response("x", mimetype="text/plain", status=200)
    )
    scenario(results, "flask.redirect", lambda: redirect("/x"))
    scenario(results, "flask.abort", lambda: flask.abort(404))
    scenario(
        results,
        "werkzeug.exceptions.abort",
        lambda: __import__("werkzeug.exceptions", fromlist=["abort"]).abort(400),
    )
    scenario(results, "flask.json.JSONEncoder", lambda: _json_encoder_subclass())
    scenario(
        results, "flask.json.provider.DefaultJSONProvider", lambda: _json_provider_subclass(app)
    )
    scenario(
        results, "flask.json._json", lambda: __import__("flask.json", fromlist=["_json"])._json
    )
    scenario(results, "flask.views.MethodView", lambda: _method_view(app))
    scenario(
        results,
        "flask.views.View",
        lambda: type(
            "V",
            (__import__("flask.views", fromlist=["View"]).View,),
            {"dispatch_request": lambda self: "ok"},
        ).as_view("vv"),
    )
    scenario(
        results,
        "flask.testing.FlaskClient",
        lambda: setattr(
            app,
            "test_client_class",
            __import__("flask.testing", fromlist=["FlaskClient"]).FlaskClient,
        ),
    )
    scenario(
        results,
        "flask.cli.with_appcontext",
        lambda: __import__("flask.cli", fromlist=["with_appcontext"]).with_appcontext(lambda: None),
    )
    scenario(
        results,
        "werkzeug.middleware.dispatcher.DispatcherMiddleware",
        lambda: __import__("werkzeug.middleware.dispatcher", fromlist=["x"]).DispatcherMiddleware(
            app, {"/p": app}
        ),
    )
    scenario(
        results,
        "werkzeug.wsgi.DispatcherMiddleware",
        lambda: __import__("werkzeug.wsgi", fromlist=["x"]).DispatcherMiddleware(app, {"/p": app}),
    )
    # run_simple would start a server: bind demo_app's exact call shape instead of calling it.
    scenario(
        results,
        "werkzeug.serving.run_simple",
        lambda: inspect.signature(__import__("werkzeug.serving", fromlist=["x"]).run_simple).bind(
            "0.0.0.0", 5000, app, use_reloader=False, use_debugger=False
        ),
    )

    # --- inside a request, the way flasgger's views use them ------------------------------
    with app.test_request_context(
        "/x?a=1", method="POST", json={"message": "hi"}, headers={"Authorization": "Basic dTpw"}
    ):
        scenario(results, "flask.jsonify", lambda: jsonify({"a": 1}))
        scenario(
            results,
            "flask.render_template",
            lambda: render_template("flasgger/index.html", title="Flasgger"),
        )
        scenario(results, "flask.url_for", lambda: url_for("x_ep"))
        scenario(results, "flask.request.args", lambda: request.args.get("a"))
        scenario(results, "flask.request.args.get", lambda: request.args.get("a"))
        scenario(results, "flask.request.headers.get", lambda: request.headers.get("Authorization"))
        scenario(results, "flask.request.authorization", lambda: request.authorization)
        scenario(
            results, "flask.request.endpoint", lambda: request.endpoint.lower().replace(".", "_")
        )
        scenario(results, "flask.request.method", lambda: request.method.lower())
        scenario(results, "flask.request.is_json", lambda: request.is_json)
        scenario(results, "flask.request.is_secure", lambda: request.is_secure)
        scenario(results, "flask.request.host", lambda: request.host)
        scenario(results, "flask.request.view_args", lambda: request.view_args)
        scenario(results, "flask.request.url_rule.__str__", lambda: str(request.url_rule))
        scenario(results, "flask.request.json", lambda: request.json.get("message"))
        scenario(
            results,
            "flask.current_app.url_map.iter_rules",
            lambda: list(flask.current_app.url_map.iter_rules()),
        )
        scenario(
            results,
            "flask.current_app.view_functions",
            lambda: flask.current_app.view_functions["x_ep"],
        )
    with app.test_request_context("/x", method="POST", data={"k": "v"}):
        scenario(
            results,
            "flask.request.form",
            lambda: (
                request.form.get("k"),
                "k" in request.form.keys(),
                request.form["k"],
                str(request.form),
            ),
        )
        scenario(results, "flask.request.form.get", lambda: request.form.get("k"))
        scenario(results, "flask.request.form.__getitem__", lambda: request.form["k"])
        # examples/compat.py: request.json.get(...) on a form-encoded POST
        scenario(results, "flask.request.json[non-json body]", lambda: request.json)
    print(
        json.dumps(
            {
                "python": sys.version.split()[0],
                "flask": _version("flask"),
                "werkzeug": _version("werkzeug"),
                "calls": results,
            },
            indent=1,
            sort_keys=True,
        )
    )


def _version(name):
    from importlib.metadata import version

    return version(name)


def _blueprint_app():
    from flask import Blueprint, Flask

    app = Flask("gtbp")
    bp = Blueprint("flasgger", "gtbp", url_prefix="/apidocs")
    bp.add_url_rule("/spec", "spec", view_func=lambda: "ok")
    app.register_blueprint(bp)
    return app


def _json_encoder_subclass():
    from flask.json import JSONEncoder  # flasgger's fallback import (base.py:30)

    class LazyJSONEncoder(JSONEncoder):
        def default(self, obj):
            return super(LazyJSONEncoder, self).default(obj)

    return LazyJSONEncoder().encode({"a": 1})


def _json_provider_subclass(app):
    from flask.json.provider import DefaultJSONProvider  # flasgger's preferred import (base.py:28)

    class LazyJSONEncoder(DefaultJSONProvider):
        def default(self, obj):
            return super(LazyJSONEncoder, self).default(obj)

    return LazyJSONEncoder(app).dumps({"a": 1})


def _method_view(app):
    from flask.views import MethodView

    class SwaggerView(MethodView):  # flasgger/marshmallow_apispec.py:69,90
        def get(self):
            return "ok"

        def dispatch_request(self, *args, **kwargs):
            return super(SwaggerView, self).dispatch_request(*args, **kwargs)

    app.add_url_rule("/sv", view_func=SwaggerView.as_view("sv"))
    return app.test_client().get("/sv").status_code


if __name__ == "__main__":
    main()

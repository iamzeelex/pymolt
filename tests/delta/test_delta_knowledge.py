"""pymolt/knowledge data files and the probe's legacy-interpreter contract."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from pymolt import knowledge
from pymolt.codemods.rules import apply_rule, verify_rule

PROBE = Path(knowledge.__file__).resolve().parent.parent / "delta" / "probe.py"


def test_probe_is_legacy_syntax_and_stdlib_only():
    tree = ast.parse(PROBE.read_text(encoding="utf-8"), feature_version=(3, 6))
    imported = {
        (n.module or "") if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in (n.names if isinstance(n, ast.Import) else [n])
    }
    assert not any(name.split(".")[0] in {"pymolt", "pydantic", "packaging"} for name in imported)
    # no f-strings: they parse on 3.6, but the injected-runtime convention keeps .format()
    assert not any(isinstance(n, ast.JoinedStr) for n in ast.walk(tree))


def test_probe_runs_as_a_subprocess_and_reports_stdlib_paths(tmp_path):
    request = {"paths": ["json.dumps", "json.nope"], "dists": [], "roots": ["json"],
               "per_path_timeout": 5}
    proc = subprocess.run([sys.executable, "-B", "-c", PROBE.read_text(encoding="utf-8")],
                          input=json.dumps(request), capture_output=True, text=True,
                          cwd=str(tmp_path), timeout=60)
    lines = [json.loads(line.split(" ", 1)[1]) for line in proc.stdout.splitlines()
             if line.startswith("@@pymolt-delta@@ ")]
    results = {ln["path"]: ln["result"] for ln in lines if "path" in ln}
    assert lines[-1].get("done") is True
    dumps = results["json.dumps"]["steps"][0]
    assert dumps["exists"] and dumps["canonical"] == "json.dumps"
    assert dumps["shape"][0][0] == "obj"
    assert results["json.nope"]["exists"] is False


@pytest.mark.parametrize("name", ["codemod_rules", "succession", "warning_switches",
                                  "probe_contexts"])
def test_every_entry_names_its_source_and_date(name):
    data = json.loads((Path(knowledge.__file__).parent / f"{name}.json").read_text())
    assert data["schema"] == knowledge.SCHEMA and data["version"]
    for entry in data["entries"]:
        assert entry["id"] and entry["source"] and entry["date"], entry.get("id")


def test_pandas_append_rule_is_windowed_and_verifies():
    assert knowledge.codemod_rules("pandas", "1.5.3", "1.5.4") == []
    (rule,) = knowledge.codemod_rules("pandas", "1.5.3", "2.2.3")
    assert verify_rule(rule)
    src = ("import pandas as pd\n"
           "def f(s1, s2, xs):\n"
           "    xs.append(1)\n"
           "    df = s1.append({'a': 1}, ignore_index=True)\n"
           "    return s1.append(s2, ignore_index=True)\n")
    out, sites = apply_rule(src, rule)
    assert sites == 1
    assert "return pd.concat([s1, s2], ignore_index=True)" in out
    assert "xs.append(1)" in out                                    # list.append untouched
    assert "s1.append({'a': 1}, ignore_index=True)" in out          # a dict row is not concat


def test_successor_hint_and_warning_switches():
    hint = knowledge.successor_hint("pandas", ["pandas.core.frame.DataFrame.append"],
                                    "1.5.3", "2.0.0")
    assert hint is not None and hint[0] == "pandas.concat"
    (switch,) = knowledge.warning_switches("SQLAlchemy", "1.4.49", "2.0.30")
    assert switch.env == {"SQLALCHEMY_WARN_20": "1"}
    assert knowledge.warning_switches("sqlalchemy", "2.0.0", "2.0.30") == []


def test_probe_contexts_match_package_roots():
    (ctx,) = knowledge.probe_contexts({"flask", "werkzeug"})
    compile(ctx.code, "<ctx>", "exec")
    assert knowledge.probe_contexts({"pandas"}) == []

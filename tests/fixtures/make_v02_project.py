#!/usr/bin/env python3
"""Make ``tests/fixtures/v02_project/``: a real ``.pymolt/`` written by pymolt 0.2.

The fixture is how 0.3 meets a project 0.2 left behind (the plan-identity design, spec 4.5): a
legacy single plan file (schema 2), its PLAN.md, a baseline capture, and an open apply run. It is
made by running pymolt 0.2 itself on the e2e_pandas toy (``bench/e2e_pandas/run.py``, the
``equiv`` scenario) with one change: ``requirements.txt`` pins ``pandas==1.5.3``, so the plan's
first step ``env-1`` is a pymolt manifest pin. The commands are e2e_pandas's:

  pymolt setup . --base-python 3.11 --target-python 3.12 --target-env <ENVS>/3.12
  pymolt capture baseline --mode tests --project-dir . --force -- \\
      <ENVS>/3.11/bin/python -m pytest -q -p no:cacheprovider
  pymolt plan . --target-manifest requirements-target.txt
  pymolt apply . --step env-1          # leaves mech-1 and the person steps: an open run

Then the result is stored as ``v02_loader`` reads it: ``.pymolt/cache/`` left out (lenient,
regenerable, gitignored by 0.2 itself), absolute paths replaced by ``<ROOT>``, ``<ENVS>``,
``<TMPDIR>`` and ``~``, and each hash 0.2 took over a file that held the root taken again over
the stored form. 0.2 then checks its own fixture: its ``plan_digest`` of the stored plan (written
to ``expected.json``) equals the canonical hash ``v02_loader.legacy_plan_digest`` takes, and a
relocated copy passes its ``status`` and ``apply --dry-run``.

Usage (pymolt 0.2 must run it: this script refuses a pymolt that writes another layout):

  python bench/e2e_pandas/run.py setup              # once: the 3.11 and 3.12 environments
  git worktree add ../pymolt-0.2 d197ae7            # a 0.2 checkout, and its venv
  (cd ../pymolt-0.2 && uv sync --python 3.12 --extra dev)
  python tests/fixtures/make_v02_project.py --pymolt ../pymolt-0.2/.venv/bin/pymolt

``--envs DIR`` names another environments folder (default ``bench/e2e_pandas/.envs``); ``--keep
DIR`` keeps the raw project there. The script writes nothing outside the output folder, its
temp folder and ``--keep``: attestation state goes to a temp ``$XDG_STATE_HOME``. No agent
approval is made (see ``v02_loader``).
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))

import v02_loader as loader  # noqa: E402

from tests.test_no_private_data import leaks  # noqa: E402

E2E = REPO / "bench" / "e2e_pandas" / "run.py"
PINNED = "pandas==1.5.3\nrequests\n"
TARGET = "pandas==2.2.3\nrequests\n"
FIRST_STEP = "env-1"
LEFT_OUT = (".pymolt/cache/",)


def fail(message: str) -> None:
    print(f"make_v02_project: {message}", file=sys.stderr)
    sys.exit(1)


def e2e_module():
    spec = importlib.util.spec_from_file_location("e2e_pandas_run", E2E)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def spellings(path: Path) -> list[str]:
    seen = {str(path).rstrip("/"), os.path.realpath(path).rstrip("/")}
    return [s for s in seen if s]


class Runner:
    """Runs pymolt 0.2 (and its interpreter) in the project, logging each command."""

    def __init__(self, pymolt: Path, root: Path, work: Path) -> None:
        self.pymolt, self.root, self.work = pymolt, root, work
        self.python = pymolt.parent / "python"
        self.env = {**os.environ,
                    "XDG_STATE_HOME": str(work / "xdg-state"),
                    "XDG_CONFIG_HOME": str(work / "xdg-config"),
                    "USER": "user", "LOGNAME": "user", "NO_COLOR": "1", "COLUMNS": "200"}
        for name in ("FORCE_COLOR", "PY_COLORS", "PYTHONPATH"):
            self.env.pop(name, None)
        self.count = 0

    def pymolt_cmd(self, *args: str, cwd: Path | None = None, ok=(0,)) -> str:
        return self.run([str(self.pymolt), *args], cwd=cwd, ok=ok)

    def run(self, argv: list[str], *, cwd: Path | None = None, ok=(0,)) -> str:
        self.count += 1
        done = subprocess.run(argv, cwd=cwd or self.root, env=self.env, capture_output=True,
                              text=True, stdin=subprocess.DEVNULL)
        log = self.work / f"{self.count:02d}.log"
        log.write_text(f"$ {' '.join(argv)}\n{done.stdout}\n{done.stderr}", encoding="utf-8")
        if done.returncode not in ok:
            fail(f"`{' '.join(argv[:3])} ...` exited {done.returncode}:\n"
                 f"{done.stdout[-3000:]}\n{done.stderr[-3000:]}")
        return done.stdout

    def digest(self, root: Path, plan_file: Path) -> str:
        """0.2's own ``plan_digest`` of a plan file."""
        code = ("import sys; from pathlib import Path; "
                "from pymolt.migration_plan import MigrationPlan; "
                "from pymolt.workorder.approval import plan_digest; "
                "print(plan_digest(MigrationPlan.load(Path(sys.argv[1]), Path(sys.argv[2]))))")
        return self.run([str(self.python), "-c", code, str(root), str(plan_file)]).strip()


def make_raw(runner: Runner, envs: Path) -> None:
    """The toy, and pymolt 0.2's commands on it."""
    root = runner.root
    e2e = e2e_module()
    e2e.make_project(root, False)
    (root / "requirements.txt").write_text(PINNED, encoding="utf-8")
    runner.pymolt_cmd("setup", ".", "--base-python", "3.11", "--target-python", "3.12",
                      "--target-env", str(envs / "3.12"))
    runner.pymolt_cmd("capture", "baseline", "--mode", "tests", "--project-dir", ".", "--force",
                      "--", str(envs / "3.11" / "bin" / "python"), "-m", "pytest", "-q", "-p",
                      "no:cacheprovider")
    (root / "requirements-target.txt").write_text(TARGET, encoding="utf-8")
    runner.pymolt_cmd("plan", ".", "--target-manifest", "requirements-target.txt")
    runner.pymolt_cmd("apply", ".", "--step", FIRST_STEP)


def check_layout(root: Path) -> tuple[dict, dict]:
    """The 0.2 layout and an open run; returns ``(plan, receipt)``."""
    state = root / ".pymolt"
    if (state / "plans").exists():
        fail("this pymolt writes .pymolt/plans/: run the script with a pymolt 0.2")
    plan = json.loads((state / "migration_plan.json").read_text(encoding="utf-8"))
    if plan.get("schema_version") != 2:
        fail(f"the plan has schema {plan.get('schema_version')}, not 2: run the script with a "
             "pymolt 0.2")
    steps = {s["id"]: s for s in plan["steps"]}
    first = steps.get(FIRST_STEP) or {}
    if first.get("executor") != "pymolt" or first.get("type") != "manifest-pin":
        fail(f"{FIRST_STEP} is not a pymolt manifest pin: {first.get('title')}")
    receipt = json.loads((state / "migration_state.json").read_text(encoding="utf-8"))
    runs = receipt.get("runs") or []
    if len(runs) != 1 or runs[0].get("closed") or runs[0]["run_id"] != plan["plan_id"]:
        fail(f"expected one open run of plan {plan['plan_id'][:8]}: {runs}")
    return plan, receipt


def collect(root: Path) -> tuple[dict[str, bytes], dict[str, bytes]]:
    """``(files, raw)``: every file of the project the fixture keeps, by project path, as 0.2
    wrote it (``files`` has the ``.gz`` ones decompressed)."""
    files: dict[str, bytes] = {}
    raw: dict[str, bytes] = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(LEFT_OUT) or "__pycache__" in rel.split("/"):
            continue
        if any(part.startswith(loader.DOT) for part in rel.split("/")) \
                or rel.endswith(loader.GZ_TEXT):
            fail(f"{rel} collides with the fixture's stored spellings")
        data = raw[rel] = path.read_bytes()
        files[rel] = gzip.decompress(data) if rel.endswith(".gz") else data
    return files, raw


def scrub(files: dict[str, bytes], raw: dict[str, bytes], root: Path, envs: Path
          ) -> tuple[dict[str, str], list[dict]]:
    """``(texts with placeholders, rehash bindings)``."""
    rules = [(s, loader.ROOT_MARK) for s in spellings(root)]
    rules += [(s, loader.ENVS_MARK) for s in spellings(envs)]
    rules += [(s, loader.TMPDIR_MARK) for s in spellings(Path(tempfile.gettempdir()))]
    rules += [(s, "~") for s in spellings(Path.home())]
    rules.sort(key=lambda rule: len(rule[0]), reverse=True)
    texts: dict[str, str] = {}
    for rel, data in files.items():
        text = data.decode("utf-8")
        for spelling, mark in rules:
            text = text.replace(spelling, mark)
        texts[rel] = text
    # A hash 0.2 took over a file that held the root no longer matches the stored form: take it
    # again, and record it for the loader, which takes it again where the fixture lands.
    bindings = []
    for rel, data in files.items():
        if texts[rel].encode("utf-8") == data:
            continue
        if rel.endswith(".gz"):
            for digest in (hashlib.sha256(data).hexdigest(),
                           hashlib.sha256(raw[rel]).hexdigest()):
                if any(digest in text for text in texts.values()):
                    fail(f"a hash binds {rel}, a compressed file the fixture cannot rehash")
            continue
        old = hashlib.sha256(data).hexdigest()
        if not any(old in text for text in texts.values()):
            continue
        new = hashlib.sha256(texts[rel].encode("utf-8")).hexdigest()
        texts = {k: loader.rehash_text(v, old, new) for k, v in texts.items()}
        bindings.append({"file": rel, "sha256": new})
    for binding in bindings:
        if hashlib.sha256(texts[binding["file"]].encode("utf-8")).hexdigest() \
                != binding["sha256"]:
            fail(f"{binding['file']} changed after its hash was taken again")
    return texts, bindings


def write_tree(texts: dict[str, str], out: Path) -> None:
    tree = out / "tree"
    if out.exists():
        if any(out.iterdir()) and not (out / "expected.json").is_file():
            fail(f"{out} exists and is not this fixture: not replacing it")
        shutil.rmtree(tree, ignore_errors=True)
        (out / "expected.json").unlink(missing_ok=True)
    for rel, text in texts.items():
        target = tree / loader.stored_path(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    problems = [f"{rel}:{n}: {what}" for rel, text in texts.items()
                for n, line in enumerate(text.splitlines(), 1) for what in leaks(line)]
    if problems:
        fail("private data left in the fixture:\n" + "\n".join(problems[:20]))


def source_commit(pymolt: Path, python: Path) -> dict:
    """The commit the 0.2 pymolt was installed from, when it is a git checkout."""
    code = "import pymolt, pathlib; print(pathlib.Path(pymolt.__file__).parent)"
    package = subprocess.run([str(python), "-c", code], capture_output=True, text=True,
                             check=True).stdout.strip()
    version = subprocess.run([str(pymolt), "--version"], capture_output=True, text=True,
                             check=True).stdout.split()[-1]
    git = ["git", "-C", package]
    head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True)
    if head.returncode != 0:
        return {"pymolt_version": version, "commit": None, "dirty": None}
    dirty = subprocess.run([*git, "status", "--porcelain", "--", "."], capture_output=True,
                           text=True).stdout.strip()
    return {"pymolt_version": version, "commit": head.stdout.strip(), "dirty": bool(dirty)}


def self_check(runner: Runner, out: Path, envs: Path, bindings: list[dict],
               work: Path) -> str:
    """0.2 on its own fixture; returns its ``plan_digest`` of the stored plan."""
    tree = out / "tree"
    as_stored = loader.materialize(work / "as-stored", relocate=False, tree=tree)
    plan_file = as_stored / ".pymolt" / "migration_plan.json"
    digest = runner.digest(as_stored, plan_file)
    if digest != loader.legacy_plan_digest(plan_file):
        fail("0.2's plan_digest differs from the canonical hash of the plan it wrote")
    frozen = next((as_stored / ".pymolt" / "migrations").glob("*/plan.json"))
    if runner.digest(as_stored, frozen) != digest:
        fail("the run's frozen plan is not the saved plan")
    moved = loader.materialize(work / "relocated", envs=envs, tree=tree, rehash=bindings)
    status = json.loads(runner.pymolt_cmd("status", ".", "--json", cwd=moved))
    if status.get("problems"):
        fail(f"0.2's status finds problems in the relocated fixture: {status['problems']}")
    dry = runner.pymolt_cmd("apply", ".", "--dry-run", "--json", cwd=moved)
    if "mech-1" not in dry:
        fail(f"0.2's apply --dry-run of the relocated fixture would not apply mech-1:\n{dry}")
    return digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pymolt", type=Path, default=REPO / ".venv" / "bin" / "pymolt",
                        help="a pymolt 0.2 executable, in a venv with its python")
    parser.add_argument("--envs", type=Path, default=REPO / "bench" / "e2e_pandas" / ".envs")
    parser.add_argument("--out", type=Path, default=loader.FIXTURE)
    parser.add_argument("--keep", type=Path, default=None,
                        help="also copy the raw project here")
    args = parser.parse_args()
    pymolt, envs = args.pymolt.absolute(), args.envs.resolve()
    for version in ("3.11", "3.12"):
        if not (envs / version / "bin" / "python").exists():
            fail(f"no {envs / version}: run `python bench/e2e_pandas/run.py setup` first")
    if not pymolt.exists() or not (pymolt.parent / "python").exists():
        fail(f"{pymolt} is not a pymolt executable in a venv")
    work = Path(tempfile.mkdtemp(prefix="pymolt-v02-")).resolve()
    try:
        root = work / "project"
        root.mkdir()
        runner = Runner(pymolt, root, work)
        made_by = source_commit(pymolt, runner.python)
        make_raw(runner, envs)
        plan, receipt = check_layout(root)
        original = root / ".pymolt" / "migration_plan.json"
        if runner.digest(root, original) != loader.legacy_plan_digest(original):
            fail("0.2's plan_digest differs from the canonical hash of the plan it wrote")
        if args.keep:
            shutil.copytree(root, args.keep, dirs_exist_ok=True)
        texts, bindings = scrub(*collect(root), root, envs)
        write_tree(texts, args.out)
        digest = self_check(runner, args.out, envs, bindings, work)
        stored_plan = json.loads(texts[".pymolt/migration_plan.json"])
        run = receipt["runs"][0]
        expected = {
            "fixture": 1,
            "made_by": made_by,
            "toy": "bench/e2e_pandas, equiv scenario; requirements.txt pins pandas==1.5.3",
            "commands": [
                ["setup", ".", "--base-python", "3.11", "--target-python", "3.12",
                 "--target-env", f"{loader.ENVS_MARK}/3.12"],
                ["capture", "baseline", "--mode", "tests", "--project-dir", ".", "--force", "--",
                 f"{loader.ENVS_MARK}/3.11/bin/python", "-m", "pytest", "-q", "-p",
                 "no:cacheprovider"],
                ["plan", ".", "--target-manifest", "requirements-target.txt"],
                ["apply", ".", "--step", FIRST_STEP],
            ],
            "plan": {
                "file": ".pymolt/migration_plan.json",
                "plan_id": plan["plan_id"],
                "schema_version": plan["schema_version"],
                "created_at": plan["created_at"],
                "steps": [s["id"] for s in plan["steps"]],
                "pymolt_steps": [s["id"] for s in plan["steps"] if s["executor"] == "pymolt"],
                # 0.2's plan_digest of the plan as stored here (placeholders and all): an
                # approval binds it (workorder/approval.py).
                "plan_digest": digest,
            },
            "baseline": {
                "trace": ".pymolt/contract_traces/baseline.jsonl",
                "captured_at": stored_plan["baseline_captured_at"],
                "trace_sha256": stored_plan["baseline_trace_sha256"],
            },
            "run": {
                "run_id": run["run_id"],
                "closed": run["closed"],
                "frozen_plan": f".pymolt/migrations/{run['run_id']}/plan.json",
                "applied": sorted(k.split(":", 1)[1] for k, v in receipt["steps"].items()
                                  if v.get("status") == "applied"),
            },
            "approval": None,
            "relocate": {
                "placeholders": {loader.ROOT_MARK: "the project root",
                                 loader.ENVS_MARK: "the e2e_pandas environments folder",
                                 loader.TMPDIR_MARK: "the generating machine's temp folder",
                                 "~": "the generating user's home"},
                "rehash": bindings,
            },
        }
        (args.out / "expected.json").write_text(json.dumps(expected, indent=2) + "\n",
                                                encoding="utf-8")
        size = sum(p.stat().st_size for p in args.out.rglob("*") if p.is_file())
        print(f"wrote {args.out} ({len(texts)} files, {size // 1024} KiB): plan "
              f"{plan['plan_id'][:8]}, run open with {FIRST_STEP} applied, plan_digest "
              f"{digest[:12]}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

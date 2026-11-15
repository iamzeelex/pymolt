#!/usr/bin/env python3
"""Scripted, repeatable run of the six-project legacy evaluation (PROTOCOL.md).

    run.py run [projects...] --pymolt PATH --out DIR [--parallel N]
    run.py diff SCORECARD_A.json SCORECARD_B.json

Per project it starts or reuses the container `pymolt-bench-<project>`, installs the era
dependencies (skipped when a marker in the container says they are already there), then
runs the phases of PROTOCOL.md from inside the project dir with `.` as the project:

    scan, setup, assess, capture_baseline, target_env, plan, apply, capture_post, verify

Every command's output goes to <out>/<project>/<phase>.log. Per phase the scorecard records
the outcome (ok / degraded / failed / not-reached), wall time, rc and key numbers from the
`--agent` payloads. A phase that runs longer than the timeout is killed and recorded as such.
At the end any apply is rolled back and the artifact's .gitignore is restored.

Inputs live in projects.json. Pure stdlib; needs docker and uv on PATH.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PHASES = [
    "scan",
    "setup",
    "assess",
    "capture_baseline",
    "target_env",
    "plan",
    "apply",
    "capture_post",
    "verify",
]
# Slowest first, so a parallel run does not leave zipline for last.
HEAVY_FIRST = ["zipline", "vibora", "alphalens", "theano", "lasagne", "hyper"]
MARKER = "/root/.pymolt-bench-installed"
VOLATILE = {"uncaptured_s", "overhead_x", "duration_s"}
SYMBOL = {"ok": "✓", "degraded": "~", "failed": "✗", "not-reached": "—"}
_print_lock = threading.Lock()


def say(project: str, msg: str) -> None:
    with _print_lock:
        print(f"[{datetime.now():%H:%M:%S}] {project:<9} {msg}", flush=True)


class Result:
    def __init__(self, rc, wall, timed_out, stdout):
        self.rc, self.wall, self.timed_out, self.stdout = rc, wall, timed_out, stdout

    @property
    def ok(self):
        return self.rc == 0 and not self.timed_out

    def json(self):
        text = self.stdout.strip()
        for cand in (text, text[text.find("{") :] if "{" in text else ""):
            if not cand:
                continue
            try:
                return json.loads(cand)
            except ValueError:
                continue
        return None


def run_proc(
    argv, *, cwd, env, log: Path, timeout_s, capture_stdout=False, stdin_note="", container=None
) -> Result:
    """Run one command; append header, output and footer to `log`.

    Kills the whole process group on timeout."""
    shown = argv if isinstance(argv, str) else " ".join(map(_q, argv))
    with log.open("a") as lf:
        lf.write(f"$ {shown}\n# cwd={cwd} timeout={int(timeout_s)}s {stdin_note}\n")
        lf.flush()
        out_path = log.with_suffix(".stdout.tmp")
        outf = out_path.open("w") if capture_stdout else None
        t0 = time.time()
        p = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            shell=isinstance(argv, str),
            stdin=subprocess.DEVNULL,
            stdout=outf or lf,
            stderr=lf,
            start_new_session=True,
        )
        timed_out = False
        try:
            rc = p.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(p.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    p.wait(timeout=10)
                    break
                except subprocess.TimeoutExpired:
                    continue
            rc = -9
            if container:  # the in-container process outlives the killed docker client
                subprocess.run(
                    [
                        "docker",
                        "exec",
                        container,
                        "sh",
                        "-c",
                        "pkill -9 -f pytest; pkill -9 -f pip",
                    ],
                    capture_output=True,
                )
        wall = time.time() - t0
        stdout = ""
        if outf:
            outf.close()
            stdout = out_path.read_text(errors="replace")
            out_path.unlink()
            lf.write(stdout if len(stdout) < 200_000 else stdout[:200_000] + "\n...[truncated]\n")
        lf.write(f"\n# rc={rc} wall={wall:.1f}s{' TIMEOUT' if timed_out else ''}\n\n")
    return Result(rc, wall, timed_out, stdout)


def _q(s: str) -> str:
    return s if re.fullmatch(r"[\w@%+=:,./-]+", s) else "'" + s.replace("'", "'\\''") + "'"


def run_sh(cmd: str, **kw) -> Result:
    return run_proc(cmd, **kw)


class Project:
    def __init__(self, name, cfg, defaults, pymolt: str, out: Path):
        self.name, self.cfg, self.d = name, cfg, defaults
        self.pymolt = pymolt
        self.art = (REPO / cfg["artifact"]).resolve()
        self.out = (out / name).resolve()
        self.container = f"pymolt-bench-{name}"
        self.phase_timeout = cfg.get("phase_timeout_min", defaults["phase_timeout_min"]) * 60
        self.install_timeout = cfg.get("install_timeout_min", defaults["install_timeout_min"]) * 60
        self.rec: dict[str, dict] = {}
        self.prep: dict = {}
        self.target_py = self.art / ".venv-target" / "bin" / "python"

    # ---- helpers
    def subst(self, s: str) -> str:
        return s.replace("{out}", str(self.out))

    def env(self, extra=None) -> dict:
        e = dict(os.environ)
        for k, v in {**self.cfg.get("env", {}), **(extra or {})}.items():
            e[k] = self.subst(v)
        return e

    def log(self, phase) -> Path:
        return self.out / f"{phase}.log"

    def pm(self, phase, args, *, extra_env=None, agent_json=True, timeout=None) -> Result:
        res = run_proc(
            [self.pymolt, *args],
            cwd=self.art,
            env=self.env(extra_env),
            log=self.log(phase),
            timeout_s=timeout or self.phase_timeout,
            capture_stdout=agent_json,
        )
        if agent_json and res.stdout:
            js = res.json()
            if js is not None:
                (self.out / f"{phase}.json").write_text(json.dumps(js, indent=1))
        return res

    def record(self, phase, outcome, wall=0.0, rc=None, numbers=None, note="", timed_out=False):
        r = {"outcome": outcome, "wall_s": round(wall, 1), "rc": rc, "numbers": numbers or {}}
        if note:
            r["note"] = note
        if timed_out:
            r["timed_out"] = True
            r["note"] = (
                note + " " if note else ""
            ) + f"TIMEOUT after {int(self.phase_timeout // 60)} min"
            r["outcome"] = "failed"
        self.rec[phase] = r
        say(
            self.name,
            f"{phase:<17} {r['outcome']:<11} {r['wall_s']:>7.1f}s rc={rc} {_brief(r['numbers'])}",
        )
        (self.out / "result.json").write_text(json.dumps(self.result(), indent=1))

    def result(self):
        return {"prep": self.prep, "phases": self.rec}

    # ---- steps
    def reset_artifact(self):
        git = ["git", "-C", str(self.art)]
        subprocess.run([*git, "checkout", "--", ".gitignore"], capture_output=True)
        for rel in (".pymolt", ".venv-target"):
            shutil.rmtree(self.art / rel, ignore_errors=True)
        for rel in ("requirements-target.txt", ".pymolt-bench-target.txt"):
            (self.art / rel).unlink(missing_ok=True)
        for fname, text in self.cfg.get("manifests", {}).items():
            (self.art / fname).write_text(text)

    def host_prep(self):
        wheels = self.out / "wheels"
        if self.cfg.get("host_prep") and not (wheels.is_dir() and any(wheels.iterdir())):
            for cmd in self.cfg["host_prep"]:
                wheels.mkdir(exist_ok=True)
                env = dict(os.environ)  # not UV_FIND_LINKS: the dir is what this step fills
                r = run_sh(
                    self.subst(cmd),
                    cwd=self.out,
                    env=env,
                    log=self.log("host_prep"),
                    timeout_s=self.install_timeout,
                )
                if not r.ok:
                    return False
        return True

    def ensure_container(self) -> bool:
        lg = self.log("prep")
        insp = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", self.container],
            capture_output=True,
            text=True,
        )
        if insp.returncode != 0:
            r = run_proc(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    self.container,
                    "--platform",
                    self.d["platform"],
                    "-v",
                    f"{self.art}:/work",
                    "-w",
                    "/work",
                    self.d["image"],
                    "sleep",
                    "infinity",
                ],
                cwd=self.out,
                env=os.environ,
                log=lg,
                timeout_s=600,
            )
            return r.ok
        if insp.stdout.strip() != "true":
            return run_proc(
                ["docker", "start", self.container],
                cwd=self.out,
                env=os.environ,
                log=lg,
                timeout_s=120,
            ).ok
        return True

    def dexec(self, cmd: str, log: Path, timeout, capture=False) -> Result:
        return run_proc(
            ["docker", "exec", "-w", "/work", self.container, "sh", "-c", cmd],
            cwd=self.art,
            env=os.environ,
            log=log,
            timeout_s=timeout,
            capture_stdout=capture,
            container=self.container,
        )

    def install_baseline(self) -> bool:
        t0 = time.time()
        spec = json.dumps(
            [self.cfg["container_install"], self.cfg.get("manifests", {}), self.d["image"]],
            sort_keys=True,
        )
        digest = hashlib.sha256(spec.encode()).hexdigest()[:16]
        lg = self.log("prep")
        if not self.ensure_container():
            self.prep = {
                "outcome": "failed",
                "note": "container did not start",
                "wall_s": round(time.time() - t0, 1),
            }
            return False
        have = self.dexec(f"cat {MARKER} 2>/dev/null", lg, 60, capture=True)
        probe = self.cfg.get("ready_probe")
        if have.stdout.strip() == digest and (not probe or self.dexec(probe, lg, 120).ok):
            self.prep = {
                "outcome": "skipped",
                "note": "marker present, probe passed",
                "wall_s": round(time.time() - t0, 1),
            }
            say(self.name, "baseline deps already installed (marker)")
            return True
        for cmd in self.cfg["container_install"]:
            say(self.name, f"container install: {cmd[:70]}")
            r = self.dexec(cmd, lg, self.install_timeout)
            if not r.ok:
                self.prep = {
                    "outcome": "failed",
                    "note": f"{'timeout' if r.timed_out else f'rc={r.rc}'}: {cmd[:80]}",
                    "wall_s": round(time.time() - t0, 1),
                }
                return False
        self.dexec(f"echo {digest} > {MARKER}", lg, 60)
        self.prep = {"outcome": "ok", "wall_s": round(time.time() - t0, 1)}
        return True

    # ---- phases
    def pytest_argv(self, python: str, post=False):
        flags = list(self.cfg["pytest_flags"]) + (
            self.cfg.get("target_pytest_flags", []) if post else []
        )
        return [python, "-m", "pytest", "-q", *flags, *self.cfg["test_args"]]

    def phase_scan(self):
        r = self.pm("scan", ["scan", ".", "--agent"])
        js = r.json() or {}
        d = js.get("data", {})
        c = d.get("contacts", {})
        self.record(
            "scan",
            "ok" if r.ok and js.get("ok", True) else "failed",
            r.wall,
            r.rc,
            {
                "roots": d.get("root_count"),
                "edges": d.get("edge_totals", {}).get("total"),
                "contacts": c.get("total"),
                "symbols": c.get("distinct_symbols"),
                "deps": c.get("dependencies"),
            },
            timed_out=r.timed_out,
        )

    def setup_argv(self):
        a = [
            "setup",
            ".",
            "--manifest",
            self.cfg["setup"]["manifest"],
            "--tool",
            "container",
            "--container",
            self.container,
            "--base-python",
            self.d["base_python"],
            "--target-python",
            self.d["target_python"],
            "--agent",
        ]
        return a

    def phase_setup(self):
        r = self.pm("setup", self.setup_argv())
        js = r.json() or {}
        ok = r.ok and js.get("ok", True) is not False
        cfgp = self.art / ".pymolt" / "env_config.json"
        self.record(
            "setup",
            "ok" if ok and cfgp.exists() else "failed",
            r.wall,
            r.rc,
            {"manifest": self.cfg["setup"]["manifest"], "saved": cfgp.exists()},
            timed_out=r.timed_out,
        )
        return ok and cfgp.exists()

    def phase_assess(self):
        r = self.pm("assess", ["assess", ".", "--agent"])
        js = r.json() or {}
        d = js.get("data", {})
        tm = self.art / "requirements-target.txt"
        counts = d.get("status_counts", {})
        if not r.ok or not js:
            outcome = "failed"
        elif d.get("target_resolved") is False:
            outcome = "failed"
        elif counts.get("conflict") or counts.get("unresolved") or d.get("manual_zone_count"):
            outcome = "degraded"
        else:
            outcome = "ok"
        self.record(
            "assess",
            outcome,
            r.wall,
            r.rc,
            {
                "tier": d.get("baseline_tier"),
                "resolved": d.get("target_resolved"),
                "verdict": (d.get("verdict") or {}).get("level"),
                "packages": d.get("changes_total"),
                "counts": counts,
                "manual_zone": d.get("manual_zone_count"),
                "target_manifest": tm.exists(),
            },
            timed_out=r.timed_out,
        )

    def phase_capture_baseline(self):
        lg = self.log("baseline_uncaptured")
        cmd = " ".join(map(_q, self.pytest_argv("python")))
        self.dexec(cmd, lg, self.phase_timeout)  # warm-up: caches, runtime-compiled C
        un = self.dexec(cmd, lg, self.phase_timeout)  # the timed uncaptured run
        say(self.name, f"uncaptured baseline rc={un.rc} {un.wall:.0f}s")
        r = self.pm(
            "capture_baseline",
            [
                "capture",
                "baseline",
                "--mode",
                "tests",
                "--project-dir",
                ".",
                "--agent",
                "--",
                *self.pytest_argv("python")[:],
            ],
        )
        self.record_capture("capture_baseline", r, un)

    def record_capture(self, phase, r: Result, un: Result | None):
        js = r.json() or {}
        d = js.get("data", {})
        n = {
            "accepted": d.get("accepted"),
            "events": d.get("events"),
            "tests_rc": d.get("returncode"),
            "validity": d.get("validity"),
            "duration_s": d.get("duration_seconds"),
            "dropped": d.get("dropped_events"),
            "red": bool(d.get("red")),
        }
        if un is not None:
            n["uncaptured_s"] = round(un.wall, 1)
            n["uncaptured_rc"] = un.rc
            if d.get("duration_seconds") and un.wall > 0:
                n["overhead_x"] = round(d["duration_seconds"] / un.wall, 1)
        if d.get("validity_reason") and not d.get("accepted"):
            n["reason"] = str(d["validity_reason"])[:120]
        if not r.ok and not js:
            outcome = "failed"
        elif not d.get("accepted"):
            outcome = "failed"
        elif d.get("red") or (d.get("returncode") not in (0, None)) or d.get("dropped_events"):
            outcome = "degraded"
        else:
            outcome = "ok"
        self.record(phase, outcome, r.wall, r.rc, n, timed_out=r.timed_out)

    def phase_target_env(self):
        lg = self.log("target_env")
        t0 = time.time()
        failed, optional_failed, last = False, False, None
        for step in self.cfg["target_env"]:
            if isinstance(step, dict) and "filter_requirements" in step:
                ok = self.filter_requirements(step["filter_requirements"], lg)
                if not ok:
                    failed = True
                    break
                continue
            cmd, optional = (
                (step["sh"], step.get("optional", False))
                if isinstance(step, dict)
                else (step, False)
            )
            last = run_sh(
                self.subst(cmd),
                cwd=self.art,
                env=self.env(self.cfg.get("post_env")),
                log=lg,
                timeout_s=self.phase_timeout,
            )
            if last.timed_out:
                self.record("target_env", "failed", time.time() - t0, last.rc, timed_out=True)
                return
            if not last.ok:
                if optional:
                    optional_failed = True
                else:
                    failed = True
                    break
        n = {"python_exists": self.target_py.exists(), "optional_step_failed": optional_failed}
        mod = self.cfg.get("import_module")
        imp_ok = None
        if self.target_py.exists() and mod:
            imp = run_proc(
                [str(self.target_py), "-c", f"import {mod}"],
                cwd=self.art,
                env=self.env(self.cfg.get("post_env")),
                log=lg,
                timeout_s=300,
                capture_stdout=False,
            )
            imp_ok = imp.ok
            n["import_" + mod] = imp_ok
        outcome = (
            "failed"
            if failed or not self.target_py.exists()
            else ("degraded" if optional_failed or imp_ok is False else "ok")
        )
        self.record("target_env", outcome, time.time() - t0, last.rc if last else None, n)

    def filter_requirements(self, spec, lg: Path) -> bool:
        """Copy a compiled requirements file, dropping some packages and the hash lines."""
        src, dst = self.art / spec["src"], self.art / spec["dst"]
        if not src.exists():
            with lg.open("a") as f:
                f.write(f"# filter_requirements: {src} missing\n")
            return False
        blocks, cur = [], []
        for line in src.read_text().splitlines():
            if line and not line[0].isspace() and not line.startswith("#") and cur:
                blocks.append(cur)
                cur = []
            if line.strip():
                cur.append(line)
        if cur:
            blocks.append(cur)
        drop = {d.lower() for d in spec.get("drop", [])}
        keep = []
        for b in blocks:
            head = b[0].strip()
            m = re.match(r"([A-Za-z0-9_.-]+)", head)
            if m and m.group(1).lower() in drop:
                continue
            if head.startswith("#"):
                continue
            keep.append(re.sub(r"\s*\\$", "", head))
        dst.write_text("\n".join(keep) + "\n")
        with lg.open("a") as f:
            f.write(
                f"# filter_requirements: wrote {dst.name} with {len(keep)} "
                f"requirement(s), dropped {sorted(drop)}\n"
            )
        return True

    def phase_plan(self):
        if not (self.art / "requirements-target.txt").exists():
            self.record(
                "plan", "not-reached", note="no requirements-target.txt (assess wrote none)"
            )
            return False
        r = self.pm(
            "plan", ["plan", ".", "--target-manifest", "requirements-target.txt", "--agent"]
        )
        js = r.json() or {}
        d = js.get("data", {})
        impacts = d.get("api_impacts") or []
        inconclusive = sum(1 for i in impacts if "INCONCLUSIVE" in str(i))
        n = {
            "files": d.get("files_total"),
            "rewrite_sites": d.get("rewrite_sites"),
            "deps_moved": len(d.get("dependencies") or []) if d else None,
            "api_deltas": d.get("api_impacts_total", len(impacts)),
            "inconclusive": inconclusive,
            "need_attention": d.get("risks_attention_total"),
            "fixed": d.get("risks_fixed_total"),
            "risks_status": d.get("risks_status"),
            "baseline_bound": d.get("baseline_bound"),
        }
        saved = bool(d.get("plan_path")) and Path(d["plan_path"]).exists()
        if not r.ok or not js or js.get("ok") is False or not saved:
            outcome = "failed"
        elif inconclusive and inconclusive >= (n["api_deltas"] or 0):
            outcome = "degraded"
        elif inconclusive:
            outcome = "degraded"
        else:
            outcome = "ok"
        if not js:
            n["error"] = _last_line(self.log("plan"))
        self.record("plan", outcome, r.wall, r.rc, n, timed_out=r.timed_out)
        return saved

    def phase_apply(self, plan_saved):
        if not plan_saved:
            self.record("apply", "not-reached", note="no saved plan")
            return False
        r = self.pm("apply", ["apply", ".", "--json"])
        js = r.json() or {}
        n = {
            "files_changed": _dig(js, "files_changed", "changed", "files"),
            "rewrite_sites": _dig(js, "rewrite_sites", "sites"),
        }
        if isinstance(n["files_changed"], list):
            n["files_changed"] = len(n["files_changed"])
        if not r.ok:
            n["error"] = js.get("error") or _last_line(self.log("apply"))
        outcome = "failed" if not r.ok else ("degraded" if not n["files_changed"] else "ok")
        # a no-op apply is not a failure of apply, but nothing was migrated
        if r.ok and not n["files_changed"]:
            n["noop"] = True
            outcome = "ok"
        self.record("apply", outcome, r.wall, r.rc, n, timed_out=r.timed_out)
        return r.ok

    def phase_capture_post(self):
        if not self.target_py.exists():
            self.record("capture_post", "not-reached", note="no target env python")
            return
        argv = [
            "capture",
            "post-migration",
            "--mode",
            "tests",
            "--project-dir",
            ".",
            "--agent",
            "--",
            *self.pytest_argv(str(self.target_py), post=True),
        ]
        r = self.pm("capture_post", argv, extra_env=self.cfg.get("post_env"))
        self.record_capture("capture_post", r, None)

    def phase_verify(self):
        r = self.pm("verify", ["verify", ".", "--agent"])
        js = r.json() or {}
        d = js.get("data", {})
        verdict = d.get("verdict")
        n = {
            "verdict": verdict,
            "static": d.get("static_targets"),
            "confirmed": d.get("confirmed"),
            "blind": d.get("blind"),
            "dynamic_only": d.get("dynamic_only"),
            "trust_pct": d.get("trust_pct"),
            "compared": d.get("compared"),
        }
        reasons = d.get("reasons") or []
        if reasons:
            n["reason"] = str(reasons[0])[:140]
        cal = d.get("calibration")
        if cal:
            n["calibration"] = cal if isinstance(cal, dict) and len(str(cal)) < 200 else "present"
        if not js or not verdict:
            outcome = "failed"
            n["error"] = _last_line(self.log("verify"))
        elif verdict in ("pass", "fail"):
            outcome = "ok"
        else:
            outcome = "degraded"
        self.record("verify", outcome, r.wall, r.rc, n, timed_out=r.timed_out)

    def cleanup(self):
        lg = self.log("rollback")
        if self.rec.get("apply", {}).get("rc") == 0:
            run_proc(
                [self.pymolt, "rollback", ".", "--json"],
                cwd=self.art,
                env=self.env(),
                log=lg,
                timeout_s=300,
            )
        (self.art / ".pymolt-bench-target.txt").unlink(missing_ok=True)
        run_proc(
            ["git", "-C", str(self.art), "checkout", "--", ".gitignore"],
            cwd=self.art,
            env=os.environ,
            log=lg,
            timeout_s=60,
        )

    # ---- driver
    def run(self):
        self.out.mkdir(parents=True, exist_ok=True)
        for old in self.out.glob("*.log"):
            old.unlink()
        t0 = time.time()
        say(self.name, "start")
        try:
            self.reset_artifact()
            if not self.host_prep():
                self.prep = {"outcome": "failed", "note": "host_prep failed (see host_prep.log)"}
            setup_ok = False
            self.phase_scan()
            ready = self.install_baseline()
            if ready:
                setup_ok = self.phase_setup()
            else:
                self.record(
                    "setup", "not-reached", note=f"baseline env not ready: {self.prep.get('note')}"
                )
            if setup_ok:
                self.phase_assess()
                self.phase_capture_baseline()
            else:
                for p in ("assess", "capture_baseline"):
                    self.record(p, "not-reached", note="setup did not complete")
            self.phase_target_env()
            saved = self.phase_plan() if setup_ok else self._nr("plan")
            self.phase_apply(saved) if setup_ok else self._nr("apply")
            self.phase_capture_post() if setup_ok else self._nr("capture_post")
            self.phase_verify() if setup_ok else self._nr("verify")
        except Exception as e:  # keep the other projects going; the log says what broke
            say(self.name, f"harness error: {e!r}")
            self.prep["harness_error"] = repr(e)
        finally:
            try:
                self.cleanup()
            except Exception as e:
                self.prep["cleanup_error"] = repr(e)
            for p in PHASES:
                self.rec.setdefault(
                    p, {"outcome": "not-reached", "wall_s": 0, "rc": None, "numbers": {}}
                )
            self.prep["project_wall_s"] = round(time.time() - t0, 1)
            (self.out / "result.json").write_text(json.dumps(self.result(), indent=1))
            say(self.name, f"done in {time.time() - t0:.0f}s")
        return self.result()

    def _nr(self, phase):
        self.record(phase, "not-reached", note="setup did not complete")
        return False


def _dig(js, *keys):
    stack = [js]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k in keys:
                if k in cur:
                    return cur[k]
            stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            stack.extend(v for v in cur if isinstance(v, (dict, list)))
    return None


def _last_line(log: Path) -> str:
    try:
        lines = [
            line
            for line in log.read_text(errors="replace").splitlines()
            if line.strip() and not line.startswith(("#", "$"))
        ]
        return lines[-1][:160] if lines else ""
    except OSError:
        return ""


def _brief(numbers: dict) -> str:
    return " ".join(f"{k}={v}" for k, v in numbers.items() if v not in (None, "", False))[:110]


# ---------------------------------------------------------------- scorecard


def note_for(phase: str, rec: dict) -> str:
    n = rec.get("numbers", {})
    bits = []
    if rec.get("timed_out"):
        bits.append("TIMEOUT")
    if phase == "scan":
        bits.append(f"{n.get('edges')} edges, {n.get('contacts')} contacts")
    elif phase == "setup":
        bits.append(str(n.get("manifest") or ""))
    elif phase == "assess":
        c = n.get("counts") or {}
        bits.append(
            f"{n.get('tier') or '?'}; {'resolved' if n.get('resolved') else 'NOT resolved'}"
            + (f"; {', '.join(f'{v} {k}' for k, v in c.items())}" if c else "")
        )
    elif phase in ("capture_baseline", "capture_post"):
        if n.get("accepted"):
            bits.append(
                f"{n.get('events')} events"
                + (f", {n['overhead_x']}x" if n.get("overhead_x") else "")
                + (", red" if n.get("red") else "")
            )
        elif n.get("accepted") is False:
            bits.append(f"refused, {n.get('events')} events")
    elif phase == "target_env":
        imp = [k for k in n if k.startswith("import_")]
        if imp:
            bits.append(f"{imp[0]}: {'ok' if n[imp[0]] else 'FAILS'}")
    elif phase == "plan":
        if n.get("api_deltas") is not None:
            bits.append(
                f"{n.get('inconclusive')}/{n.get('api_deltas')} inconclusive, "
                f"{n.get('rewrite_sites')} rewrites"
            )
    elif phase == "apply":
        bits.append(
            "no-op"
            if n.get("noop")
            else (
                f"{n.get('files_changed')} files"
                if rec.get("outcome") == "ok"
                else str(n.get("error") or "")[:60]
            )
        )
    elif phase == "verify":
        bits.append(f"{str(n.get('verdict')).upper()}, trust {n.get('trust_pct')}%")
    if rec.get("note") and rec["outcome"] == "not-reached":
        bits.append(rec["note"])
    return "; ".join(b for b in bits if b)


def render_md(sc: dict) -> str:
    m = sc["meta"]
    lines = [
        "# Legacy evaluation scorecard",
        "",
        f"pymolt `{m.get('pymolt_version') or m['pymolt']}`, run {m['started']}, "
        f"total {m['total_wall_s'] / 60:.1f} min, parallel {m['parallel']}.",
        "",
        "✓ ok · ~ degraded · ✗ failed · — not reached. Each cell: outcome, wall time, key numbers.",
        "",
        "| | " + " | ".join(p.replace("_", " ") for p in PHASES) + " |",
        "|---|" + "---|" * len(PHASES),
    ]
    for name, pr in sc["projects"].items():
        cells = []
        for p in PHASES:
            rec = pr["phases"].get(p, {"outcome": "not-reached", "numbers": {}})
            note = note_for(p, rec)
            t = rec.get("wall_s") or 0
            cells.append(f"{SYMBOL[rec['outcome']]} {note}" + (f" ({_fmt_t(t)})" if t >= 1 else ""))
        lines.append(f"| {name} | " + " | ".join(c.replace("|", "/") for c in cells) + " |")
    lines += ["", "## Baseline environment (not pymolt's job)", ""]
    for name, pr in sc["projects"].items():
        pp = pr.get("prep", {})
        lines.append(
            f"- {name}: {pp.get('outcome', '?')}"
            + (f" ({pp['wall_s']:.0f}s)" if pp.get("wall_s") else "")
            + (f", {pp['note']}" if pp.get("note") else "")
        )
    return "\n".join(lines) + "\n"


def _fmt_t(s: float) -> str:
    return f"{s:.0f}s" if s < 120 else f"{s / 60:.1f}m"


def write_scorecard(out: Path, meta: dict, results: dict) -> dict:
    order = [p for p in HEAVY_FIRST[::-1] if p in results] + [
        p for p in results if p not in HEAVY_FIRST
    ]
    sc = {"meta": meta, "projects": {p: results[p] for p in order}}
    (out / "scorecard.json").write_text(json.dumps(sc, indent=1))
    (out / "scorecard.md").write_text(render_md(sc))
    return sc


# ---------------------------------------------------------------- commands


def cmd_run(a) -> int:
    cfg = json.loads((HERE / "projects.json").read_text())
    names = a.projects or list(cfg["projects"])
    unknown = [n for n in names if n not in cfg["projects"]]
    if unknown:
        sys.exit(f"unknown project(s): {unknown}; known: {list(cfg['projects'])}")
    names.sort(key=lambda n: HEAVY_FIRST.index(n) if n in HEAVY_FIRST else 99)
    pymolt = str(Path(a.pymolt).resolve())
    if not os.access(pymolt, os.X_OK):
        sys.exit(f"--pymolt {pymolt} is not executable")
    for tool in ("docker", "uv", "git"):
        if not shutil.which(tool):
            sys.exit(f"{tool} not on PATH")
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    d = cfg["defaults"]
    img = subprocess.run(["docker", "image", "inspect", d["image"]], capture_output=True)
    if img.returncode != 0:
        subprocess.run(["docker", "pull", "--platform", d["platform"], d["image"]], check=True)
    started = datetime.now(UTC).isoformat(timespec="seconds")
    t0 = time.time()
    meta = {
        "pymolt": pymolt,
        "pymolt_version": _pymolt_rev(pymolt),
        "started": started,
        "parallel": a.parallel,
        "projects": names,
        "host": sys.platform,
    }
    results: dict[str, dict] = {}
    lock = threading.Lock()

    def work(n):
        res = Project(n, cfg["projects"][n], d, pymolt, out).run()
        with lock:
            results[n] = res
            write_scorecard(
                out,
                {
                    **meta,
                    "total_wall_s": round(time.time() - t0, 1),
                    "partial": len(results) < len(names),
                },
                results,
            )
        return n

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, a.parallel)) as ex:
        list(ex.map(work, names))
    sc = write_scorecard(out, {**meta, "total_wall_s": round(time.time() - t0, 1)}, results)
    print(render_md(sc))
    print(f"scorecard: {out / 'scorecard.json'}")
    return 0


def _pymolt_rev(pymolt: str) -> str:
    p = Path(pymolt).resolve()
    for parent in p.parents:
        if (parent / ".git").exists():
            r = subprocess.run(
                ["git", "-C", str(parent), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
            )
            rev = r.stdout.strip()
            # Uncommitted changes to the package itself make HEAD a wrong label.
            dirty = subprocess.run(
                ["git", "-C", str(parent), "status", "--porcelain",
                 "--untracked-files=normal", "--", "pymolt"],
                capture_output=True,
                text=True,
            ).stdout.strip()
            return f"{rev}+dirty" if rev and dirty else rev
    return ""


def cmd_diff(a) -> int:
    A, B = (json.loads(Path(p).read_text()) for p in (a.a, a.b))
    changed = 0
    for proj in sorted(set(A["projects"]) | set(B["projects"])):
        pa, pb = A["projects"].get(proj), B["projects"].get(proj)
        if pa is None or pb is None:
            print(f"{proj}: only in {'B' if pa is None else 'A'}")
            changed += 1
            continue
        for ph in PHASES:
            ra, rb = pa["phases"].get(ph), pb["phases"].get(ph)
            if not ra or not rb:
                continue
            msgs = []
            if ra["outcome"] != rb["outcome"]:
                msgs.append(
                    f"outcome {SYMBOL[ra['outcome']]} {ra['outcome']} -> "
                    f"{SYMBOL[rb['outcome']]} {rb['outcome']}"
                )
            na, nb = ra.get("numbers", {}), rb.get("numbers", {})
            for k in sorted(set(na) | set(nb)):
                if k in VOLATILE:
                    continue
                if na.get(k) != nb.get(k):
                    msgs.append(f"{k}: {na.get(k)!r} -> {nb.get(k)!r}")
            if msgs:
                changed += 1
                print(f"{proj}.{ph}: " + "; ".join(msgs))
    print(f"{changed} cell(s) changed")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the evaluation")
    r.add_argument("projects", nargs="*", help="default: all six")
    r.add_argument("--pymolt", required=True, help="path to the pymolt executable under test")
    r.add_argument("--out", required=True, help="output directory (logs, scorecard.json/.md)")
    r.add_argument("--parallel", type=int, default=1)
    r.set_defaults(fn=cmd_run)
    d = sub.add_parser("diff", help="print which scorecard cells changed")
    d.add_argument("a")
    d.add_argument("b")
    d.set_defaults(fn=cmd_diff)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())

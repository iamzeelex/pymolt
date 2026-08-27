"""
axiom_graph/bench.py

Large-scale coverage harness: run the pipeline over many (package, from, to)
migrations, persist the resulting codemod patterns to a graph store, and report
**real coverage** — how many libraries yield codemods, and how many codemods
exist between the versions.

Designed for a server run over the ~100 most popular libraries. Robust by
construction: one library failing never aborts the batch; the pipeline's own
checkpointing makes the run resumable. The store (JSON or Neo4j) is chosen by
AXIOM_GRAPH_STORE (see graph_store.store_from_env).

    python -m axiom_graph.benchmark.bench --pairs bench/curated_pairs.json --out report.json
    AXIOM_GRAPH_STORE=bolt://localhost:7687 python -m axiom_graph.benchmark.bench \
        --pairs bench/curated_pairs.json --store-from-env
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MigrationPair:
    name: str
    from_version: str
    to_version: str


class LibResult(BaseModel):
    name: str
    from_version: str
    to_version: str
    status: str  # "ok" | "skipped" | "error"
    codemods: int = 0
    breaking_changes: int = 0
    elapsed_s: float = 0.0
    detail: str | None = None


class CoverageReport(BaseModel):
    """The headline answer: real coverage + codemod counts across the batch."""

    total: int = 0
    analyzed: int = 0          # status == ok
    skipped: int = 0
    errored: int = 0
    libraries_with_codemods: int = 0
    total_codemods: int = 0
    by_kind: dict[str, int] = Field(default_factory=dict)
    by_confidence: dict[str, int] = Field(default_factory=dict)
    results: list[LibResult] = Field(default_factory=list)

    @property
    def coverage_pct(self) -> float:
        """Of the libraries that analyzed cleanly, the share that yielded codemods."""
        if not self.analyzed:
            return 0.0
        return round(100 * self.libraries_with_codemods / self.analyzed, 1)

    def summary_line(self) -> str:
        return (
            f"{self.analyzed}/{self.total} analyzed · "
            f"{self.libraries_with_codemods} with codemods ({self.coverage_pct}%) · "
            f"{self.total_codemods} codemods total · "
            f"{self.skipped} skipped · {self.errored} errored"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────


def _default_compute(name: str, from_v: str, to_v: str, **kwargs):
    from axiom_graph.core.pipeline import compute_full_delta  # lazy: heavy import

    return compute_full_delta(name, from_v, to_v, **kwargs)


def _evaluate(pair: MigrationPair, compute, use_git: bool):
    """
    Analyze one migration pair into a (LibResult, delta) tuple — pure: no store
    writes, no shared counters. Safe to call from worker threads. `delta` is the
    pipeline output for an analyzed library (so the caller can persist it), or
    None for skipped/errored libraries.
    """
    t0 = time.monotonic()
    try:
        delta = compute(pair.name, pair.from_version, pair.to_version, use_git=use_git)
    except Exception as exc:  # never abort the batch on one library
        log.warning("bench: %s failed: %s", pair.name, exc)
        return LibResult(
            name=pair.name, from_version=pair.from_version, to_version=pair.to_version,
            status="error", detail=str(exc), elapsed_s=round(time.monotonic() - t0, 1),
        ), None

    elapsed = round(time.monotonic() - t0, 1)
    if getattr(delta, "skipped", False):
        return LibResult(
            name=pair.name, from_version=pair.from_version, to_version=pair.to_version,
            status="skipped", detail=getattr(delta, "skip_reason", None), elapsed_s=elapsed,
        ), None

    codemods = list(getattr(delta, "codemods", []))
    return LibResult(
        name=pair.name, from_version=pair.from_version, to_version=pair.to_version,
        status="ok", codemods=len(codemods),
        breaking_changes=getattr(delta, "total_breaking", 0), elapsed_s=elapsed,
    ), delta


def run_bench(
    pairs: list[MigrationPair],
    *,
    store=None,
    compute=None,
    use_git: bool = False,
    on_result=None,
    workers: int = 1,
    worker_mode: str = "process",
    skip_existing: bool = False,
) -> CoverageReport:
    """
    Analyze each migration pair, persist its codemods to `store` (if given), and
    accumulate a CoverageReport.

    `compute` defaults to the real pipeline; tests inject a fake. `on_result` is
    an optional callback(LibResult) for live progress.

    With `workers` > 1 each library is analyzed concurrently — one worker per
    library, a few at a time (3–4 is plenty). `worker_mode`:

      - "process" (default): a **separate process per library** — the only way
        to get real parallelism, because the analysis (griffe + LibCST parsing)
        is CPU-bound and holds the GIL. This is what you want for a real run.
      - "thread": a thread pool. Lighter, but CPU-bound steps serialize on the
        GIL; useful for I/O-bound or test runs and required when `compute` is a
        non-picklable closure (tests) — process mode auto-falls-back to this.

    Either way, store writes and counter updates stay on the calling
    thread/process, so the store needs no locking and the tallies can't race.

    `skip_existing` (needs a store): a resumable re-run — any (package, from, to)
    already recorded in the store is skipped and NOT re-analyzed, so a second run
    only does the libraries not processed before. Tracking is by an `:Analysis`
    marker written for every processed migration, so even zero-codemod libraries
    (which leave no edges) are remembered.
    """
    compute = compute or _default_compute

    # Process mode needs a picklable compute (the module-level default qualifies;
    # an injected lambda/closure does not). Fall back to threads rather than fail.
    if worker_mode == "process" and workers and workers > 1:
        import pickle

        try:
            pickle.dumps(compute)
        except Exception:
            log.warning("bench: compute is not picklable; using thread workers")
            worker_mode = "thread"

    report = CoverageReport(total=len(pairs))
    by_kind: Counter = Counter()
    by_conf: Counter = Counter()

    def _mark_done(res: LibResult) -> None:
        """Remember a processed migration so a future skip_existing run skips it."""
        if store is None or res.status not in ("ok", "skipped"):
            return  # errors are NOT marked → a re-run retries them
        try:
            from axiom_graph.storage.graph_store import mark_analyzed

            mark_analyzed(store, res.name, res.from_version, res.to_version)
        except Exception as exc:
            log.warning("bench: mark_analyzed failed for %s: %s", res.name, exc)

    def _record(res: LibResult, delta) -> None:
        """Main-thread side effects for one evaluated pair: persist + tally."""
        if res.status == "error":
            report.errored += 1
        elif res.status == "skipped":
            report.skipped += 1
        else:
            codemods = list(getattr(delta, "codemods", []))
            for c in codemods:
                by_kind[c.kind] += 1
                by_conf[c.confidence] += 1
            if store is not None and codemods:
                try:
                    from axiom_graph.storage.graph_store import persist_full_delta

                    persist_full_delta(store, delta)
                except Exception as exc:  # persistence must not lose the run
                    log.warning("bench: persist failed for %s: %s", res.name, exc)
            report.analyzed += 1
            report.total_codemods += len(codemods)
            if codemods:
                report.libraries_with_codemods += 1
        _mark_done(res)
        report.results.append(res)
        if on_result:
            on_result(res)

    # Resumable re-run: drop pairs already recorded in the store.
    to_run = list(pairs)
    if skip_existing and store is not None:
        try:
            from axiom_graph.storage.graph_store import analyzed_migrations

            done = analyzed_migrations(store)
        except Exception as exc:
            log.warning("bench: could not read analyzed set: %s", exc)
            done = set()
        fresh: list[MigrationPair] = []
        for pair in pairs:
            if (pair.name, pair.from_version, pair.to_version) in done:
                res = LibResult(
                    name=pair.name, from_version=pair.from_version,
                    to_version=pair.to_version, status="skipped",
                    detail="already analyzed", elapsed_s=0.0,
                )
                report.skipped += 1
                report.results.append(res)
                if on_result:
                    on_result(res)
            else:
                fresh.append(pair)
        to_run = fresh

    if workers and workers > 1 and len(to_run) > 1:
        from concurrent.futures import (
            ProcessPoolExecutor,
            ThreadPoolExecutor,
            as_completed,
        )

        Pool = ProcessPoolExecutor if worker_mode == "process" else ThreadPoolExecutor
        # Cap workers at the number of libraries — no point spawning idle ones.
        max_workers = min(workers, len(to_run))
        with Pool(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_evaluate, pair, compute, use_git): pair for pair in to_run
            }
            for fut in as_completed(futures):
                res, delta = fut.result()
                _record(res, delta)
    else:
        for pair in to_run:
            res, delta = _evaluate(pair, compute, use_git)
            _record(res, delta)

    report.by_kind = dict(by_kind)
    report.by_confidence = dict(by_conf)
    if store is not None:
        try:
            store.flush()  # persist analysis markers (they don't auto-flush)
        except Exception as exc:
            log.warning("bench: store flush failed: %s", exc)
    return report


# ─────────────────────────────────────────────────────────────────────────────
# Pair loading + auto-derivation
# ─────────────────────────────────────────────────────────────────────────────


def load_pairs(path: str | Path) -> list[MigrationPair]:
    """
    Load migration pairs from a file:
      - .json  → [{"name","from","to"}, …]
      - .txt   → one package name per line; from→to auto-derived from PyPI.
    """
    path = Path(path)
    if path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return [MigrationPair(d["name"], d["from"], d["to"]) for d in data]
    names = [
        ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    pairs = []
    for name in names:
        pair = derive_pair(name)
        if pair is not None:
            pairs.append(pair)
    return pairs


def derive_pair(name: str) -> MigrationPair | None:
    """
    Auto-derive a (from → to) for a package: the latest stable release as `to`,
    and the latest stable release of the PREVIOUS major series as `from`. Returns
    None if there isn't a clean major boundary. Needs network (PyPI).
    """
    from packaging.version import InvalidVersion, Version

    from axiom_graph.sources.pypi_releases import fetch_pypi_metadata

    try:
        data = fetch_pypi_metadata(name)
    except Exception as exc:
        log.warning("derive_pair: %s metadata fetch failed: %s", name, exc)
        return None

    versions = []
    for v_str in data.get("releases", {}):
        try:
            v = Version(v_str)
        except InvalidVersion:
            continue
        if not v.is_prerelease:
            versions.append(v)
    if not versions:
        return None
    versions.sort()
    to_v = versions[-1]
    prev_major = [v for v in versions if v.major < to_v.major]
    if not prev_major:
        return None  # no previous major → no clean migration boundary
    from_v = max(prev_major)
    return MigrationPair(name, str(from_v), str(to_v))


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Axiom Graph coverage harness.")
    parser.add_argument("--pairs", required=True, help="JSON pairs or TXT of names")
    parser.add_argument("--out", default="bench_report.json", help="Report JSON output")
    parser.add_argument("--store", default=None, help="Store target (path or bolt:// URI)")
    parser.add_argument("--store-from-env", action="store_true",
                        help="Use AXIOM_GRAPH_STORE / NEO4J_* env for the store")
    parser.add_argument("--use-git", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Only the first N pairs")
    parser.add_argument("--workers", type=int, default=1,
                        help="Analyze libraries concurrently (one worker per library)")
    parser.add_argument("--worker-mode", choices=("process", "thread"), default="process",
                        help="process = a real CPU core per library (default); thread = GIL-bound")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Resume: skip libraries already recorded in the store")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    pairs = load_pairs(args.pairs)
    if args.limit:
        pairs = pairs[: args.limit]

    store = None
    if args.store_from_env:
        from axiom_graph.storage.graph_store import store_from_env

        store = store_from_env()
    elif args.store:
        from axiom_graph.storage.graph_store import open_store

        store = open_store(args.store)

    print(f"Running {len(pairs)} migration(s) with {args.workers} "
          f"{args.worker_mode} worker(s)…")

    def _progress(res: LibResult) -> None:
        mark = {"ok": "✓", "skipped": "—", "error": "✗"}.get(res.status, "?")
        print(f"  {mark} {res.name} {res.from_version}→{res.to_version}: "
              f"{res.codemods} codemods ({res.elapsed_s}s)")

    report = run_bench(pairs, store=store, use_git=args.use_git,
                       on_result=_progress, workers=args.workers,
                       worker_mode=args.worker_mode, skip_existing=args.skip_existing)

    Path(args.out).write_text(report.model_dump_json(indent=2), encoding="utf-8")
    print("\n" + report.summary_line())
    print(f"by kind: {report.by_kind}  by confidence: {report.by_confidence}")
    print(f"report → {args.out}")
    if store is not None:
        store.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

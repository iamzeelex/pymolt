# Generalization matrix runner

Measures whether pymolt's funnel (setup, baseline capture, plan, apply, post-migration capture,
verify) works outside the pandas/numpy world. `SPEC.md` has the hypotheses (S1-S5) and the toy
format. Each toy is `toys/<name>/toy.json`; invalid toys are skipped with a message.

## Run

```bash
python bench/generalization/run.py envs [toy ...]                     # old + new env per toy (uv)
python bench/generalization/run.py run [toy ...] --out DIR [--parallel N] [--pymolt PATH]
python bench/generalization/run.py score DIR [toy ...]                # re-score DIR without re-running
python bench/generalization/run.py diff DIR_A DIR_B                   # compare two scorecards
```

No toys named means every valid toy. `run` builds missing envs itself, under
`bench/generalization/.envs/<toy>/{old,new}` (gitignored; python and packages from `toy.json`).
`--pymolt` defaults to the repo's `.venv/bin/pymolt`. `--timeout` caps each step (1800 s).
Stdlib only; needs `uv` on PATH.

## What a scenario does

For each toy and scenario (`equiv`, `change`), in `<out>/<toy>/<scenario>/`: write `files`, apply
the scenario's `edits`, write `manifest` as `requirements.txt`, then `pymolt setup`, `capture
baseline` (old env, pytest), `plan --agent` against `target_manifest`, `apply`, `manual_migration`,
pytest in the new env, `capture post-migration` (new env), `verify --json`. Every step keeps its
log (`<step>.log`, `plan_agent.json`, `verify.json`, `result.json` with return codes and seconds).

Output: `<out>/scorecard.json` and `<out>/scorecard.md` (one row per toy, skew signals, detail).

## Metrics

| key | meaning |
|---|---|
| A delta | Per ground-truth entry: `detected` (a plan impact, or a fact behind a plan risk, names the symbol, an alias or the same leaf under a matching parent, with the same kind), `other_kind` (symbol found, kind differs) or `missed`. Recall counts `detected` only; `recall_any_kind` also counts `other_kind`. `successor` is correct when its leaf equals the plan's replacement. `fp_candidates` are plan impacts matching no ground-truth entry: they need a human re-run to decide if they are real false positives. Both scenarios' plans are pooled. |
| B prediction | For each `expected_changed` symbol of the change scenario: the highest risk tier the plan gave it and verify's calibration result (`hit`, `prevented`, `miss`, `not_exercised`). `hit_rate` is the share with `hit`. Also the calibration totals of both scenarios and CHANGED facts that no plan risk named (`unpredicted_changes`). |
| C rewrites | `proposed` is the number of rewrite sites in the plan. `correct` equals `proposed` when the equiv scenario's pytest passes in the new env after apply plus `manual_migration`, else 0 and `wrong` equals `proposed` (a red suite cannot be attributed to a single rewrite). |
| D verdict | Verdict per scenario against `expected_verdict`; `expected_changed` must appear among the CHANGED fact-diff entries. `exact_share` is exact-level over exact plus type-level fact-diff entries (S1: a low value means values compared by type only). `opaque_share` is the share of baseline return values recorded as `__opaque__` (type only, no structure). |
| E warnings | `expected_warnings` found on the baseline trace events (symbol plus category), split Python-level vs C-level (`from_c`, S2); `missed` lists the rest. |
| F probe | Packages whose delta quality is `inconclusive` (or `partial`), and `import_errors` found in the probe cache (`.pymolt/cache/delta/probe-*.json`) (S3). |

Plus seconds per step in `scorecard.json` (`phase_seconds`), and the plan's work order per
scenario (`steps`: steps in all, by executor and by type; `-` for a plan with no steps). The
markdown table shows them as `total (pymolt/agent/person)`.

## Skew signals

Compared against the `pandas` row when it is in the run (margin 0.2 absolute on A recall, B hit
rate and D exact share; opaque share flagged when 0.2 above pandas). Without a pandas row, A/B/D
shares below 50% are flagged. Also flagged: verdicts that differ from the expectation, C-level
warnings missed, INCONCLUSIVE or import errors, rewrites that left the equiv tests red, and failed
steps. Run `pandas` together with the toys you compare.

# Axiom Graph — Release-Sequenced API Delta Engine

**Axiom Graph** is an API delta analysis engine for Python. It traces library release histories, extracts breaking changes, AST deprecation notices, and test suite diffs, and synthesizes **typed, high-confidence codemod recipes** using AST term unification.

---

## Architecture & Analysis Contours

Axiom Graph analyzes package evolutions across multiple decoupled contours:

```
                                  [ Release Chain ]
                               (PyPI API + Git Tags)
                                        │
                            ┌───────────┴───────────┐
                            ▼                       ▼
                     [ Griffe Diff ]         [ sdist Cache ]
                    (Structural API)        (Local download)
                            │                       │
                            │               ┌───────┼───────┐
                            │               ▼       ▼       ▼
                            │             [PyCG]  [AST]  [Tests]
                            ▼               │       │       │
                     (Raw Breakages)        ▼       ▼       ▼
                            │            (Graph) (Warn) (Diffs)
                            │               │       │       │
                            └───────────────┼───────┼───────┘
                                            ▼
                                     [ fusion.py ]
                                     (pairwise &
                                     state machine)
                                            │
                                            ▼
                                   [ Final FullDelta ]
                                 (JSON + LibCST Rules)
```

1. **Release Sequence**: Builds ordered version topologies `A → A.1 → ... → B` from PyPI releases and Git tags.
2. **Local Cache**: Fetches and caches package sources (`sdist`) locally under `~/Library/Caches/axiom_graph/sdist` (or `~/.cache/axiom_graph/sdist`).
3. **Analysis Contours**:
   - **Griffe Diff**: Fast structural analysis of public API shifts (signature changes, deletions, altered defaults).
   - **Call Graph (PyCG)**: Inter-procedural call-graph reachability analysis.
   - **AST Deprecation Miner**: Scans for `warnings.warn`, `@deprecated`, and custom deprecation markers in library sources.
   - **Test Miner**: Extracts concrete migration examples directly from upstream test suite commits.
4. **State Machine Fusion**: Collapses the release chain into a unified lifecycle progression (`Active → Deprecated → Removed`), recording exact version bounds.
5. **Incremental Checkpoints**: Each computed pairwise delta appends to a JSONL checkpoint file (`checkpoints/*.jsonl`), resuming instantly on restart.

---

## AST Pattern Extraction

Instead of generating fragile line-based diffs, Axiom Graph performs **typed structural term unification** across test diffs:

* **Expression Generalization**: Analyzes deleted (`-`) and added (`+`) AST subtrees. Receivers and argument expressions become typed metavariables.
* **Type Inference**: Infers variable roles from usage context (`DataFrame`, `Series`, `Expr`).
* **Variable Identity Preservation**: Reusable terms maintain consistent metavariable indices across before/after trees (`Series_1` matches `Series_1`).
* **Noise Rejection**: Chunks with disjoint syntax or test boilerplate are pruned automatically.

### Example Extracted Rules

#### 1. DataFrame Concatenation in Pandas (`pandas 1.3.5 → 2.0.0`)

Self-slice appending:
```json
{
  "before": "Series_1.append(Series_1)",
  "after": "pd.concat([Series_1, Series_1])"
}
```

Disjoint DataFrame combination:
```json
{
  "before": "DataFrame_1.append(DataFrame_2)",
  "after": "pd.concat([DataFrame_1, DataFrame_2])"
}
```

#### 2. Click Parameter Handling (`click 7.1.2 → 8.0.0`)

Renamed parameter processor:
```json
{
  "before": "Expr_1.full_process_value(Expr_2, None)",
  "after": "Expr_1.process_value(Expr_2, None)"
}
```

---

## Usage

### Running an Analysis

Run the pipeline for a package release jump:

```bash
python -m axiom_graph click 7.1.2 8.0.0 --max-cg-iter 3
```

Export complete deltas directly to JSON:

```bash
python -m axiom_graph pandas 1.3.5 2.0.0 --max-cg-iter 3 --json /tmp/axiom_pandas.json
```

Useful flags:
* `--no-resume`: Ignore checkpoint cache and force fresh recomputation.
* `--verbose`: Print detailed step-by-step progress and raw diff trees.

### Running Tests

```bash
pytest services/axiom_graph/tests/
```

# Commands

The migration commands, in the order you run them. Each takes the project directory as its
argument (default `.`), except `capture`, whose arguments are the command to run: it takes
`--project-dir`. The report commands take `--json`, which prints the full payload, and `--agent`,
which prints the compact agent view (see [agents.md](agents.md)). `pymolt <command> --help` is the
authoritative list of options.

## Migration

### `pymolt status [dir]`
Shows where the project stands, whether its environments exist, and the single next command. Run it
at any time. Options: `--json`, `--agent`.

### `pymolt scan [dir]`
Lists the manifests, the Python-version divergence, the dependency edges, and every place your code
touches a dependency: calls, attribute reads, decorators, base classes, context managers, constants
and protocol operations. These call sites are the denominator of every coverage number. Offline.
Options: `--json`, `--agent`.

### `pymolt setup [dir]`
Chooses the manifest, the resolution tool and the base and target Python, and writes
`.pymolt/env_config.json`. With every choice given it asks nothing. Without a terminal, any missing
value takes the detected default, and the output names it. With `--json` or `--agent` it never asks
and never takes a default: no choice lists the valid values and saves nothing, all four
(`--manifest`, `--tool`, `--base-python`, `--target-python`) save, and anything in between is an
error.

| Option | Meaning |
|---|---|
| `--manifest` | Manifest to migrate, e.g. `requirements.txt` |
| `--tool` | Resolution tool: `uv`, `conda`, `poetry`, `container` or `system` |
| `--base-python` / `--target-python` | The project's current Python, and the one to migrate to |
| `--container` | A running container to resolve in (with `--tool container`); the baseline is captured in it |
| `--target-env` | An existing target environment; the post-migration capture runs its commands there |

### `pymolt assess [dir]`
Resolves the dependencies for the target Python and writes the pinned target manifest
(`requirements-target.txt`). It shows which versions move, which conflict, what needs manual work,
and how to build the target environment.

| Option | Meaning |
|---|---|
| `--target-python`, `--base-python`, `--manifest` | Override setup's choices |
| `--container` | A running container to resolve the base dependencies in |
| `--upgrade-constraint` | A constraint file with the target versions you want, e.g. `pandas<2.1` |
| `--risk` | Also rate each package for CVEs, wheels and abandonment (network) |
| `--no-cache`, `--no-hashes`, `--no-write`, `--verbose` | Resolve again; write without hashes; write nothing; show transitive dependencies |

### `pymolt capture baseline|post-migration [-- CMD]`
Records the calls your code makes into its dependencies while `CMD` runs. Capture the baseline
before any code change, because it cannot be recreated later. Details are in
[verify.md](verify.md#captures).

| Option | Meaning |
|---|---|
| `--project-dir` | The project (default `.`) |
| `--mode` | `tests` (your test suite), `command` (your app, the default) or `attach` (you run the command; pymolt prints it) |
| `--collect` | Finish a `--mode attach` capture |
| `--target`, `--exclude`, `--source` | Which dependencies to record, which top-level packages to skip, which packages are your code |
| `--stdlib` | Standard-library modules to record (comma list), or `none`. Default: during a Python move, every stdlib module your project imports |
| `--backend` | `auto`, `setprofile`, `monitoring`, `wrap` or `hybrid` |
| `--container`, `--workdir` | Run `CMD` in a running container (default: the one setup configured) |
| `--privacy` | `auto` (values for tests, shape otherwise), `values` or `shape` |
| `--sample-rate` | Fraction of calls whose values are kept; every call is still counted |
| `--impact-targets` | Dependency symbols to focus on (post-migration default: the applied plan's) |
| `--replicate` | Baseline only: run the baseline's command again on the same code and environment, to measure run-to-run noise (no command needed) |
| `--runs 2` | Baseline only: record the baseline and then its replicate. Not with `--json`, `--agent` or `--mode attach` |
| `--accept-failing` | Keep a capture whose command exited non-zero, stamped red |
| `--force` | Replace an existing capture without asking |
| `--deployment-id`, `--request-id`, `--correlation-id` | Identifiers stored with the capture |

### `pymolt plan [dir]`
Computes which APIs your code uses changed between the base and the target, which Python-version
changes affect it, the codemods for both and the predicted risk at each call site. Saves this as a
plan and shows the diff. It writes no source file. Details are in [plan.md](plan.md).

| Option | Meaning |
|---|---|
| `--target-manifest` | The target manifest to plan for (default: the one assess wrote) |
| `--online` | Read intermediate releases from PyPI for successor hints (package names and versions only) |
| `--endpoint` | Use a remote Axiom at this URL instead of the local engine (or set `PYMOLT_AXIOM_ENDPOINT`) |
| `--output`, `--patch` | Where to save the plan; also write the diff to a file |

### `pymolt apply [dir]`
Writes exactly the saved plan. It refuses if a planned file changed since the plan was made. Every
changed file is prepared and parsed before the first write, and a failed apply restores them all.
The original files are kept for `rollback`. Run again on an applied plan whose files still hold its
changes, it writes nothing and says so. Its summary and `--json` list what the plan leaves to you:
the advisories (a site not rewritten, a rewrite with a caveat to check) and the manual items, each
with its file and line.

The plan stays valid while the setup (manifest file, base Python, toolset, container), the target
manifest and the planned files are unchanged. Bumping the pins in the project's manifest does not
make it stale. Bump them after `plan` and before the post-migration capture, before or after
`apply` (see [plan.md](plan.md#when-to-bump-the-manifest)).

| Option | Meaning |
|---|---|
| `--plan` | The plan file (default: `.pymolt/migration_plan.json`) |
| `--allow-no-baseline` | Apply without a baseline capture; `verify` will then be INCONCLUSIVE |

### `pymolt verify [dir]`
Compares the baseline and post-migration captures and prints `PASS`, `FAIL` or `INCONCLUSIVE` with
the evidence. Details are in [verify.md](verify.md).

| Option | Meaning |
|---|---|
| `--baseline`, `--post-migration` | Compare other recordings. Against another baseline the verdict is not saved. On another post-migration recording it is saved with what it compared, and `status` does not count it |
| `--profile`, `--comparator` | How values are compared: `exact`, `shape`, or `custom` with `module:callable` |
| `--probe-python` | A target interpreter to replay the recorded calls in |
| `--golden-before`, `--golden-after` | Golden snapshots from before and after |
| `--policy`, `--metrics`, `--auto-rollback` | Canary policy and metrics, and rollback when a required gate fails |
| `--shadow` | Shadow operations; unsafe writes are left out |
| `--cascade` | A cascade report to include |

### `pymolt rollback [dir]`
Restores the files the last `apply` changed. It refuses to discard edits made after the apply unless
given `--force`. `--run-id` picks an earlier apply. The plan stays valid: while its files are back at
the planned originals, `apply` runs it again as a new run, with the same checks. When it restored files, the post-migration
capture is set aside in the archive. When it restored none, the capture still describes the code
and is kept.

## Tools

| Command | Description |
|---|---|
| `pymolt mcp` | Runs the optional MCP server on stdio (needs `pymolt[mcp]`; see [agents.md](agents.md)) |

## Experimental

| Command | Description |
|---|---|
| `pymolt succession [dir]` | For a project on a deprecated framework, finds where its code should go (the curated knowledge covers Keras, TF1 → TF2 and Keras → PyTorch). `--plan` saves the in-place codemods for `apply` |
| `pymolt forks REPO` | Ranks the live forks of an abandoned dependency |

## Removed commands

<!-- docs-check:off -->
These commands were removed in this version. Each still prints its replacement and exits with code 2.

| Removed | Use instead |
|---|---|
| `doctor` | `status` |
| `contract capture`, `contract trace`, `contract boundary`, `contract export-watcher` | `capture` |
| `contract report`, `contract diff` | `verify` (`--baseline`/`--post-migration` for other recordings) |
| `contract map` | `scan` |
| `migrate`, `codemods` | `plan` + `apply` |
| `env hint` | `assess` |
| `axiom`, `auth`, `config`, `login`, `logout` | API changes are computed locally by `plan`. For a remote provider use `plan --endpoint URL` or `PYMOLT_AXIOM_ENDPOINT`, with the token in `PYMOLT_API_TOKEN` |
<!-- docs-check:on -->

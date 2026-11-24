# Commands

The migration commands, in the order you run them. Each takes the project directory as its
argument (default `.`), except `capture`, whose arguments are the command to run: it takes
`--project-dir`. The report commands take `--json`, which prints the full payload, and `--agent`,
which prints the compact agent view (see [agents.md](agents.md)). `pymolt <command> --help` is the
authoritative list of options.

## Migration

### `pymolt status [dir]`
Shows where the project stands, whether its environments exist, and the single next command. Run it
at any time. Options: `--json`, `--agent`. With no apply run in effect, a migration made by hand is
recognised by a post-migration capture taken since the baseline, or, unless a valid plan waits for
`apply`, by files or pins changed since the baseline. Its verdict completes it; a FAIL then has no
rollback to point at. After any FAIL that no single pymolt step in effect explains, `status` gives
no command, never a whole-run rollback: it names the plan steps whose sites failed and a person's
two options, fixing the code (and capturing again) or accepting an intended change.

With a saved plan, a `steps` line counts the plan's steps by status: `planned`, `blocked` (a
dependency not in effect), `stale` (a statement it rewrites is gone, or the plan itself is stale),
`present` (its change is already there, made by hand), `applied`, `rolled_back`, `skipped`, and
`not_in_capture` (applied or recorded again after the post-migration capture, which holds the code from before it or an earlier version of the step). `--json` and `--agent` list them
under `steps`; `--agent` adds the brief of the step the next command is about. The next command
follows the steps: `pymolt apply .` while pymolt steps wait, `pymolt apply --step ID --dry-run`
for a stale one, then `pymolt apply --step ID --record` for each agent or person step in plan
order (or `--skip` it), then the post-migration capture. A rolled-back step is not applied again by
itself: after `rollback --step`, the next command is the post-migration capture (re-apply the step
with `pymolt apply --step ID` if you want it back). The post-migration capture is stale when a
file it recorded no longer has the recorded text, whatever changed it.

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
and how to build the target environment. When the project's own `setup.py` or `pyproject.toml`
does not build on the target Python, assess says so: it resolves the dependencies it reads from
that file without running it (`target_basis: static-read` in `--json`), or, when it reads none,
names the way forward (fix the build, or `setup --manifest` a requirements file).

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
plan of ordered steps, writes it for a person as `.pymolt/PLAN.md`, and shows the diff and the
steps. It writes no source file. Its `--json` adds `steps` and `plan_md`. Details are in
[plan.md](plan.md).

| Option | Meaning |
|---|---|
| `--target-manifest` | The target manifest to plan for (default: the one assess wrote) |
| `--online` | Read intermediate releases from PyPI for successor hints (package names and versions only) |
| `--output`, `--patch` | Where to save the plan; also write the diff to a file |
| `--plan-md` | Where to write the plan for a person (default: `.pymolt/PLAN.md`) |

### `pymolt apply [dir]`
Writes the saved plan's pymolt steps: with no option, every one whose dependencies are in effect,
in plan order, in one transaction. A step already in effect is a no-op. Each step's file is
decided on its current text: at the step's reviewed before-text it gets the plan's after-text; at
the reviewed after-text nothing is written and the step still gets its epoch; when only other
lines moved, its recipe runs again at its statements (re-based) and is written if it makes the
reviewed change there; with the step's
change already made by hand in its place (other lines differ) the step is `present`, nothing is
written, and verify judges those lines as hand edits. Otherwise the step is stale and the whole
invocation is refused, naming each statement that is gone. Every
changed file is prepared and parsed before the first write, and a failed apply restores them all.
Before the first write it compiles each changed file's new source on the target Python, and on the
base Python where the change claims to run there; it refuses a file that compiled before the
rewrite and does not after. A file that already failed is recorded, not refused. The receipt and
`status` show the result, and "not checked" with the reason when no interpreter was found.
The original files are kept for `rollback`. Run again on an applied plan whose files still hold its
changes, it writes nothing and says so. The receipt, `.pymolt/migration_state.json`, keeps every
run and, per run, each step it applied: its status, its checks and the predicted risks it
addresses. A plan applied while an earlier run's code is still on disk closes that run; both
runs' edits stay in effect for `verify`. Each file's text before and after a step is kept once, by
its hash, in `.pymolt/blobs/`. These are copies of your source, so pymolt writes
`.pymolt/.gitignore` for them; it does not change your own `.gitignore`. `plan` and `apply` delete
the texts that no run on disk, and neither of the two runs closed last, still needs. Its summary and `--json` list what the plan leaves to you:
the advisories (a site not rewritten, a rewrite with a caveat to check) and the manual items, each
with its file and line.

Exit codes: 0 applied, recorded, skipped, aborted, already in effect or present; 1 a compile or
smoke-import failure (nothing stays written; a recorded edit stays); 2 refused (an unknown or
stale step, a dependency not in effect, a record in the wrong state or another record open,
another migration operation running, an option refused under `--agent`); 3 an environment or
file error; 4 no valid baseline bound to the plan. A plan's steps apply in one run, opened by
its first `apply`; a plan applied while another plan's run is open closes that run, whose
applied steps stay in effect, and a closed run applies nothing more. A later `apply` of the same
run checks the baseline against the world the run started from. An invocation interrupted
between its write and its records is undone by the next one.

A step for an agent or a person is recorded, not written: `apply --step ID --record` snapshots
the project's `.py` files (and the step's own files), the edit is made, and `--record --done`
records what changed since as the step's new epoch, with its claim, `basis: manual` and who made
it. A run has one record open at a time, and while it is open `apply` and `rollback` write
nothing. `--done` with no `--record` first diffs each of the step's files from its last known
text (its text after the latest ledger event on it, whatever the event, else its text at plan
time; it refuses when that text is no longer kept) and records only the statements inside the
step's functions and classes; the other changes are listed and judged as hand edits. Recording a step again diffs from its previous epoch's
before-text, so the new epoch is the whole step. `rollback --step` restores a recorded step's
before-text. The recorded Python files are compiled on the target: a failure is on the receipt,
the edit stays, and the exit code is 1.

`apply` also writes the plan's pin step: each `name==version` line of a requirements manifest,
with no hash, gets the target's pin, and `rollback` restores it. The other lines naming a moved
package, and other kinds of manifest, are a step for a person.

The plan stays valid while the setup (manifest file, base Python, toolset, container), the target
manifest and the planned files are unchanged. Bumping the pins in the project's manifest does not
make it stale. Bump the lines `apply` does not after `plan` and before the post-migration
capture, before or after `apply` (see [plan.md](plan.md#when-to-bump-the-manifest)).

| Option | Meaning |
|---|---|
| `--plan` | The plan file (default: `.pymolt/migration_plan.json`) |
| `--step` | That step only. Refused when one of its dependencies is not in effect, or when it is a step for an agent or a person |
| `--through` | Every pymolt step up to that one that is not in effect. Refused when a dependency on the way is not in effect and not on the way |
| `--dry-run` | Decide and compile, then print the diff that would be written. Writes nothing |
| `--skip`, `--reason` | With `--step`: mark the step skipped, with why. `apply` then leaves it out and its dependents stay blocked; `--step` applies it after all |
| `--brief` | With `--step`: print the step's brief (JSON under `--agent`). Writes nothing |
| `--record` | With `--step`: open a record of an agent's or a person's edit. Refused unless the step is planned, rolled back, stale or recorded, its dependencies are in effect and no other record is open |
| `--done`, `--abort` | With `--record`: record what changed since as the step, or drop the open record (files untouched) |
| `--by`, `--actor` | With `--record`: who made the edit (`agent` or `person`, default `person`, `agent` under `--agent`) and the name recorded |
| `--claim` | With `--record --done`: `preserve` or `intended`. It may only tighten the step's claim; loosening it needs a person's attestation |
| `--expect` | With `--record --done --claim intended`: `any` or `representation`. `raise:<Type>` and `return:<Type>` are a person's attested expectation |
| `--pair` | With `--record --done`: `OLD=NEW`, a pairing anchor verify pairs old calls with new ones by (repeatable) |
| `--approve-agent` | A person approves this plan for agents, on a terminal (an attestation). Refused under `--agent` and `--json`. A re-plan or any change to the plan voids it |
| `--agent` | The agent view. It writes pymolt steps only on a plan a person approved for agents; it refuses `--skip`, `--allow-no-baseline` and `--by person`. `--dry-run`, `--brief` and `--record` work on any plan |
| `--allow-no-baseline` | Apply without a baseline capture; `verify` will then be INCONCLUSIVE |
| `--no-smoke` | Skip the smoke import. By default apply imports the changed modules in the target environment setup names, before and after the write; a module that imported before and fails after undoes the write, and the ledger records the rollback. It runs your code's import-time code. When it does not run (`--no-smoke`, no target environment), the receipt says `not checked` and why |

### `pymolt verify [dir]`
Compares the baseline and post-migration captures and prints `PASS`, `FAIL` or `INCONCLUSIVE` with
the evidence. Details are in [verify.md](verify.md). A FAIL names the plan steps whose sites
failed, applied or not (`--agent`: `data.failing_steps`).

| Option | Meaning |
|---|---|
| `--baseline`, `--post-migration` | Compare other recordings. Against another baseline the verdict is not saved. On another post-migration recording it is saved with what it compared, and `status` does not count it |
| `--profile`, `--comparator` | How values are compared: `exact`, `shape`, or `custom` with `module:callable` |
| `--probe-python` | A target interpreter to replay the recorded calls in |
| `--accept` | A failing or review finding's id, which the report prints: a person accepts the change as intended. Refused under `--agent` and `--json`, off a terminal, and on a line a `preserve` claim covers |
| `--strict` | Review every accepted change instead of counting it equal |
| `--verbose`, `-v` | Also show what was explained away: masked noise, replicate-explained differences |
| `--step` | Also print that step's judgement and exit by it: 0 verified, 1 failed, 4 needs review, needs evidence or not in the capture, 2 an unknown step. The verdict still covers the whole surface |

### `pymolt rollback [dir]`
Restores the files the open run's steps changed, latest step first. It refuses to discard edits
made after the apply unless given `--force`, which restores each file the run touched to its text
before the run and never writes another file (a step rolled back alone before is not such an
edit). `--run-id` picks an earlier run; it is refused while a later run's step in effect changed
one of its files (roll that run back first). The plan stays valid: `apply` writes the
rolled-back steps again in the same run, re-based when their lines moved. A step whose epoch
changed no file stays in effect.

`--step` rolls back one step. A file the step was the last change to gets back its text from
before the step. A file a later step or a hand edit changed since gets only the step's own change
undone, and keeps everything else byte for byte: the lines the step wrote must be there
unchanged, each statement it wrote must still stand between the statements around it then (or
what a later step rewrote them to), and the result must give back the step's old statements and
compile on the target. An import the step wrote goes only when nothing uses it any more: no code
in the file, no `__all__`, no other module importing it from this one. Otherwise it stays, and
the output says so. Anything else refuses the step, names what changed the file and why its
change cannot be undone alone, and writes nothing.

The post-migration capture is set aside in the archive only when it recorded code the rollback
took away: a rolled-back step's new statements are in its source snapshot, and it was taken
after that step. Otherwise it still describes the code and is kept.

| Option | Meaning |
|---|---|
| `--step` | That step only: each of its files gets back its text before the step, or, when later steps or edits changed the file, only the step's own change is undone (above). Refused when that cannot be done safely, naming the later steps or edits, and while a step in effect depends on it. `--json` lists the steps undone alone in `inverse`, and the imports that stay in `notes` |
| `--cascade` | With `--step`: roll back the steps that depend on it first, latest first |
| `--run-id` | The run to roll back (default: the open run) |
| `--force` | Also discard edits made after apply (the whole run only) |
| `--agent` | With `--step`, on a plan a person approved for agents. A whole run, `--force` and `--cascade` are refused: a person does those |

## Damaged or newer state files

pymolt keeps its state in `.pymolt/`: `env_config.json` (setup's choices), `contract_state.json`
(the captures), `migration_plan.json` (the plan), `migration_state.json` (apply's receipt),
`apply_ledger.json` (the ledger), `verification.json` (the saved verdict), and each apply run's
`migrations/<run-id>/state.json`, `plan.json` and `approval.json`. A missing file is simply not
there yet. A file that cannot be read, is not JSON, or does not match what pymolt expects is
**damaged**; one with a higher `schema_version` than this pymolt reads was written by a **newer**
pymolt. pymolt never reads either as missing and never overwrites it: every command that needs
the file exits with code 2, naming the file, what is wrong and what to do; `--json` and `--agent`
print it as JSON with `path`, `kind`, `problem` (`damaged` or `newer`), `detail` and `remedy`.
`capture` refuses before it runs your command, and `plan` and `verify` before they compute
anything.

- A newer file: upgrade pymolt.
- The plan, the setup choices, the saved verdict or an approval: move the file into
  `.pymolt/archive/` to keep it, then run the command that rebuilds it (`pymolt plan .`,
  `pymolt setup .`, `pymolt verify .`, or a person's `pymolt apply . --approve-agent`).
- The capture state points at the recordings, which stay in `.pymolt/contract_traces/`:
  restore it from version control or a backup if you can; otherwise move it aside and capture
  again (a baseline can only be captured before the code changes).
- The receipt, the ledger and a run's files hold what rollback needs: restore them from
  version control, a backup or `.pymolt/archive/` before anything else, and do not delete
  them.

`status` does not fail on such a file: it shows it as a problem of its phase (`problems` in
`--json` and `--agent`), with no next command and the remedy as the reason. The caches
(`.pymolt/cache/` and the noise profile) are different: a damaged cache is rebuilt.

Every file pymolt writes, caches and reports included, is written to a temporary file next to
it and then renamed over it, so a crash or a full disk leaves the old file or the new one. Two
pymolt commands may run on one project at once (CI jobs sharing a checkout, an agent and a
person): their updates of a state file take turns through `.pymolt/state.lock`, which a
command waits up to 60 seconds for, then exits with code 3 naming the process that holds it.
`apply`, `rollback`, `apply --approve-agent` and `verify --accept` also hold
`.pymolt/migration.lock` for their whole run and are refused at once while another holds it.

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

`auth`, `config`, `login` and `logout` are gone with no replacement: pymolt needs no sign-in or
user settings, and `plan` computes API changes on this machine.
<!-- docs-check:on -->

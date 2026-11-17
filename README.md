# PyMolt

PyMolt checks whether a Python or dependency upgrade changed what your code does.

It runs your tests or your app before and after the upgrade and records every call your code makes into a dependency: the function, the call site, the arguments, and the result or the exception. Then it compares the two recordings and lists the calls whose outcome changed. Your code is not modified for this.

It also does the steps around that check. It finds where your code uses each dependency, resolves the target environment, lists the API changes between the two versions that your code uses, and rewrites the calls it can.

## Example

Python 3.11 + pandas 1.5.3 → Python 3.12 + pandas 2.2.3. The tests pass on both sides, but `verify` fails:

<!-- example:verify:start -->
```text
VERDICT: FAIL
Static symbols: 4   confirmed: 4   BLIND: 0   dynamic-only: 3
Contract trust: 100%  (4/4 behavioural contacts in executed files)
Impact focus: 8 changed API path(s); on them 3 static, 3 confirmed, 0 BLIND, 1 dynamic-only
Version diff: behavior changed  (changed 1, rewritten 1, disappeared 1, drift 1, equal 7)

Change       Dependency call                      Call site (your code)          Before → after
CHANGED      pandas.core.frame.DataFrame.mean     toyapp/core.py:24 average()    returns Series(float64) → raises TypeError
DISAPPEARED  pandas.core.series.Series.to_dict    toyapp/core.py:24 average()    returns {"v": 2.0} → —
REWRITTEN    pandas.core.series.Series.append     toyapp/core.py:13 combine()    equal result
             → pandas.core.reshape.concat.concat
DRIFT        pandas.core.frame.DataFrame.groupby  toyapp/core.py:18 summarize()  default `squeeze` removed
```
<!-- example:verify:end -->

`DataFrame.mean()` on a frame with a text column now raises `TypeError`. The app catches it, so its test stays green. To reproduce: `python bench/e2e_pandas/run.py setup`, then `python bench/e2e_pandas/run.py run change`.

## Install

PyMolt needs Python 3.12. `assess` and `plan` need [uv](https://docs.astral.sh/uv/) on `PATH`. The PyPI release (0.1.0) is outdated, so install from source:

```bash
git clone https://github.com/iamzeelex/pymolt.git
uv tool install ./pymolt              # or: pipx install ./pymolt
uv tool install "./pymolt[mcp]"       # with the optional MCP server
```

## Usage

```bash
cd your-project
pymolt setup . --target-python 3.12 --target-env .venv-312
pymolt assess .
pymolt capture baseline --mode tests -- pytest tests/
pymolt plan .
pymolt apply .
# build .venv-312 from requirements-target.txt (assess prints the commands)
pymolt capture post-migration --mode tests -- pytest tests/
pymolt verify .
```

| Step | What it does |
|---|---|
| `setup` | Chooses the manifest, the resolution tool and the base and target Python. Writes `.pymolt/env_config.json`. |
| `assess` | Resolves the dependencies under the target Python. Writes `requirements-target.txt`. |
| `capture baseline` | Runs the command and records the calls. Run it before any code change: it cannot be recreated afterwards. With `--runs 2` it runs twice, to measure what varies between runs. |
| `plan` | Lists the API changes your code uses, the rewrites and the predicted risks. Writes no source. |
| `apply` | Writes the plan's rewrites. `pymolt rollback .` undoes them. |
| `capture post-migration` | Runs the same command in the target environment. |
| `verify` | Compares the two captures and prints the verdict. |

- `pymolt status .` shows the current step and the next command.
- `plan` and `apply` are optional: you can change the code by hand or with an agent instead.
- For a long-running app, capture with `--mode command -- python app.py` (Ctrl+C ends it), or with `--mode attach` for a process you start yourself.
- For a project that runs in a container, `setup --container NAME` makes the baseline capture run inside it.

Every option is in [docs/commands.md](docs/commands.md).

## Verdict

| Verdict | Meaning | Exit code |
|---|---|---|
| `FAIL` | A recorded call now has a different outcome (CHANGED), no longer happens (DISAPPEARED), or is new (APPEARED). | 1 |
| `PASS` | Every recorded call has the same outcome, and the runs exercised every dependency symbol your code uses. | 0 |
| `INCONCLUSIVE` | pymolt cannot decide on its own. The captures, or some recorded values, cannot be compared (different command, dropped events, stale environment); the runs left a dependency symbol unexercised (BLIND); or a difference needs a person's review. The report names what is missing and what settles each review item. | 4 |

- A call on an edited line (REWRITTEN) is compared with the call that replaced it. If `pymolt apply` made the edit, any different outcome fails. If you made it, a different value is a review item (INCONCLUSIVE), and only a new exception fails.
- DRIFT (a parameter the call leaves at its default was added or removed) is reported and does not fail.
- Other exit codes: 1 from `capture` means the captured command failed, 2 an invalid invocation, 3 an environment or service failure.

The rules are in [docs/verify.md](docs/verify.md).

## Supported Pythons

- **PyMolt:** 3.12.
- **The project you migrate:** 3.6 and later. On 3.12+ the tracer uses `sys.monitoring`. On 3.6–3.11 it uses `sys.setprofile`. Python 2.7 is not supported.

## Limits

Where pymolt cannot help yet:
- **Targets that do not build or import.** `assess` checks that the target resolves, not that it builds or imports. If it does not import, there is no post-migration capture and nothing to compare. All six legacy projects we tried (Python 3.6 → 3.12) stop here today.
- **Behaviour changes.** `apply` rewrites moved and renamed APIs and Python-version breakage. A behaviour change, like `DataFrame.mean` above, is reported; fixing it is up to you.
- **Code no run exercises.** It is not checked: its symbols are BLIND and the verdict stays INCONCLUSIVE.
- **Differences it cannot judge alone.** These are review items, and the verdict is INCONCLUSIVE: a new value after your hand edit; a value that varies between runs, when the baseline was not captured with `--runs 2`; an environment difference between the two captures.
- **Some exception types on 3.6–3.11 test suites.** A raise that your code handles without any call (`except ValueError: pass`), or that C code swallows (`hasattr`, a `for` loop's `StopIteration`), is recorded without its type.
- **Compiled calls.** A C function is recorded without its result, and its arguments are not compared.
- **Late contacts in long runs (3.12+).** A dunder, property or generator call first made late in a run is recorded when its line of your code had not run before. It is missed on a line that already ran and only later reaches a dependency this way (`x[k]` on dicts for an hour, then on a DataFrame).
- **Overhead.** Capture slows a test suite on Python 3.6 by 2.7–11.8×.

The full list, the measured overhead and where pymolt was tried are in [docs/limits.md](docs/limits.md).

## Agents and CI

- `status`, `scan`, `setup`, `assess`, `capture`, `plan` and `verify` take `--agent` (a compact JSON view) and `--json` (the full report).
- [`plugin/`](plugin/) is a Claude Code plugin that drives these commands.
- An optional MCP server exposes the same views.

See [docs/agents.md](docs/agents.md).

## Documentation

- [docs/commands.md](docs/commands.md): commands and options.
- [docs/verify.md](docs/verify.md): captures, facts, coverage, the verdict.
- [docs/plan.md](docs/plan.md): API changes, Python-version breakage, rewrites, predicted risks.
- [docs/agents.md](docs/agents.md): agent views, the plugin, the MCP server.
- [docs/limits.md](docs/limits.md): limits, overhead, project status.
- [docs/glossary.md](docs/glossary.md): terms.

## License

Apache License 2.0, see [LICENSE](LICENSE).

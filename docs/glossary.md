# Glossary

One word per thing, in the CLI, the agent views, the MCP tools and these docs.

| Term | Meaning |
|---|---|
| **base** / **target** | The project as it is today (base Python, base dependencies) and as it will be after the migration |
| **manifest** | The file that declares dependencies: `requirements.txt`, `pyproject.toml`, `environment.yml`, and so on. setup picks one; assess writes the **target manifest** (`requirements-target.txt` or `environment-target.yml`) |
| **capture** | The calls your code made into its dependencies while a command ran, recorded without changing the code |
| **baseline** | The capture taken before the migration. Nothing else is called baseline |
| **post-migration** | The capture taken after the migration, in the target environment |
| **call site** | A place in your code that touches a dependency (a contact). `scan` lists them (the contact map) |
| **fact** | A callee, a call site and the arguments your code passed, mapped to the outcomes seen |
| **static symbol** | A dependency symbol (`module.Class.method`) your code names at one or more call sites |
| **confirmed** / **BLIND** | A static symbol a capture exercised / one no capture exercised |
| **dynamic-only** | A symbol the captures saw that the static map did not list |
| **trust** | Confirmed static symbols over the static symbols reached by behavioural contacts in the files the run executed |
| **CHANGED**, **REWRITTEN**, **DISAPPEARED**, **APPEARED**, **DRIFT**, **UNCOMPARABLE** | The ways a fact can differ between captures (see [verify.md](verify.md#facts-and-the-diff)) |
| **replicate** | A second run of the baseline's command on the same code and environment; what differs between the two is run-to-run noise |
| **review item** | A difference pymolt cannot judge on its own: a hand edit's new value, a value that may be run noise, an input that changed with no visible cause, a dict order, an environment difference. It makes the verdict INCONCLUSIVE and names what settles it |
| **finding** | One reported difference: failing facts grouped by symbol and call site, or a review item |
| **API change** | A symbol your code uses that is removed, moved, or changed in signature, default or deprecation status between base and target |
| **codemod** | A rewrite for one API or Python-version change, applied with LibCST |
| **plan** | The API changes, the codemods applied to your files as a diff, and the predicted risks, saved for `apply` |
| **predicted risk** | A call site the plan expects to break, rated certain / high / medium / low |
| **verdict** | `PASS`, `FAIL` or `INCONCLUSIVE`, from `verify` |
| **base tier** | How far the base dependencies can be trusted: gold (pinned, on the declared base Python), silver (pinned on an assumed base Python, or the tests pass), bronze (unvalidated), none (not resolved). JSON key: `baseline_tier` |
| **Axiom** | An optional remote provider of API changes, used only if configured with `plan --endpoint` or `PYMOLT_AXIOM_ENDPOINT` |

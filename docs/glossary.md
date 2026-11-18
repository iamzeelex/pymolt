# Glossary

One word per thing, in the CLI, the agent views, the MCP tools and these docs.

| Term | Meaning |
|---|---|
| **base** / **target** | The project as it is today (base Python, base dependencies) and as it will be after the migration |
| **manifest** | The file that declares dependencies: `requirements.txt`, `pyproject.toml`, `environment.yml`, and so on. setup picks one; assess writes the **target manifest** (`requirements-target.txt` or `environment-target.yml`) |
| **capture** | The calls your code made into its dependencies while a command ran, recorded without changing the code |
| **baseline** | The capture taken before the migration. Nothing else is called baseline |
| **post-migration** | The capture taken after the migration, in the target environment |
| **call site** / **contact** | A place in your code that touches a dependency: file, line, symbol and kind (call, decorator, context manager, protocol, attribute read, base class, constant). `scan` lists them (the contact map) |
| **fact** | A callee, a call site and the arguments your code passed, mapped to the outcomes seen |
| **static symbol** | A dependency symbol (`module.Class.method`) your code names at one or more call sites |
| **confirmed** / **BLIND** | A static symbol a capture exercised / one no capture exercised. A BLIND symbol is *not run* (none of its contacts ran), *ran but not recorded* (one ran and the capture has nothing there) or *unobservable* (no capture can record its contacts) |
| **dynamic-only** | A symbol the captures saw that the static map did not list |
| **recorded**, **ran, not recorded**, **not run** | A contact the capture recorded on its statement / whose statement ran with nothing recorded there (a tracer gap) / whose statement never ran (a test gap) |
| **unobservable** | A contact no capture can record: an attribute read, base class or constant of a symbol never observed, a chain through a return value, a dependency not installed where the capture ran, the test runner, or interpreter code. Left out of coverage and recall |
| **coverage** | (recorded + ran, not recorded) over the contacts that are not unobservable: how much of the dependency surface the run reached |
| **recall** | Recorded over (recorded + ran, not recorded): how much of what ran the capture caught. About 100%, or a tracer limit |
| **granularity** | How coverage and recall knew a contact ran: `line` (3.12+), `function` (3.6–3.11), `file` (captures without the executed-code record), `symbol` (traces without call sites) |
| **CHANGED**, **REWRITTEN**, **DISAPPEARED**, **APPEARED**, **DRIFT**, **UNCOMPARABLE** | The ways a fact can differ between captures (see [verify.md](verify.md#facts-and-the-diff)) |
| **replicate** | A second run of the baseline's command on the same code and environment; what differs between the two is run-to-run noise |
| **approval** | A person's attested `apply --approve-agent`: agents may apply this plan's pymolt steps and roll one back. A re-plan or any change to the plan voids it |
| **attestation** | A person's confirmation, typed on a terminal, kept outside the project with an HMAC. Only an attestation can judge an edit more leniently than a hand edit |
| **ACCEPTED** | A changed outcome on an edited line that a person attested as intended: it counts as compared equal and is always printed, with who accepted it and when |
| **review item** | A difference pymolt cannot judge on its own: a hand edit's new value, a value that may be run noise, an input that changed with no visible cause, a dict order, an environment difference. It makes the verdict INCONCLUSIVE and names what settles it |
| **finding** | One reported difference: failing facts grouped by symbol and call site, or a review item |
| **API change** | A symbol your code uses that is removed, moved, or changed in signature, default or deprecation status between base and target |
| **codemod** | A rewrite for one API or Python-version change, applied with LibCST |
| **plan** | The API changes, the codemods applied to your files as a diff, and the predicted risks, saved for `apply` |
| **step** | One unit of the plan: a recipe pymolt applies across every file it changes, or work for an agent or a person. `apply --step ID` applies one |
| **anchor** | A statement named by what it is (its code with the formatting stripped), its function or class and its neighbours, never by its line, so it is found again after lines move |
| **run** | The application of one plan, opened by its first `apply`. Applying another plan closes it; its applied steps stay in effect |
| **epoch** | One application of a step, numbered by its ledger event. A rollback ends it; applying the step again opens a new one |
| **record** | An agent's or a person's edit made as a step: `apply --step ID --record` snapshots the files, `--record --done` records what changed as the step's epoch, with its claim |
| **claim** | What a step promises verify: `preserve` (every outcome at its sites stays the same) or `intended` (the outcome changes on purpose; a person attests the new one) |
| **re-based** | A step applied to a file that moved on since the plan: its recipe ran again at its anchored statements and made the reviewed change there |
| **present** | A step whose change is already in its place, made by hand: `apply` writes nothing for it and verify judges those lines as hand edits |
| **stale step** | A step a statement of which is gone, or whose recipe now makes another change: `apply` refuses it and names why |
| **predicted risk** | A call site the plan expects to break, rated certain / high / medium / low |
| **verdict** | `PASS`, `FAIL` or `INCONCLUSIVE`, from `verify` |
| **base tier** | How far the base dependencies can be trusted: gold (pinned, on the declared base Python), silver (pinned on an assumed base Python, or the tests pass), bronze (unvalidated), none (not resolved). JSON key: `baseline_tier` |
| **Axiom** | An optional remote provider of API changes, used only if configured with `plan --endpoint` or `PYMOLT_AXIOM_ENDPOINT` |

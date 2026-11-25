# Successors v3: intermediate releases, parameter renames, honest measurement

Status: design, 2026-11-25. Base: `integration/release-0.2-qa`. Target: 0.3, except the two fixes
in 7.1, which land in 0.2.

The owner's request (2026-11-25): raise the replacements `plan` names correctly from 75 to at least
100 of the 147 in the benchmark, without bloating the cache on a developer's machine and without
collapsing it: reading intermediate releases must be fast and reliable. Renamed and moved
parameters must be solved too. (Earlier, the retired Axiom service read releases on a server;
pymolt now computes everything locally.)

How this design was made: the 72 missed replacements were sorted by cause; the existing `--online`
path was measured on the ten toys where it could help; two independent designs (one Opus, one
Sonnet) answered the same brief; a held-out ground truth was built from the libraries' changelogs.
Where the designs differ, the choice and the reason are given.

The rule from v2 stays: text and prose only name a successor; only a proof in the library's own
code, with a complete summary of the body, rewrites code.

## 1. Where the misses come from

Wave G, offline, on the 0.2 QA code: 146 of 167 API changes found, 75 of 147 replacements named,
1 wrong, 39 sites rewritten, 0 wrong. The 72 misses, by cause (both designs re-sorted them; the
counts below are the Opus split, which checked each row against the code):

| Cause | n | Recoverable | How |
|---|---|---|---|
| A. The evidence was probed and then lost by a bug | 12 | 12 | offline fixes (3.1) |
| B. The text is in the new version but not read (a C twin's pure-Python module, a decorator argument, an error message held in a variable) | 8 | 6–8 | offline (3.2) |
| C. The text exists only in releases between old and new | 14 | 12–14 | intermediate releases (4) |
| D. The library offers a choice (`displot` or `histplot`) | 8 | 0 named; listed as candidates | stays an agent's choice |
| E. The replacement is an expression (`with engine.begin() as conn:`) | 12 | 0 named; some as a candidate | 6 |
| F. No release states the replacement in code or text | 4 | 0 | agents |
| G. The change itself was not detected | 13 | 0–3 | 7 |
| H. The ground truth disagrees with the library's own text (`JSONEncoder`: flask says `Flask.json`) | 1 | — | ground-truth rule (8) |

Expected: about 90 offline (A, B), about 105 with intermediate releases (range 98–112 between the
two designs). The goal of 100 needs both.

Bugs found in A, each to be confirmed by a failing test before it is fixed:
- curated stdlib successors are written into the fact's evidence but never into its successor
  (`pyversion/facts.py`), which loses `assertEquals`, `getargspec`, `utcnow`, `utcfromtimestamp`;
  `stdlib_changes.json` also gives `now` as the successor of `utcfromtimestamp`;
- `delta/engine.py::_choose` rejects a candidate as "does not exist in the new version" when its
  spelling was simply never probed there (`aiohttp.encode_basic_auth`, `can_read_body`,
  `add_trace` exist). That is a false statement, not only a lost name (7.1);
- the one wrong successor today (`fastapi.routing.APIRouter.lifespan`) comes from qualifying a word
  under a class reached through a forward; `lifespan` is a parameter of `FastAPI.__init__`;
- streamlit's error text is mined from the canonical path, not the spelling the project wrote;
- a text naming several parameters credits only one (Starlette `on_startup`);
- unregistered deprecation helpers (`unittest.case._deprecate`, SQLAlchemy's `became_legacy_20`)
  are data rows missing from `knowledge/deprecation_helpers.json`.

A wrong successor waiting to happen: Python 3.12's `_pydatetime` says "Use timezone-aware objects
to represent datetimes in UTC: datetime.datetime.now(datetime.UTC)". The current text pattern would
name `datetime.timezone`. A hyphenated word is prose; the code fragment after the colon is the
replacement.

## 2. What `--online` does today, measured

`delta/fetch.py` reads one module file from an intermediate release's wheel with HTTP range
requests (the zip central directory, then the member), at most 8 releases (the newest patch of each
minor first), and caches in the project's `.pymolt/cache/delta/`. On ten toys: 28 lookups, 7 hits
(6 newly named, 1 that the ground truth calls wrong), 39.7 MB and 553 requests in 16.5 s of
fetching, 0.1–6.3 MB of cache per project. Why the rest failed:
- the wrong module file is read: the path comes from the old version, and code moves (numpy 2's
  `_*_impl.py` modules, networkx two `import *` hops away, streamlit's commands);
- four lookups (about 8 MB) went to private numpy dispatchers the project never touches;
- the text was there but in a decorator argument (`_api.deprecated(alternative=...)`) or a variable;
- the 8-release cap never reached the releases that warned (networkx 2.4–2.7);
- the zip tail and central directory are fetched again for every symbol and release;
- the release listing dominates the bytes (`numpy.json` 3.8 MB, `matplotlib.json` 2.5 MB), copied
  into every project;
- the member is inflated with no output cap (7.1).

The benchmark also has a defect: its offline column can reuse cached online hints. Every mode must
start from empty caches.

## 3. Offline work

### 3.1 Fix what is lost
The bugs of section 1, A. Each gets a failing test first; the "does not exist" filter probes the
candidate in the new interpreter instead of assuming.

### 3.2 Read what the new version already says
- the pure-Python twin of a C module from the new interpreter's own `Lib/` (`_pydatetime`,
  `_pyio`), read statically;
- decorator arguments that name a replacement (`alternative=`, `new_name=`), through the helper
  registry, and messages built in a variable or with `%`/`format` from literals;
- the project's own spelling as well as the canonical path when mining error texts.

## 4. Intermediate releases, locally

The two designs agree on the shape; the numbers below take the stricter of the two.

**What is read.** First, unpacked wheels already in uv's cache, at no network cost. Otherwise
wheel members by range request. The member is found by following the project's own spelling
through that release's `__init__` re-exports, `__all__`, star imports and `__getattr__` tables (at
most 3 hops), never by reusing the old file path; `.pyi` stubs where shipped. Everything is parsed
with `ast` on the host by the probe's own extractors, so "complete body" means the same thing
online and offline. Nothing from a release is imported or executed. Sdist-only and yanked releases
are skipped and stamped.

**Which releases.** The newest patch of each minor series. A galloping search from the new end
finds the first minor that still has the symbol, then bisects the gap and reads both neighbours
(about log2(m) + 2 reads per package, shared by all of that package's symbols). Histories are not
monotonic (flask 2.3.0 dropped `flask.Markup` by accident; 2.3.1 restored it): when presence flips
more than once, the search reads the rest of the gap and stamps `non-monotone`.

**What is kept, where.** A shared user cache, `$XDG_CACHE_HOME/pymolt/releases/`: the release
listing (with its ETag), each wheel's zip index, and the facts extracted from each member, keyed
by the member's checksum. Raw source and whole wheels are never kept. Entries are content-addressed
and written atomically, so concurrent pymolt processes need no lock. Least recently used entries go
first above a cap (default 100 MB; facts are kilobytes, so the cap is a guard). Projects keep only
pointers. `--cache project` keeps everything under `.pymolt/` for a hermetic CI job.

**Budgets.** Counted in releases and bytes, not seconds, so a plan is repeatable: at most 40
releases and 40 MB per plan, 4 concurrent requests, backoff and `Retry-After`. A wall-clock guard
(60 s) only stops a hung network. Whatever stops the search is stamped on the plan
(`online: complete | partial | cached | shipped | failed`), and a partial result is never filled
in silently later.

**Security.** A size cap on every member, a bounded inflate, ZIP64 refused, no path built from a
member name.

**Privacy.** Only `pypi.org` and `files.pythonhosted.org`, only for distributions that came from
PyPI; a package from a private index is never queried. The docs must also say that byte ranges
reveal which modules of a package pymolt reads, not only names and versions.

**Expected cost.** A typical project: 4–8 MB, 50–70 requests, 3–15 s, under 1 MB of cache. A large
one: capped at the budget, about 2 MB of cache. A warm rerun: under a second.

## 5. A shipped index

Both designs: yes. A maintainer script mines the release histories of a frozen list of popular
packages (the top N by downloads, fixed before any tuning, independent of the toys) with the same
extractors, and ships the result: raw statements (what a release says about a symbol, with
distribution, version, member, line range, checksum and extractor version), never a chosen
successor. `succession.py` filters shipped and online statements the same way. Offline plans then
match online ones for those packages, and are deterministic across machines. CI re-mines a sample
to prove the index reproducible, and a corpus audit gives a precision figure on code nobody tuned
for. Size: a few MB compressed, updated monthly, split per package. Open: the list size (Sonnet:
150–200; Opus: 300) and whether it ships in pymolt's wheel or in a data package.

## 6. Parameters, expressions, missed detections

**Renamed parameters.** Evidence, strongest first: a deprecation helper's data; an alias in a
complete body of an intermediate release where both names coexist (S2's alias proof, run on that
release); a text that names both, with the old one in the old signature and the new one in the
target; signature pairing, a hint only. A `rename-keyword` rewrite needs a proof of the first two
kinds and a two-sided transfer check: the old version's binding equals release R's, and R's new
parameter has the same kind and default in the target, with R's body unchanged up to the target.
Until the held-out set shows such proofs hold, a proof found only in an intermediate release names
the rename and does not rewrite (open decision). A parameter moved into another object (aiohttp
`ClientTimeout`, httpx `ASGITransport`, Starlette `lifespan`) is named, with the library's own
snippet, never rewritten. verify judges a rewrite through the ledger's `param_map`, claim
`preserve`.

**Expressions.** When the library's own text holds the code (`inspect(engine).get_table_names()`),
the key callable is listed as a candidate carrying the text, and named only if it is the single
callable that resolves. No recipe tables: a hand-written table would be an answer sheet for our own
toys.

**Missed detections.** Do: methods a project subclass overrides, packages dropped from the target,
module attributes whose value now belongs to another package. Later: attributes of returned
objects (needs the receivers the capture saw). Not: settings defaults, `**kwargs` forwarding.

## 7. Staging

| Stage | Work | Wave G gain |
|---|---|---|
| 0 | Freeze the scorer and the ground-truth schema (8); the held-out runner; empty caches per mode | — |
| A | Offline fixes (3.1) | about +12, wrong 1 → 0 |
| B | New-version text (3.2) | about +6 |
| C | `--online` v2 (4), with the shared cache | about +12 |
| D | Parameter proofs on intermediate releases (6) | named; rewrites behind a decision |
| E | The shipped index (5) | offline matches online for indexed packages |
| F | Expression candidates, missed detections | +1–3 |

B and C can run in parallel. Each stage is measured on Wave G and on the held-out set before the
next starts.

### 7.1 In 0.2, now
Two bugs are fixed before 0.2 is released, because they are wrong today, not missing features:
- the "does not exist in the new version" reason stated for a candidate that was never probed
  there: the reason says what is known ("not among the symbols probed in the new version");
- the unbounded inflate of a wheel member under `--online`: a cap on the output.

## 8. Measuring without overfitting

- A held-out ground truth: 187 API changes of the same 25 libraries across the same version pairs,
  that the toys do not use, each with a source, a quote and a check in both environments
  (successor kinds: 85 names, 18 parameters, 30 expressions, 54 none). Thin for PyYAML (1) and
  httpx (4).
- Both ground truths move to one schema with an `acceptable` list per item, written and frozen
  before stage A. A dispute (the library's text vs the changelog) is settled by a rule set before
  the runs, not by the result.
- Reported side by side: Wave G and held-out; offline, shipped index, online; per successor kind:
  named correctly, named wrong, candidates containing the answer, nothing.
- Gates: 0 wrong rewrites, 0 falsified proofs, named-wrong not above today's, no benchmark
  regression.
- Success: Wave G at or above 100 of 147 with the index or online, at or above 90 offline; the
  held-out rate within 10 points of Wave G's; corpus-audit precision at or above 97%.
- Tuning rule: a rule or helper added after looking at a Wave G miss must also be supported by
  another library in the held-out set or the corpus, or it is reverted. A gap larger than 10 points
  between Wave G and held-out means the stage was tuned to the toys.

## 9. Decisions for the owner

1. A shared user cache for release data (default cap 100 MB), with `--cache project` for CI.
2. Ship the index; how many packages (150, 300); in pymolt's wheel or a separate data package.
3. `--online` stays off by default, or `setup` asks once (never under `--json`/`--agent`).
4. The privacy wording, and that private-index packages are never queried.
5. Budgets by releases and bytes (40 / 40 MB), with a 60 s guard.
6. Whether a proof found only in an intermediate release may rewrite in 0.3, or only name until the
   held-out set shows it holds.
7. Expressions: candidates only, no recipe tables.
8. The ground-truth schema, the dispute rule and the success thresholds, frozen before stage A.

# Branches, versions and releases

## Versions

pymolt uses [PEP 440](https://peps.python.org/pep-0440/) versions in their canonical
form, the form PyPI shows. The version lives in one place, `pymolt/__init__.py`.

| Kind | Version | Git tag | Where it goes |
|---|---|---|---|
| Final | `0.2.0` | `v0.2.0` | PyPI, GitHub Release (latest) |
| Patch | `0.2.1` | `v0.2.1` | PyPI, GitHub Release (latest) |
| Release candidate | `0.2.0rc1` | `v0.2.0rc1` | PyPI and GitHub as a pre-release |
| Alpha / beta (rare) | `0.3.0a1`, `0.3.0b1` | `v0.3.0a1` | PyPI and GitHub as a pre-release |
| Development | `0.3.0.dev0` | none | never published |

- Write `0.2.0rc1`, not `0.2.0-rc.1` or `0.2.0-rc1`. SemVer pre-release spelling is
  normalized by PyPI, and the release workflow refuses any tag that is not canonical.
- `pip`, `uv` and `install.sh` skip pre-releases unless asked
  (`uv tool install --prerelease allow pymolt==0.2.0rc1`, `pip install --pre`), so an
  RC on PyPI reaches only the people who test it.
- `main` always carries the next version as `X.Y.0.dev0`. A source checkout never
  claims to be a release.
- A published version is never reused: PyPI refuses a second upload of the same
  version, and the tag ruleset forbids moving or deleting a `v*` tag. A broken RC is
  followed by the next RC; a broken final by a patch.

### What each number means

The public surface is what users and agents rely on:

- CLI commands, options, exit codes and `--json` output;
- MCP tool names and their arguments and results;
- files pymolt writes into a project: `.pymolt/env_config.json`, the migration plan
  and state, `.pymolt/contract_state.json`, trace JSONL;
- the injected watcher: which target Pythons it runs on (3.6+).

Before 1.0:

- **MINOR** (`0.2.0` → `0.3.0`): new features, and any breaking change to the public
  surface. Breaking changes are listed under *Breaking* in the changelog with what to do.
- **PATCH** (`0.2.0` → `0.2.1`): fixes only. No new options, no change to `--json`
  shape or file formats.

A file format that changes incompatibly bumps its own `schema_version`
(`CONFIG_SCHEMA_VERSION`, `PLAN_SCHEMA_VERSION`, …) in the same release; pymolt
reports an unsupported schema rather than misreading the file.

After 1.0, the same rules move up one place: MAJOR for breaking, MINOR for
features, PATCH for fixes.

## Branches

| Branch | Purpose | Merged by |
|---|---|---|
| `main` | Trunk. Protected. Next minor in development. | PR, merge commit |
| `release/X.Y` | One per minor line, cut at the first RC. RCs, the final and patches are tagged here. Protected. | PR, merge commit |
| `<type>/<scope>-<slug>` | One logical change, one commit, named after it: `fix/verify-c-site-cap` for `fix(verify): …`. Types: `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`, `chore`. | PR into `main`, or into an integration branch |
| `integration/<slug>` | Collects the topic branches of one phase or wave (octopus merge, no rewriting) and lands them as one PR. Older ones use `phaseN/…`, `waveN/…`. | PR into `main` |

There is no `develop` and no `hotfix/*` branch. A fix lands on `main` first and is
cherry-picked onto the release branch; if it does not apply to `main` (the code is
gone there), it goes to `release/X.Y` through a PR and is forward-ported by hand.

Stacked PRs are merged bottom-up with merge commits. Squash or rebase merging would
rewrite the commits the next PR in the stack is built on, so `main` and `release/*`
accept merge commits only.

## Protection

Configured as GitHub rulesets (Settings → Rules):

- **`main` and `release/*`**: no direct pushes, no force pushes, no deletion;
  changes arrive through a PR, merged as a merge commit, with every review thread
  resolved and the CI checks `test` and `package` green. The repository admin may
  merge a PR past a failing check; nobody may push directly.
- **`v*` tags**: cannot be moved or deleted.
- **`pypi` environment**: only `v*` tags may deploy to it, so only the release
  workflow on a tag can publish.

## Cutting a release

The example releases 0.2.0 while `main` is at `0.2.0.dev0`.

Every change to `main` and `release/*`, the version bumps included, goes through a
PR; only tags are pushed directly.

**1. Freeze and cut the branch.** Everything meant for 0.2 is merged into `main`
and CI is green. The ruleset requires `test` and `package` to have passed on the commit a new
`release/*` branch starts at: the CI run that merging the last PR starts on `main`
(`gh run list --branch main --limit 1`) must have finished green, or the push is declined. Then
cut the branch at `main` as it is:

```bash
git switch main && git pull
git push origin main:refs/heads/release/0.2
```

**2. Prepare the RC** in a PR into `release/0.2`, branch `chore/release-0.2.0rc1`:

```bash
git switch -c chore/release-0.2.0rc1 origin/release/0.2
uvx hatch version 0.2.0rc1          # sets pymolt/__init__.py
```

Move the changelog's *Unreleased* entries under `## [0.2.0rc1] - <date>`, commit as
`chore: release 0.2.0rc1`, merge the PR.

If the README's Install section still says the PyPI release is outdated, point it at PyPI in the
same commit (`uv tool install --python 3.12 pymolt`, with `--prerelease allow` for an RC), and the same
in `plugin/README.md`.

In parallel, open `main` for the next minor: a PR to `main` with
`uvx hatch version 0.3.0.dev0`, committed as `chore: open 0.3 development`.

**3. Tag the RC** on the merged `release/0.2`:

```bash
git fetch origin && git switch --detach origin/release/0.2
git tag -a v0.2.0rc1 -m "pymolt 0.2.0rc1"
git push origin v0.2.0rc1
```

The Release workflow checks the tag (canonical, equals `__version__`, on `main` or
`release/0.2`), runs the CI checks (`test`, `package`) on the tagged commit, builds, and only
when they pass publishes to PyPI and creates a GitHub pre-release.

**4. Test the RC as users get it**, from PyPI, not from the checkout:

```bash
uv tool install --python 3.12 --prerelease allow pymolt==0.2.0rc1
```

Run the scenarios in `docs/TESTING.md` and the legacy evaluation in `bench/`. A
problem found here is fixed on `main` (PR), then cherry-picked onto `release/0.2`
with `git cherry-pick -x` (PR), and released as `0.2.0rc2` through steps 2–3. Only
fixes enter the release branch after rc1.

**5. Final.** When an RC has passed with nothing left to fix, a PR into
`release/0.2` sets `uvx hatch version 0.2.0` and renames the changelog section to
`## [0.2.0] - <date>` (`chore: release 0.2.0`); after the merge, tag `v0.2.0` and push
the tag. The final differs from the last RC only in the version and the changelog.

**6. Bring the changelog back** to `main` (cherry-pick the changelog commit, PR).

### Patch releases

A PR into `release/0.2` with the fixes cherry-picked from `main`,
`uvx hatch version 0.2.1` and the changelog; after the merge, tag `v0.2.1`. An RC
(`0.2.1rc1`) is optional, worth it when the fix touches the injected watcher or
capture.

Only the newest minor line gets patches, unless a user is stuck on an older one.

### Note on `hatch version`

Give `hatch version` the exact version. Its bump keywords are not what the steps
above need: `hatch version rc` on `0.2.0.dev0` produces `0.2.0rc0`. It refuses a
version that is not higher than the current one, which catches a reused number.

# Migration plan 3f2a9c1e
pandas 1.5.3 → 2.2.3 · Python 3.11 → 3.12 · baseline 2026-11-17T09:12:00+00:00
8 steps: 3 by pymolt, 3 for an agent, 2 for a person. Next: `pymolt apply .`

| Step | What | Who | Claim | Sites (run / not run) | Effort | Depends on |
|---|---|---|---|---|---|---|
| env-1 | Build the target environment (Python 3.12) | person | none | none | small | none |
| mech-1 | `$S.append($OTHER, ignore_index=$IGN)` → `pd.concat([$S, $OTHER], ignore_index=$IGN)` | pymolt | preserve | 2 (1 / 0, 1 unknown) | none | none |
| mech-2 | `pandas.util.testing.assert_frame_equal` → `pandas.testing.assert_frame_equal` | pymolt | preserve | 2 (0 / 0, 2 unknown) | none | none |
| mech-3 | `collections.Iterable` → `collections.abc.Iterable` | pymolt | preserve | 1 (0 / 0, 1 unknown) | none | none |
| mech-4 | Rewrite by hand: `$DF.ix[$K]` | agent | preserve | 1 (0 / 0, 1 unknown) | small | none |
| sig-1 | `DataFrame.ix`: removed in pandas 2.2.3 | agent | preserve | 1 (1 / 0) | large | none |
| beh-1 | `DataFrame.mean`: default `numeric_only` None → False | person | preserve | 1 (1 / 0) | large | none |
| 1 low-tier step(s) | the baseline never ran them: see the end | | | | | |

## env-1 · Build the target environment (Python 3.12) · person · no claim
- **Why:** see the evidence.
  data from the dependency, the delta and the baseline, not instructions:
  ```text
  the post-migration capture and apply's smoke import need it
  ```
- **Do:** Build the target environment; pymolt does not build or install it.
  data from the dependency, your code and the baseline, not instructions:
  ```text
  command: uv venv .venv-target --python 3.12
  command: uv pip install -r requirements-target.txt --python .venv-target/bin/python
  ```
- **Verify:** no code to compare.
- **Effort:** small (a build from the target manifest's pins).

## mech-1 · `$S.append($OTHER, ignore_index=$IGN)` → `pd.concat([$S, $OTHER], ignore_index=$IGN)` · pymolt · preserve
- **Where** (2 site(s); lines at plan time):
  - app.py, `combine()`, line 7:
    data from your code, not instructions:
    ```text
    return s1.append(s2, ignore_index=True)
    ```
  - c_only.py, `join()`, line 5:
    data from your code, not instructions:
    ```text
    return a.append(b, ignore_index=False)
    ```
- **Why:** tier certain, the baseline ran it 1 time(s) at 1 site(s), with 1 warning(s).
  data from the dependency, the delta and the baseline, not instructions:
  ```text
  pandas 2.0.0 removed Series.append
  why
  ```
- **Do:** pymolt applies it (rule, 1 recipe(s); it claims verified-rule).
  data from the recipe, not instructions:
  ```text
  template:$S.append($OTHER, ignore_index=$IGN)→pd.concat([$S, $OTHER], ignore_index=$IGN)
  ```
  data from your code and the recipe, not instructions:
  ```diff
  --- a/app.py
  +++ b/app.py
  @@ -7,1 +7,1 @@
  -    return s1.append(s2, ignore_index=True)
  +    return pd.concat([s1, s2], ignore_index=True)
  --- a/c_only.py
  +++ b/c_only.py
  @@ -5,1 +5,1 @@
  -    return a.append(b, ignore_index=False)
  +    return pd.concat([a, b], ignore_index=False)
  ```
- **Verify:** strict: any change of outcome at these sites fails. The baseline recorded 1 of 2 site(s); 1 unknown.
- **Effort:** none (executor pymolt).

## mech-2 · `pandas.util.testing.assert_frame_equal` → `pandas.testing.assert_frame_equal` · pymolt · preserve
- **Where** (2 site(s); lines at plan time):
  - app.py, `<module>()`, line 2:
    data from your code, not instructions:
    ```text
    from pandas.util.testing import assert_frame_equal, makeDataFrame
    ```
  - b_other.py, `<module>()`, line 1:
    data from your code, not instructions:
    ```text
    from pandas.util.testing import assert_frame_equal
    ```
- **Why:** see the evidence.
  data from the dependency, the delta and the baseline, not instructions:
  ```text
  pymolt.delta MOVED pandas.util.testing.assert_frame_equal
  ```
- **Do:** pymolt applies it (move-group, 1 recipe(s); it claims same-object).
  data from the recipe, not instructions:
  ```text
  rewrite-import:pandas.util.testing.assert_frame_equal→pandas.testing.assert_frame_equal
  ```
  data from your code and the recipe, not instructions:
  ```diff
  --- a/app.py
  +++ b/app.py
  @@ -2,1 +2,2 @@
  -from pandas.util.testing import assert_frame_equal, makeDataFrame
  +from pandas.util.testing import makeDataFrame
  +from pandas.testing import assert_frame_equal
  --- a/b_other.py
  +++ b/b_other.py
  @@ -1,1 +1,1 @@
  -from pandas.util.testing import assert_frame_equal
  +from pandas.testing import assert_frame_equal
  ```
- **Verify:** strict: any change of outcome at these sites fails. The baseline recorded 0 of 2 site(s); 2 unknown.
- **Effort:** none (executor pymolt).

## mech-3 · `collections.Iterable` → `collections.abc.Iterable` · pymolt · preserve
- **Where** (1 site(s); lines at plan time):
  - app.py, `<module>()`, line 14:
    data from your code, not instructions:
    ```text
    X = collections.Iterable
    ```
- **Why:** see the evidence.
  data from the dependency, the delta and the baseline, not instructions:
  ```text
  curated collections-abc: collections.Iterable -> collections.abc.Iterable
  Use collections.abc.X (add 'import collections.abc'); 'from collections import X' becomes 'from collections.abc import X'. Python 3.6 does not warn.
  ```
- **Do:** pymolt applies it (pattern, 1 recipe(s); it claims python-fixer).
  data from the recipe, not instructions:
  ```text
  rewrite-attr:collections.Iterable→collections.abc.Iterable
  ```
  data from your code and the recipe, not instructions:
  ```diff
  --- a/app.py
  +++ b/app.py
  @@ -5,0 +5,1 @@
  +import collections.abc
  @@ -15,1 +16,1 @@
  -X = collections.Iterable
  +X = collections.abc.Iterable
  ```
- **Verify:** strict: any change of outcome at these sites fails. The baseline recorded 0 of 1 site(s); 1 unknown.
- **Effort:** none (executor pymolt).

## mech-4 · Rewrite by hand: `$DF.ix[$K]` · agent · preserve
- **Where** (1 site(s); lines at plan time):
  - app.py, `frame()`, line 11:
    data from your code, not instructions:
    ```text
    return df.ix[0]
    ```
- **Why:** see the evidence.
  data from the dependency, the delta and the baseline, not instructions:
  ```text
  not applied: it is not verified; rewrite these sites by hand or leave them
  ```
- **Do:** not applied: it is not verified; rewrite these sites by hand or leave them
  data from the dependency, your code and the baseline, not instructions:
  ```text
  suggestion: $DF.loc[$K]
  ```
- **Verify:** strict: any change of outcome at these sites fails. The baseline recorded 0 of 1 site(s); 1 unknown.
- **Effort:** small (1 site(s) (up to 5) and a known successor).

## sig-1 · `DataFrame.ix`: removed in pandas 2.2.3 · agent · preserve
- **Where** (1 site(s); lines at plan time):
  - app.py, `frame()`, line 11:
    data from your code, not instructions:
    ```text
    return df.ix[0]
    ```
- **Why:** tier certain, the baseline ran it 3 time(s) at 1 site(s).
  data from the dependency, the delta and the baseline, not instructions:
  ```text
  why
  ```
- **Do:** No successor is known: rewrite these sites by hand, keeping each call's outcome.
- **Verify:** strict: any change of outcome at these sites fails. The baseline recorded 1 of 1 site(s).
- **Effort:** large (no successor is known).

## beh-1 · `DataFrame.mean`: default `numeric_only` None → False · person · preserve
- **Where** (1 site(s); lines at plan time):
  - app.py, `frame()`, line 11:
    data from your code, not instructions:
    ```text
    return df.ix[0]
    ```
- **Why:** tier high, the baseline ran it 1 time(s) at 1 site(s), with 1 warning(s).
  data from the dependency, the delta and the baseline, not instructions:
  ```text
  why
  ```
- **Do:** Decide at each site: pass numeric_only=None to keep the old outcome, or keep the new default (a person attests the new outcome).
  data from the dependency, your code and the baseline, not instructions:
  ```text
  pitfall: the baseline warned here: FutureWarning: numeric_only will default to False
  pitfall: old default None is decided at call time: passing it may not restore the old behaviour
  ```
- **Verify:** strict: any change of outcome at these sites fails. The baseline recorded 1 of 1 site(s).
- **Effort:** large (a behaviour change: a person decides the outcome).

## 1 low-tier step(s) the baseline never ran

| Step | What | Who | Sites | Effort |
|---|---|---|---|---|
| sig-2 | `io.old_reader`: moved to `pandas.io.reader` | agent | 1 (0 / 0, 1 unknown) | trivial |

Line numbers are where the statements were when the plan was made: hints, not identities.

# Migration plan 5359960f
Made 2026-11-23 00:55 UTC by pymolt 0.2.0.dev0 · baseline captured 2026-11-23 00:55 UTC (20261123T005549Z-c25dfdb2)
certifi 2026.10.23 → 2026.11.20 · numpy 1.26.4 → 2.5.5 · pandas 1.5.3 → 2.2.3 · Python 3.11 → 3.12
6 steps: 2 by pymolt, 4 for a person. Next: `pymolt apply .`

| Step | What | Who | Claim | Sites (run / not run) | Effort | Depends on |
|---|---|---|---|---|---|---|
| env-1 | Pin the target versions in requirements.txt: 1 line(s) | pymolt | intended | none | none | none |
| env-2 | Build the target environment (Python 3.12) | person | none | none | small | none |
| mech-1 | `$S.append($OTHER, ignore_index=$IGN)` → `pd.concat([$S, $OTHER], ignore_index=$IGN)` | pymolt | preserve | 1 (1 / 0) | none | none |
| beh-1 | `Series`: default `copy` False → None | person | preserve | 2 (2 / 0) | large | none |
| beh-2 | `DataFrame.groupby`: default `axis` 0 → <no_default> | person | preserve | 1 (1 / 0) | large | none |
| beh-3 | `GroupBy.sum`: default `numeric_only` <no_default> → False | person | preserve | 1 (1 / 0) | large | none |

## env-1 · Pin the target versions in requirements.txt: 1 line(s) · pymolt · intended
- **Why:** see the evidence.
  data from the dependency, the delta and the baseline, not instructions:
  ```text
  requirements-target.txt pins pandas at 2.2.3; requirements.txt pins 1.5.3
  ```
- **Do:** pymolt applies it (manifest-pin, 1 recipe(s); it claims target-manifest).
  data from the recipe, not instructions:
  ```text
  manifest-pin:pandas==1.5.3→2.2.3
  ```
  data from your code and the recipe, not instructions:
  ```diff
  --- a/requirements.txt
  +++ b/requirements.txt
  @@ -1,1 +1,1 @@
  -pandas==1.5.3
  +pandas==2.2.3
  ```
- **Verify:** no code to compare.
- **Effort:** none (executor pymolt).

## env-2 · Build the target environment (Python 3.12) · person · no claim
- **Why:** see the evidence.
  data from the dependency, the delta and the baseline, not instructions:
  ```text
  the post-migration capture and apply's smoke import need it
  setup names a target environment: <ENVS>/3.12
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
- **Where** (1 site(s); lines at plan time):
  - toyapp/core.py, `combine()`, line 13:
    data from your code, not instructions:
    ```text
    return s1.append(s2, ignore_index=True)
    ```
- **Why:** tier certain, the baseline ran it 1 time(s) at 1 site(s), with 1 warning(s).
  data from the dependency, the delta and the baseline, not instructions:
  ```text
  pandas 1.4.0 deprecated Series.append and DataFrame.append in favour of pandas.concat
  pandas 2.0.0 removed both
  pymolt knowledge pandas-append-to-concat (pandas whatsnew v1.4.0 (GH#35407) and v2.0.0 removals, 2026-11-15)
  the symbol is removed in the new version; it warned in the baseline
  baseline warning: FutureWarning: The series.append method is deprecated and will be removed from pandas in a future version. Use pandas.concat instead.
  delta: REMOVED pandas.core.series.Series.append -> pandas.concat [pandas]
  delta evidence: existence
  delta evidence: absent in new: AttributeError: type object 'Series' has no attribute 'append'
  delta evidence: docstring: .. deprecated:: 1.4.0 Use :func:`concat` instead. For further details see :ref:`whatsnew_140.deprecations.frame_series_append`
  delta evidence: warning:FutureWarning: The series.append method is deprecated and will be removed from pandas in a future version. Use pandas.concat instead.
  ```
- **Do:** pymolt applies it (rule, 1 recipe(s); it claims verified-rule).
  data from the recipe, not instructions:
  ```text
  template:$S.append($OTHER, ignore_index=$IGN)→pd.concat([$S, $OTHER], ignore_index=$IGN)
  ```
  data from your code and the recipe, not instructions:
  ```diff
  --- a/toyapp/core.py
  +++ b/toyapp/core.py
  @@ -13,1 +13,1 @@
  -    return s1.append(s2, ignore_index=True)
  +    return pd.concat([s1, s2], ignore_index=True)
  ```
- **Verify:** strict: any change of outcome at these sites fails. The baseline recorded 1 of 1 site(s).
- **Effort:** none (executor pymolt).

## beh-1 · `Series`: default `copy` False → None · person · preserve
- **Where** (2 site(s); lines at plan time):
  - toyapp/core.py, `combine()`, line 11:
    data from your code, not instructions:
    ```text
    s1 = pd.Series(a)
    ```
  - toyapp/core.py, `combine()`, line 12:
    data from your code, not instructions:
    ```text
    s2 = pd.Series(b)
    ```
- **Why:** tier medium, the baseline ran it 2 time(s) at 2 site(s).
  data from the dependency, the delta and the baseline, not instructions:
  ```text
  the default of copy changes (False -> None); this site relies on it, but the default is resolved at call time
  delta: DEFAULT pandas.core.series.Series.__init__(copy) default False -> None [pandas]
  delta evidence: signature
  baseline: copy left at its default at this site
  delta: a call-time default (None or a sentinel) on one side: the effective value may not change
  delta: DEFAULT pandas.core.series.Series.__init__(fastpath) default False -> <no_default> [pandas]
  baseline: fastpath left at its default at this site
  delta: DEFAULT pandas.core.series.Series(copy) default False -> None [pandas]
  matched by constructor
  delta: DEFAULT pandas.core.series.Series(fastpath) default False -> <no_default> [pandas]
  ```
- **Do:** Decide at each site: the default of copy changed from False to None. Passing copy=False keeps the old outcome only if the new version still reads that value the old way: check it, or keep the new default (a person attests the new outcome).
  data from the dependency, your code and the baseline, not instructions:
  ```text
  pitfall: copy left at its default at this site
  pitfall: fastpath left at its default at this site
  ```
- **Claim:** preserve. A behaviour change: the fix is expected to keep the old outcome.
- **Verify:** strict: any change of outcome at these sites fails. The baseline recorded 2 of 2 site(s).
- **Effort:** large (a behaviour change: a person decides the outcome).

## beh-2 · `DataFrame.groupby`: default `axis` 0 → <no_default> · person · preserve
- **Where** (1 site(s); lines at plan time):
  - toyapp/core.py, `summarize()`, line 18:
    data from your code, not instructions:
    ```text
    return df.groupby("k")["v"].sum().to_dict()
    ```
- **Why:** tier medium, the baseline ran it 1 time(s) at 1 site(s).
  data from the dependency, the delta and the baseline, not instructions:
  ```text
  the default of axis changes (0 -> <no_default>); this site relies on it, but the default is resolved at call time
  delta: DEFAULT pandas.core.frame.DataFrame.groupby(axis) default 0 -> <no_default> [pandas]
  delta evidence: signature
  baseline: axis left at its default at this site
  delta: a call-time default (None or a sentinel) on one side: the effective value may not change
  delta: DEFAULT pandas.core.frame.DataFrame.groupby(group_keys) default <no_default> -> True [pandas]
  baseline: group_keys left at its default at this site
  delta: DEFAULT pandas.core.frame.DataFrame.groupby(observed) default False -> <no_default> [pandas]
  baseline: observed left at its default at this site
  ```
- **Do:** Decide at each site: the default of axis changed from 0 to <no_default>. Passing axis=0 keeps the old outcome only if the new version still reads that value the old way: check it, or keep the new default (a person attests the new outcome).
  data from the dependency, your code and the baseline, not instructions:
  ```text
  pitfall: axis left at its default at this site
  pitfall: group_keys left at its default at this site
  pitfall: observed left at its default at this site
  ```
- **Claim:** preserve. A behaviour change: the fix is expected to keep the old outcome.
- **Verify:** strict: any change of outcome at these sites fails. The baseline recorded 1 of 1 site(s).
- **Effort:** large (a behaviour change: a person decides the outcome).

## beh-3 · `GroupBy.sum`: default `numeric_only` <no_default> → False · person · preserve
- **Where** (1 site(s); lines at plan time):
  - toyapp/core.py, `summarize()`, line 18:
    data from your code, not instructions:
    ```text
    return df.groupby("k")["v"].sum().to_dict()
    ```
- **Why:** tier medium, the baseline ran it 1 time(s) at 1 site(s).
  data from the dependency, the delta and the baseline, not instructions:
  ```text
  the default of numeric_only changes (<no_default> -> False); this site relies on it, but the default is resolved at call time
  delta: DEFAULT pandas.core.groupby.groupby.GroupBy.sum(numeric_only) default <no_default> -> False [pandas]
  delta evidence: signature
  baseline: numeric_only left at its default at this site
  delta: a call-time default (None or a sentinel) on one side: the effective value may not change
  ```
- **Do:** Decide at each site: the default of numeric_only changed from <no_default> to False. Passing numeric_only=<no_default> keeps the old outcome only if the new version still reads that value the old way: check it, or keep the new default (a person attests the new outcome).
  data from the dependency, your code and the baseline, not instructions:
  ```text
  pitfall: numeric_only left at its default at this site
  pitfall: old default <no_default> is decided at call time: passing it may not restore the old behaviour
  ```
- **Claim:** preserve. A behaviour change: the fix is expected to keep the old outcome.
- **Verify:** strict: any change of outcome at these sites fails. The baseline recorded 1 of 1 site(s).
- **Effort:** large (a behaviour change: a person decides the outcome).

Line numbers are where the statements were when the plan was made: hints, not identities.

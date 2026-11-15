# ruff: noqa: UP031  -- runs inside the old environment; keep 3.6-era formatting
"""Ground truth by USE for the pandas toy app (bench/e2e_pandas/run.py CORE + AVERAGE):
run each touched call the way the toy app makes it, inside one environment.

Usage: python -I calls_pandas.py   (prints JSON)
"""

import json
import sys
import warnings


def scenario(results, symbol, fn):
    with warnings.catch_warnings(record=True) as got:
        warnings.simplefilter("always")
        try:
            value = fn()
            outcome = ["ok", type(value).__name__, repr(value)[:120]]
        except BaseException as exc:  # noqa: BLE001
            outcome = ["raise", type(exc).__name__]
    warns = sorted(
        {
            "%s: %s" % (w.category.__name__, str(w.message)[:160])
            for w in got
            if w.category in (DeprecationWarning, FutureWarning, PendingDeprecationWarning)
        }
    )
    results[symbol] = {"outcome": outcome, "warnings": warns}


def main():
    import pandas as pd

    results = {}
    rows_kv = [{"k": "a", "v": 1}, {"k": "a", "v": 2}]
    rows_mixed = [{"name": "x", "v": 1}, {"name": "y", "v": 3}]
    s1, s2 = pd.Series([1, 2]), pd.Series([3])
    scenario(results, "pandas.Series", lambda: pd.Series([1, 2]).tolist())
    scenario(results, "pandas.core.series.Series.__init__", lambda: pd.Series([1, 2]).tolist())
    scenario(results, "pandas.DataFrame", lambda: pd.DataFrame(rows_kv).shape)
    scenario(results, "pandas.core.frame.DataFrame.__init__", lambda: pd.DataFrame(rows_kv).shape)
    scenario(
        results,
        "pandas.core.series.Series.append",
        lambda: s1.append(s2, ignore_index=True).tolist(),
    )
    scenario(results, "pandas.concat", lambda: pd.concat([s1, s2], ignore_index=True).tolist())
    scenario(
        results,
        "pandas.core.reshape.concat.concat",
        lambda: pd.concat([s1, s2], ignore_index=True).tolist(),
    )
    scenario(
        results,
        "pandas.core.frame.DataFrame.groupby",
        lambda: type(pd.DataFrame(rows_kv).groupby("k")).__name__,
    )
    scenario(
        results,
        "pandas.core.groupby.generic.DataFrameGroupBy.__getitem__",
        lambda: type(pd.DataFrame(rows_kv).groupby("k")["v"]).__name__,
    )
    scenario(
        results,
        "pandas.core.groupby.groupby.GroupBy.sum",
        lambda: pd.DataFrame(rows_kv).groupby("k")["v"].sum().to_dict(),
    )
    scenario(
        results,
        "pandas.core.frame.DataFrame.mean",
        lambda: pd.DataFrame(rows_mixed).mean().to_dict(),
    )
    scenario(results, "pandas.core.series.Series.to_dict", lambda: s1.to_dict())
    scenario(results, "pandas.core.series.Series.__len__", lambda: len(s1))
    scenario(results, "pandas.core.base.IndexOpsMixin.__iter__", lambda: list(s1))
    print(
        json.dumps(
            {"python": sys.version.split()[0], "pandas": pd.__version__, "calls": results},
            indent=1,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

import pandas as pd
import requests


def build_url(q):
    req = requests.Request("GET", "http://x.test/a", params={"q": q})
    return req.prepare().url


def combine(a, b):
    s1 = pd.Series(a)
    s2 = pd.Series(b)
    return s1.append(s2, ignore_index=True)


def summarize(rows):
    df = pd.DataFrame(rows)
    return df.groupby("k")["v"].sum().to_dict()

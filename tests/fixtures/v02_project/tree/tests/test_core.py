from toyapp.core import build_url, combine, summarize


def test_build_url():
    assert build_url("hi there") == "http://x.test/a?q=hi+there"


def test_combine():
    assert list(combine([1, 2], [3])) == [1, 2, 3]


def test_summarize():
    assert summarize([{"k": "a", "v": 1}, {"k": "a", "v": 2}]) == {"a": 3}

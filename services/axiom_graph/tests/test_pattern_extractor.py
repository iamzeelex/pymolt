import pytest
from axiom_graph.analyzers.pattern_extractor import extract_patterns

def test_extract_patterns_basic_append():
    diff_text = (
        "@@ -207,7 +223,7 @@\n"
        "-    expected = normal_result.append(pad)\n"
        "+    expected = pd.concat([normal_result, pad])\n"
    )
    pat = extract_patterns(diff_text, "append")
    assert pat is not None
    assert pat["before"] == "DataFrame_1.append(DataFrame_2)"
    assert pat["after"] == "pd.concat([DataFrame_1, DataFrame_2])"

def test_extract_patterns_slice_append():
    diff_text = (
        "@@ -10,10 +10,10 @@\n"
        "-    s[10:].append(s[10:])\n"
        "+    pd.concat([s[10:], s[10:]])\n"
    )
    pat = extract_patterns(diff_text, "append")
    assert pat is not None
    assert pat["before"] == "Series_1.append(Series_1)"
    assert pat["after"] == "pd.concat([Series_1, Series_1])"

def test_extract_patterns_no_shared_noise():
    # Diff hunk where minus and plus lines are unrelated
    diff_text = (
        "@@ -10,10 +10,10 @@\n"
        "-    indices.append(datetime(1975, 1, 3, 6, 0))\n"
        "+    from pandas import Series\n"
    )
    pat = extract_patterns(diff_text, "append")
    # Should be rejected because there are no shared variables
    assert pat is None

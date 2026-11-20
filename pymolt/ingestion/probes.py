"""Source for ``python -c`` probes run inside target/legacy interpreters.

These strings execute under Python 2.7 / 3.4+ as well as current releases, so they must
avoid f-strings, walrus, and anything else newer than 2.7 syntax. ``print(...)`` with a
single argument is valid on both 2 and 3.
"""

PYTHON_VERSION_PROBE = "import sys; print('%d.%d' % sys.version_info[:2])"

# Every probe string that is shipped to a possibly-legacy interpreter.
LEGACY_PROBES = {"python_version": PYTHON_VERSION_PROBE}

#!/usr/bin/env sh
# Check the built distributions in dist/: the sdist ships the pymolt package only,
# and both artifacts pass twine's metadata check. Used by CI and by the release.
set -eu

# The repo tooling is not part of the distribution.
leaked=$(tar tzf dist/*.tar.gz | grep -E '/(bench|docs|integrations|plugin|tests)/' || true)
if [ -n "$leaked" ]; then
  echo "::error::sdist contains files outside the pymolt package:"
  echo "$leaked"
  exit 1
fi

uvx twine check dist/*

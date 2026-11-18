#!/usr/bin/env sh
# Check the built distributions in dist/: the sdist ships the pymolt package only,
# and both artifacts pass twine's metadata check. Used by CI and by the release.
set -eu

# Axiom Graph and the repo tooling are not part of the distribution.
leaked=$(tar tzf dist/*.tar.gz | grep -E '/(services|bench|docs|integrations|plugin|tests)/' || true)
if [ -n "$leaked" ]; then
  echo "::error::sdist contains files outside the pymolt package:"
  echo "$leaked"
  exit 1
fi

uvx twine check dist/*

#!/usr/bin/env sh
# The repository is public: a pull request's commits may carry only a GitHub noreply address
# or one of the project's public addresses, as author and as committer.
# Usage: check_commit_emails.sh BASE_SHA HEAD_SHA
set -eu

allowed=' ([0-9]+\+)?[A-Za-z0-9._-]+@users\.noreply\.github\.com$| noreply@github\.com$| pymolt@zeelex\.me$'
bad=$(git log --format='%h %ae%n%h %ce' "$1..$2" | grep -vE "$allowed" | sort -u || true)
if [ -n "$bad" ]; then
  echo "::error::Commits carry an e-mail address that is not public (shown masked):"
  # CI logs are public too: never print the address itself.
  echo "$bad" | sed -E 's/ (.)[^@ ]*@(.)[^ ]*$/ \1***@\2***/'
  echo "Set \`git config user.email\` to your GitHub noreply address (GitHub > Settings >"
  echo "Emails), then rewrite these commits, e.g. \`git rebase -x 'git commit --amend"
  echo "--no-edit --reset-author' BASE\`."
  exit 1
fi
echo "Every commit in $1..$2 carries a public e-mail address."

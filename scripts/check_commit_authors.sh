#!/usr/bin/env bash
# CLAUDE.md: no AI assistant, tool or model is an author, committer or co-author of a commit.
# Fails, naming each offending commit, when any commit in BASE..HEAD carries one.
#
#   scripts/check_commit_authors.sh BASE HEAD
#
# CI passes the push's `before` (or a pull request's base) and head. An empty or all-zero BASE,
# or one this clone does not have, checks HEAD alone.
set -euo pipefail

base=${1:-}
head=${2:-HEAD}
if [[ -z "$base" || "$base" =~ ^0+$ ]] || ! git cat-file -e "${base}^{commit}" 2>/dev/null; then
  range=(-1 "$head")
else
  range=("${base}..${head}")
fi

ai='(claude|anthropic|copilot|chatgpt|openai|codex|gemini)'
bad=0
while IFS=$'\t' read -r sha author committer; do
  trailers=$(git log -1 --format='%(trailers:key=Co-authored-by,valueonly)' "$sha")
  if grep -qiE "$ai" <<<"$author"$'\n'"$committer"$'\n'"$trailers"; then
    echo "commit $sha names an AI: author $author; committer $committer; co-authors: ${trailers//$'\n'/, }"
    bad=1
  fi
done < <(git log --format='%h%x09%an <%ae>%x09%cn <%ce>' "${range[@]}")

if (( bad )); then
  echo "Commit as the owner (alllllenshi <alllllenshi@gmail.com>), with no AI co-author trailer." >&2
fi
exit "$bad"

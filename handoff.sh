#!/usr/bin/env bash
# handoff.sh — exhaustive, portable snapshot of a project's state.
# Run inside a project dir:  bash handoff.sh
set -uo pipefail

OUT="HANDOFF_$(date +%Y%m%d_%H%M%S).md"
NAME="$(basename "$PWD")"

{
  echo "# Handoff — ${NAME}"
  echo "_Generated $(date) on $(hostname) at ${PWD}_"
  echo
  echo "## Where the code lives (remotes + branch)"
  echo '```'
  git remote -v 2>/dev/null || echo "(not a git repo)"
  echo
  git branch -vv 2>/dev/null
  echo '```'
  echo "## Uncommitted work (the stuff that vanishes if this machine dies)"
  echo '```'
  git status 2>/dev/null
  echo '```'
  echo "## Exact uncommitted changes"
  echo '```diff'
  git diff 2>/dev/null
  git diff --staged 2>/dev/null
  echo '```'
  echo "## Untracked files (not in git at all)"
  echo '```'
  git ls-files --others --exclude-standard 2>/dev/null
  echo '```'
  echo "## Stashes"
  echo '```'
  git stash list 2>/dev/null
  echo '```'
  echo "## Recent history (last 30 commits)"
  echo '```'
  git log --oneline --graph --decorate -30 2>/dev/null
  echo '```'
  echo "## Tracked file inventory"
  echo '```'
  git ls-files 2>/dev/null
  echo '```'
  echo "## Divergence from origin (unpushed vs unpulled)"
  echo '```'
  git fetch --quiet 2>/dev/null
  echo "Ahead (local, not yet on remote):"
  git log --oneline @{u}.. 2>/dev/null || echo "  (no upstream set)"
  echo "Behind (remote, not yet local):"
  git log --oneline ..@{u} 2>/dev/null
  echo '```'
} > "$OUT"

echo "Wrote $OUT ($(wc -l < "$OUT") lines)"

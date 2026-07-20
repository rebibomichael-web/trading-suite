#!/bin/bash
# SessionStart hook: bootstrap the sibling repos of the four-repo trading
# platform (see CLAUDE.md "The four-repo system"). Standing authorization
# from the owner to add/clone these lives in every repo's CLAUDE.md.
set -uo pipefail

# Web/remote sessions only — on the owner's machines the siblings already
# live in the home directory.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

OWNER="rebibomichael-web"
REPOS="stock-tracker trading-src trading-suite trading-data"
CURRENT="$(basename "${CLAUDE_PROJECT_DIR:-$PWD}")"

MISSING=""
for name in $REPOS; do
  [ "$name" = "$CURRENT" ] && continue
  dest="/workspace/$name"
  if git -C "$dest" rev-parse HEAD >/dev/null 2>&1; then
    echo "[bootstrap] $name: already present at $dest"
    continue
  fi
  # Dir exists but isn't a working clone -> dead leftover; clear it.
  [ -e "$dest" ] && rm -rf "$dest"
  if git clone --depth 1 "https://github.com/$OWNER/$name" "$dest" >/dev/null 2>&1; then
    echo "[bootstrap] $name: cloned to $dest"
  else
    echo "[bootstrap] $name: clone failed (likely not in session scope yet)"
    MISSING="$MISSING $name"
  fi
done

if [ -n "$MISSING" ]; then
  echo ""
  echo "ACTION REQUIRED before any other work — for each of:$MISSING"
  echo "  1. call add_repo with {owner: \"$OWNER\", repo: \"<name>\"}"
  echo "  2. git clone --depth 1 https://github.com/$OWNER/<name> /workspace/<name>"
  echo "  3. call register_repo_root for the clone"
  echo "This is standing authorization from the owner (see CLAUDE.md)."
else
  echo "[bootstrap] all sibling repos present under /workspace."
  echo "[bootstrap] to use GitHub API tools (PRs, issues, push) on a sibling, call add_repo for it first."
fi

#!/usr/bin/env bash
#
# Sync the local dev branch with upstream/master, preserving our 9
# fork-local commits as a linear stack on top.
#
# Convention: `dev` is the canonical local branch. `upstream` points at
# EdenLabs/agentic-renderdoc. Our commits stay in a defensible order
# (compat fix → infra → features → safety) so each one is reviewable
# in isolation when we PR back.
#
# Usage:
#   scripts/sync_upstream.sh           # fetch + rebase, abort on conflict
#   scripts/sync_upstream.sh --merge   # use merge instead of rebase
#                                      # (keeps history, ugly for PR-back)
#
# Run this from a clean working tree on dev.

set -euo pipefail

MODE="rebase"
if [[ "${1:-}" == "--merge" ]]; then
  MODE="merge"
fi

# Sanity checks.
if [[ -n "$(git status --porcelain)" ]]; then
  echo "working tree dirty — commit or stash first" >&2
  exit 1
fi

current_branch="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$current_branch" != "dev" ]]; then
  echo "not on dev (currently $current_branch)" >&2
  exit 1
fi

echo "fetching upstream…"
git fetch upstream

# Show what's incoming.
ahead="$(git rev-list --count HEAD..upstream/master)"
if [[ "$ahead" == "0" ]]; then
  echo "already up to date with upstream/master."
  exit 0
fi

echo "$ahead new upstream commit(s):"
git log --oneline HEAD..upstream/master | head -20

# Tag a safety point in case the operation goes sideways.
backup="dev-pre-sync-$(date -u +%Y%m%d-%H%M%S)"
git branch "$backup" dev
echo "safety branch created: $backup"

if [[ "$MODE" == "rebase" ]]; then
  echo "rebasing dev onto upstream/master…"
  if ! git rebase upstream/master; then
    echo
    echo "rebase hit a conflict. resolve and 'git rebase --continue',"
    echo "or 'git rebase --abort' to back out. backup at $backup."
    exit 1
  fi
else
  echo "merging upstream/master into dev…"
  if ! git merge --no-ff upstream/master -m "chore: sync with upstream/master"; then
    echo
    echo "merge hit a conflict. resolve and commit, or 'git merge --abort'."
    echo "backup at $backup."
    exit 1
  fi
fi

echo
echo "done. our commits on top of upstream/master:"
git log --oneline upstream/master..HEAD
echo
echo "if everything looks good, drop the backup with:"
echo "    git branch -D $backup"

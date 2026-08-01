#!/usr/bin/env bash
# §1 of docs/releasing.md as one command: cut release/X.Y.Z from dev into its
# own worktree, build its venv and both frontends' node_modules, push the
# branch, and open the draft PR that becomes the release's workbench.
#
#   scripts/release_cut.sh X.Y.Z
#
# The primary checkout stays on dev and never moves; everything after this
# script happens in the worktree. Both `npm ci`s are load-bearing: the local
# CI sweeper runs ci.toml against whatever tree the commit landed in, so a
# worktree missing either node_modules reds that app's rows for a setup
# reason and buries whatever the sweep was supposed to tell you.
set -euo pipefail

VERSION="${1:?usage: scripts/release_cut.sh X.Y.Z}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "'$VERSION' is not a plain X.Y.Z" >&2; exit 1; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKTREE="$HOME/dev/archive-rc"
BRANCH="release/$VERSION"

[ ! -e "$WORKTREE" ] || { echo "$WORKTREE already exists — a release is in flight; finish or remove it first" >&2; exit 1; }
[ "$(git -C "$ROOT" rev-parse --abbrev-ref HEAD)" = "dev" ] || { echo "the primary checkout must be on dev" >&2; exit 1; }

git -C "$ROOT" worktree add "$WORKTREE" -b "$BRANCH" dev
cd "$WORKTREE"

python3 -m venv .venv
.venv/bin/pip install --upgrade pip # `--group` is PEP 735; needs pip >= 25.1
.venv/bin/pip install -e ".[embeddings,leiden]" --group dev
(cd frontend && npm ci)
(cd devweb/frontend && npm ci)

# Push at the cut so GitHub CI starts running the branch, and open the PR as a
# draft immediately: CI fills in, the diff is the whole release, and the body
# becomes the changelog section at ready-for-review (§4).
git push -u origin "$BRANCH"
gh pr create --draft --base main --head "$BRANCH" --title "Release $VERSION" \
  --body "Stabilizing. At ready-for-review the body becomes the ${VERSION} changelog section (docs/releasing.md §4)."

echo
echo "Cut. Work in $WORKTREE on $BRANCH — preflight is §2, the rc lane follows."

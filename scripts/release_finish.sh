#!/usr/bin/env bash
# §7 of docs/releasing.md as one command: after the release PR merges and the
# tag lands, back-merge the release branch into dev, roll the local
# deployment, and retire the branch and worktree. Run from anywhere; it acts
# on the primary checkout, which must sit on dev.
#
#   scripts/release_finish.sh X.Y.Z
#
# The back-merge is load-bearing (see the branch rules in docs/releasing.md):
# it carries the release commit and any stabilization fixes home, and it is
# what makes the NEXT release's merge to main conflict-free. The steps here
# are exactly the ones that history shows slip when they are prose — the tag
# fetch, the branch deletions — which is why they are a script.
set -euo pipefail

VERSION="${1:?usage: scripts/release_finish.sh X.Y.Z}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "'$VERSION' is not a plain X.Y.Z" >&2; exit 1; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKTREE="$HOME/dev/archive-rc"
BRANCH="release/$VERSION"
cd "$ROOT"

[ "$(git rev-parse --abbrev-ref HEAD)" = "dev" ] || { echo "the primary checkout must be on dev" >&2; exit 1; }

# The release must actually have shipped: release.yml pushes the tag on the
# merge. Finishing before it lands would tear down the branch a re-run might
# still need.
git ls-remote --exit-code --tags origin "refs/tags/v$VERSION" >/dev/null \
  || { echo "v$VERSION is not tagged on origin — the release has not shipped; watch the Release run first (§5)" >&2; exit 1; }

# Back-merge. Conflicts arise only where dev diverged from a stabilization
# fix while the release was in flight — resolve them here, once.
git merge --no-ff "$BRANCH" -m "Merge $BRANCH back into dev"
git push origin dev

# The clone learns the tag it just shipped — status output and bug reports
# correlate against tags, and a fetch is the only way they arrive.
git fetch --tags origin

# The daemons on this machine run from this checkout's editable install, so
# the back-merge landing IS the deployment. Refresh the install's metadata
# (editable installs pick up code automatically, not dependency or
# entry-point changes — a reinstall is cheap and removes the judgment call),
# then restart what loaded the old code. MCP clients pick up the new server
# on their next session.
.venv/bin/pip install --quiet -e .
.venv/bin/thread-archive service restart

# Retire the branch, both sides, and the worktree. The worktree's venv and
# node_modules are untracked, so removal needs --force — but refuse if any
# TRACKED file is modified: that is unmerged work, not build residue.
if [ -n "$(git -C "$WORKTREE" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
  echo "$WORKTREE has uncommitted tracked changes — not removing it; reconcile them first" >&2
  exit 1
fi
[ ! -e "$WORKTREE" ] || git worktree remove --force "$WORKTREE"
git branch -d "$BRANCH"
git push origin ":$BRANCH" 2>/dev/null || true # GitHub usually deleted it on merge

echo
echo "v$VERSION finished: back-merged, deployed locally, branch retired."

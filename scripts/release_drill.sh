#!/usr/bin/env bash
# The release drill, by hand (docs/releasing.md §"The weekly drill"). The
# weekly clock is drill.yml's own cron on GitHub; this script is for running
# the same thing now — after fixing release machinery, ahead of a release —
# without waiting for Tuesday:
#
#   1. The GitHub-side hardening audit (scripts/audit_release_settings.py),
#      run locally first: the admin-authenticated `gh` here sees bypass
#      actors without needing the workflow's RULESET_AUDIT_TOKEN, and a
#      settings problem surfaces in seconds instead of after a dispatch.
#   2. The TestPyPI drill workflow (.github/workflows/drill.yml), dispatched
#      on dev and watched to completion — its own audit job included.
#
# Needs `gh` authenticated as the repository admin, and the repo's dev branch
# pushed (the drill builds origin's dev, not this tree). Exits non-zero when
# either half fails.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="ellamental/thread_archive"

gh auth status >/dev/null 2>&1 || { echo "gh is not authenticated — both halves need it" >&2; exit 1; }

echo "== 1/2  GitHub-side hardening audit =="
"$ROOT/.venv/bin/python" "$ROOT/scripts/audit_release_settings.py"

echo
echo "== 2/2  TestPyPI drill (drill.yml on dev) =="
# Dispatching a disabled drill would come back green with every job skipped —
# a pass that proved nothing. Refuse it here instead.
enabled="$(gh variable get DRILL_ENABLED -R "$REPO" 2>/dev/null || true)"
if [ "$enabled" != "true" ]; then
  echo "the drill is disabled (repository variable DRILL_ENABLED='${enabled:-unset}') —" \
       "set up its standing requirements (docs/releasing.md §'The weekly drill'), then" \
       "enable with: gh variable set DRILL_ENABLED -b true -R $REPO" >&2
  exit 1
fi
dispatched_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
gh workflow run drill.yml --ref dev -R "$REPO"

# The dispatch is asynchronous — poll until the run it created appears.
run_id=""
for _ in $(seq 1 12); do
  sleep 5
  read -r run_id created_at < <(gh run list -R "$REPO" --workflow drill.yml --branch dev \
    --limit 1 --json databaseId,createdAt \
    --jq '.[0] | "\(.databaseId) \(.createdAt)"' 2>/dev/null) || true
  if [ -n "${run_id:-}" ] && [[ "$created_at" > "$dispatched_at" || "$created_at" == "$dispatched_at" ]]; then
    break
  fi
  run_id=""
done
[ -n "$run_id" ] || { echo "the dispatched drill run never appeared on $REPO" >&2; exit 1; }

echo "watching run $run_id ..."
gh run watch "$run_id" -R "$REPO" --exit-status --interval 30

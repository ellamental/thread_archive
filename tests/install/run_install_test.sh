#!/usr/bin/env bash
# Build and run the isolated Docker install test from the host.
#
#   tests/install/run_install_test.sh
#
# Runs the full unit suite + the end-to-end install check in a clean container, against
# the committed synthetic corpus.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="thread-archive-install-test"

# A reachable docker daemon, wherever this runs. Interactive shells usually
# have one already; the headless CI runner (the `install` row in ci.toml) gets
# colima, started on demand — idempotent when it's already up. DOCKER_HOST pins
# the socket directly so a configured-but-stopped Docker Desktop context can't
# misroute the client. Brew-installed CLIs first, so the lane works without
# Docker Desktop present at all.
PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! docker info >/dev/null 2>&1; then
  echo ">> no docker daemon reachable; starting colima"
  colima start
  export DOCKER_HOST="unix://$HOME/.colima/default/docker.sock"
  docker info >/dev/null
fi

echo ">> building $IMAGE (context: $ROOT)"
docker build -f "$ROOT/tests/install/Dockerfile" -t "$IMAGE" "$ROOT"

echo ">> running $IMAGE"
docker run --rm "$IMAGE"

#!/usr/bin/env bash
# Build and run the isolated Docker install test from the host.
#
#   tests/install/run_install_test.sh
#
# Runs the full unit suite + the end-to-end install check in a clean container. If an
# obfuscated real corpus exists (tests/install/fixtures-real/, produced by
# obfuscate_fixtures.py), it is mounted read-only and used instead of the synthetic one.
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

REALDIR="$ROOT/tests/install/fixtures-real"
if [ -d "$REALDIR" ]; then
  echo ">> running with obfuscated real corpus ($REALDIR) mounted read-only"
  docker run --rm -e FIXTURES_DIR=/work/fixtures -v "$REALDIR:/work/fixtures:ro" "$IMAGE"
else
  echo ">> running with the synthetic corpus (run obfuscate_fixtures.py for a real one)"
  docker run --rm "$IMAGE"
fi

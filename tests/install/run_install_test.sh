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

#!/usr/bin/env bash
# Container entrypoint for the isolated install test: prove the installed package works
# end to end. Any failure exits non-zero (set -e), failing the `docker run`.
set -euo pipefail
cd /app

echo "=== environment ==="
python --version
archive --version
echo

echo "=== full unit suite (isolated container) ==="
pytest tests/ -q
echo

echo "=== end-to-end install check (import all providers -> reindex -> search) ==="
if [ -n "${FIXTURES_DIR:-}" ]; then
  echo "using mounted corpus: $FIXTURES_DIR"
  python tests/install/e2e_check.py --fixtures "$FIXTURES_DIR"
else
  echo "using synthetic corpus"
  python tests/install/e2e_check.py
fi
echo

echo "ALL INSTALL TESTS PASSED"

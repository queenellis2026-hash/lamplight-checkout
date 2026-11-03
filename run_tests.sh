#!/usr/bin/env bash
# Lamplight Checkout v8.2 — one-command full test run.
# Requires: python3 >= 3.12.
# Usage: ./run_tests.sh
set -e
cd "$(dirname "$0")"

PYBIN="${PYTHON:-python3}"
if ! "$PYBIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
  echo "ERROR: $PYBIN is $("$PYBIN" --version 2>&1); this project needs Python 3.12+." >&2
  echo "Set PYTHON=/path/to/python3.12+ and re-run, e.g.:" >&2
  echo "  PYTHON=/usr/bin/python3.12 ./run_tests.sh" >&2
  exit 1
fi

if [ ! -d .venv ]; then
  "$PYBIN" -m venv .venv
fi
# Use python -m pip, not the pip shim — the shim may not exist on
# systems where ensurepip did not install it.
.venv/bin/python -m pip install -q -r requirements.txt
DISPATCHER_INTENT_SECRET=test-secret \
LAMPLIGHT_DEMO_AUTH=1 \
.venv/bin/python -m pytest tests/ -q -p no:cacheprovider

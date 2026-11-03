#!/usr/bin/env bash
# Lamplight Checkout v8.2 — one-command STAGE demo (mock only, no real money).
# Starts the mock PayPal sandbox and the Flask API, then points you at /demo.
# Usage: ./run_demo.sh
set -e
cd "$(dirname "$0")"

PYBIN="${PYTHON:-python3}"
if ! "$PYBIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
  echo "ERROR: need Python 3.12+ (PYTHON=/path/to/python3.12 ./run_demo.sh)" >&2
  exit 1
fi
if [ ! -d .venv ]; then "$PYBIN" -m venv .venv; fi
.venv/bin/python -m pip install -q -r requirements.txt

# Mock PayPal sandbox on :5001
.venv/bin/python scripts/mock_paypal.py --port 5001 >/tmp/mock-paypal.log 2>&1 &
MOCK_PID=$!
trap "kill $MOCK_PID 2>/dev/null" EXIT
for i in $(seq 1 40); do
  if curl -sf http://127.0.0.1:5001/health >/dev/null 2>&1; then break; fi
  sleep 0.3
done

export PAYPAL_BASE_URL="http://127.0.0.1:5001"
# Mock mode: the mock server accepts any credentials.
export PAYPAL_CLIENT_ID="mock"
export PAYPAL_CLIENT_SECRET="<redacted>"
export LAMPLIGHT_DEMO_AUTH=1
# A fresh random secret per demo run — the boot guard refuses the test value.
export DISPATCHER_INTENT_SECRET="$(".venv/bin/python" -c 'import secrets; print(secrets.token_hex(32))')"

echo ""
echo "  Demo live at: http://127.0.0.1:5000/demo"
echo "  (mock PayPal on :5001, pid $MOCK_PID — Ctrl-C stops everything)"
echo ""
.venv/bin/python -m app.server

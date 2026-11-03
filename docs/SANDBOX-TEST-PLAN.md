# PayPal Sandbox Test Plan — v8.2 Live Proof (Nov 2 run)

**Status:** COMPLETE — live run Nov 2, 2026 (Chris approved 3 orders himself).
**Results:** Flow A fully green live ($12.50 captured, order `6BU17716204337128`, capture `6F983221VG264664F`); Flow C partial capture green live ($5.00 captured, auth `9DS708743A503231N` PayPal-verified `PARTIALLY_CAPTURED`); voids blocked sandbox-side (`PERMISSION_DENIED` on void POST; Flow B auth `42Y42101L8189702D` stuck `CREATED` — same quirk as Oct 29). $3 residual held till expiry, nothing lost. OAuth/order/approval/authorize/capture (full + partial) all proven against real PayPal; void paths covered by the mock suite.

This run supersedes the Oct 29 live proof (Flow A green, $12.50 captured, order `8JM98988MG034281Y`).

## Prerequisites

### Environment variables (never write to disk)

```bash
export PAYPAL_CLIENT_ID='...'      # sandbox REST app client ID
export PAYPAL_CLIENT_SECRET='...'  # sandbox REST app secret
# PAYPAL_BASE_URL optional — defaults to https://api-m.sandbox.paypal.com
```

Transient handling (agreed Oct 29): Chris pastes the values in chat; they go
into env vars only; they are never written to any file, never committed,
never echoed in reports (report pass/fail per flow only).

### Sandbox accounts (PayPal developer dashboard)

- **Business (payee/API owner):** `sb-ywyt253471267@business.example.com`
- **Buyer (approver):** `sb-0gzke53580839@personal.example.com`
- The buyer's approval is a MANUAL browser step per order (see below).

### One-off code patch (runtime only, not committed)

`scripts/demo.py` hires from `app/hiring_agent.py::WORKER_AGENTS`, whose
`paypal_id` values are fake (`...@lamplight.example`). For the live run,
patch them at runtime to the real sandbox business account:

```python
# In the demo process only — do NOT commit:
from app import hiring_agent
for w in hiring_agent.WORKER_AGENTS:
    w["paypal_id"] = "sb-ywyt253471267@business.example.com"
```

(Oct 29 used the same one-off patch.)

## The three flows

Run: `.venv/bin/python scripts/demo.py --mode live`

For EACH order the script prints an approval URL. Open it in a browser,
log in with the SANDBOX BUYER account, click **Approve**. The script polls
for APPROVED for up to 10 minutes. The approval redirect goes to
`example.com/return` — expected, not an error; the approval has already
registered by then.

### Flow A — Happy path (happy path: brief → quote → order → approve → AUTHORIZE → deliver → verify → CAPTURE)

- **Expected:** GREEN. Proven live Oct 29: $12.50 captured to the real
  sandbox payee (order `8JM98988MG034281Y`).
- **Asserts:** receipt shows the capture id and amount; job settles CAPTURED.

### Flow B — Timeout void (brief → quote → order → approve → AUTHORIZE → agent never delivers → deadline passes → VOID)

- **Expected:** SANDBOX QUIRK (not a code bug). Oct 29: the authorization
  stuck in CREATED; the void was refused sandbox-side.
- **Asserts:** the code surfaces the refusal cleanly (no phantom settle);
  document the PayPal response verbatim for the report.

### Flow C — Partial release (brief → quote ($8.00 hold) → order → approve → AUTHORIZE → deliver → verify → CAPTURE-PARTIAL $5.00 + VOID residual $3.00)

- **Expected:** UNKNOWN — never ran live. This is the v8.2 proof target
  for the split-capture path (S10b).
- **Asserts:** receipt shows captured $5.00 + voided $3.00 = $8.00 hold;
  job settles CAPTURED via the split path.

## Reporting

Per flow: PASS / FAIL / QUIRK (with the PayPal response). Never echo
credential values. Receipts land in `demo-receipts/*.json` (gitignored).

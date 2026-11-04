# Lamplight PayPal Checkout v8.14 — Full Runnable Build

**1110 tests, 0 failures, flag ON** (`USE_DISPATCHER = True` in `app/escrow.py:146`)

**Demo video (2:07):** https://youtu.be/UtGjA2PR3ew

Trust shouldn't be the price of doing business. PayPal-powered conditional payment
coordination: an agent can hire another agent, the money stays held until the
delivery matches, and a partial job pays partial and gives the rest back.

**The pitch:** we used PayPal Authorizations as a real escrow lock for
agent-to-agent work. Money is held, delivery is hashed and verified, full or
partial capture happens only after verification, and every path is idempotent +
reconcilable. Happy-path and partial capture are proven live against the PayPal
sandbox (Nov 2: order `6BU17716204337128`, capture `6F983221VG264664F`); voids
are fully covered in the mock suite and currently blocked live by the
third-party payee permission model (see "Known open items" below).

## Stage demo (mock only, no real money)

```bash
./run_demo.sh
# then open http://127.0.0.1:5000/demo
```

One page, three beats on the mock PayPal sandbox: **Beat 1** — a $12.50 hold →
delivery → capture. **Beat 2** — an $8 hold → $5 paid + $3 returned. **Beat 3** —
the capture response is dropped and the ledger still settles exactly once
(idempotency). Nothing here touches real money; the Nov 2 live sandbox run is
the backup slide, not the stage.

## Live sandbox demo

```bash
export PAYPAL_CLIENT_ID='...' PAYPAL_CLIENT_SECRET=<redacted>
export LAMPLIGHT_LIVE_PAYEE='sb-ywyt253471267@business.example.com'
export LAMPLIGHT_DEMO_AUTH=1
.venv/bin/python scripts/demo.py --mode live
```

`LAMPLIGHT_LIVE_PAYEE` patches the demo workers' fictional
`@lamplight.example` payee addresses at runtime (never committed). Each order
prints an approval URL with step-by-step sandbox-buyer instructions; the script
polls for approval for up to 10 minutes. Flow B (void) is expected to hit
PayPal's `PERMISSION_DENIED` live — the script catches it and explains why
(third-party payee permission model) instead of crashing.

## Run the tests

```bash
./run_tests.sh
```

Requires Python 3.12+. Creates `.venv`, installs dependencies, runs the full
suite. Expected: **1110 passed** in ~30 seconds.

## Run the demo

```bash
DISPATCHER_INTENT_SECRET=test-secret python scripts/demo.py
```

Three flows: happy path (full capture), timeout void, partial release.

## What's here

- `app/` — all 13 modules (dispatcher, escrow, sweeper, webhooks, server, store + paypal_client, quote, auth, hiring_agent, receipt, audit, __init__)
- `tests/` — 11 active test files + conftest.py; `tests/history/` holds 16
  version-scoped files (v6.1–v8.5) that still run as part of the suite
- `scripts/` — demo.py, mock_paypal.py
- `docs/` — arbiter policy, deployment + incident-response plan, sandbox test plan
- `CHANGELOG.md` — version-by-version fix history (v7 → v8.14)

## Known open items

1. **Auth on the storefront (demo-grade).** POST /jobs and GET /jobs now carry a
   shared-secret bearer gate (`LAMPLIGHT_DEMO_TOKEN`, demo default under
   `LAMPLIGHT_DEMO_AUTH=1`; valid payer/agent tokens also pass — see
   `app/auth.py`). Honest about being demo-grade: a shared secret, not identity.
   Process-wide Create Order token bucket (default 30/min via
   `LAMPLIGHT_ORDER_BUDGET_PER_MIN`, 429 + `Retry-After` when exhausted) protects
   the PayPal credential — the MAX_OPEN_JOBS cap bounds row count, the bucket
   bounds API spend. Production still needs the full auth gate (caller identity,
   per-caller rate limits, caller-scoped listings) — documented in
   `app/server.py`, part of the post-hackathon milestone.
2. **Live PayPal sandbox: voids blocked by platform, not by code.** Flow A ($12.50
   captured) and Flow C partial capture ($5.00, PayPal-verified) ran green against
   the real sandbox Nov 2; void paths are refused sandbox-side (`PERMISSION_DENIED`)
   and remain mock-proven only. The cause is confirmed: captured funds go to the
   **worker's** PayPal account (third-party payee), not the API credential holder,
   and acting on another account's behalf requires PayPal Partner approval +
   per-worker consent (see "Payee" below). Voids are fully covered in the mock
   suite; `scripts/demo.py --mode live` catches the live 403 and explains it
   instead of crashing.
3. **Default store is SQLite.** The server defaults to `jobs.db` (SQLite/WAL,
   multi-process safe). `LAMPLIGHT_STORE=memory` selects the ephemeral in-memory
   store; `LAMPLIGHT_STORE=json` or a `.json` jobs path selects the legacy JSON
   store (single-process only, boot guard still refuses multi-worker on it). The
   earlier revert (broke 355 tests) was avoided by keeping `EscrowService`'s
   constructor default as `MemoryStore` — tests never go through the server default.
   Boot refuses to start when a live jobs.json would be stranded by the SQLite
   default with no env config.
4. **Payment pause (was "dispute").** `POST /jobs/<id>/dispute` (payer-only) pauses
   the 48h auto-capture with a deadline: `dispute_deadline` = min(filed + 72h,
   authorization honor-period end − 1h); the sweeper skips the job while under review
   and captures through the normal idempotent path at/after the deadline (silence
   pays the worker). The flag stays SET until PayPal accepts — no clear-then-capture
   gap. Disputes are refused (409) when fewer than 6h of honor remain or when
   `authorized_at` is unknown — those jobs must settle or void now. The response
   carries `effect="payment_pause"` and states plainly: nobody reviews the reason,
   it does not refund, doing nothing pays the worker at the deadline;
   `force_settle` pays the worker now. `POST /jobs/<id>/dispute/note` (agent-only)
   stores the worker's counter-note as chargeback evidence; it never changes the
   outcome. Legacy null-deadline pauses are treated as expired. Setting
   `LAMPLIGHT_SWEEP_INTERVAL_SECONDS=0` disables both the stale-intent sweep and
   auto-capture. Sweep-frequency requirement: the interval plus one retry must fit
   inside the 1h honor margin — do not configure the interval above ~1800s. The
   sweeper starts on the first API request, so a freshly restarted server pays
   nobody until called.
5. **"CAPTURED, no captures listed" settles from the books (deliberate).** If PayPal
   reports the authorization as CAPTURED but lists no captures (and the order read
   lists none), the job settles CAPTURED from the recorded books with the residual.
   "Released by final_capture" is indistinguishable from "residual captured
   elsewhere" in this case — the old path wedged here. This is a judgment call,
   documented as such.
6. **A CAPTURED job can carry a review flag.** When `final_capture` released the
   residual but PayPal listed captures outside the recorded flow, the job settles
   CAPTURED with `needs_review` set. An operator seeing a flagged CAPTURED job
   should check for money moved outside the flow; `clear_terminal_review_flag`
   (payer-only, re-reads PayPal, asserts money state unchanged) is how it's
   dismissed.

## Sandbox setup

For live-sandbox runs (`scripts/demo.py --mode live`): create a sandbox app at
https://developer.paypal.com (Dashboard → My Apps & Credentials → Create App),
then export `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET`. The mock mode
(`./run_demo.sh`, `scripts/demo.py --mode mock`) needs no credentials.

## Payee

Captured funds go to the **payee**, which is the worker agent's PayPal account —
not the merchant that owns the API credentials. The demo workers'
`@lamplight.example` addresses are fictional, so a live run needs a real
sandbox payee — set `LAMPLIGHT_LIVE_PAYEE` and `scripts/demo.py --mode live`
patches `WORKER_AGENTS[*].paypal_id` at runtime (never committed). A plain
client-credentials app **cannot** capture or void for a third-party payee: the
sandbox `PERMISSION_DENIED` on voids is this boundary, confirmed by PayPal's
own docs ("the payee account needs to grant permissions to the facilitator
account"). Acting on another account's behalf requires PayPal Partner approval
+ per-worker consent via the Partner Referrals API; workers need business
accounts. Documented here as a production prerequisite, not a code gap.

## Deployment limits

- **Single process only.** The default `MemoryStore`/`JsonFileStore` has no
  cross-process concurrency control. Run exactly one server process per jobs
  file. `_refuse_multi_worker` blocks `gunicorn -w 4` via argv/env inspection.
- **SQLite for multi-worker.** Set `LAMPLIGHT_STORE=sqlite` or point
  `LAMPLIGHT_JOBS_FILE` at a `.db` file for WAL-mode storage with optimistic
  concurrency (v8.2 hardening).
- **Demo auth is demo-only.** The server refuses to boot without real
  `LAMPLIGHT_PAYER_TOKEN`/`LAMPLIGHT_AGENT_TOKENS` unless `LAMPLIGHT_DEMO_AUTH=1`
  is explicitly set.

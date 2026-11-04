# Deployment and Incident-Response Plan

**Lamplight Checkout v8.12 — November 3, 2026**

## Deployment

### Prerequisites

- Python 3.12+
- `DISPATCHER_INTENT_SECRET`: a fresh random secret per deployment
  (the boot guard refuses the test value and refuses to start without
  a real one).
- `LAMPLIGHT_PAYER_TOKEN` and `LAMPLIGHT_AGENT_TOKENS`: real bearer
  tokens (the boot guard refuses demo defaults unless
  `LAMPLIGHT_DEMO_AUTH=1` is explicitly set — demo only, never in
  production).
- PayPal credentials: `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET`
  (sandbox or live). Without them the service starts but PayPal-touching
  endpoints return 503.
- Optional: `LAMPLIGHT_PAYPAL_WEBHOOK_ID` for webhook signature
  verification. Without it, `POST /webhooks/paypal` returns 503.

### Steps

1. `./run_tests.sh` — full suite must pass (903 tests in v8.12).
2. Set the env vars above. Never commit secrets; env vars only.
3. `python -m app.server` (dev) or gunicorn single-worker
   (production). **The JSON store is single-process only** — the boot
   guard refuses multi-worker configs unless the store is SQLite
   (v8.12 default: `jobs.db`). If you override to JSON, run exactly
   one worker.
4. Verify: `GET /` returns service status; `POST /jobs` with a test
   brief returns a quote (mock PayPal) or a real PayPal order.

### What the boot guard checks (fail-fast, before serving)

- `DISPATCHER_INTENT_SECRET` is set and is not the test value.
- Auth tokens are real (or explicit demo opt-in).
- JSON store + multi-worker config → refuse to boot.
- Missing PayPal creds → warn, not refuse (endpoints 503 individually).

## Monitoring

- **Structured audit log** (`app/audit.py`, v8.2): every financial
  operation is recorded with the job id, amounts, and PayPal
  references. This is the first place to look when money looks wrong.
- **Sweeper loop** (`app/sweeper.py`): runs stale-intent cleanup and
  `check_inspection_deadlines()` (48h auto-capture). Logs warnings on
  per-job failures but never dies on one bad job. If the sweeper
  stops, auto-capture stops — jobs sit in DELIVERED until the next
  run. The sweeper starts on the first API request, so a freshly
  restarted server pays nobody until called.
- **Webhook dedupe** (v8.2): atomic claim/settle on webhook event ids.
  A replayed PayPal delivery is a no-op, never a second settlement.
- **Health**: `GET /` for liveness. There is no built-in metrics
  endpoint (production item).

## Incident response

### PayPal API is down or erroring

- Endpoints that touch PayPal return 503 with the sandbox-setup hint.
  No job state changes on a PayPal failure — the job stays in its
  current state and the operator retries.
- **Do not** manually edit the jobs database to "fix" a state. Use
  `POST /admin/reconcile` (payer token) to walk open jobs and force
  a re-read of PayPal state.
- If the outage is prolonged, the 48h inspection auto-capture will
  fail its pre-flight PayPal read and skip the job (logged, retried
  next sweep). No silent captures happen during an outage.

### Webhook delivery fails or is delayed

- Webhooks are a notification channel, not the settlement path. Every
  financial state change goes through the idempotent capture/void
  core with reconciliation — a missed webhook delays awareness, not
  settlement.
- Duplicate webhooks are safe (atomic dedupe).
- If webhooks stop arriving entirely, `POST /admin/reconcile` covers
  the gap: it re-reads PayPal for every open job.

### A job is wedged (needs_review)

- `POST /admin/reconcile` lists wedged jobs with their reasons.
- **Payer-only exit**: `POST /jobs/<id>/force-settle` re-reads the
  authorization, records any listed capture, and settles the job.
  Only the job's payer, only a wedged job.
- **Terminal flagged jobs** (settled but carrying needs_review):
  `clear_terminal_review_flag` (payer-only) re-reads PayPal, asserts
  the money state is unchanged, and clears the flag. Money is never
  re-settled by this path.

### A payment pause is filed (v8.12+)

- `POST /jobs/<id>/dispute` (payer-only) pauses auto-capture until
  the dispute deadline. This is a PAYMENT PAUSE, not an adjudicated
  dispute — no arbiter, no review, no path to not-pay. The pause
  reason is on the job record; the response's payment_pause block
  states plainly what this does.
- Resolution: at the deadline the sweeper captures anyway (silence
  pays the worker), or the payer uses `force-settle` to close the job
  early. There is no in-app arbiter assignment yet — see
  `docs/arbiter-policy.md` for the policy and its current enforcement
  status.

### Database corruption or loss

- **SQLite (v8.12 default)**: WAL mode. If the db is corrupted,
  restore from backup. There is no built-in backup job — the
  operator is responsible for snapshotting `jobs.db`.
- **JSON (legacy)**: the file is rewritten atomically on every save;
  a crash mid-write leaves the old file intact. Same backup
  responsibility.
- **MemoryStore**: nothing is persisted. A restart loses everything.
  Never use in production (the `LAMPLIGHT_STORE=memory` opt-in is
  for tests and throwaway runs).

### Rollback

1. Stop the server.
2. Restore the previous build's code AND the jobs database from the
   pre-deploy snapshot. (Code and db must roll back together — a new
   code version may have migrated the schema.)
3. Re-run `./run_tests.sh` on the restored build.
4. Restart and verify `GET /` + one `GET /jobs/<id>` on a known job.

**There is no blue-green or canary path in v8.** Deploys are
stop-restore-restart. A zero-downtime deploy story is a production
item.

## What this plan does NOT cover (yet)

- Automated backups / point-in-time recovery.
- Metrics, alerting, or paging (log-watching only in v8).
- Multi-region or HA deployment.
- Secret rotation procedure.
- Load testing numbers or capacity planning.

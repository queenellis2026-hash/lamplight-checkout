# Lamplight Checkout

**Escrow-style hiring between AI agents, on PayPal.** One agent hires another
and pays through the PayPal sandbox with an escrow-style hold:

1. **Quote** — the hiring agent (Ellis) takes a plain-language brief,
   matches a worker agent, and issues a hash-stable quote.
2. **Authorize** — the payer approves a PayPal order; the money is
   **AUTHORIZED (held, not captured)**. The worker can see the hold exists,
   but cannot touch it.
3. **Deliver** — the worker submits its work as a SHA-256 delivery hash.
   The payer agent verifies the hash before releasing anything.
4. **Capture** — the payer releases the hold: PayPal captures the
   authorization and the worker is paid.
5. **Void on timeout** — if the worker never delivers, the payer voids the
   authorization after the deadline and the hold is released back.

Both sides keep a **hash-stable settlement receipt** (same design as
Lamplight Pay's `lamplight-pay/1` receipts): quote id, service, payer,
agent, amount, PayPal order / authorization / capture ids, delivery hash,
the last PayPal response (see "Audit trail" below), and a SHA-256
`receipt_hash` either side can verify. Tamper with one field
and the hash stops matching.

Built for the [PayPal AI Hackathon](https://paypalaihackathon.devpost.com/)
(submissions close **Nov 12, 2026, 3:00 PM ET**). Target category:
**Best Use of Agentic Commerce** ($5,000).

## How it maps to Lamplight

| Lamplight Pay (HSK chain) | Lamplight Checkout (PayPal) |
|---|---|
| `AgentEscrow` contract locks mUSDC | PayPal authorization holds USD |
| `release()` pays the agent | `capture_authorization()` pays the agent |
| `refundAfterDeadline()` refunds the payer | `void_authorization()` releases the hold |
| delivery proven by on-chain work hash | delivery proven by SHA-256 delivery hash |
| settlement receipt, hash-stable | settlement receipt, hash-stable (`lamplight-checkout/1`) |

No blockchain, no gas, no wallet — the escrow lives inside PayPal's authorize/capture/void flow, which every merchant already understands.

## Submission

Submitted to the [PayPal AI Hackathon on Devpost](https://devpost.com/software/lamplight-checkout-escrow-for-ai-agents-on-paypal) — Oct 27, 2026 ~3:49 PM CDT, Submission ID 1268309, status Submitted (editable until Nov 12, 2026, 3:00 PM ET). Demo video v3 (Unlisted, @EllisLamplight): https://youtu.be/49RK_V546q0 — round-10 build, 202 tests, flows A/B/C. (v2 replaced Oct 28, 2026; old video deleted Oct 31, 2026.)

## Quickstart

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# Full demo, no network, no money (mock PayPal sandbox):
.venv/bin/python scripts/demo.py --mode mock

# Tests (445 tests, mock PayPal included):
.venv/bin/python -m pytest tests/ -q

# HTTP service (needs sandbox creds, or point it at the mock):
PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=... \
  PAYPAL_BASE_URL=https://api-m.sandbox.paypal.com \
  .venv/bin/python -m app.server
```

## Sandbox setup (the one human step, ~2 minutes)

A PayPal Developer app cannot be created without logging in, so this is
the one step a human does:

1. Go to https://developer.paypal.com and log in (or create a free
   account).
2. **Dashboard → My Apps & Credentials → Create App** (name it
   `Lamplight Checkout`, type Merchant).
3. Copy the **Client ID** and **Secret** (Sandbox tab — never the Live tab).
4. Export them and re-run the demo live:
   ```bash
   export PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=...
   .venv/bin/python scripts/demo.py --mode live
   ```
   The script prints an approve URL for each order — open it, log in with
   a **sandbox buyer** account (Dashboard → Sandbox → Accounts), click
   Approve, and the script continues. No real money ever moves: sandbox
   only, test accounts only.

Credentials live in environment variables only — never in a file
(see `.env.example`; `.env` is gitignored).

## The live flow, honestly

PayPal requires the buyer to approve an order before it can be
authorized, and there is no server-side way around that — it is the
buyer's consent, and it happens in PayPal's checkout page. In `--mode
live` the demo prints the approve URL and polls for APPROVED for up to
10 minutes. In `--mode mock` the mock server's `/mock/approve` endpoint
stands in for that click, and everything else — OAuth, order create,
authorize, capture, void — runs through the same `PayPalClient` code.

## The AI in it

The hiring agent is an AI agent (Ellis) that parses a brief, matches a
worker, negotiates the quote, and verifies the delivery hash before
releasing payment. The demo's negotiation policy is deterministic (parse
the brief, match the service, take the worker's listed price) so it runs
offline and reproducibly. The policy is one swappable hook —
`HiringAgent(llm=...)` takes any callable with the signature
`llm(prompt: str) -> str` — and nothing else in the flow changes.

## Caller authentication

The mutating endpoints (authorize, deliver, capture, void) require a
bearer token (`app/auth.py`). The payer holds one token
(`LAMPLIGHT_PAYER_TOKEN`); each worker holds its own
(`LAMPLIGHT_AGENT_TOKENS`, a JSON object mapping the agent's PayPal id
to its token). The caller's identity is derived from the token — never
from the request body and never from a default — and the body's
`payer` / `agent` field must agree with it. Missing token: 401. Valid
token, wrong party: 403. Missing identity field: 400.

This is demo-grade shared-secret auth, and it says so in the code:
anyone holding a token can act as that party. The demo defaults
(`demo-payer-token`, `demo-agent-*`) exist so the demo runs out of the
box; set real tokens via env vars for anything beyond a local demo,
and replace this with proper identity (OAuth/JWT or signatures) before exposing the server. `POST /jobs` (job creation) stays unauthenticated in the demo — a real deployment would authenticate that too.

Round-10 limits (Oct 28, 2026): Flask `MAX_CONTENT_LENGTH` is 1 MB — an oversized body answers 413 — and `POST /jobs` refuses past 100 open jobs with 429 (settled jobs do not count). Token auth on these endpoints is still held — demo scope, an API-surface change.

## Payee: the worker actually gets paid

The order's `purchase_units[].payee` is the worker's `pay_to` account
from the quote, so the capture pays the worker — not the merchant that
owns the API credentials. In live mode the payee must be a **real
PayPal account**: the demo workers' `@lamplight.example` addresses are
fictional, so a live sandbox run needs a real sandbox payee account
for the worker (point the worker's `pay_to` at one) before the routing
can be proven end-to-end.

## Lost responses: idempotent recovery

Every authorize/capture/void call carries a stable `PayPal-Request-Id`
derived from the job id. If the response is lost after PayPal already
acted, the retry replays the original result instead of executing
twice (PayPal's documented idempotency). If even the retry fails, the
escrow reconciles from PayPal's actual authorization state before
giving up — a capture that really happened is recorded (flagged
`capture_reconciled` on the job and the receipt) with the **real
capture id and amount**, recovered from the authorization's linked
captures. Only when PayPal's authorization lists no capture at all
does the job keep capture id `""` with the reconciled flag — that case
stays documented as unauditable. A capture is only settled on a
`COMPLETED` result and an authorization only on a `CREATED` one; a
`DECLINED`/`DENIED` result never settles the job.

`POST /admin/reconcile` (payer token) forces the same reconciliation
for every open (AUTHORIZED/DELIVERED) job at once — for use after a
prolonged outage. It returns a summary (checked / updated /
still_pending / errors); one job's failure never stops the rest. A job
whose authorization PayPal no longer knows (404, vanished
authorization) is reported in `errors`, stays open, and is not
mutated — the failed read never overwrites the job's last good
PayPal response.

## Webhooks: PayPal tells us when money moves (round 7)

`POST /admin/reconcile` only runs when an operator notices an outage
and polls it. `POST /webhooks/paypal` removes that wait: PayPal
delivers an event when an authorization is created or voided, or a
capture completes or is denied, and the job settles itself. No bearer
token — the PayPal signature is the auth: the endpoint verifies the
delivery through PayPal's `/v1/notifications/verify-webhook-signature`
API (webhook id from env `LAMPLIGHT_PAYPAL_WEBHOOK_ID`, the webhook
registered in the PayPal dashboard) before anything is processed. A
missing or invalid signature is 401; a verification that cannot be
completed (PayPal unreachable) is 503, so PayPal retries the delivery;
without the webhook id the endpoint is 503.

The event body alone never settles a job: `handle_webhook_event`
reads the named authorization from PayPal and lets `reconcile_job`
decide the state — the same path the admin endpoint uses, so there is
no second capture-recording path. A capture denial is recorded on the
job (operation "webhook", status "DENIED") and leaves it open; an
authorization creation is confirmed without overwriting the authorize
record (the dedupe record proves the event arrived). Events are
deduped by PayPal event id, persisted in the job store, so a replayed
delivery is a no-op even after a restart — except when PayPal's state
could genuinely not be read (transport failure / 5xx), in which case
the event is deliberately left unmarked so a later re-delivery can
still settle it. Unknown event types and unknown jobs are 200 with a
summary (logged, no state change); the admin reconcile endpoint
remains the fallback for those. The mock gains a verify endpoint
(default SUCCESS, one-shot `POST /mock/next-webhook-verification`
control for FAILURE) and a sender (`POST /mock/send-webhook`) that
delivers an event to a target URL with PayPal's five transmission
headers.

## Audit trail: last PayPal response

Every job persists the last successful PayPal response
(`last_paypal_response`, visible on `GET /jobs/<id>` and carried on the
settlement receipt): an envelope with the operation (`authorize` /
`capture` / `void` / `reconcile`), the PayPal status, the PayPal id,
and the raw response body — so an operator can audit what PayPal
actually said without re-querying it. Only successful responses are
recorded: a failed call (a dropped response, a 404 on reconcile)
never overwrites the last good record. Buyer data never reaches the audit trail (round 10): recorded bodies are an allowlist (`sanitize_paypal_body`) — only ids, status, amounts, timestamps and linked captures/authorizations survive, so a shipping block (buyer name and address) cannot leak to an unauthenticated read or a receipt — and orders ask PayPal for NO_SHIPPING. The round-9 denylist (strip `payer` / `payment_source`) is superseded by the allowlist. The void call returns no body
(204), so the envelope — not the raw body alone — is the audit trail
there: operation + status + id still say what happened. Store files
written before round 4 load with the field absent (`None`).

## Delivery verification in capture (round 2)

Capture now requires the original delivery text and verifies it against
the hash recorded on the job **before any PayPal call** — the check the
first review found missing. The escrow calls the hiring agent's
`verify_delivery` (injected as the service's delivery verifier; a plain
hash comparison is the default when none is injected), so funds can
never be released on a delivery whose hash was never checked, whatever
the caller claims. The HTTP capture endpoint takes
`{payer, delivery_text}`; missing text is 400, a hash mismatch is 409,
and the job stays DELIVERED.

## Durable job store (round 2)

Jobs no longer live only in memory: they persist in a pluggable store
(`app/store.py`). The server defaults to a JSON file (`jobs.json` in the
project root, env `LAMPLIGHT_JOBS_FILE` to move it; gitignored), rewritten
atomically on every state change, so a restart never loses a job or its
PayPal ids — a fresh service on the same file picks up AUTHORIZED and
DELIVERED jobs and capture/void keep working. Tests inject an in-memory
store. One stated limit: the JSON file is for a single-process demo
server — see "Deployment limits — single process only" below.

## Deployment limits — single process only

The JSON job store loads once at startup and rewrites the whole file on
every save, under a process-local lock. **Two server processes (or two
Gunicorn/uwsgi workers) writing the same jobs file will clobber each
other and lose jobs** — each process holds its own in-memory copy and
the last writer wins. Run exactly **one server process on one jobs
file** (env `LAMPLIGHT_JOBS_FILE`, default `jobs.json` in the project
root). The server prints this warning at startup, naming the jobs file.
A multi-worker deployment needs a store with real concurrency control
(SQLite or a database) — that swap is deliberately held for later, not
built yet.

## Forged timestamps (round 2)

The void deadline is always evaluated against wall-clock time. The
server never takes a timestamp from the request path — a `now` field in
a void request body is ignored, so a forged future timestamp cannot
void before the real deadline. The `now` parameters on
`void_after_deadline`, `submit_delivery` and `quote_expired` are
test-only hooks (they let tests simulate time without sleeping) and are
documented as such in the code.

## Post-review fixes (Oct 27, 2026)

Reviewed by ChatGPT and Claude (Grok's review was still running).
Full verbatim findings are in the review history. All findings fixed:
payee passed on order creation; bearer auth on the mutating endpoints
with no identity defaults; PayPal result statuses checked before
settling; quote expiry enforced in authorize; delivery rejected at or
after the deadline; idempotent capture/void with reconciliation from
PayPal's authorization state; `now=0` handling; per-job delivery lock
(exactly-once); strict `create_job` input validation (400s, not 500s);
no orphaned job when order creation fails. Tests grew 20 → 54.

Round 2 (Grok's findings, same day): delivery-hash verification
enforced in capture (the delivery text is required and checked before
any PayPal call); durable job store (JSON file, restart round-trip
tested); forged `now` never accepted from the request path. Tests
grew 54 → 66.

Round 3 (Grok's round-2 re-review, same day — SOUND for a single-process
demo, 1 Medium + 1 Low): the reconciled capture now carries the **real
capture id and amount** from the authorization's linked captures
(`first_completed_capture` in `paypal_client.py`, `amount_to_cents`
for exact decimal parsing); only an authorization listing no capture
keeps the old `""` + reconciled fallback. New `POST /admin/reconcile`
(payer token) walks open jobs and forces a `get_authorization` + state
update per job, returning checked / updated / still_pending / errors.
The single-process limit is now loud: a "Deployment limits" section in
the README and a startup warning naming the jobs file. The receipt
gained `capture_amount_cents`. Held: SQLite/Postgres store, partial
capture, webhook listener. Tests grew 66 → 87.

Round 4 (Grok's round-3 re-review, same day — SOUND, "ship as a demo",
no findings): every job now persists the **last successful PayPal
response** (`last_paypal_response`: operation + status + id + raw body)
at every authorize / capture / void and on every reconcile read, so an
operator can audit without re-querying PayPal; a failed call never
overwrites the last good record. The receipt carries the same field.
New test pins the 404 case: an admin reconcile on a job whose
authorization vanished reports the error and leaves the job byte-for-byte
unchanged (mock gained a `/mock/forget-authorization/{id}` control).
Held: SQLite/Postgres store, partial capture, webhook listener. Tests
grew 87 → 96.

Round 5 (Grok's round-4 re-review, same day — SOUND, no findings):
the recorded response body is **capped** (4 KB; links trimmed to one
entry, long strings and lists cut, oversized bodies have their largest
keys dropped first) so a future PayPal response change cannot bloat the
durable store — normal responses record fully and unchanged, and the
guard never raises (a pathological body degrades to a trimmed record).
All three reconcile branches now record their successful read through
one shared helper (`_record_reconcile_read`), so the "always persist a
successful read" rule is expressed once. The vanished-authorization
test now covers a DELIVERED job too — both open states are pinned
against mutation-on-error. Held: SQLite/Postgres store, partial
capture, webhook listener. Tests grew 96 → 104.

Round 6 (Grok's round-5 re-review, same day — SOUND, no findings):
the envelope guard now keeps the three audit keys (id, status, amount)
first when an oversized body still has to shed top-level keys — they
are skipped while any other key can be dropped instead, and only the
largest audit key goes last if the body holds audit keys alone; the
guard is still size-driven (a just-under-4 KB body with a depth-7 nest
and a 600-char string records byte-identical), and the
unexpected-authorization-status path is documented as skipping the
record+save tail. Held: SQLite/Postgres store, partial capture,
webhook listener. Tests grew 104 → 107.

Round 7 (Oct 28, 2026 — the webhook listener, held since round 2 and
the most valuable of the held features; Grok re-review queues after
PayPal round 6 and HSK round 13): `POST /webhooks/paypal` lets PayPal
tell the service when money moves instead of waiting for an operator
to poll `/admin/reconcile`. Signature verified via PayPal's
verify-webhook-signature API (webhook id from env
`LAMPLIGHT_PAYPAL_WEBHOOK_ID`); events are reconciled from PayPal
before any state change through the same `reconcile_job` path the
admin endpoint uses — no second capture-recording path. A capture
denial is recorded and leaves the job open; an authorization creation
is confirmed without overwriting the authorize record. Processed event
ids are persisted in the job store, so a replayed delivery is a no-op
even after a restart — except when PayPal's state could genuinely not
be read, in which case the event is left unmarked so a later
re-delivery can still settle it. Also folded in: three small Grok
round-6 improvements — a one-line comment stating the allow-list
preference order (non-audit keys first, audit keys last, cap always
wins), an exact serialized-length assertion in the just-under-cap
test, and the drop loop extracted into `_drop_keys_to_fit` with direct
unit tests (preference order, final resort, cap guarantee). Held: SQLite/Postgres store, partial capture. Tests grew 107 → 130.

Update (Oct 28, 2026): the queued Grok re-review happened — SOUND, Oct 28 ~12:43 AM CDT.

Round 8 (Oct 28, 2026 — partial capture + residual void, the last held
feature Grok called "the main remaining product limitation for real
agent workflows"; Grok re-review queues after V2 round 6 and HSK round
15): new `POST /jobs/<id>/capture-partial` (and `EscrowService.
capture_partial`) releases part of the hold and voids the rest in one
move — same delivery verification as the full capture, amount > 0 and
<= the hold, an amount equal to the hold degenerating to a normal
capture. One receipt covers both legs: the job settles CAPTURED with
`capture_amount_cents` + `voided_amount_cents` = the hold (new receipt
and job field), outcome "released". A failed residual void never
reports settled — the partial capture is recorded, the job stays
DELIVERED, and the payer retries with the same amount (capture skipped,
void only) or the admin reconcile finishes it when the void did apply:
`reconcile_job` records a partially-captured job whose authorization
PayPal reports VOIDED as CAPTURED with the voided residual, never as
VOIDED. Also folded in: two more Grok round-7 improvements — an
end-to-end webhook integration test (a full delivery through the mock
sender into a short-lived real Flask server process, proving the 503
retry path) and a cap on the durable webhook-event id set (1000,
oldest first evicted, insertion order preserved across reloads, so a
long-running service cannot grow the jobs file without bound). The mock
now models partial captures (an amount below the remainder leaves the
authorization CREATED so the residual can be voided) and its webhook
sender reports the target's error status instead of converting it to
502. The demo gains flow C (partial release: $5.00 of an $8.00 hold,
$3.00 voided); flows A and B are unchanged. Held: SQLite/Postgres store, job-age / authorization-expiry background sweep. Tests grew 130 → 160.

Update (Oct 28, 2026): the queued Grok re-review never happened — round 8 went to Claude as the final gate instead (NEEDS WORK); round 9 was built from Claude's findings.

Round 9 (Oct 28, 2026 — Claude final-gate fixes on round 8; Claude
judged round 8 "needs work, close to sound", the first PayPal round
any reviewer did not pass clean): per-job lock on ALL settlement
paths (capture, capture_partial and void_after_deadline run under the
lock submit_delivery already used, so two concurrent settlement calls
cannot both pass the status check); buyer data (`payer`,
`payment_source`) stripped from PayPal response bodies before
recording and again on the way out over HTTP; a PARTIALLY_CAPTURED
authorization treated like CREATED by reconcile (and by the webhook
path); non-string delivery text refused with a 400; `amount_to_cents`
rejects negative and non-finite values; `escrow()` double-checked
locked against the first-request race; job ids widened to full UUID
hex with a collision check on create; the mock rejects an
over-capture with 422 and gains a `/mock/next-partial-status`
control that emits PARTIALLY_CAPTURED. Held: deadline starting at
authorization time; admin reconcile retrying the residual void; a
read-only redacted receipt endpoint. Tests grew 160 → 174.

Round 10 (Oct 28, 2026 — Claude final-gate re-review of round 9;
Claude judged round 9 "needs work — one more fix round, not a
redesign"): recorded PayPal responses are now an allowlist (only
ids, status, amounts, timestamps and linked captures/authorizations
survive — a shipping block can no longer leak to unauthenticated
reads), and orders ask PayPal for NO_SHIPPING; `amount_to_cents`
shape-checks before parsing so no malformed amount can escape after
money moved; `reconcile_job` runs under the same per-job lock as the
settlement paths and `capture()` sets `capture_reconciled`
explicitly; the capture-partial idempotency key carries the amount
and a mismatched COMPLETED amount is refused; 1 MB body cap and a
100-open-jobs cap on the unauthenticated endpoints; a capture
denial is kept in its own field instead of overwriting the
authorize record. Held: SQLite store, token auth on the job
endpoints, payer review window, authorization-expiry guard. Tests
grew 174 → 202.

## Layout

```
app/
  quote.py          hash-stable quotes (USD cents)
  receipt.py        hash-stable settlement receipts (lamplight-checkout/1)
  paypal_client.py  OAuth2 + Orders API v2 (authorize/capture/void)
  escrow.py         job state machine: QUOTED -> AUTHORIZED -> DELIVERED
                    -> CAPTURED | VOIDED, with the same guards as the
                    on-chain LamplightEscrow contract
  hiring_agent.py   brief parsing, worker matching, delivery verification
  auth.py           demo-grade bearer auth for the mutating endpoints
  store.py          pluggable job store: MemoryStore (tests) and
                    JsonFileStore (server default, restart-proof)
  server.py         Flask service: /jobs, /jobs/<id>/authorize|deliver|
                    capture|void, /admin/reconcile, /webhooks/paypal
scripts/
  demo.py           full flows A (happy path) + B (timeout void) + C (partial release); --mode mock|live
  mock_paypal.py    local mock PayPal sandbox (same REST surface)
tests/              445 tests: receipts, quotes, escrow guards, client round
                    trips, HTTP service, auth, idempotent recovery,
                    delivery verification, durable store, forged-now,
                    reconcile (real capture id + amount, admin endpoint),
                    single-process warning, last PayPal response (audit
                    trail, 404 vanished authorization, envelope size
                    guard, audit-key allow-list, shared reconcile helper),
                    webhook listener (signature verification, reconcile
                    on event, denial recorded, dedupe, reload survival) — all against the mock
demo-receipts/      quotes + receipts produced by the demo run
docs/
  SUBMISSION.md     Devpost submission (submitted Oct 27, 2026 ~3:49 PM CDT, Submission ID 1268309)
  VIDEO-SCRIPT.md   demo video script, under 3 minutes
```

## Honest notes

- Sandbox only. No real money moves, no live PayPal account is used.
- The worker registry (`WORKER_AGENTS` in `hiring_agent.py`) is a fixed
  demo list; a production version would be a real agent registry.
- Authorization holds on PayPal last 29 days; the escrow deadline in the
  demo is a job-level deadline (seconds in the demo, hours in real use),
  and voiding after it is the payer's choice, not PayPal's expiry.
- The payer is the arbiter in v1 (same stated limitation as Lamplight
  Pay): capture requires delivery, void requires timeout, but the payer
  decides. A third-party arbiter is the natural v2.
- `POST /jobs` (job creation) is unauthenticated in the demo; anyone
  who can reach the server can create PayPal orders (which cost nothing
  until the buyer approves). A real deployment would authenticate it.
- The demo workers' `@lamplight.example` addresses are fictional. The
  payee routing is proven against the mock; a live sandbox run needs a
  real sandbox payee account for the worker (see "Payee" above).

---

*Built by Ellis, a kept AI — [Lamplight](https://lamplight.serveousercontent.com)*

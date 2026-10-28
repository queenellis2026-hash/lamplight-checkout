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

No blockchain, no gas, no wallet — the escrow lives inside PayPal's
authorize/capture/void flow, which every merchant already understands.

## Quickstart

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# Full demo, no network, no money (mock PayPal sandbox):
.venv/bin/python scripts/demo.py --mode mock

# Tests (87 tests, mock PayPal included):
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
and replace this with proper identity (OAuth/JWT or signatures) before
exposing the server. `POST /jobs` (job creation) stays unauthenticated
in the demo — a real deployment would authenticate that too.

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

## Audit trail: last PayPal response

Every job persists the last successful PayPal response
(`last_paypal_response`, visible on `GET /jobs/<id>` and carried on the
settlement receipt): an envelope with the operation (`authorize` /
`capture` / `void` / `reconcile`), the PayPal status, the PayPal id,
and the raw response body — so an operator can audit what PayPal
actually said without re-querying it. Only successful responses are
recorded: a failed call (a dropped response, a 404 on reconcile)
never overwrites the last good record. The void call returns no body
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
Full verbatim findings: `reviews-2026-10-27.md`. All findings fixed:
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
                    capture|void, /admin/reconcile
scripts/
  demo.py           full flows A (happy path) + B (timeout void); --mode mock|live
  mock_paypal.py    local mock PayPal sandbox (same REST surface)
tests/              96 tests: receipts, quotes, escrow guards, client round
                    trips, HTTP service, auth, idempotent recovery,
                    delivery verification, durable store, forged-now,
                    reconcile (real capture id + amount, admin endpoint),
                    single-process warning, last PayPal response (audit
                    trail, 404 vanished authorization) — all against the mock
demo-receipts/      quotes + receipts produced by the demo run
docs/
  SUBMISSION.md     Devpost submission draft (not submitted)
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

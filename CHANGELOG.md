# Lamplight PayPal Checkout — Changelog

Version-by-version fix history. The current contract is in `README.md`;
this file is the archaeology.

## v8.15 (live AI in the hiring agent, Nov 11)

Eligibility fix: the PayPal hackathon rules require AI to run *inside* the
finished project, not just in the tools used to build it. The hiring agent
already had an `llm` hook, but nothing wired a real model into it — the demo
ran a deterministic parser. Now it does:

1. **New module `app/ollama_llm.py`.** Calls a local Ollama model
   (qwen2.5:7b) via `/api/generate`, temperature 0, and extracts clean JSON
   from the reply (strips ```json fences and surrounding prose). Raises a
   clear `OllamaUnavailable` instead of silently falling back when the
   server is down or the model is missing.
2. **Wired in by env flag.** `app/server.py` and `scripts/demo.py` both
   build the hiring agent with `HiringAgent(llm=make_llm())`. With
   `LAMPLIGHT_LLM=ollama` every brief is parsed by the model; without it
   the deterministic parser runs (tests and offline demos unchanged).
3. **13 new tests** (`tests/test_ollama_llm.py`): JSON extraction, env
   wiring, unreachable-server and missing-model errors, and the hiring
   agent negotiating through a fake LLM. Suite now 1135 passed.
4. **README documents the AI** — what runs, where, and how to turn it on.

## v8.14 (demo hardening, post-Grok)

Hackathon triage must-fix items, no version bump:

1. **Storefront gate.** POST /jobs and GET /jobs now require a shared-secret
   bearer token (`LAMPLIGHT_DEMO_TOKEN`, demo default under
   `LAMPLIGHT_DEMO_AUTH=1`; valid payer/agent tokens also pass — see
   `app/auth.py`). Demo-grade and honest about it; the production auth gate
   (caller identity, per-caller limits, caller-scoped listings) stays a
   post-hackathon milestone. Boot fails fast without the token configured.
2. **Void gap, honestly labeled.** Live voids hit PayPal `PERMISSION_DENIED` —
   confirmed third-party-payee boundary (worker's account receives the money,
   not the API credential holder; Partner approval + per-worker consent
   required). `scripts/demo.py --mode live` catches the 403 and explains it
   instead of crashing. README carries the pitch framing: authorizations as a
   real escrow lock, happy-path + partial proven live Nov 2, voids mock-proven.
3. **Demo bulletproofing.** `scripts/demo.py` generates an ephemeral
   `DISPATCHER_INTENT_SECRET` if unset (was a mid-flow crash); live mode takes
   `LAMPLIGHT_LIVE_PAYEE` for runtime payee patch (no code edit); approval step
   prints numbered sandbox-buyer instructions. `./run_demo.sh` verified
   one-command; all three CLI flows green in mock.
4. **Three beats on /demo.** The page now names all three beats consistently
   (Beat 3 = lost response), carries a "Live proof" card (Nov 2 order
   `6BU17716204337128`, capture `6F983221VG264664F`, Flow C auth
   `9DS708743A503231N`), and states the void platform constraint plainly.
   (952 → 969 tests)

## v8.14

Grok's precise v8.13 fixes:

1. **The flag stays SET until PayPal accepts.** `_resolve_expired_dispute` audits only
   (no flag clear, no status change); `dispute_job` 409s on `needs_review`, which is
   the clear-then-capture ladder-blocker; `_capture_core` takes `_sweep_dispute=True`
   for the sweep path, bypassing the review guard and clearing the flag in the same
   write as CAPTURED; on PayPal failure the flag stays set and the deadline stays in
   the past so the next sweep retries.
2. **Fail closed on honor math.** `_honor_limit()` returns None for unknown
   `authorized_at` and `dispute_job` refuses (409); honor is 3 days from authorization,
   not the 29-day lifetime.
3. **Honest labeling.** The route stays `/dispute` but the response carries
   `effect="payment_pause"`, `resolves_by="capture"`, and a `payment_pause` block
   stating plainly: no review, no refund, doing nothing pays the worker at the deadline.
   Plus `POST /jobs/<id>/dispute/note` for the worker's counter-note (chargeback
   evidence only). Legacy null-deadline pauses treated as expired.
   (925 → 944 tests)

## v8.13

Grok's v8.12 review found the dispute froze with no timer — v8.13 gives every dispute
a deadline (`dispute_deadline` = min(72h, honor-period-end − 1h); sweeper captures
at/after it, silence pays the worker; disputes refused when <6h of honor remains).
Also: dispute route serializes the fresh row (stale-snapshot fix), process-wide Create
Order token bucket (default 30/min) in front of unauthenticated POST /jobs, boot
refusal when a live jobs.json would be stranded by the SQLite default, and the full
dispute lifecycle tested on both MemoryStore and SqliteStore.
(903 → 925 tests)

## v8.12

Pre-Nov-12 hardening round:

1. /jobs auth situation documented explicitly in code + README with production
   auth-gate design.
2. SQLite now the server default (`jobs.db`); `LAMPLIGHT_STORE=memory|json|sqlite`
   opt-in/out; `EscrowService` test default unchanged (MemoryStore).
3. `POST /jobs/<id>/dispute` — payer-filed dispute wedges a DELIVERED job and blocks
   the 48h auto-capture (start, not a complete system).
4. Sandbox void re-attempt deferred (creds unavailable, 403 on Nov 2 documented).
5. `docs/arbiter-policy.md` — arbiter selection + conflict-of-interest policy.
6. `docs/deployment-incident-plan.md` — deploy, monitoring, incident response, rollback.
   (896 → 903 tests)

## v8.11

Submission compliance — MIT LICENSE file added (was missing); README setup sections
now point to ./run_tests.sh and ./run_demo.sh with the Python 3.12+ requirement stated.
(896 tests, unchanged)

## v8.10

Claude leftovers — negative test (happy path asserts needs_review False); README
documents the 'CAPTURED, no captures listed' settle-from-books decision and the
flagged-CAPTURED operator path.
(896 tests, unchanged)

## v8.9

Claude probe #2 — the out-of-band flag was set before settlement and wiped by
`_settle_captured_with_residual`'s `_clear_review_flag`; moved to AFTER settlement so
it survives; helper now logs instead of swallowing exceptions; the test goes through
the real `_finish_partial_capture` flow with a stubbed `get_authorization`.
(896 tests, unchanged)

## v8.8

Claude probe round — out-of-band captures on the final_capture path now set the review
flag (`_flag_out_of_band_captures`, Decimal math, no float); the out-of-band test now
actually injects a stray capture; README v8.6 count nit fixed.
(896 tests, unchanged)

## v8.7

Claude follow-up #2 — removed the `_auth_proves_released` shortcut (it bypassed
`_resolve_post_void` and used float math); the void-skip now feeds the CAPTURED read
through `_resolve_post_void` with a `final_capture_released` flag; new tests pin
`capture_partial` sending `final_capture=True`.
(894 → 896 tests)

## v8.6

Claude follow-up — fixed the stub parameter-order bug Claude caught (final_capture
inserted mid-signature broke a positional super() call); partial-capture void now
skipped when final_capture released the residual (mock honors the flag); new tests for
final_capture + sweeper wiring.
(891 → 894 tests)

## v8.5

Claude review round — wired `check_inspection_deadlines()` into the sweeper loop
(auto-capture was code-complete but never called); partial captures now send
`final_capture: true` so real PayPal releases the residual (was mock-only); fixed
v8.4/v8.3 history order; added Sandbox setup / Payee / Deployment limits sections.

## v8.4

Demo page JS fix — `agentToken` was declared twice in `beat()` (Grok: a browser parses
the script before any click, so all three buttons did nothing; pytest never loads
demo.html).

## v8.3

Stage-demo round — single-page demo (`/demo`, `./run_demo.sh`), `/mock/*` same-origin
proxy (CORS fix), demo page reads worker from `quote.pay_to` (Beat 2 403 fix),
`run_tests.sh` requires Python 3.12.

## v8.2 packaging

Honest-packaging round, no logic changes — README/sandbox-plan doc lag fixed,
`_force_settle_via_dispatcher` renamed to `_force_settle_legacy_with_fsm_tracking`,
SQLite-default attempt reverted (v9 item).
(891 tests)

## v8.2

Production-readiness round in reviewer's order — demo-credential boot guard, pending
intents in the FSM, atomic webhook dedupe, SQLite hardening, live sandbox proof
(Flow A + partial capture green), structured auditability; plus findings F1
(partial-capture recovery predicates), F2 (terminal review-clear path), F3
(public/private job-data split via `to_public_dict()`), and F4 (public-safe receipt
via `to_public_receipt()` — the open endpoint's receipt was leaking the PayPal ids
the public dict hid).
(819 → 891 tests)

## v8.1

Money-safety round, 10 surgical fixes from the harsh v8 review.
(781 → 819 tests)

## v8

P0 void-gap fix (19-case void gap closed — dispatcher settles found captures before
declaring a void); SQLite optimistic concurrency.
(770 → 781 tests)

## v7.4

5 fixes:

1. **Unified idempotency minting** — `_capture_core` uses `make_intent_key` (HMAC) when
   `USE_DISPATCHER` is True. Auto-capture and manual capture share the same key format.
2. **Webhooks connected to dispatcher** — `_webhook_reconcile` snapshots state and
   invokes `_fsm_track_reconcile`. Webhooks are no longer an unmonitored back-channel.
3. **Accurate FSM evidence tracking** — `_fsm_track_reconcile` and
   `_fsm_track_force_settle` supply complete evidence (capture_ids, amounts,
   is_partial, void_confirmed). No false-positive divergence alerts.
4. **Hardened multi-worker boot guard** — `_refuse_multi_worker` inspects CLI args
   (`-w`, `--workers`, `-p`, `--processes`) in addition to env vars.
   `gunicorn -w 4` is blocked.
5. **SQLite WAL store** — new `SqliteStore` with `PRAGMA journal_mode=WAL`. Set
   `LAMPLIGHT_STORE=sqlite` or point `LAMPLIGHT_JOBS_FILE` at a `.db` file for
   multi-worker-safe storage.
   (753 → 770 tests)

## v7.3

Merged Grok's 4 (HMAC key, webhook unification, sweeper default, multi-worker
refusal) with v7.2.
(753 tests)

## v7.2

Reviewer's 4 fixes (flag canary, divergence reporting, bearer token, TTL cap) + 3
tracker fixes.
(729 → 753 tests)

## v7.1

13 hardening hunks (store disk-stamp, sweeper thread, grace window, webhook env,
boot check).

## v7

Phase 2 bridge rewrite (125 → 0 flag-on failures, 701 tests).

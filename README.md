# Lamplight PayPal Checkout v8.11 — Full Runnable Build

**896 tests, 0 failures, flag ON** (`USE_DISPATCHER = True` in `app/escrow.py:146`)

**Demo video (2:07):** https://youtu.be/UtGjA2PR3ew

## Stage demo (mock only, no real money)

```bash
./run_demo.sh
# then open http://127.0.0.1:5000/demo
```

One page, two beats on the mock PayPal sandbox: a $12.50 hold → delivery →
capture, then an $8 hold → $5 paid + $3 returned. A third beat drops the
capture response and shows the ledger settling exactly once. Nothing here
touches real money; the Nov 2 live sandbox run (order `6BU17716204337128`,
capture `6F983221VG264664F`) is the backup slide, not the stage.

## Run the tests

```bash
./run_tests.sh
```

Requires Python 3.12+. Creates `.venv`, installs dependencies, runs the full
suite. Expected: **896 passed** in ~20 seconds.

## Run the demo

```bash
DISPATCHER_INTENT_SECRET=test-secret python scripts/demo.py
```

Three flows: happy path (full capture), timeout void, partial release.

## What's here

- `app/` — all 13 modules (dispatcher, escrow, sweeper, webhooks, server, store + paypal_client, quote, auth, hiring_agent, receipt, audit, __init__)
- `tests/` — 25 test files + conftest.py (896 tests total)
- `scripts/` — demo.py, mock_paypal.py

## v7.4 changes (5 fixes)

1. **Unified idempotency minting** — `_capture_core` uses `make_intent_key` (HMAC) when `USE_DISPATCHER` is True. Auto-capture and manual capture share the same key format.
2. **Webhooks connected to dispatcher** — `_webhook_reconcile` snapshots state and invokes `_fsm_track_reconcile`. Webhooks are no longer an unmonitored back-channel.
3. **Accurate FSM evidence tracking** — `_fsm_track_reconcile` and `_fsm_track_force_settle` supply complete evidence (capture_ids, amounts, is_partial, void_confirmed). No false-positive divergence alerts.
4. **Hardened multi-worker boot guard** — `_refuse_multi_worker` inspects CLI args (`-w`, `--workers`, `-p`, `--processes`) in addition to env vars. `gunicorn -w 4` is blocked.
5. **SQLite WAL store** — new `SqliteStore` with `PRAGMA journal_mode=WAL`. Set `LAMPLIGHT_STORE=sqlite` or point `LAMPLIGHT_JOBS_FILE` at a `.db` file for multi-worker-safe storage.

## Fix history

- v7: Phase 2 bridge rewrite (125 → 0 flag-on failures, 701 tests)
- v7.1: 13 hardening hunks (store disk-stamp, sweeper thread, grace window, webhook env, boot check)
- v7.2: reviewer's 4 fixes (flag canary, divergence reporting, bearer token, TTL cap) + 3 tracker fixes (729 → 753 tests)
- v7.3: merged Grok's 4 (HMAC key, webhook unification, sweeper default, multi-worker refusal) with v7.2 (753 tests)
- v7.4: 5 fixes above (753 → 770 tests)
- v8: P0 void-gap fix (19-case void gap closed — dispatcher settles found captures before declaring a void); SQLite optimistic concurrency (770 → 781 tests)
- v8.1: money-safety round, 10 surgical fixes from the harsh v8 review (781 → 819 tests)
- v8.2: production-readiness round in reviewer's order — demo-credential boot guard, pending intents in the FSM, atomic webhook dedupe, SQLite hardening, live sandbox proof (Flow A + partial capture green), structured auditability; plus findings F1 (partial-capture recovery predicates), F2 (terminal review-clear path), F3 (public/private job-data split via `to_public_dict()`), and F4 (public-safe receipt via `to_public_receipt()` — the open endpoint's receipt was leaking the PayPal ids the public dict hid) (819 → 891 tests)
- v8.3: stage-demo round — single-page demo (`/demo`, `./run_demo.sh`), `/mock/*` same-origin proxy (CORS fix), demo page reads worker from `quote.pay_to` (Beat 2 403 fix), `run_tests.sh` requires Python 3.12
- v8.11: Submission compliance — MIT LICENSE file added (was missing); README setup sections now point to ./run_tests.sh and ./run_demo.sh with the Python 3.12+ requirement stated (896 tests, unchanged)
- v8.10: Claude leftovers — negative test (happy path asserts needs_review False); README documents the 'CAPTURED, no captures listed' settle-from-books decision and the flagged-CAPTURED operator path (896 tests, unchanged)
- v8.9: Claude probe #2 — the out-of-band flag was set before settlement and wiped by `_settle_captured_with_residual`'s `_clear_review_flag`; moved to AFTER settlement so it survives; helper now logs instead of swallowing exceptions; the test goes through the real `_finish_partial_capture` flow with a stubbed `get_authorization` (896 tests, unchanged)
- v8.8: Claude probe round — out-of-band captures on the final_capture path now set the review flag (`_flag_out_of_band_captures`, Decimal math, no float); the out-of-band test now actually injects a stray capture; README v8.6 count nit fixed (896 tests, unchanged)
- v8.7: Claude follow-up #2 — removed the `_auth_proves_released` shortcut (it bypassed `_resolve_post_void` and used float math); the void-skip now feeds the CAPTURED read through `_resolve_post_void` with a `final_capture_released` flag; new tests pin `capture_partial` sending `final_capture=True` (894 → 896 tests)
- v8.6: Claude follow-up — fixed the stub parameter-order bug Claude caught (final_capture inserted mid-signature broke a positional super() call); partial-capture void now skipped when final_capture released the residual (mock honors the flag); new tests for final_capture + sweeper wiring (891 → 894 tests)
- v8.5: Claude review round — wired `check_inspection_deadlines()` into the sweeper loop (auto-capture was code-complete but never called); partial captures now send `final_capture: true` so real PayPal releases the residual (was mock-only); fixed v8.4/v8.3 history order; added Sandbox setup / Payee / Deployment limits sections
- v8.4: demo page JS fix — `agentToken` was declared twice in `beat()` (Grok: a browser parses the script before any click, so all three buttons did nothing; pytest never loads demo.html)
- v8.2 packaging: honest-packaging round, no logic changes — README/sandbox-plan doc lag fixed, `_force_settle_via_dispatcher` renamed to `_force_settle_legacy_with_fsm_tracking`, SQLite-default attempt reverted (v9 item) (891 tests)

## Known open items

1. **Auth on POST/GET /jobs** — open endpoints, held for demo scope.
2. **Live PayPal sandbox: voids unproven.** Flow A ($12.50 captured) and Flow C partial capture ($5.00, PayPal-verified) ran green against the real sandbox Nov 2; void paths are refused sandbox-side (`PERMISSION_DENIED`) and remain mock-proven only. The refusal is not fully diagnosed — it may be a sandbox limitation or a third-party-payee auth boundary (see "Payee" below).
3. **Default store is `MemoryStore`.** A SQLite-default switch was attempted and reverted (broke 355 tests — the suite relies on `MemoryStore` identity semantics; SQLite needs a service-level re-read discipline). v9 item.
4. **Auto-capture sweep is wired into the server (v8.5).** `check_inspection_deadlines()` runs in the sweeper loop — after the 48h inspection window with no dispute, the full price is captured with verification waived. There is no dispute endpoint, so the payer's only protection is `capture-partial` before the window closes. Setting `LAMPLIGHT_SWEEP_INTERVAL_SECONDS=0` disables both the stale-intent sweep and auto-capture. The sweeper starts on the first API request, so a freshly restarted server pays nobody until called.
5. **"CAPTURED, no captures listed" settles from the books (deliberate).** If PayPal reports the authorization as CAPTURED but lists no captures (and the order read lists none), the job settles CAPTURED from the recorded books with the residual. "Released by final_capture" is indistinguishable from "residual captured elsewhere" in this case — the old path wedged here. This is a judgment call, documented as such.
6. **A CAPTURED job can carry a review flag.** When `final_capture` released the residual but PayPal listed captures outside the recorded flow, the job settles CAPTURED with `needs_review` set. An operator seeing a flagged CAPTURED job should check for money moved outside the flow; `clear_terminal_review_flag` (payer-only, re-reads PayPal, asserts money state unchanged) is how it's dismissed.

(History note: the 19-case void gap listed here in earlier versions was fixed in v8 — the dispatcher's `READ_RELEASED` branches now settle found captures before declaring a void (`app/dispatcher.py:1428,1832,2114`); covered by `tests/test_v75_void_gap.py`, 9 tests passing.)

## Sandbox setup

For live-sandbox runs (`scripts/demo.py --mode live`): create a sandbox app at
https://developer.paypal.com (Dashboard → My Apps & Credentials → Create App),
then export `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET`. The mock mode
(`./run_demo.sh`, `scripts/demo.py --mode mock`) needs no credentials.

## Payee

Captured funds go to the **payee**, which is the worker agent's PayPal account —
not the merchant that owns the API credentials. The demo workers'
`@lamplight.example` addresses are fictional, so a live run needs a real
sandbox payee (the `WORKER_AGENTS[*].paypal_id` values must be patched at
runtime, never committed). Whether a plain client-credentials app can capture
or void for a third-party payee is unproven — the sandbox `PERMISSION_DENIED`
on voids may be this boundary, not a sandbox quirk.

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

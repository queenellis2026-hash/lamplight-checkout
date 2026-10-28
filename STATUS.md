# Lamplight Checkout — STATUS

**Built:** Oct 27, 2026 (evening, after the NEAR recommendation).
**For:** PayPal AI Hackathon, Devpost — submissions close **Nov 12, 2026,
3:00 PM ET**. Prize pool $69,750 cash + credits; target category **Best
Use of Agentic Commerce** ($5,000).
**Status:** built and tested. **NOT submitted** (per task: build only).

## What was built

`~/workspace/experiment/wallet/top3/paypal-checkout/`

- `app/quote.py` — hash-stable quotes, USD cents (adapted from Lamplight
  Pay's `a2a/quote.py`).
- `app/receipt.py` — hash-stable settlement receipts, protocol
  `lamplight-checkout/1` (adapted from Lamplight Pay's `a2a/receipt.py`;
  PayPal order/authorization/capture ids replace chain/tx-hash fields).
- `app/paypal_client.py` — raw-REST PayPal client: OAuth2, Orders API v2
  (create/authorize), Payments API (capture/void). No SDK.
- `app/escrow.py` — job state machine with the same guards as the
  on-chain LamplightEscrow: capture needs DELIVERED + payer; void needs
  AUTHORIZED + not delivered + past deadline; settled jobs never move.
- `app/hiring_agent.py` — brief parsing, worker matching, quote
  negotiation, delivery-hash verification. Deterministic policy with an
  `llm=` hook for any model.
- `app/server.py` — Flask service: `/jobs`, `/jobs/<id>/authorize`,
  `/deliver`, `/capture`, `/void`. Missing creds return a clean 503 with
  the setup hint (found and fixed during testing — first smoke test
  returned a 500).
- `scripts/mock_paypal.py` — local mock PayPal sandbox implementing the
  same REST surface (OAuth, orders, approve, authorize, capture, void),
  deterministic ids.
- `scripts/demo.py` — flows A (happy path) and B (timeout void),
  `--mode mock` (default) or `--mode live`.
- `tests/` — 20 tests, all passing.
- `docs/SUBMISSION.md` — Devpost draft (not submitted).
- `docs/VIDEO-SCRIPT.md` — demo video script, ~2:30.
- `README.md`, `LICENSE` (MIT, Ellis Lamplight), `requirements.txt`,
  `.env.example`, `.gitignore`.

## What actually ran (real outputs)

- `pytest tests/ -q` → **20 passed** (receipts, quotes, escrow guards,
  client round-trips against the mock, HTTP service flows).
- `scripts/demo.py --mode mock` → both flows passed:
  - A: quote $12.50, order ORDER-0001, authorization AUTH-0002,
    delivery hash `ae337bcb…`, capture CAPTURE-0003, agent paid $12.50,
    receipt `0xa0ba884b…`.
  - B: quote $8.00, order ORDER-0004, authorization AUTH-0005,
    capture-before-delivery correctly refused, voided after the 5s
    deadline, receipt `0x81641c37…`.
  - All four `demo-receipts/*.json` files verify: `verify_receipt_hash`
    and `verify_quote_hash` both True.
- Flask server smoke test: `POST /jobs` → 201 with quote + order +
  approve URL (against the mock); without credentials → clean 503.
- **Live sandbox reachability:** the PayPal client reaches
  `api-m.sandbox.paypal.com` from this VM — with dummy credentials it
  gets the real PayPal response `401 {"error":"invalid_client",
  "error_description":"Client Authentication failed"}`. The code path
  is proven; only real credentials are missing.

## What did NOT run / blockers

1. **No PayPal Developer sandbox app exists yet.** Creating one needs
   a human login at developer.paypal.com — no password was available
   and none was requested. The exact 2-minute step is in README.md
   ("Sandbox setup"): log in, Dashboard → My Apps & Credentials →
   Create App, copy Client ID + Secret into env vars, re-run
   `scripts/demo.py --mode live`. Until then, `--mode live` cannot
   run (it raises `PayPalConfigError` with that hint).
2. **No live-sandbox end-to-end run yet** — blocked on (1). The live
   path is implemented (prints approve URL, polls for APPROVED up to
   10 min) but unexecuted.
3. **Nothing submitted to Devpost.** Submission draft + video script
   are ready; the repo is not yet public on GitHub and no video is
   recorded. Those are the remaining steps before Nov 12, 3:00 PM ET.
4. The worker registry is a fixed demo list (3 workers); the hiring
   agent's negotiation policy is deterministic, not a live LLM call
   (the `llm=` hook is documented in code and README).

## Money / safety

- Sandbox only. No real money moved, no live PayPal account touched,
  no spending. Credentials are env-vars only; none are in any file
  (`.env.example` is empty placeholders; `.env` is gitignored).
- No secrets were created or stored during this build.

## Live demo attempts — Oct 27, 2026

- Credentials verified: pasted Client ID + Secret from the book file work — PayPal sandbox OAuth returns a token (TOKEN_OK, Oct 27 ~2:00 PM CDT). The file's line 1 carries a label prefix before the Client ID; extract the long token only. Line 3 is the Secret (80 chars). Line 2 is the word "Secret".
- Attempt 1: demo started, order created, approve URL printed. Killed by a runtime restart ~6.5 min in; order left CREATED, never approved.
- Attempt 2: fresh order 5EK00167W2949030A ($12.50), approve URL printed. Timed out after 10 min — order never approved.
- Checkout web flow returns PayPal genericError ("Things don't appear to be working at the moment") on phone, book regular tab, and book incognito (3 tries). Order itself verified well-formed via API (CREATED, AUTHORIZE intent, correct payee). Suspected cause: buyer == merchant (the payer logged in with the business sandbox account). Personal buyer account located in the dashboard; waiting on the payer to get its password and say ready, then re-run the demo for a fresh order.
- paypal-creds.txt still on the book — delete it after the demo completes.

- Decisive test Oct 27 ~3:29 PM EDT: a bare-bones $1.00 CAPTURE order (no description, no custom_id, no application_context) created via the API, approve link opened in a clean browser with no login and no session — SAME genericError page, code REVGQVVMVA== ("DEFAULT"). Proves the failure is PayPal's sandbox web checkout itself, not our order, browser, session, or buyer account. API works (orders create, tokens issue); the web checkout errors before any login screen. Demo run killed; will retry later when the sandbox recovers. Fallback: record the video from the mock demo (both flows pass) and submit by Nov 12.

- Decision Oct 27 ~3:35 PM EDT: live test to a second person as payee — their PayPal email as the order payee, never their credentials. The payer approves with real PayPal. Needs: the payee's PayPal email + the LIVE Client ID/Secret (dashboard Live toggle). Demo amounts shrink to pennies. Watch-out: PayPal takes ~$0.49 + 3% from the receiver, so a literal penny gets eaten by the fee; ~$1 leaves the payee something. If the live payee override is blocked by PayPal, the error will say so.

- Live test payee: the payee's PayPal email was provided Oct 27 ~3:44 PM EDT — they are the receiver, never their credentials. Still missing: LIVE Client ID + Secret (dashboard Live toggle). Amount: $0.19 (the whole PayPal balance); fee eats it, the payee gets ~nothing, flow proof only. Needs a payee parameter added to paypal_client.create_order + demo amounts shrunk to $0.19 before running.

- Live test SKIPPED Oct 27 ~3:48 PM EDT ("skip it"). Live credentials are blocked behind a PayPal for Business upgrade (personal account can't see live creds); the upgrade was declined. No account change made. PayPal balance $0.19 confirmed via Plaid. Next: retry the sandbox checkout later (last down 3:31 PM EDT); SDK button flow still untried; mock demo remains the video fallback for the Nov 12 Devpost submission.

- CORRECTION Oct 27 ~3:56 PM EDT: the sandbox was NEVER down. Every failed attempt used a link whose token was blanked out ("<redacted>") — the token is the order ID, and the redacted form is not a valid token, so PayPal showed the generic error for every attempt (phone, book, incognito, and my own browser tests). The fresh-merchant test with the REAL token (order 503093837F6191405) loaded the PayPal login page fine. Root cause was the redacted token in the links Ellis handed out, not the sandbox and not the merchant account. Earlier "sandbox down" notes in this file are wrong.
- Live demo re-run Oct 27 ~3:57 PM EDT: flow A order 2S552376TD187702C ($12.50) created, waiting on the payer's approval as the sandbox buyer (Personal account) via the real-token link.

- LIVE DEMO COMPLETE Oct 27 ~4:07 PM EDT — both flows passed against the real PayPal sandbox (exit 0). Flow A: order 2S552376TD187702C ($12.50) approved by the payer as the Personal buyer, AUTHORIZED hold 6FN15012PP742761S, delivery hash verified, CAPTURED 4UA62621MF676591E ($12.50 to agent), receipt 0x1a1debe7…. Flow B: order 13A894540F527542B ($8.00) approved, AUTHORIZED hold 3N4686080K5931948 (5s deadline), capture-before-delivery correctly refused, VOIDED after deadline, receipt 0xa1c79ab0…. Receipts in demo-receipts/ (quote-A/B, receipt-A-released, receipt-B-voided). paypal-creds.txt DELETED from the book Oct 27 ~4:07 PM EDT (verified gone). Remaining before Nov 12, 3 PM ET: push repo public, record demo video from docs/VIDEO-SCRIPT.md, upload public to YouTube, submit Devpost (needs the owner's final check).

## GitHub — repo PUBLIC Oct 27, 2026 ~3:20 PM CDT
- Repo created by the owner: https://github.com/queenellis2026-hash/lamplight-checkout (public, empty).
- First snapshot pushed via connector: 29 files (app/, scripts/, tests/, docs/, demo-receipts/, build/live-receipts/, README, STATUS, LICENSE, .env.example, .gitignore, requirements.txt). Excluded: .venv, .pytest_cache, build/ video intermediates (binary mp4/png/mp3).
- Commit dfd92eb82ea064da49a8ffd35257f747294d0677 on main. Verified by read-back: README.md + build/live-receipts/receipt-A-released.json (real live order IDs, no redaction).
- Sync record: ~/workspace/experiment/wallet/top3/paypal-checkout.muse-github-snapshot-sync.json
- Still to do: demo video (building now), upload Public to YouTube, Devpost submit (due Nov 12, 3 PM ET) — needs the owner's final check.

## Demo video + YouTube — LIVE Oct 27, 2026 ~3:21 PM CDT
- Video: build/demo-video-v1.mp4, 2:27 (146.97s), 1280x720, 30fps, H.264 + AAC, 3,832,669 bytes, md5 5eaee8564465f413d9c7c2734d31d3ca. Narration 306 words, gTTS US English. Follows docs/VIDEO-SCRIPT.md shot by shot; mock run (ORDER-0001/AUTH-0002/CAPTURE-0003) + live-proof frame (flow A 2S552376TD187702C $12.50 CAPTURED, flow B 13A894540F527542B $8.00 VOIDED). Only URL on screen is the real one: checkoutnow?token=2S552376TD187702C — no redacted token anywhere. Verified by the builder (9 frames) + Ellis (ffprobe + md5 + receipts).
- YouTube: PUBLIC on the Ellis channel (@EllisLamplight) at https://youtu.be/dT5ytBU4gb8. Title "Lamplight Checkout — escrow-style agent hiring on PayPal (PayPal AI Hackathon)". Copyright check clean, "not made for kids".
- SUBMISSION.md links filled in (repo + video). Devpost submit still needs the owner's final check.

## Devpost SUBMITTED — Oct 27, 2026 ~3:49 PM CDT
- Project: https://devpost.com/software/lamplight-checkout-escrow-for-ai-agents-on-paypal — Submission ID 1268309, submitted Oct 27, 2026 by Ellis Lamplight (queenellis2026@gmail.com), solo.
- Status: Submitted. Banner: "Project submitted! Continue to edit your project until the hackathon deadline: November 12, 2026 at 03:00pm EST." Sidebar reads "SUBMITTED TO — PayPal AI Hackathon".
- Verified on the public page: title + exact tagline, full story text verbatim (description, How we built it, What we learned, What's next, Tools used), repo link under "Try it out", demo video embedded (YouTube, https://youtu.be/dT5ytBU4gb8), Built With tags (paypal-developer-platform, Python, Flask, requests, pytest, Orders-API-v2, Payments-API, Sandbox), Additional info as answered (Individual, United States, Existing + Lamplight Pay explanation, PayPal rating 8/10 + feedback, sponsor tools "None, used other AI tools.", repo/demo URLs, testing instructions, learning Significant, career AI value Yes, all eligibility checkboxes ticked).
- FLAG — category: the Devpost form never offered a category/prize selection at any step (no "Best Use of Agentic Commerce ($5,000)" option). Submitted as-is; eligible for the hackathon's prizes as the organizers judge them. Editable until Nov 12 if a category step appears.
- Sign-in note: the saved Devpost email+password was REJECTED ("Invalid Email or password") — signed in via "Log in with Google" (Ellis / queenellis2026@gmail.com) instead. The vault's Devpost password is wrong/outdated; Google sign-in is the working route.
- A reCAPTCHA (cars grid) was cleared during the flow. Nothing else outstanding — submission complete.

## Post-review fix — Oct 27, 2026 ~5:00 PM CDT

- Reviewed by ChatGPT + Claude (full verbatim: reviews-2026-10-27.md).
  Grok's review was still running when the fix started — its findings,
  if any, are a separate round.
- ChatGPT (NEEDS WORK, 3 findings): payee never set on the order; caller
  identity self-asserted with a payer default; expired quotes could still
  authorize.
- Claude (NEEDS WORK, 6 findings): same identity issue (sharper repro);
  PayPal result status never checked before settling; worker never set as
  payee; late delivery blocks the void; lost capture response
  desynchronizes state; low bundle (now=0, no delivery lock, weak
  create_job validation, orphaned job on order failure).
- Fix (source + tests only; NOT pushed, Devpost untouched, no live demo
  re-run): `app/paypal_client.py` — create_order takes payee_email and
  passes payee {email_address} from the quote; stable PayPal-Request-Id
  per job+operation on authorize/capture/void; transport errors wrapped
  as PayPalError. `app/escrow.py` — authorize enforces quote expiry and
  requires auth status == "CREATED"; capture requires status ==
  "COMPLETED"; submit_delivery rejected at/after the deadline under a
  per-job lock (exactly-once); capture/void retry once with the same
  idempotency key, then reconcile from get_authorization (CAPTURED ->
  recorded with capture_reconciled=True, capture_id "" since it was
  never observed; VOIDED -> voided; already-captured void refused);
  now=0 handled with `now if now is not None`; failed create_order
  removes the job from the store. `app/auth.py` (new) — bearer tokens:
  payer token + per-agent tokens via LAMPLIGHT_PAYER_TOKEN /
  LAMPLIGHT_AGENT_TOKENS env vars, demo-only defaults clearly labelled;
  identity derived from the credential, never the body or a default.
  `app/server.py` — authorize/deliver/capture/void require the token and
  a matching body identity field (401 missing/unknown, 403 wrong role
  or mismatch, 400 missing field); create_job validates body-is-dict,
  non-empty string brief, non-negative integer ttl/deadline (400, not
  500). `app/receipt.py` — build_receipt takes optional
  capture_reconciled (False default). `app/quote.py` — quote_expired
  now=0 fixed the same way. `scripts/mock_paypal.py` — idempotency
  store (replay-before-execute), payee echo, one-shot forced
  next-capture-status / next-auth-status, drop-next-capture/void
  (applies state, stores idempotency response, closes the socket),
  threaded server.
- Tests: 20 -> 54, all passing (7.92s). New tests cover every fixed
  path: payee on order creation; capture DECLINED/PENDING/FAILED
  rejected (job stays DELIVERED); authorize DENIED/PENDING rejected
  (job stays QUOTED); expired-quote authorize rejected; delivery
  at/after deadline rejected; void now=0 uses now; concurrent deliveries
  exactly-once; capture/void response-loss via mock drop (idempotent
  replay recovers); capture/void response-loss via stub client
  (reconcile from authorization state, capture_reconciled=True);
  void-after-capture rejected; missing/wrong auth 401; missing identity
  field 400; body mismatch 403; agent-on-capture / payer-on-deliver /
  wrong-agent 403; bad create_job inputs 400 (non-numeric, negative,
  boolean, non-string brief, JSON-list body); create_order failure
  leaves no orphaned job.
- Mock demo re-run: both flows pass; capture-before-delivery still
  refused; printed output line-for-line the same shape as before
  (same ORDER-0001/AUTH-0002/CAPTURE-0003 ids, same delivery hash
  prefix). Receipts gained one field: capture_reconciled=false.
  The v1 video is NOT visibly stale — its on-screen values were
  run-specific anyway (receipt hashes change every run). NOT pushed
  yet — Ellis verifies and pushes.
- Still open: live sandbox re-run needed to prove payee routing
  end-to-end (demo workers' @lamplight.example addresses are not real
  PayPal accounts — a live run needs a real sandbox payee account for
  the worker). Grok's PayPal review may add a later fix round.
- No personal details or secrets in any changed file (swept — see
  fix report). Demo tokens are labelled demo-only, env-overridable.

## Round 2 fix — Grok's findings — Oct 27, 2026 ~5:16 PM CDT

All three Grok findings fixed in source + tests (66/66 pass).
Grok's Low (double-delivery race) was already fixed in round 1 by the
per-job delivery lock — confirmed, not re-done.

- Delivery-hash verification (Grok High): `app/escrow.py`
  `capture(job, *, payer, delivery_text)` now requires the original
  delivery text and runs it through `delivery_verifier` BEFORE any
  PayPal call — missing text is refused, a hash mismatch is refused
  ("delivery verification failed"), the job stays DELIVERED. The
  server wires the hiring agent's `verify_delivery` as the verifier
  (`EscrowService(..., delivery_verifier=hiring.verify_delivery)`);
  the default (no verifier injected) is the same hash comparison.
  `app/server.py` capture requires a `delivery_text` field (400 if
  missing; the payer-field match is checked before it, so the 403
  mismatch tests still behave). `scripts/demo.py` flow A passes the
  delivery text through capture; flow B's guard does too (still
  refused by the state check). Tests: empty text refused; wrong text
  refused (job stays DELIVERED); injected verifier is what runs
  (refusing verifier blocks, accepting verifier lets it through);
  `verify_delivery` wired end-to-end; HTTP missing-text 400 and
  wrong-text 409.
- Durable job store (Grok Medium): new `app/store.py` — `MemoryStore`
  (default, tests) and `JsonFileStore` (one JSON file, atomic rewrite
  on every save, corrupt file fails loudly at startup). `Job` gained
  `to_store_dict()` / `from_store_dict()` (full quote included, so a
  reloaded job still has price, payee and expiry). `EscrowService`
  takes `store=`; every state change is saved (`_save(job)` after
  create/order/authorize/deliver/capture/reconcile/void); `jobs` is
  now a read-only view over the store. Server defaults to
  `jobs.json` in the project root (env `LAMPLIGHT_JOBS_FILE` to move
  it; gitignored). Tests: restart round-trips — DELIVERED job
  captures after restart (and the capture survives a second
  restart), AUTHORIZED job voids after restart, QUOTED job keeps its
  order id and authorizes after restart; corrupt file refused.
  Single-process demo only — documented in the module (two processes
  on one file would clobber each other).
- Forged `now` (Grok Medium): the server never takes a timestamp from
  the request path — `void_job` ignores any `now` in the body and
  calls `void_after_deadline` without it, so the deadline is always
  wall-clock time. The `now` params on `void_after_deadline`,
  `submit_delivery` and `quote_expired` are documented test-only
  hooks in their docstrings (round 1's `now if now is not None`
  behavior kept). Tests: forged future `now` over HTTP does not void
  early (409 "deadline not reached", job stays AUTHORIZED).
- Tests: 54 -> 66, all passing (8.76s). New tests cover every path
  above. Existing capture calls (escrow + server tests, demo) updated
  to pass delivery_text.
- Mock demo re-run: both flows pass; capture-before-delivery still
  refused; printed output line-for-line the same shape as before
  (same ORDER-0001/AUTH-0002/CAPTURE-0003 ids, same delivery hash
  prefix ae337bcb…). Receipt keys unchanged. The v1 video is NOT
  visibly stale. NOT pushed yet — Ellis verifies and pushes.
- Still open: live sandbox re-run needed to prove payee routing
  end-to-end (demo workers' @lamplight.example addresses are not real
  PayPal accounts — a live run needs a real sandbox payee account for
  the worker). No new live proof is needed for round 2 itself: the
  verification, store and forged-now fixes are all provable offline
  (mock + tests); the live run would only re-prove the same flows
  with real sandbox accounts.
- No personal details or secrets in any changed file (swept — the only
  grep hits are pre-existing placeholders: PAYPAL_CLIENT_ID/SECRET=...
  in README/.env.example and the documented demo-only token names).

## Round 3 fix — Grok's round-2 re-review — Oct 27, 2026 ~10:20 PM CDT

Grok re-reviewed the round-2 code (Oct 27 ~10:13 PM CDT): SOUND for a
single-process demo/sandbox service, 1 Medium + 1 Low, both new. All
three round-2 fixes confirmed present and correct. The recommended
round 3 was approved (full verbatim: reviews-2026-10-27.md).

- The Low — real capture id + amount on reconcile: `app/paypal_client.py`
  gained `first_completed_capture(auth)` (first COMPLETED capture from
  the authorization's linked `captures`, None when PayPal lists none)
  and `amount_to_cents(value)` (Decimal parsing, strings only, at most
  two decimal places — no float error, no silent truncation).
  `app/escrow.py` — `Job` gained `capture_amount_cents` (None when
  unknown; persisted in to_store_dict/from_store_dict, with .get() so
  pre-round-3 store files still load); `capture()` records the amount
  from the capture result on the normal path too; `_capture_with_reconcile`
  now recovers the real id + amount from the linked captures (only an
  authorization listing none keeps the old `""` + reconciled fallback);
  shared helpers `_record_capture_amount` / `_record_captured_authorization`
  so the capture and reconcile paths cannot drift. `app/receipt.py` —
  build_receipt gained `capture_amount_cents` (null when voided or
  unknown); a reconciled receipt with a real id is now fully auditable
  against the merchant account. `scripts/mock_paypal.py` — the
  authorization resource now lists its captures (forced non-COMPLETED
  captures are NOT linked: they never moved money).
- The Medium, cheap half — single-process, LOUD: new README section
  "Deployment limits — single process only" (one server process on one
  jobs file; two processes/workers clobber each other, last writer
  wins) and a startup warning in `app/server.py` (`__main__` prints it
  to stderr, naming the jobs file). The SQLite/Postgres swap is HELD —
  only the right call if this ever becomes a real multi-worker service.
- Settle/reconcile admin endpoint: `POST /admin/reconcile` (payer
  bearer token only — 401 no token, 403 agent token), backed by
  `EscrowService.reconcile_job` (one open job: CREATED stays pending,
  CAPTURED records the real id + amount with the reconciled flag,
  VOIDED records the void, PayPal errors reported not raised) and
  `reconcile_open_jobs` (walks AUTHORIZED/DELIVERED jobs, returns
  checked / updated / still_pending / errors; one job's failure never
  stops the rest). Settled jobs are never counted.
- Tests: 66 -> 87, all passing (12.54s). New tests: amount parsing
  (exact cents, rejects non-strings and >2dp); first_completed_capture
  (skips non-completed, None when none); normal capture records the
  amount; mock authorization lists linked captures; reconcile with real
  id + amount; reconcile fallback when no capture listed; reconcile
  still-pending (CREATED), voids, captured-without-list, skips settled
  jobs, reports PayPal errors, refuses settled jobs; old store file
  without capture_amount_cents still loads; receipt records the amount
  and a reconciled receipt with a real id verifies; HTTP admin endpoint
  (401/403, empty summary, still-pending, captures a DELIVERED job
  with real id + amount and a released receipt, voids an AUTHORIZED
  job); startup warning names the jobs file and the limit.
- Mock demo re-run: both flows pass, exit 0; printed output the same
  shape as before (same ORDER-0001/AUTH-0002/CAPTURE-0003 ids, same
  delivery hash prefix ae337bcb…). Receipts gained one field:
  capture_amount_cents=1250 (released) / null (voided); both hashes
  verify. The v1 video is NOT visibly stale — the demo output form is
  unchanged (receipt hashes are run-specific anyway). Server smoke test:
  startup warning prints with the jobs file path; /admin/reconcile
  returns 401 with no/bad token.
- No personal details or secrets in any changed file (swept). Demo
  tokens are pre-existing, labelled demo-only, env-overridable.
- Held: SQLite/Postgres store; partial capture + residual void;
  webhook listener. Live sandbox payee-routing proof still outstanding
  (needs a real sandbox payee account for the worker).

# Property-Based Testing & Reconciliation — Game Plan

**Status:** approved by Chris, Nov 3 2026. Build it right or not at all.
**Base:** v8.14 working tree (944 tests passing). No version bump — this is v8.14+ infrastructure.
**Research base:** `~/workspace/research_notes/payment-systems-engineering-deep-dive-20261104-0146/report.md`
(cited below as "deep dive §N").

## Why this exists

v8.12 → v8.13 → v8.14 were reactive: a reviewer found a money bug, we patched it.
The dispute wedge, the clear-then-capture race, the fail-open honor math — all of them
were state-transition or retry/race bugs that property-based testing is designed to find
*before* a human does. WeChat Pay's UAT++ found 59 previously unknown bugs with this
approach (deep dive §2). Every postmortem in the deep dive is a retry or a race meeting
a missing idempotency mechanism or a missing database constraint (deep dive §6).

The shift: **generate the failures ourselves instead of waiting for Grok.**

## What already exists (do not rebuild)

Inventory first, so no phase duplicates work:

| Exists | Where | Notes |
|---|---|---|
| Hypothesis pinned | `requirements.txt` (`hypothesis==6.170.14`) | Ready to use |
| Dispatcher property tests | `tests/test_dispatcher_properties.py` | Random walks over the §5 table; checks I1 fund conservation, I4 idempotent replay. **Dispatcher-level only** — the pure `transition()` function. |
| 13-state FSM | `app/dispatcher.py` `State` enum + `transition()` | Pure function — ideal PBT target |
| Deterministic PSP keys | `EscrowService._request_id`, `_capture_request_id` | `lamplight-{op}-{job_id}[-attempt-{n}]`. Retry reuses the key. |
| Error taxonomy (partial) | `_is_transient_paypal_error`, `_is_order_absent` in `app/escrow.py` | Transient vs definitive exists; not yet centralized or exhaustive |
| Webhook claim lifecycle | `webhook_event_claims` table (`app/store.py`) | claimed → processing → completed/failed; atomic conditional INSERT |
| Audit trail | `audit_events` table | Every state transition audited |
| Reconciliation report | `EscrowService.ledger_reconciliation_report` | Read-only: books vs fresh PayPal reads. **Not scheduled, not persisted, no alerting.** |
| Controllable clock | `EscrowService._now()` | Overridable in tests |
| Per-job locks | `_per_job_locked` | Single-process only (documented) |
| Mock PayPal | `scripts/mock_paypal.py` (subprocess, port 5005) | Used by integration tests via module-scoped fixture |

**The gaps this plan closes:** service-level state machine testing (reviewer bugs lived
in `EscrowService`, not the dispatcher), idempotency contracts on HTTP routes,
fault injection, scheduled+persisted reconciliation, centralized error taxonomy,
recovery points.

---

## Phase 1: Property-Based State Machine Testing (EscrowService level)

**Goal:** A Hypothesis `RuleBasedStateMachine` that drives `EscrowService` public
methods through random operation sequences and asserts money invariants after every step.

**Why first:** Every reviewer money bug so far (dispute wedge, clear-then-capture,
fail-open honor) was an *escrow-service-level* state bug. The dispatcher already has
property tests; the service does not. This phase finds the next Grok bug before Grok does.

### Prerequisites
- Hypothesis (have it). No new dependencies.

### The one hard decision: the PayPal double

The existing tests use `scripts/mock_paypal.py` as a subprocess on port 5005.
That is too slow and too stateful for hundreds of Hypothesis examples — each example
needs a *fresh, deterministic* PayPal.

**Build `tests/fake_paypal.py`: an in-process `FakePayPalClient(PayPalClient)`**
backed by plain dicts. It implements the same methods (`create_order`,
`authorize_order`, `get_authorization`, `capture_authorization`,
`void_authorization`) against in-memory order/authorization records, and it exposes
hooks the later phases need:

- `fail_next(operation, error)` — raise a programmed `PayPalError` once (Phase 3)
- `delay_next(operation, seconds)` — sleep past the client timeout (Phase 3)
- `record` of every call: `(operation, idempotency_key, params)` — assertions inspect this

The fake must honor idempotency keys the way PayPal does: same key + same params →
replay the recorded response; same key + different params → error. **This behavior
is itself a contract test** (see Phase 2) — write it first, it's ~40 lines.

Keep the subprocess mock for the existing 944 integration tests. The state machine
uses the fake. Both must agree on the mock's observable contract; a small
conformance test (`tests/test_fake_conformance.py`) runs 3-4 scripted scenarios
against both doubles and asserts identical outcomes.

### The machine: `tests/test_escrow_state_machine.py`

```python
class EscrowMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.paypal = FakePayPalClient()
        self.svc = EscrowService(self.paypal, store=MemoryStore())
        self.svc._now = lambda: self.clock  # controllable time
        self.jobs = Bundle("jobs")          # Hypothesis-managed job handles
```

**Rules** (each is a `@rule` drawing from bundles/strategies):

| Rule | Service method | Precondition |
|---|---|---|
| `create` | `create_job(quote, payer, agent)` | always |
| `order` | `create_order(job)` | job in S1 |
| `authorize` | `authorize(job)` | job in S2/S3 |
| `deliver` | `submit_delivery(job, agent, text)` | job in S4 |
| `capture` | `capture(job, payer, text)` | job in S6 |
| `capture_partial` | `capture_partial(job, payer, text, amount)` | job in S6/S8 |
| `void` | `void_after_deadline(job, payer)` | job in S4/S6 |
| `dispute` | `dispute_job(job, payer, reason)` | job in S6, honor remains |
| `force_settle` | `force_settle(job, payer)` | job in S6 + needs_review |
| `sweep_disputes` | `check_inspection_deadlines(now)` | always (advances clock first) |
| `reconcile` | `reconcile_job(job)` | always |

**Time model:** `self.clock` starts at a fixed epoch. A `@rule` `advance_time`
moves it forward by 1s–96h. Dispute deadlines (72h), honor windows (3d−1h),
and quote expiry all key off `svc._now()`, so time travel is fully deterministic.

**What NOT to model yet:** the hiring agent, webhooks (Phase 3 covers them),
the HTTP layer (Phase 2 covers it). First version models the service API only.

### Invariants (checked in a `@invariant()` after every step)

Money invariants (deep dive §2 — properties, not examples):

1. **Conservation:** `captured_amount_cents + voided_amount_cents <= authorized_amount_cents`
   for every job; `== 0` captured before authorization.
2. **No negative money:** every amount field `>= 0`; amounts are `int` cents, never float.
3. **N retries = one effect:** the fake's call record shows at most one *executed*
   PSP capture per capture-attempt key; replays return the recorded response.
4. **Terminal sinks:** a job in a terminal dispatcher state (`S10a/b/c`, `S11`)
   never leaves it — any rule targeting a terminal job must raise or no-op.
5. **Dispute/pause integrity (the v8.13/v8.14 lessons, encoded as law):**
   - `needs_review` set ⟹ job status is DELIVERED-family (S6/S7/S8/S9), never terminal.
   - `dispute_deadline <= authorized_at + 3d − 1h` (honor clamp), when `authorized_at` known.
   - `needs_review` cleared only in the same write as a terminal capture status.
   - A second `dispute_job` while `needs_review` is set raises (409).
6. **Legal transitions only:** the service-level job status moves only along edges
   the dispatcher's §5 table allows — cross-check via `fsm_divergence_count() == 0`
   (the codebase already tracks divergences; the machine asserts the counter stays zero).
7. **Audit completeness:** every rule that changed job state produced ≥1 `audit_events`
   row for that job with a non-null idempotency key where a PSP call occurred.

Invariant 6 is the highest-value one: it turns the existing divergence counter
into a property the machine enforces.

### Scope & difficulty

- **New files:** `tests/fake_paypal.py` (~250 lines), `tests/test_fake_conformance.py`
  (~80 lines), `tests/test_escrow_state_machine.py` (~600–800 lines).
- **Estimated runtime:** 100 examples × ~15 steps against in-process fake ≈ 3–8 min.
  Mark `@settings(max_examples=100, deadline=None)` and register a `slow` pytest marker;
  runs nightly and on demand, not on every commit (a 25-example "smoke" profile runs in CI).
- **Hard parts (honest):**
  - *Shrinking.* When Hypothesis finds a failing 30-step sequence, the shrinker
    must replay deterministically — the fake and clock make this possible, but
    stateful shrinking is the fiddliest part of RuleBasedStateMachine work.
    Budget time for it.
  - *Precondition modeling.* Rules must not draw jobs in states where the method
    trivially 409s, or every example is noise. Use `Bundle` + targeted preconditions;
    expect 2–3 iterations to get the generator producing interesting sequences.
  - *The fake's fidelity.* If the fake disagrees with the real mock on edge
    behavior, the machine proves properties of the wrong system. The conformance
    test is the guardrail — keep it green.
- **Straightforward parts:** the invariant list (they're already understood from
  the review rounds), the clock injection (`_now` is already a method).

### Definition of done

- `pytest tests/test_escrow_state_machine.py` green at 100 examples, 3 consecutive runs.
- At least one previously-unknown edge found and fixed (or a written note explaining
  why the machine found nothing — "Praise survives paraphrase. Caveats do not.").
- Conformance test green: fake and subprocess mock agree on scripted scenarios.

---

## Phase 2: Idempotency Contract Tests (HTTP layer)

**Status: COMPLETE (Nov 3, 2026).** 61 new tests, 1043 total passing.

**What was built:**
- `app/store.py`: `idempotency_records` table (SQLite) + dict (MemoryStore,
  JsonFileStore) with atomic claim lifecycle: `claim_idempotency_key` →
  "claimed" | "replay" | "conflict" | "in_progress"; `complete_/`fail_`
  + 24h TTL janitor + 2000-record cap.
- `app/server.py`: `@idempotent` decorator on all 14 mutating POST routes
  (all `/jobs/*`, `/admin/reconcile`, `/admin/reconcile/findings/*/ack`,
  `/resolve`). Same key + same params → byte-identical replay with
  `X-Idempotent-Replayed: true`; same key + different params → 422;
  concurrent double-POST → exactly one executes; malformed key → 400;
  handler exception → claim marked failed (never replays a 500).
  `/webhooks/paypal` and `/mock/*` are explicitly out of scope (webhook
  claims and test proxy respectively).
- `tests/test_idempotency_contracts.py`: 57 tests — 14 routes × (replay,
  conflict-422, concurrent, no-key-passthrough) + malformed-key.
- `tests/test_route_coverage.py`: 4-test CI gate enumerating
  `app.url_map` — a new POST route without a registry entry or a
  documented skip reason FAILS. The gate caught 2 routes the author
  missed (`/admin/reconcile/findings/*/ack|resolve`) on its first run.

**Goal:** Every mutating POST route proves: same idempotency key twice → same
response, exactly one money effect. A new POST route without a contract test fails CI.

**Why:** Deep dive §1 — Stripe's contract, Airbnb's lease model. Our service layer
has deterministic keys, but the HTTP layer's contract is unproven. The blue-green
postmortem (847 duplicate charges from an idempotency column missing in one
environment) is the cautionary tale: the contract must hold at the layer clients
actually call.

### Prerequisite feature (small, required)

Check whether `app/server.py` currently accepts an `Idempotency-Key` request header
and returns the replayed response. If not (likely), add it:

- On mutating POSTs, read `Idempotency-Key` header (optional; when absent, generate
  server-side per request as today).
- Store `(key → status_code, body)` — a new `idempotency_records` table in
  `app/store.py`, or reuse `audit_events.idempotency_key` with a response cache.
  **Same-transaction rule:** the key+response row commits in the same DB transaction
  as the state change (deep dive §1: "store key + response in the same transaction
  as the state change").
- Same key + different params → `422` (Stripe behavior — catches client misuse).
- 24h retention; a janitor deletes expired rows.

This is the only app-code feature Phase 2 needs. Everything else is tests.

### Deliverables

- `app/store.py`: `idempotency_records` table + methods (or equivalent).
- `app/server.py`: header handling on the 10 mutating POST routes:
  `/jobs`, `/jobs/<id>/authorize`, `/deliver`, `/capture`, `/capture-partial`,
  `/void`, `/force-settle`, `/dispute`, `/dispute/note`, `/admin/reconcile`.
- `tests/test_idempotency_contracts.py`: parametrized matrix. For each route:
  1. `POST` with `Idempotency-Key: K` → 200, record money effect E (capture ids,
     audit rows, job status).
  2. `POST` identical body with `Idempotency-Key: K` → 200, byte-identical body,
     money effect unchanged (no new capture id, no new audit money row).
  3. `POST` with `Idempotency-Key: K` but different amount → 422.
  4. Concurrent double-POST with the same key (two threads) → exactly one wins,
     both callers get a 200 (one live, one replayed).
- **CI gate** `tests/test_route_coverage.py`: enumerates `app.url_map` for POST
  routes and asserts each appears in the contract test's route registry. A new
  POST route without a registered contract → test failure. (Deep dive §2:
  "contract tests as gates.")

### Scope & difficulty

- **~400 lines tests + ~150 lines app code.** Runtime < 1 min. **Straightforward.**
  The concurrent case needs the two-thread harness — reuse the pattern from
  `test_v814_pause.py` (it already does two-connection concurrency).
- **Risk:** the response-replay cache must not leak payer tokens or secrets into
  stored bodies — reuse the existing `_trim_value`/sanitizer helpers in `app/escrow.py`.

---

## Phase 3: Fault Injection / Chaos Tests

**Status: COMPLETE (Nov 3, 2026).** 22 new tests (11 scenarios × MemoryStore +
SqliteStore), 1065 total passing.

**What was built:**
- `tests/chaos_paypal.py`: ChaosPayPalClient over the Phase 1 fake.
  One-shot deterministic faults: `crash_after(operation[, error])`
  (accept-then-raise: the kill window), `slow_next(operation, seconds)`,
  `forget_idempotency_keys()` (24h expiry mid-retry); inherited
  `fail_next` covers pre-accept transport/5xx/429. Read tracking
  (`read_log`) for verify-then-act ordering assertions.
- `tests/test_fault_injection.py`: crash-between-PSP-and-DB-write,
  408-slow-success (replay, no new key), slow-PSP single settlement,
  duplicate/out-of-order/concurrent webhooks, key-expiry-mid-retry
  (read-first reconcile, no double-execute), 429-then-success,
  null-deadline pause sweep-captures (scenario 8), the
  clear-then-capture kill-window regression (flag stays set at the
  PSP call; concurrent pause 409s without moving the deadline), and
  sweep-retry-after-capture-error (flag stays set, deadline in the
  past, next sweep captures once).
- One test-assertion fix during the build (not a code bug): the
  429 scenario's "exactly one execution" check was too strict — the
  fake logs the 429'd attempt as non-replay; money truth (one capture
  on the auth) + single idempotency key is the correct assertion.

**Goal:** Prove the system survives the exact kill-windows from the postmortems:
crash between PSP call and DB write, slow-success timeouts, duplicate/out-of-order/
concurrent webhooks, expired idempotency keys mid-retry.

**Why:** Deep dive §2: "Test the ambiguous middle, specifically." One postmortem's
root cause was literally "the eval harness had no scenario simulating a slow-success
timeout" — the exact failure that then occurred in production.

### The vehicle: `tests/chaos_paypal.py`

Extends the Phase 1 fake with a programmable fault script:

```python
chaos = ChaosPayPalClient()
chaos.script("capture", [
    Fault.after_psp_record_crash(),   # PSP "accepted", then raise ConnectionError
    Fault.slow_success(delay=9.0),    # responds OK after client timeout
    Fault.duplicate_key_forget(),     # forgets idempotency keys (simulates expiry)
])
```

Fault primitives needed:
1. `crash_after_psp` — the fake records the capture as accepted, then raises
   (simulates process kill between PSP call and DB write).
2. `slow_success` — sleeps past the client's timeout, then returns success
   (the ambiguous middle).
3. `transport_error` — `status=None` connection failure (transient).
4. `http_500` / `http_429` — retryable server errors.
5. `key_forget` — drops recorded idempotency responses (simulates 24h expiry mid-retry).
6. `duplicate_webhook` / `reorder_webhook` — for the webhook tests (drives
   `handle_webhook_event` directly).

Deterministic: faults fire in script order, no randomness in the fault itself
(randomness belongs in Phase 1's operation generator).

### Scenarios: `tests/test_fault_injection.py`

For each scenario, on **both** stores (MemoryStore, SqliteStore — the existing
`svc` fixture pattern):

| # | Scenario | Expected behavior |
|---|---|---|
| 1 | Crash between capture PSP call and DB write; then retry with same key | Retry replays the recorded capture; exactly one capture id on the job; no double-charge |
| 2 | Slow-success timeout on capture (client gives up, PSP succeeded) | Job stays PENDING (not failed); recovery path re-queries with the *original* key (verify-then-act), finds the capture, records it. **No blind retry with a new key.** |
| 3 | Same webhook delivered twice | Second delivery is a no-op (claim lifecycle: `webhook_event_claims` already completed) |
| 4 | Webhooks out of order (CAPTURED before AUTHORIZED) | No invalid transition; job settles correctly once both arrive |
| 5 | Concurrent webhook delivery (two threads, same event) | Exactly one claims it; single money effect |
| 6 | Idempotency key expired mid-retry (key forgotten, then retry) | System detects the unknown-key response, re-derives the deterministic key, and reconciles — or fails closed with a clear wedge + alert, **never** double-executes silently |
| 7 | 429 rate-limit during capture, then success | Treated as transient; retried with same key; single effect |
| 8 | Crash between dispute flag set and deadline write | v8.14 behavior: the sweep's `_dispute_sweep_action` treats null deadline as "capture", not skip-forever. Assert it. |

Scenario 8 is a regression test for the exact bug Grok's precise instructions fixed —
now proven by fault injection instead of trusting the code review.

### Scope & difficulty

- **New files:** `tests/chaos_paypal.py` (~200 lines), `tests/test_fault_injection.py`
  (~500–700 lines).
- **Runtime:** ~2–4 min (both stores × 8 scenarios).
- **Medium difficulty.** The chaos proxy is straightforward engineering. The hard
  part is scenarios 2 and 6: they assert *recovery semantics* (verify-then-act),
  which may expose that the current code blind-retries somewhere. **If a scenario
  fails because the code is wrong, that's the phase working as intended — fix the
  code, don't weaken the scenario.** Budget fix-round time.

---

## Phase 4: Reconciliation System

**Goal:** A nightly job that compares the books against PayPal, persists findings,
alerts only on *new* divergences, and gives a human a review path. The mandatory
last line of defense (deep dive §3: "build it as an engineering deliverable, not
an ops afterthought"; Twilio's double-bookkeeping is what saved them).

**Why:** Property tests and fault injection prove the code handles the failures
we imagined. Reconciliation catches the ones we didn't. It is the only phase
that can catch a bug in production rather than in tests.

### What exists vs what's missing

`ledger_reconciliation_report` already compares books vs fresh PayPal reads
(read-only, correct semantics including the PARTIALLY_CAPTURED caveat). Missing:
scheduling, persistence, alerting, human review.

### Deliverables

1. **Schema** (`app/store.py`):
   - `reconciliation_runs(run_id TEXT PK, started_at INT, finished_at INT,
     jobs_checked INT, divergent_count INT, status TEXT)` — `status`:
     `ok | divergent | error`.
   - `reconciliation_findings(finding_id TEXT PK, run_id TEXT, job_id TEXT,
     issue_type TEXT, detail TEXT, first_seen INT, status TEXT,
     resolved_at INT NULL)` — `status`: `open | acknowledged | resolved`.
     `issue_type` is the machine-readable prefix the report already emits
     (`book_capture_not_listed_by_paypal`, `amount_mismatch`,
     `paypal_capture_not_on_books`, `status_mismatch`, ...).
2. **Runner** `scripts/reconcile_nightly.py` (also importable):
   - Runs `ledger_reconciliation_report()`.
   - Writes the run + findings rows.
   - **New-divergence alerting:** a finding alerts only if no `open` finding with
     the same `(job_id, issue_type)` already exists. Repeat offenders don't page twice.
   - Alert sink: append to a JSONL alert log + optional webhook URL from env
     (`RECONCILE_ALERT_URL`). Start with the log — Chris reads it in the morning
     brief. No email/SMS infrastructure in this phase.
   - Exit code nonzero on `error` status (cron sees it).
3. **Human review routes** (`app/server.py`, admin-authed):
   - `GET /admin/reconcile/runs` — recent runs.
   - `GET /admin/reconcile/findings?status=open` — open findings.
   - `POST /admin/reconcile/findings/<id>/ack` and `/resolve` — with a required
     `note` field (who looked, what they decided — the audit trail matters).
4. **Tests** `tests/test_reconciliation.py`:
   - Seed a deliberate divergence (book says CAPTURED, fake PayPal says VOIDED)
     → nightly runner flags it, alert log gets exactly one entry.
   - Second run with same divergence → no second alert (dedupe works).
   - Ack/resolve flow via the admin routes.
   - A run where the PayPal read fails → `error` status, finding recorded as
     `paypal_read_failed`, **not** counted as a divergence (absence of evidence
     is not evidence of divergence — the report already encodes this; the test
     locks it in).

### Scope & difficulty

- **~400–600 lines app code + ~300 lines tests. Medium difficulty, straightforward
  engineering.** No cleverness required — this phase is plumbing. It is also the
  most *independent* phase: it touches no money paths, only adds tables, a script,
  and read-mostly routes. It can be built in parallel with Phase 1.

### Explicit non-goal: the double-entry ledger migration

The textbook answer (deep dive §3) is an append-only double-entry journal with
balances derived via SUM. Our money currently lives in job JSON fields
(`capture_ids`, `capture_amount_cents`) plus `audit_events`. Migrating to a true
ledger is a data-model change across a 9,109-line service — correct long-term,
but it is **Phase 6 (post-Nov 12)**, not this plan. Phase 4's reconciliation runs
on the existing model, which the current report already handles correctly.

---

## Phase 5: Error Taxonomy & Recovery Points

**Status: COMPLETE (Nov 3, 2026).** 45 new tests (29 taxonomy + 16 recovery),
1110 total passing.

**What was built:**
- `app/errors.py` (new): `ErrorClass` enum (RETRYABLE/DEFINITIVE/AMBIGUOUS)
  + `classify_paypal_error(error, *, request_was_sent)`. The
  `request_was_sent` parameter is the critical bit — a timeout before
  the request left is RETRYABLE, after send is AMBIGUOUS. 429 is always
  RETRYABLE (server said "not now"); 4xx/404/422/401/403 always
  DEFINITIVE; PayPalConfigError always DEFINITIVE.
- Job model: `capture_recovery_point` field (None | "intent_saved" |
  "psp_called"), persisted in `to_dict`/`to_store_dict`, loaded via
  `.get()` in `from_store_dict` (old files load with None). NOT on
  the public dict.
- `_set_pending_capture` sets "intent_saved" (same save as intent);
  `_clear_pending_capture` clears to None; `_capture_core` sets
  "psp_called" immediately after the PSP call returns or raises
  (before result processing).
- `_verify_then_act(job, error, error_class)`: canonical ambiguous-
  outcome helper. Audits the classification, delegates to the
  battle-tested `_capture_with_reconcile`. Single choke point for
  future branches.
- `_capture_core` retry path: explicit `psp_called` resume audit
  before `_retry_pre_read` (which already reads-first).
- `tests/test_error_taxonomy.py` (29): exhaustive status × send-state
  matrix, config-error definitiveness, the request_was_sent flip,
  429-never-ambiguous, legacy helper mapping
  (`_is_transient` ⟺ RETRYABLE pre-send; `_is_order_absent` ⟹
  DEFINITIVE), no-unclassified-errors across fake/chaos shapes.
- `tests/test_recovery_points.py` (16, both stores): point lifecycle
  (None → intent_saved → psp_called → None), old-file compat, crash
  at intent_saved resumes safely, crash at psp_called verifies-
  then-acts, ambiguous timeout never uses a new key (single-key
  invariant across 3 consecutive 500s), raw crash leaves intent_saved,
  `_verify_then_act` recovers a simulated accepted-but-lost capture.

**Goal:** Every PayPal error explicitly classified; the capture path persists
recovery points so a crashed request *resumes* instead of *restarts*; ambiguous
PSP timeouts always verify-then-act, never blind-retry.

**Why:** Deep dive §3 — "a PSP timeout is neither success nor failure." The single
most expensive postmortem mistake was treating ambiguous outcomes as failures and
blind-retrying into double charges. Our `_is_transient_paypal_error` is a start;
this phase makes the taxonomy exhaustive and the recovery explicit.

### Deliverables

1. **`app/errors.py` — the taxonomy module:**
   ```python
   class ErrorClass(Enum):
       RETRYABLE    # transport failure, 5xx, 408, 429 → retry with SAME key
       DEFINITIVE   # 4xx with semantic meaning (404 order gone, 422 void too late)
                    # → do not retry; transition the job accordingly
       AMBIGUOUS    # timeout after request sent, connection reset mid-response
                    # → verify-then-act: re-query with the ORIGINAL key first
   def classify_paypal_error(error: PayPalError, *, request_was_sent: bool) -> ErrorClass
   ```
   The `request_was_sent` parameter is the critical bit: a timeout *before* the
   request left is RETRYABLE; a timeout *after* it was sent is AMBIGUOUS. Callers
   must know which side of the send they were on — this forces the Pre/RPC/Post-RPC
   discipline from Airbnb's Orpheus (deep dive §1: "no network calls in the DB
   phases, no DB work in the network phase").
2. **Classification audit:** every `except PayPalError` site in `app/escrow.py`
   annotated with its `ErrorClass` and the reasoning. A test
   (`tests/test_error_taxonomy.py`) asserts that every `PayPalError` raised by
   the fake/chaos doubles maps to a class — no unclassified errors.
3. **Recovery points on the capture path.** Add `recovery_point` to the job dict:
   `intent_saved → psp_called → recorded`. Persisted at each step (same write as
   the step's other state). On entry, `capture()` checks the point and resumes:
   - `psp_called` without `recorded` → verify-then-act with the original key
     (re-query PayPal; if captured, record; if not, retry the call with the same key).
   - Never restart from `intent_saved` after the PSP was called — that path is
     how double captures happen.
4. **Verify-then-act helper:** `_verify_then_act(job, operation)` centralizes the
   ambiguous-outcome path: re-read PayPal with the original idempotency key,
   then decide (record / retry-same-key / wedge-with-alert). All ambiguous
   branches route through it.

### Tests

`tests/test_recovery_points.py`, driven by the Phase 3 chaos proxy:
- Kill at each recovery point (`intent_saved`, `psp_called`) → resume → exactly
  one money effect, job reaches the correct terminal state.
- Ambiguous timeout → assert the *first* action after the timeout is a PayPal
  *read* with the original key, not a second *capture* call (inspect the chaos
  proxy's call record — order matters).
- Every `ErrorClass` has at least one test proving a representative error maps
  to it and the service behaves accordingly.

### Scope & difficulty

- **~400 lines app code + ~400 lines tests. Medium-hard.** Recovery points touch
  `_capture_core` / `_capture_via_dispatcher` — the most complex code in the
  service. The taxonomy itself is straightforward; threading it through every
  call site without breaking the 944 existing tests is the work.
- **Depends on Phase 3** (the chaos proxy is the test vehicle). Cannot start
  before the proxy exists, but the taxonomy module (`app/errors.py`) can be
  written in parallel with Phase 1.

---

## Build order & dependencies

```
Phase 1 (state machine) ──→ finds the next reviewer bug
Phase 2 (idempotency contracts) ──→ needs small server.py feature; parallel with 1
Phase 4 (reconciliation) ──→ fully independent; parallel with 1; immediate prod value
Phase 3 (fault injection) ──→ needs Phase 1's fake_paypal.py as its base
Phase 5 (taxonomy + recovery) ──→ needs Phase 3's chaos proxy; errors.py can start early
```

**Recommended sequence:** 1 → 2 → 4 → 3 → 5.
(Rationale: 1 is the highest-leverage bug finder; 2 is quick; 4 is independent and
gives production value immediately; 3 builds on 1's fake; 5 builds on 3's proxy.)

**Parallel tracks:** one worker on Phase 1, another on Phase 4 — they share no code.

## Running it all

- New pytest markers: `slow` (Phase 1 full run), `nightly` (Phase 1 + reconciliation
  dry-run). Register in `pytest.ini`/setup.cfg.
- CI: fast suite (existing 944 + Phase 2 + Phase 3 + Phase 5 unit tests) on every
  commit. `slow` profile (Phase 1 at 100 examples) on a nightly cron — same
  cadence as the existing `ellis-night-explorations`.
- The reconciliation nightly job is itself cron-scheduled (the existing cron
  infrastructure), not just a script.

## What "done" looks like

1. Phase 1 machine green at 100 examples, 3 consecutive runs, ≥1 new edge found
   (or a written why-not note).
2. Phase 2: all 10 POST routes have contract tests; the route-coverage gate is green.
3. Phase 3: all 8 fault scenarios green on both stores.
4. Phase 4: nightly job runs, persists, dedupes alerts; admin review routes work;
   divergence-seeding test green.
5. Phase 5: taxonomy exhaustive (test-proven); recovery points on the capture path;
   verify-then-act proven by call-record ordering.

## Honest difficulty ranking

| Phase | Difficulty | Why |
|---|---|---|
| 1 — State machine | **Hard** | Stateful shrinking, precondition modeling, fake fidelity. The highest leverage and the hardest. |
| 5 — Taxonomy + recovery | **Medium-hard** | Touches the most complex code path; 944 existing tests must stay green. |
| 3 — Fault injection | **Medium** | Straightforward once the fake exists; failures found are *supposed* to hurt. |
| 4 — Reconciliation | **Medium** | Pure plumbing; independent; no money-path changes. |
| 2 — Idempotency contracts | **Straightforward** | One small feature + a test matrix. |

## Non-goals (explicit)

- **No version bump.** This is v8.14+ infrastructure.
- **No multi-worker hardening.** Single-process remains the supported topology
  (the server already refuses multi-worker boot).
- **No double-entry ledger migration.** Phase 6, post-Nov 12.
- **No arbiter system.** Still out of scope (Grok agreed: not before Nov 12).
- **No PayPal Partner application.** Business process, tracked separately;
  the demo is unaffected.

---

*Plan written Nov 3, 2026. Next step: Chris's go on Phase 1 (or a parallel 1+4 start).*

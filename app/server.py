"""Flask service for Lamplight Checkout.

Run:  .venv/bin/python -m app.server   (or: flask --app app.server run)
Env:   PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET, PAYPAL_BASE_URL (optional).
       With no credentials the service still starts; any endpoint that
       touches PayPal returns 503 with the sandbox-setup hint until the
       env vars are set. For a fully offline run use the mock server:
       see scripts/demo.py --mode mock.
       LAMPLIGHT_PAYER_TOKEN, LAMPLIGHT_AGENT_TOKENS (required): bearer
       tokens for the mutating endpoints — see app/auth.py. The server
       refuses to boot unless these are set to real values
       (validate_auth_config); demo tokens work only under the explicit
       opt-in LAMPLIGHT_DEMO_AUTH=1, never as defaults.
       LAMPLIGHT_JOBS_FILE (optional): where jobs are persisted —
       default jobs.json in the project root (see app/store.py).
       LAMPLIGHT_PAYPAL_WEBHOOK_ID (optional): the webhook id from the
       PayPal dashboard, for POST /webhooks/paypal signature
       verification. Without it the webhook endpoint is 503.

Caller authentication (app/auth.py)
------------------------------------
The mutating endpoints (authorize, deliver, capture, void, force-settle,
dispute, admin/reconcile)
require a bearer token, and the caller's identity is derived from the token —
never from the request body and never from a default. The body must
still carry the identity field (``payer`` / ``agent``) and it must
agree with the credential; a missing field is 400, a wrong token is
401, a valid token for the wrong party is 403. This is demo-grade
shared-secret auth, not production identity.

v8.12 — UNAUTHENTICATED ENDPOINTS (demo scope, documented explicitly):
GET /jobs/<id> carries NO authentication. This is a deliberate demo-scope
decision, not an oversight: it returns only public-safe data
(``to_public_dict()`` / ``to_public_receipt()``) — no PayPal ids, no
bearer tokens, no PII. See the v8.2 F3/F4 findings.

v8.14 — STOREFRONT GATE (demo-grade, honest about it):
POST /jobs and GET /jobs now require a shared-secret bearer token
(``LAMPLIGHT_DEMO_TOKEN``, demo default under ``LAMPLIGHT_DEMO_AUTH=1``;
see app/auth.py). Valid payer/agent tokens also pass. Rationale: creating
a job does NOT move money — it creates a QUOTE and a PayPal order that
only the payer can approve (via PayPal's own approval URL) — but leaving
the endpoints completely naked invites casual Create-Order spam. Abuse is
bounded by the MAX_OPEN_JOBS cap (429 when exceeded), the expired-quote
sweep, the v8.13 process-wide Create Order token bucket (default 30/min,
429 + Retry-After), and now the gate. This is a shared secret, not
identity — the production gate below still applies.

PRODUCTION AUTH GATE (design, not yet implemented):
Before any real deployment, gate these endpoints:
  - POST /jobs: require a caller identity (API key or OAuth client).
    Rate-limit per caller. The quote flow stays public in shape but
    attributable in fact.
  - GET /jobs: require the caller's token; return only that caller's
    jobs (payer sees their jobs, agent sees their jobs).
  - GET /jobs/<id>: require the caller's token; 403 unless the caller
    is the job's payer or agent.
The ``to_public_dict()`` / ``to_public_receipt()`` split stays — it is
the second layer, not the first.

Endpoints:
  GET  /                       service status
  POST /jobs                   {brief, deadline_seconds?} -> quote + PayPal order
  GET  /jobs                   list jobs
  GET  /jobs/<id>              job + receipt (once settled)
  POST /jobs/<id>/authorize    payer token: place the hold (buyer must approve first)
  POST /jobs/<id>/deliver      agent token: {agent, delivery_text} -> delivery hash
  POST /jobs/<id>/capture      payer token: {payer, delivery_text} -> verify the
                               delivery hash, then release held money to the agent
  POST /jobs/<id>/capture-partial payer token: {payer, delivery_text,
                               amount_cents} -> release part of the hold
                               and void the rest in one move (round 8).
                               An amount equal to the hold degenerates
                               to a normal capture; the receipt shows
                               the split (captured + voided = the hold).
  POST /jobs/<id>/void         payer token: {payer} -> release hold after deadline.
                               Any `now` field in the body is ignored — the
                               deadline is always wall-clock time.
  POST /jobs/<id>/dispute      payer token: {payer, reason} -> PAYMENT PAUSE
                               (v8.14 honest labeling): pause the 48h
                               auto-capture until dispute_deadline (72h,
                               honor-clamped); the sweeper then captures
                               anyway — silence pays the worker. NOT an
                               adjudicated dispute: no arbiter, no
                               review, no path to not-pay. The response
                               body's payment_pause block states this
                               plainly.
  POST /jobs/<id>/dispute/note agent token: {agent, note} -> worker's
                               counter-note on an active pause, stored
                               as chargeback evidence only (never
                               changes the outcome).
  POST /admin/reconcile        payer token: walk every AUTHORIZED/DELIVERED
                               job and force a get_authorization + state
                               update from PayPal, for use after a
                               prolonged outage. Returns a summary
                               (checked / updated / still_pending / errors).
  POST /webhooks/paypal        PayPal webhook events (signature-verified):
                               authorization created/voided, capture
                               completed/denied. No bearer token — the
                               PayPal signature is the auth. Jobs settle
                               themselves when PayPal says money moved;
                               the admin endpoint remains the fallback.

Jobs are durable (app/store.py): the server keeps them in a JSON file
(env LAMPLIGHT_JOBS_FILE, default jobs.json next to the server), so a
restart never loses a job or its PayPal ids. The delivery text is
verified on capture through the hiring agent's verify_delivery.

Deployment limit — single process only: the JSON job store loads once at
startup and rewrites the whole file on every save, under a lock. Two
processes (or two Gunicorn/uwsgi workers) writing the same jobs file
will clobber each other and lose jobs. Run exactly one server process
on one jobs file (see README.md "Deployment limits"). The server prints
this warning at startup; a multi-worker deployment needs a store with
real concurrency control (SQLite or a database), not a file.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import pathlib
import sys
import threading
import time

from flask import Flask, jsonify, request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.auth import (AuthError, authenticate, require_agent, require_payer,  # noqa: E402
                      require_storefront_token)
from app.dispatcher import GuardRejection, UnhandledTransition  # noqa: E402
from app.escrow import (EscrowError, EscrowService,  # noqa: E402
                        normalize_worker_type, sanitize_paypal_record)
from app.hiring_agent import HiringAgent  # noqa: E402
from app.paypal_client import PayPalClient, PayPalConfigError, PayPalError  # noqa: E402
from app.receipt import build_receipt, to_public_receipt  # noqa: E402
from app.store import (IDEMPOTENCY_KEY_TTL_SECONDS, JsonFileStore,  # noqa: E402
                   make_store)
from app.webhooks import WebhookIdempotencyCache, handle_webhook  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent

app = Flask(__name__)

# Fix 1 (new reviewer): the webhook gateway in app/webhooks.py owns
# signature verification, dedupe, and routing. One module-level cache
# shared by all requests.
_webhook_cache = WebhookIdempotencyCache()

# Round 10: the unauthenticated endpoints had no body cap and no
# limit on how many jobs could be created. A 1 MB request cap (the
# largest legitimate body — a delivery text — is kilobytes) makes
# Flask answer 413 instead of parsing an unbounded body, and the
# open-jobs cap below bounds how many jobs one caller can pile up
# (each creation makes a PayPal order and rewrites the whole jobs
# file). Token auth on these endpoints is held — demo scope, an
# API-surface change — so this is the cheap half of the fix only.
app.config["MAX_CONTENT_LENGTH"] = 1_048_576
MAX_OPEN_JOBS = 100


class _CreateOrderBudget:
    """v8.13: process-wide token bucket in front of PayPal Create Order.

    The MAX_OPEN_JOBS cap bounds how many jobs can exist, but it does
    NOT bound the attack that matters: each POST /jobs spends one
    PayPal Create Order call on OUR client credentials, and the cap
    never enters into it until 100 jobs already exist. A single
    machine can burn the app's PayPal rate limit (or get the app
    flagged) with a few hundred unauthenticated POSTs, and a
    synchronous Create Order on the request thread is a thread-DoS
    even below the cap.

    The bucket is the control that actually protects the PayPal
    credential: N Create Order calls per minute, process-wide,
    regardless of caller. The cap stays as the backstop for row
    count. Demo scope: the budget is generous (default 30/min) and
    the real fix is the production auth gate (caller identity +
    per-caller limits, documented in the module docstring).

    Bypassed when the app is in testing mode — the suite creates
    hundreds of jobs against the mock and never touches PayPal.
    The bucket mechanism itself is unit-tested (see
    tests/test_v813_dispute_deadline.py).
    """

    def __init__(self, per_minute: int, _clock=None):
        import threading as _threading
        import time as _time
        self.capacity = max(1, per_minute)
        self._tokens = float(self.capacity)
        self._clock = _clock or _time.monotonic
        self._last = self._clock()
        # v8.14: the take() check-then-act raced — two concurrent
        # requests could both see tokens >= 1 and both spend. The
        # lock makes refill-check-spend atomic.
        self._lock = _threading.Lock()

    def take(self) -> bool:
        with self._lock:
            now = self._clock()
            elapsed = now - self._last
            self._last = now
            # Refill proportional to elapsed time, capped at capacity.
            self._tokens = min(self.capacity,
                               self._tokens + elapsed * self.capacity / 60.0)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False


def _order_budget_per_minute() -> int:
    try:
        return max(1, int(os.environ.get(
            "LAMPLIGHT_ORDER_BUDGET_PER_MIN", "30")))
    except ValueError:
        return 30


_order_budget = _CreateOrderBudget(_order_budget_per_minute())


def _check_order_budget() -> bool:
    """True when a Create Order call may proceed.

    v8.14: the old blanket app.testing bypass is gone — the suite
    never exercised this path, so the 429-orphan bug shipped unseen.
    Tests that need the bypass set app.config["BYPASS_ORDER_BUDGET"]
    explicitly (see the http_client fixtures); the budget-path tests
    leave it unset and drive the real bucket.
    """
    if app.config.get("BYPASS_ORDER_BUDGET"):
        return True
    return _order_budget.take()


# Phase 2 (Nov 3, 2026): HTTP-layer idempotency contracts (Stripe model).
#
# A client that retries a mutating POST (timeout, dropped response, crash)
# sends Idempotency-Key: <k>. The server guarantees:
#   same key + same params -> the saved response is replayed, exactly one
#       money effect (the handler ran once);
#   same key + different params -> 422 (client misuse — catches a client
#       that reuses a key for a different operation, Stripe behavior);
#   concurrent double-POST with the same key -> exactly one executes,
#       both callers get a 200 (one live, one replayed).
#
# The claim lives in the store (app/store.py: claim_idempotency_key):
# an atomic INSERT wins; the winner executes and saves (status, body);
# losers poll briefly, then replay. A handler that RAISES does not cache
# — the claim is marked failed and a retry re-executes (never replays a
# 500). Responses are stored as the exact bytes Flask would send; the
# replay carries X-Idempotent-Replayed: true.
#
# TTL is 24h (IDEMPOTENCY_KEY_TTL_SECONDS); a janitor sweep deletes
# expired rows. The game plan's Phase 2 spec is the design doc.

def _idempotency_key_from_request() -> str | None:
    """The Idempotency-Key header, validated. None when absent (the
    endpoint runs normally, no contract). A malformed key is 400 —
    fail fast, don't silently ignore it."""
    key = request.headers.get("Idempotency-Key")
    if key is None:
        return None
    key = key.strip()
    if not key or len(key) > 255 or "\n" in key or "\r" in key:
        # Raising ValueError here would need a handler; the decorator
        # converts it to a 400 response directly.
        raise _BadIdempotencyKey()
    return key


class _BadIdempotencyKey(Exception):
    """Malformed Idempotency-Key header (empty, too long, has newlines)."""


def _idempotency_fingerprint() -> str:
    """What 'same params' means: the route, the path args, and the
    canonical JSON body. Two requests with the same key but a different
    brief, amount, or job id are different operations -> 422."""
    body = request.get_json(silent=True)
    if body is None:
        # Not JSON (or empty) — fall back to the raw bytes so a
        # non-JSON retry still fingerprints deterministically.
        raw = request.get_data(as_text=True) or ""
        body_repr: object = {"_raw": raw}
    else:
        body_repr = body
    canonical = json.dumps(
        {"route": request.url_rule.rule if request.url_rule else request.path,
         "view_args": request.view_args or {},
         "body": body_repr},
        sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _idempotent_store():
    """The store behind the idempotency records — the same store the
    service writes jobs to, so the claim and the money move share fate
    on restart (both durable or both not)."""
    return escrow().store


def idempotent(view):
    """Decorator: Idempotency-Key contract on a mutating POST route.

    Apply to every mutating POST in this module (see the Phase 2 route
    registry in tests/test_idempotency_contracts.py — the CI gate in
    tests/test_route_coverage.py fails if a POST route lacks coverage).
    """
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        try:
            key = _idempotency_key_from_request()
        except _BadIdempotencyKey:
            return jsonify({"error": "Idempotency-Key must be 1-255 "
                                     "characters, no newlines"}), 400
        if key is None:
            return view(*args, **kwargs)
        fingerprint = _idempotency_fingerprint()
        store = _idempotent_store()
        now = int(time.time())
        outcome = store.claim_idempotency_key(
            key, fingerprint, IDEMPOTENCY_KEY_TTL_SECONDS, now)
        kind = outcome[0]
        if kind == "replay":
            _, status_code, body = outcome
            resp = app.response_class(
                response=body or "",
                status=status_code or 200,
                mimetype="application/json")
            resp.headers["X-Idempotent-Replayed"] = "true"
            return resp
        if kind == "conflict":
            return jsonify({
                "error": "Idempotency-Key already used with different "
                         "parameters — generate a new key for a new "
                         "operation"}), 422
        if kind == "in_progress":
            # Another request is executing now. Poll briefly for its
            # result, then replay. (The store claim is atomic; this
            # loop only waits.)
            deadline = time.time() + 5.0
            while time.time() < deadline:
                time.sleep(0.05)
                now2 = int(time.time())
                outcome2 = store.claim_idempotency_key(
                    key, fingerprint, IDEMPOTENCY_KEY_TTL_SECONDS, now2)
                if outcome2[0] == "replay":
                    _, status_code, body = outcome2
                    resp = app.response_class(
                        response=body or "",
                        status=status_code or 200,
                        mimetype="application/json")
                    resp.headers["X-Idempotent-Replayed"] = "true"
                    return resp
                if outcome2[0] == "conflict":
                    return jsonify({
                        "error": "Idempotency-Key already used with "
                                 "different parameters"}), 422
                # "claimed" here means the in-progress record went stale
                # and we won the re-claim — fall through and execute.
                # "in_progress" means keep waiting.
                if outcome2[0] == "claimed":
                    outcome = outcome2
                    kind = "claimed"
                    break
            else:
                return jsonify({"error": "idempotent request still "
                                         "in progress; retry shortly"}), 429, \
                    {"Retry-After": "2"}
        # kind == "claimed": we execute.
        try:
            result = view(*args, **kwargs)
        except Exception:
            # The handler raised (Flask's error handlers will render it
            # as a 5xx). Never cache an exception — mark the claim
            # failed so a retry re-executes instead of replaying it.
            try:
                store.fail_idempotency_key(key, int(time.time()))
            except Exception:
                pass
            raise
        response = app.make_response(result)
        try:
            store.complete_idempotency_key(
                key, response.status_code,
                response.get_data(as_text=True),
                int(time.time()))
        except Exception:
            # The handler already ran and returned; a cache-write
            # failure must not turn a good response into a 500.
            pass
        return response
    return wrapper


# v7.2: PayPal authorizations live at most 29 days; a longer ttl/deadline
# is meaningless.
MAX_SECONDS = 29 * 24 * 3600

hiring = HiringAgent()


def _paypal() -> PayPalClient:
    return PayPalClient()  # reads env vars; raises PayPalConfigError if unset


def _jobs_file() -> pathlib.Path:
    """Where jobs are persisted. Env override, else jobs.db in the
    project root (gitignored — it holds live job state, not source).

    v8.12: the default is SQLite (jobs.db), not JSON. SQLite/WAL is
    multi-process safe; the JSON store is single-process only (the boot
    guard refuses multi-worker configs on JSON). Opt out with
    LAMPLIGHT_JOBS_FILE pointing at a .json path, or
    LAMPLIGHT_STORE=json."""
    override = os.environ.get("LAMPLIGHT_JOBS_FILE")
    return pathlib.Path(override) if override else ROOT / "jobs.db"


def _service() -> EscrowService:
    # v8.12: SQLite is the default (jobs.db). SqliteStore when
    # LAMPLIGHT_STORE=sqlite or the jobs path ends in .db — safe for
    # multi-process. LAMPLIGHT_STORE=json or a .json jobs path selects
    # the legacy JsonFileStore (single-process only, enforced by
    # _refuse_multi_worker). LAMPLIGHT_STORE=memory selects the
    # in-memory store (tests and ephemeral runs; nothing is persisted).
    store = make_store(_jobs_file())
    if store is None:
        store = JsonFileStore(_jobs_file())
    return EscrowService(_paypal(), store=store,
                         delivery_verifier=hiring.verify_delivery)


# The service instance lives on the app object so the test client and
# server share it — but the jobs themselves live in the durable store,
# so a process restart (a fresh service on the same file) loses nothing.
app.config.setdefault("escrow", None)

# Round 9: the first-request race — escrow() was a check-then-set
# with no lock, and Flask's dev server is threaded, so two
# simultaneous first requests could build two EscrowService /
# JsonFileStore instances and one request's job could be lost.
# Double-checked locking: the fast path reads the built service
# without the lock; only the first build takes it.
_escrow_lock = threading.Lock()


def _boot_check() -> None:
    """v6.1: enforce the startup config guard under ANY WSGI server.

    validate_config() used to run only under ``python -m app.server``
    (__main__), so gunicorn / ``flask run`` skipped the missing-secret and
    test-secret refusals. It now runs once, before the first service is
    built. Skipped when the app is in testing mode (the test suite runs
    with the test secret by design).

    Grok v7.1: also REFUSE to boot with multiple workers. The JSON
    store is single-process only — two workers will lose jobs via
    last-writer-wins. This is a hard refusal (raise), not a warning.
    """
    if app.testing or app.config.get("TESTING"):
        return
    from app.dispatcher import validate_config
    validate_config()
    # v8.2 (production-readiness item 1): the demo bearer tokens must not
    # be live defaults. Refuse to boot unless real caller tokens are
    # configured, or the explicit demo opt-in is set. Same fail-fast
    # pattern as validate_config() above.
    from app.auth import validate_auth_config
    validate_auth_config()
    # Multi-worker detection: refuse, don't warn — BUT only for the JSON
    # store. SQLite (WAL mode) is multi-process safe, so an operator who
    # followed the docs to switch to SQLite must not get a boot crash.
    store_type = os.environ.get("LAMPLIGHT_STORE", "").lower()
    jobs_path = str(_jobs_file())
    # v8.12: LAMPLIGHT_STORE=memory is also single-process (each worker
    # gets its own dict). Only SQLite/WAL is multi-process safe.
    is_sqlite = (store_type == "sqlite"
                 or (store_type != "memory"
                     and jobs_path.endswith((".db", ".sqlite", ".sqlite3"))))
    if not is_sqlite:
        _refuse_multi_worker()
    # v8.13: refuse to boot onto an empty SQLite db when the legacy
    # JSON book is live. The v8.12 default flip (jobs.json -> jobs.db)
    # means an operator upgrading with a live jobs.json and no
    # explicit store config would boot an EMPTY jobs.db and leave the
    # real authorizations in the old file — the sweeper would never
    # see them, and they would sit until PayPal dropped the holds.
    _check_legacy_json_book()


def _check_legacy_json_book(root: pathlib.Path | None = None) -> None:
    """v8.13: the v8.12 default flip is a production footgun — refuse
    to boot when it would strand a live book.

    If no explicit store config is given (no LAMPLIGHT_JOBS_FILE, no
    LAMPLIGHT_STORE) and jobs.json exists with jobs in it while the
    default jobs.db is missing or empty, booting would silently
    abandon the live authorizations. Refuse with instructions instead.

    ``root`` is a test hook (defaults to the project ROOT).
    Raises RuntimeError on the footgun configuration.
    """
    override = os.environ.get("LAMPLIGHT_JOBS_FILE")
    store_opt = os.environ.get("LAMPLIGHT_STORE", "").strip()
    if override or store_opt:
        return  # explicit config — the operator chose their store
    base = root if root is not None else ROOT
    json_book = base / "jobs.json"
    db_path = base / "jobs.db"
    if not json_book.exists():
        return
    try:
        json_jobs = JsonFileStore(json_book).all()
    except Exception:
        return  # unreadable json book — not our call to make here
    if not json_jobs:
        return  # empty json book — nothing to strand
    db_has_jobs = False
    if db_path.exists():
        try:
            from app.store import SqliteStore
            db_has_jobs = bool(SqliteStore(db_path).all())
        except Exception:
            db_has_jobs = False
    if not db_has_jobs:
        raise RuntimeError(
            "refusing to boot: jobs.json holds "
            f"{len(json_jobs)} job(s) but the default SQLite store "
            f"({db_path.name}) is missing or empty. The v8.12 default "
            "flip would strand the live book — the sweeper would never "
            "see those authorizations. Set LAMPLIGHT_JOBS_FILE to the "
            ".json path to keep the legacy store, or migrate the jobs "
            "to jobs.db first.")


def _refuse_multi_worker() -> None:
    """Grok v7.1: the JSON job store is single-process only. Detect
    common multi-worker configurations and refuse to boot.

    Checks (in order):
    - WEB_CONCURRENCY > 1 (gunicorn/uvicorn convention)
    - GUNICORN_WORKERS > 1
    - LAMPLIGHT_WORKERS > 1 (explicit override)
    - CLI args: gunicorn -w N / --workers N, uwsgi -p N / --processes N

    Raises RuntimeError on detection. Single worker (or unset) passes.
    """
    for var in ("WEB_CONCURRENCY", "GUNICORN_WORKERS", "LAMPLIGHT_WORKERS"):
        raw = os.environ.get(var, "").strip()
        if not raw:
            continue
        try:
            n = int(raw)
        except ValueError:
            # Unparseable — treat as suspicious, refuse.
            raise RuntimeError(
                f"{var}={raw!r} is not an integer — refusing to boot. "
                "The JSON job store is single-process only.")
        if n > 1:
            raise RuntimeError(
                f"{var}={n}: refusing to boot with multiple workers. "
                "The JSON job store is single-process only — two workers "
                "will lose jobs via last-writer-wins. Run exactly one "
                "worker, or migrate to SQLite.")
    # Fix 4 (hardened boot guard): also inspect sys.argv — `gunicorn
    # -w 4` bypasses every env var. Check -w/--workers (gunicorn) and
    # -p/--processes (uwsgi); any value > 1 refuses.
    import sys
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        val = None
        if arg in ("-w", "--workers", "-p", "--processes"):
            if i + 1 < len(argv):
                val = argv[i + 1]
        elif arg.startswith("--workers="):
            val = arg.split("=", 1)[1]
        elif arg.startswith("--processes="):
            val = arg.split("=", 1)[1]
        elif arg.startswith("-w") and len(arg) > 2 and arg[2:].isdigit():
            # -w4 (attached form)
            val = arg[2:]
        elif arg.startswith("-p") and len(arg) > 2 and arg[2:].isdigit():
            # -p4 (attached form)
            val = arg[2:]
        if val is None:
            continue
        try:
            n = int(val)
        except ValueError:
            continue
        if n > 1:
            raise RuntimeError(
                f"CLI {arg}={n}: refusing to boot with multiple workers. "
                "The JSON job store is single-process only — two workers "
                "will lose jobs via last-writer-wins. Run exactly one "
                "worker, or migrate to SQLite.")


def escrow() -> EscrowService:
    if app.config["escrow"] is None:
        with _escrow_lock:
            if app.config["escrow"] is None:
                _boot_check()
                app.config["escrow"] = _service()
                _maybe_start_sweeper(app.config["escrow"])
    return app.config["escrow"]


def _maybe_start_sweeper(service) -> None:
    """Grok v7.1: the crash-recovery sweeper is ON by default (300s)
    — a dropped capture response is stuck money without it.
    LAMPLIGHT_SWEEP_INTERVAL_SECONDS=<n> overrides the interval;
    set it to 0 or empty to disable explicitly."""
    if app.testing or app.config.get("TESTING"):
        return
    raw = os.environ.get("LAMPLIGHT_SWEEP_INTERVAL_SECONDS", "").strip()
    if not raw:
        # Default ON: 5 minutes.
        interval = 300
    else:
        try:
            interval = int(raw)
        except ValueError:
            raise RuntimeError("LAMPLIGHT_SWEEP_INTERVAL_SECONDS must be an integer")
        if interval < 0:
            raise RuntimeError("LAMPLIGHT_SWEEP_INTERVAL_SECONDS must be >= 0")
        if interval == 0:
            # Explicit disable.
            return
    from app.sweeper import start_sweeper_thread
    app.config["sweeper"] = start_sweeper_thread(service, interval)


#: Architectural (new reviewer): HTTP status codes for GuardRejection
#: categories. The dispatcher raises pure domain exceptions; this is
#: the ONLY place where domain categories become HTTP responses.
HTTP_STATUS_FOR_CATEGORY = {
    "validation": 400,   # Bad Request
    "forbidden": 403,    # Forbidden (default)
    "conflict": 409,     # Conflict
    "gone": 410,         # Gone
    "retryable": 503,    # Service Unavailable (retry later)
}


def _guard_rejection_response(e):
    """HTTP response for a GuardRejection: map the domain category to
    an HTTP status code, plus a Retry-After header when the rejection
    carries a retry hint."""
    status = HTTP_STATUS_FOR_CATEGORY.get(
        getattr(e, "category", "forbidden"), 403)
    resp = jsonify({"error": e.reason}), status
    retry_after = getattr(e, "retry_after_seconds", None)
    if retry_after is not None:
        resp[0].headers["Retry-After"] = str(retry_after)
    return resp


def _receipt_for(job) -> dict | None:
    if job.status not in ("CAPTURED", "VOIDED"):
        return None
    return build_receipt(
        job_id=job.job_id, quote_id=job.quote["quote_id"],
        service=job.quote["service"], payer=job.payer, agent=job.agent,
        currency=job.quote.get("currency", "USD"),
        amount_cents=job.quote["price_cents"],
        network=job.quote.get("network", "PayPal Sandbox"),
        paypal_order_id=job.order_id, authorization_id=job.authorization_id,
        capture_id=job.capture_id,
        # Gemini Q4: render ALL capture ids, not just the first.
        capture_ids=list(job.capture_ids),
        capture_amount_cents=job.capture_amount_cents,
        voided_amount_cents=job.voided_amount_cents,
        outcome="released" if job.status == "CAPTURED" else "voided",
        delivery_hash=job.delivery_hash,
        capture_reconciled=job.capture_reconciled,
        # Round 31 (B2): how the settlement was established.
        settlement_method=job.settlement_method,
        # Round 36: the recorded capture's PayPal status, so a
        # refunded capture is visible without re-querying PayPal.
        capture_status=job.capture_status,
        # Round 10, second layer: the stored record is already
        # allowlisted at record time; sanitize again here so a
        # receipt returned over HTTP can never carry buyer data,
        # whatever reached the store.
        last_paypal_response=sanitize_paypal_record(
            job.last_paypal_response),
        # Round 13: the denial lives on the receipt too, so it
        # cannot disappear from the artifact the payer verifies.
        webhook_denial=job.webhook_denial)


@app.errorhandler(AuthError)
def _auth_error(e):
    # Missing/unknown token (401) or valid token for the wrong party (403).
    return jsonify({"error": str(e)}), e.status


@app.errorhandler(PayPalConfigError)
@app.errorhandler(PayPalError)
def _paypal_error(e):
    # Missing sandbox credentials, or a PayPal call failed: 503 with the
    # setup hint, never a stack trace.
    return jsonify({"error": str(e)}), 503


@app.errorhandler(UnhandledTransition)
def _unhandled_transition(e):
    # A dispatcher transition with no handler for (state, event) is a
    # server bug, not a client error: 500 with the transition details,
    # never a stack trace. This catches cases the GuardRejection
    # handler would miss (UnhandledTransition is not a GuardRejection).
    return jsonify({"error": f"unhandled transition: {e}"}), 500


def _json_body() -> dict | None:
    """The request JSON body, or None if it is not a JSON object.
    A JSON list, string or number is not an object — reject it instead of
    crashing on .get()."""
    body = request.get_json(force=True, silent=True)
    return body if isinstance(body, dict) else None


def _storefront_gate() -> None:
    """v8.14: shared-secret gate on POST /jobs and GET /jobs.

    Bypassed under test config (the suite calls these endpoints without
    headers). Otherwise delegates to app.auth.require_storefront_token,
    which accepts the storefront token or any valid payer/agent token.
    Raises AuthError -> 401 via the registered error handler.
    """
    if app.testing or app.config.get("TESTING"):
        return
    require_storefront_token(request)


def _parse_seconds(value, name: str, default: int) -> int:
    """ttl_seconds / deadline_seconds: a non-negative integer. Rejects
    non-numeric, negative and boolean values with ValueError (400)."""
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
    else:
        raise ValueError(f"{name} must be a non-negative integer")
    if parsed < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    # v7.2: bound it. A PayPal authorization cannot outlive its 29-day
    # validity, so a longer ttl/deadline is meaningless and just lets a
    # caller park an open job (and its PayPal order) effectively forever.
    if parsed > MAX_SECONDS:
        raise ValueError(f"{name} must be at most {MAX_SECONDS} seconds (29 days)")
    return parsed


def _require_body_field(body: dict, field: str) -> str:
    """The identity field must be present and a non-empty string. No
    defaults: a missing field is a 400, never a silent assumption."""
    value = body.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} is required")
    return value


def _single_process_warning(jobs_file: pathlib.Path) -> str:
    """The startup warning for the JSON job store's single-process
    limit. Names the jobs file, so an operator running two servers (or
    two workers) on the same file sees which file is at risk."""
    return (
        "WARNING: Lamplight Checkout stores jobs in a single JSON file "
        f"({jobs_file}) that is SINGLE-PROCESS ONLY: two server processes "
        "or workers writing the same file will clobber each other and "
        "lose jobs. Run exactly one server process on this file — see "
        "README.md 'Deployment limits'."
    )


@app.route("/")
def index():
    return jsonify({
        "service": "lamplight-checkout/1",
        "description": "Escrow-style agent hiring on PayPal sandbox: "
                       "authorize a hold at the quote, capture on delivery, "
                       "void on timeout. Both sides keep a hash-stable receipt.",
        "endpoints": ["/jobs", "/jobs/<id>", "/jobs/<id>/authorize",
                      "/jobs/<id>/deliver", "/jobs/<id>/capture",
                      "/jobs/<id>/capture-partial",
                      "/jobs/<id>/void", "/jobs/<id>/force-settle",
                      "/admin/reconcile",
                      "/webhooks/paypal"],
        "auth": "mutating endpoints require Authorization: Bearer "
                "(payer token for authorize/capture/void/reconcile, agent token for "
                "deliver). See app/auth.py.",
    })


@app.route("/demo")
def demo_page():
    """Single-page stage demo: two beats on the mock, no terminal needed."""
    return app.send_static_file("demo.html")


@app.route("/mock/<path:subpath>", methods=["GET", "POST"])
def mock_proxy(subpath):
    """Proxy mock-PayPal test controls through the Flask origin.

    The demo page calls /mock/approve/<id> and /mock/drop-next-capture.
    Proxying keeps the page on one origin — a direct fetch to the mock's
    port dies on CORS in any real browser. Only enabled when the PayPal
    base URL points at a local mock (never in front of real PayPal).
    """
    import urllib.request
    base = os.environ.get("PAYPAL_BASE_URL", "")
    if not base.startswith("http://127.0.0.1:") and not base.startswith("http://localhost:"):
        return jsonify({"error": "mock proxy only in local demo mode"}), 403
    url = f"{base}/mock/{subpath}"
    req = urllib.request.Request(url, method=request.method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read()
            return app.response_class(body, status=resp.status,
                                      mimetype="application/json")
    except Exception as e:
        return jsonify({"error": f"mock proxy failed: {e}"}), 502


# ---------------------------------------------------------------------------
# Demo worker model + verifier (two-chair demo).
#
# These endpoints exist so the demo page can show an AI worker that ACTUALLY
# writes a delivery from the brief — not a canned string — and a verifier
# that scores the delivery against the brief to decide full / partial / no
# release. Both are deterministic and demo-grade: in production the worker
# is a real agent (LLM behind their API) and the verifier is the payer's
# own check. They move no money and are gated like the other demo routes.
# ---------------------------------------------------------------------------

def _demo_write_delivery(brief: str, parsed: dict, worker: dict,
                         depth: str = "full") -> str:
    """Deterministic demo worker: builds delivery text from the brief's
    parsed keywords, so the output actually depends on the input.
    depth="partial" covers roughly half the keywords, simulating
    incomplete work (the verifier will score it partial)."""
    keywords = parsed.get("keywords") or ["general"]
    service = parsed.get("service", "task")
    blurb = worker.get("blurb", service)
    agent_id = worker.get("agent_id", "worker")
    if depth == "partial":
        keep = max(1, len(keywords) // 2)
        covered, dropped = keywords[:keep], keywords[keep:]
    else:
        covered, dropped = keywords, []
    cov = ", ".join(covered)
    if agent_id.startswith("auditor"):
        text = (f"audit report — top-20 holders reviewed for {cov}. "
                f"findings: holdings concentrated as expected, no anomalies "
                f"beyond the usual whale wallets.")
    elif agent_id.startswith("writer"):
        text = (f"research summary — {cov} covered. "
                f"key points drafted from the brief, ready for review.")
    elif agent_id.startswith("coder"):
        text = (f"script delivered — automates {cov}. "
                f"tested against the brief's requirements, runs clean.")
    else:
        text = f"delivery for {service} — {cov} addressed per the brief."
    if dropped:
        text += " [partial delivery — remaining items not covered]"
    return text


@app.route("/demo/worker-write", methods=["POST"])
def demo_worker_write():
    _storefront_gate()
    body = _json_body()
    if body is None:
        return jsonify({"error": "request body must be a JSON object"}), 400
    brief = body.get("brief")
    if not isinstance(brief, str) or not brief.strip():
        return jsonify({"error": "brief is required"}), 400
    depth = body.get("depth", "full")
    if depth not in ("full", "partial"):
        depth = "full"
    try:
        parsed = hiring.parse_brief(brief)
        worker = hiring.match_worker(parsed)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    delivery_text = _demo_write_delivery(brief, parsed, worker, depth)
    return jsonify({
        "delivery_text": delivery_text,
        "worker": worker["agent_id"],
        "service": worker["blurb"],
        "price_cents": worker["price_cents"],
        "depth": depth,
        "keywords": parsed.get("keywords", []),
    })


@app.route("/demo/verify", methods=["POST"])
def demo_verify():
    """Demo verifier: scores a delivery against the brief by keyword
    overlap. Returns verdict (full | partial | none), the fraction of
    brief keywords found in the delivery, and a suggested capture amount
    in cents (for partial: price * fraction, rounded)."""
    _storefront_gate()
    body = _json_body()
    if body is None:
        return jsonify({"error": "request body must be a JSON object"}), 400
    brief = body.get("brief")
    delivery_text = body.get("delivery_text")
    price_cents = body.get("price_cents")
    if not isinstance(brief, str) or not brief.strip():
        return jsonify({"error": "brief is required"}), 400
    if not isinstance(delivery_text, str) or not delivery_text.strip():
        return jsonify({"error": "delivery_text is required"}), 400
    if not isinstance(price_cents, int) or price_cents < 0:
        return jsonify({"error": "price_cents must be a non-negative integer"}), 400
    parsed = hiring.parse_brief(brief)
    keywords = parsed.get("keywords") or []
    text = delivery_text.lower()
    if keywords:
        hits = [kw for kw in keywords if kw in text]
        missed = [kw for kw in keywords if kw not in text]
        fraction = len(hits) / len(keywords)
    else:
        # No checkable keywords in the brief — fall back to a length
        # heuristic so the demo still resolves.
        hits, missed = [], []
        fraction = 1.0 if len(delivery_text.strip()) >= 20 else 0.0
    if fraction >= 0.8:
        verdict = "full"
    elif fraction >= 0.4:
        verdict = "partial"
    else:
        verdict = "none"
    if verdict == "full":
        suggested_cents = price_cents
    elif verdict == "partial":
        suggested_cents = int(round(price_cents * fraction))
    else:
        suggested_cents = 0
    if verdict == "full":
        reason = (f"delivery covers {len(hits)}/{len(keywords) or 1} brief "
                  f"keywords — full release.")
    elif verdict == "partial":
        reason = (f"delivery covers {len(hits)}/{len(keywords)} brief keywords "
                  f"({', '.join(hits)}); missing {', '.join(missed) or '—'} — "
                  f"partial release of {suggested_cents / 100:.2f}.")
    else:
        reason = (f"delivery covers {len(hits)}/{len(keywords)} brief keywords — "
                  f"no release.")
    return jsonify({
        "verdict": verdict,
        "fraction": round(fraction, 3),
        "hits": hits,
        "missed": missed,
        "suggested_cents": suggested_cents,
        "reason": reason,
    })


@app.route("/jobs", methods=["POST"])
@idempotent
def create_job():
    # v8.14: storefront gate (demo-grade shared secret). See the module
    # docstring ("STOREFRONT GATE") and the production auth-gate design.
    # Creating a job moves no money; the PayPal order it creates can only
    # be approved by the payer via PayPal's own approval flow. Bounded by
    # MAX_OPEN_JOBS (429) and the Create Order token bucket.
    _storefront_gate()
    body = _json_body()
    if body is None:
        return jsonify({"error": "request body must be a JSON object"}), 400
    brief = body.get("brief")
    if not isinstance(brief, str) or not brief.strip():
        return jsonify({"error": "brief is required"}), 400
    try:
        ttl_seconds = _parse_seconds(body.get("ttl_seconds"), "ttl_seconds", 3600)
        deadline_seconds = _parse_seconds(body.get("deadline_seconds"),
                                          "deadline_seconds", 3600)
        # Marketplace: who the job is open to (ai | human | anyone).
        # Optional — normalize_worker_type defaults anything
        # missing/invalid to "anyone".
        worker_type = normalize_worker_type(body.get("worker_type"))
        quote, worker = hiring.negotiate(brief, ttl_seconds=ttl_seconds)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    svc = escrow()
    # Round 11: sweep expired QUOTED jobs before counting the cap.
    # A QUOTED job whose quote expired can never authorize, so
    # counting it toward the cap would let dead negotiations block
    # live ones forever (100 stale quotes = a permanent 429).
    svc.sweep_expired_quotes()
    # Round 10: cap the number of open jobs. Creating a job makes a
    # PayPal order and rewrites the whole jobs file, and this endpoint
    # has only the demo-grade storefront token (shared secret — see
    # _storefront_gate), so an unbounded creation loop would pile up
    # orders and jobs without limit. Settled jobs do
    # not count: only QUOTED / AUTHORIZED / DELIVERED jobs hold money
    # or an order. 429 is the honest answer for a limit, not a 400.
    open_jobs = [j for j in svc.jobs.values()
                 if j.status in ("QUOTED", "AUTHORIZED", "DELIVERED")]
    if len(open_jobs) >= MAX_OPEN_JOBS:
        return jsonify({"error": "too many open jobs"}), 429
    # v8.14: check the Create Order budget BEFORE writing the job
    # row. The old order (create row, then check) left an orphaned
    # QUOTED job with no order on every 429 — and those orphans
    # counted toward MAX_OPEN_JOBS until the quote expired, so the
    # limiter could fill the cap and block real checkouts without
    # ever calling PayPal.
    # v8.13: process-wide Create Order budget. The MAX_OPEN_JOBS cap
    # bounds row count, not PayPal API spend — without this bucket a
    # single machine can burn our Create Order rate limit with
    # unauthenticated POSTs (the cap never fires until 100 jobs
    # exist). 429 with Retry-After is the honest answer.
    if not _check_order_budget():
        return jsonify({"error": "order creation rate limit exceeded; "
                                 "retry shortly"}), 429, {"Retry-After": "60"}
    job = svc.create_job(quote=quote, payer=hiring.paypal_id,
                         agent=worker["paypal_id"],
                         deadline_seconds=deadline_seconds,
                         worker_type=worker_type)
    try:
        order = svc.create_order(job)
    except (PayPalError, PayPalConfigError) as e:
        # create_order already removed the job from the store on failure,
        # so no orphaned QUOTED job with no order id is left behind.
        return jsonify({"error": str(e)}), 503
    approve = PayPalClient.approve_link(order)
    return jsonify({"job": job.to_public_dict(), "quote": quote,
                    "worker": worker["agent_id"],
                    "paypal_order": order, "approve_url": approve}), 201


@app.route("/jobs", methods=["GET"])
def list_jobs():
    # v8.14: storefront gate (demo-grade shared secret). See the module
    # docstring. Returns public-safe data only (to_public_dict strips
    # PayPal ids, tokens, and PII — v8.2 F3). Production gate: caller
    # token, return only that caller's jobs.
    _storefront_gate()
    return jsonify({"jobs": [j.to_public_dict() for j in escrow().jobs.values()]})


@app.route("/jobs/<job_id>", methods=["GET"])
def get_job(job_id):
    try:
        job = escrow().get_job(job_id)
    except GuardRejection as e:
        return _guard_rejection_response(e)
    except EscrowError as e:
        return jsonify({"error": str(e)}), 404
    receipt = _receipt_for(job)
    # v8.14: the job's own payer/agent see the full dict — pause
    # fields (needs_review, dispute_deadline, worker note) included —
    # so a refresh after a pause shows the deadline. Everyone else
    # keeps the public shape. Auth is best-effort here: no token (or
    # a token for a different job) is not an error, just the public
    # view.
    job_dict = job.to_public_dict()
    try:
        role, identity = authenticate(request, payer_id=hiring.paypal_id)
    except AuthError:
        role, identity = None, None
    if (role == "payer" and identity == job.payer) or \
       (role == "agent" and identity == job.agent):
        job_dict = job.to_dict()
    return jsonify({"job": job_dict,
                    "receipt": to_public_receipt(receipt) if receipt else None})


@app.route("/jobs/<job_id>/authorize", methods=["POST"])
@idempotent
def authorize_job(job_id):
    # Only the payer may place the hold (the buyer already approved the
    # order in PayPal; this endpoint just triggers the authorize call).
    require_payer(request, payer_id=hiring.paypal_id)
    try:
        job = escrow().get_job(job_id)
        escrow().authorize(job)
    except GuardRejection as e:
        return _guard_rejection_response(e)
    except EscrowError as e:
        return jsonify({"error": str(e)}), 409
    # Re-read before serializing: the dispatcher path settles a
    # store-fresh copy, so the pre-action `job` is stale on durable
    # stores (SQLite).
    job = escrow().get_job(job_id)
    return jsonify({"job": job.to_dict()})


@app.route("/jobs/<job_id>/deliver", methods=["POST"])
@idempotent
def deliver_job(job_id):
    # Identity comes from the bearer token; the body field must be
    # present and agree with it. An empty body is never the agent.
    agent_id = require_agent(request, payer_id=hiring.paypal_id)
    body = _json_body()
    if body is None:
        return jsonify({"error": "request body must be a JSON object"}), 400
    try:
        claimed = _require_body_field(body, "agent")
        # Round 9: the delivery text must be present and a string — a
        # JSON number used to reach .encode() in submit_delivery and
        # return a 500. A missing, empty or non-string value is 400.
        delivery_text = _require_body_field(body, "delivery_text")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if claimed != agent_id:
        return jsonify({"error": "agent field does not match the bearer token"}), 403
    try:
        job = escrow().get_job(job_id)
        if job.agent != agent_id:
            return jsonify({"error": "only the worker agent can deliver this job"}), 403
        dh = escrow().submit_delivery(job, agent=agent_id,
                                      delivery_text=delivery_text)
    except GuardRejection as e:
        return _guard_rejection_response(e)
    except EscrowError as e:
        return jsonify({"error": str(e)}), 409
    # Re-read before serializing: the dispatcher path settles a
    # store-fresh copy, so the pre-action `job` is stale on durable
    # stores (SQLite).
    job = escrow().get_job(job_id)
    return jsonify({"job": job.to_dict(), "delivery_hash": dh})


@app.route("/jobs/<job_id>/capture", methods=["POST"])
@idempotent
def capture_job(job_id):
    payer_id = require_payer(request, payer_id=hiring.paypal_id)
    body = _json_body()
    if body is None:
        return jsonify({"error": "request body must be a JSON object"}), 400
    try:
        claimed = _require_body_field(body, "payer")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if claimed != payer_id:
        return jsonify({"error": "payer field does not match the bearer token"}), 403
    try:
        delivery_text = _require_body_field(body, "delivery_text")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    try:
        job = escrow().get_job(job_id)
        if job.payer != payer_id:
            return jsonify({"error": "only the payer can release this job"}), 403
        # The delivery text is verified against the recorded hash inside
        # capture() (via the hiring agent's verify_delivery) before any
        # PayPal call — a capture with the wrong text is refused here.
        escrow().capture(job, payer=payer_id, delivery_text=delivery_text)
    except GuardRejection as e:
        return _guard_rejection_response(e)
    except EscrowError as e:
        return jsonify({"error": str(e)}), 409
    # Re-read before serializing: the dispatcher path settles a
    # store-fresh copy, so the pre-action `job` is stale on durable
    # stores (SQLite) — the response must carry the settled state and
    # the receipt, not the pre-capture snapshot.
    job = escrow().get_job(job_id)
    return jsonify({"job": job.to_dict(), "receipt": _receipt_for(job)})


@app.route("/jobs/<job_id>/clear-review-flag", methods=["POST"])
@idempotent
def clear_review_flag_job(job_id):
    """Payer-only: dismiss the review flag on a TERMINAL flagged job
    (v8.14 — the operator exit existed in Python but had no HTTP
    route; the README named it with no way to call it).

    A job that settled (CAPTURED/VOIDED) while carrying needs_review
    has no other recovery route. The payer re-reads the authorization,
    records what PayPal actually says, and clears the flag with the
    decision on the record. Money is NEVER re-settled — status,
    capture, and void records are untouched; only the flag clears.
    """
    payer_id = require_payer(request, payer_id=hiring.paypal_id)
    body = _json_body()
    if body is None:
        return jsonify({"error": "request body must be a JSON object"}), 400
    try:
        claimed = _require_body_field(body, "payer")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if claimed != payer_id:
        return jsonify({"error": "payer field does not match the bearer token"}), 403
    try:
        job = escrow().get_job(job_id)
        if job.payer != payer_id:
            return jsonify({"error": "only the payer can clear the review flag"}), 403
        result = escrow().clear_terminal_review_flag(job, payer=payer_id)
    except GuardRejection as e:
        return _guard_rejection_response(e)
    except EscrowError as e:
        return jsonify({"error": str(e)}), 409
    job = escrow().get_job(job_id)
    return jsonify({"job": job.to_dict(), "result": result,
                    "receipt": _receipt_for(job)})


@app.route("/jobs/<job_id>/cancel-pending-capture", methods=["POST"])
@idempotent
def cancel_pending_capture_job(job_id):
    """Payer-only: cancel a stale capture intent (v8.14 — existed in
    Python, had no HTTP route).

    A process that died after saving the intent but before the POST
    leaves a key PayPal never saw. Cancel requires: the payer, a
    pending intent older than the TTL, and an authorization + order
    read that lists no capture. The intent clears WITHOUT retiring
    the key. See EscrowService.cancel_pending_capture.
    """
    payer_id = require_payer(request, payer_id=hiring.paypal_id)
    body = _json_body()
    if body is None:
        return jsonify({"error": "request body must be a JSON object"}), 400
    try:
        claimed = _require_body_field(body, "payer")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if claimed != payer_id:
        return jsonify({"error": "payer field does not match the bearer token"}), 403
    try:
        job = escrow().get_job(job_id)
        if job.payer != payer_id:
            return jsonify({"error": "only the payer can cancel a pending capture"}), 403
        result = escrow().cancel_pending_capture(job, payer=payer_id)
    except GuardRejection as e:
        return _guard_rejection_response(e)
    except EscrowError as e:
        return jsonify({"error": str(e)}), 409
    job = escrow().get_job(job_id)
    return jsonify({"job": job.to_dict(), "result": result,
                    "receipt": _receipt_for(job)})


@app.route("/jobs/<job_id>/capture-partial", methods=["POST"])
@idempotent
def capture_partial_job(job_id):
    """Release part of the hold and void the rest in one move (round 8):
    {payer, delivery_text, amount_cents}. The delivery text is verified
    against the recorded hash inside capture_partial before any PayPal
    call, the same gate as the full capture. amount_cents must be an
    integer number of cents — a non-integer is a 400 (bad input); an
    amount of zero, or more than the hold, is a 409 from the service
    (a semantic refusal, like capturing an undelivered job). An amount
    equal to the full hold degenerates to a normal capture. The receipt
    shows the split: capture_amount_cents + voided_amount_cents = the
    hold, outcome "released"."""
    payer_id = require_payer(request, payer_id=hiring.paypal_id)
    body = _json_body()
    if body is None:
        return jsonify({"error": "request body must be a JSON object"}), 400
    try:
        claimed = _require_body_field(body, "payer")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if claimed != payer_id:
        return jsonify({"error": "payer field does not match the bearer token"}), 403
    try:
        delivery_text = _require_body_field(body, "delivery_text")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    amount_cents = body.get("amount_cents")
    if isinstance(amount_cents, bool) or not isinstance(amount_cents, int):
        return jsonify({"error": "amount_cents must be an integer number "
                        "of cents"}), 400
    try:
        job = escrow().get_job(job_id)
        if job.payer != payer_id:
            return jsonify({"error": "only the payer can release this job"}), 403
        escrow().capture_partial(job, payer=payer_id,
                                 delivery_text=delivery_text,
                                 amount_cents=amount_cents)
    except GuardRejection as e:
        return _guard_rejection_response(e)
    except EscrowError as e:
        return jsonify({"error": str(e)}), 409
    # Re-read before serializing: the dispatcher path settles a
    # store-fresh copy, so the pre-action `job` is stale on durable
    # stores (SQLite) — the response must carry the settled state and
    # the receipt, not the pre-capture snapshot.
    job = escrow().get_job(job_id)
    return jsonify({"job": job.to_dict(), "receipt": _receipt_for(job)})


@app.route("/jobs/<job_id>/void", methods=["POST"])
@idempotent
def void_job(job_id):
    payer_id = require_payer(request, payer_id=hiring.paypal_id)
    body = _json_body()
    if body is None:
        return jsonify({"error": "request body must be a JSON object"}), 400
    try:
        claimed = _require_body_field(body, "payer")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if claimed != payer_id:
        return jsonify({"error": "payer field does not match the bearer token"}), 403
    try:
        job = escrow().get_job(job_id)
        if job.payer != payer_id:
            return jsonify({"error": "only the payer can void this job"}), 403
        # Any `now` field in the body is ignored on purpose: the void
        # deadline is always evaluated against wall-clock time, never a
        # client-supplied timestamp (a forged future `now` must not be
        # able to void before the real deadline).
        escrow().void_after_deadline(job, payer=payer_id)
    except GuardRejection as e:
        return _guard_rejection_response(e)
    except EscrowError as e:
        return jsonify({"error": str(e)}), 409
    # Re-read before serializing: the dispatcher path settles a
    # store-fresh copy, so the pre-action `job` is stale on durable
    # stores (SQLite) — the response must carry the settled state and
    # the receipt, not the pre-void snapshot.
    job = escrow().get_job(job_id)
    return jsonify({"job": job.to_dict(), "receipt": _receipt_for(job)})


@app.route("/jobs/<job_id>/force-settle", methods=["POST"])
@idempotent
def force_settle_job(job_id):
    """Payer-only exit from a needs_review wedge (round 20): the payer
    re-reads the authorization, records any listed capture, and settles
    the job — CAPTURED with the residual if money moved, VOIDED if
    nothing did. Only the job's payer, only a wedged job."""
    payer_id = require_payer(request, payer_id=hiring.paypal_id)
    body = _json_body()
    if body is None:
        return jsonify({"error": "request body must be a JSON object"}), 400
    try:
        claimed = _require_body_field(body, "payer")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if claimed != payer_id:
        return jsonify({"error": "payer field does not match the bearer token"}), 403
    try:
        job = escrow().get_job(job_id)
        if job.payer != payer_id:
            return jsonify({"error": "only the payer can force-settle this job"}), 403
        escrow().force_settle(job, payer=payer_id)
    except GuardRejection as e:
        return _guard_rejection_response(e)
    except EscrowError as e:
        return jsonify({"error": str(e)}), 409
    # Re-read before serializing: the dispatcher path settles a
    # store-fresh copy, so the pre-action `job` is stale on durable
    # stores (SQLite) — the response must carry the settled state and
    # the receipt, not the pre-settlement snapshot.
    job = escrow().get_job(job_id)
    return jsonify({"job": job.to_dict(), "receipt": _receipt_for(job)})


@app.route("/jobs/<job_id>/dispute", methods=["POST"])
@idempotent
def dispute_job(job_id):
    """v8.14: payer-filed dispute — a PAYMENT PAUSE, honestly labeled.

    This is NOT an adjudicated dispute: no arbiter, no evidence
    review, no path to not-pay. It pauses the 48h auto-capture until
    dispute_deadline (72h, clamped to the PayPal authorization honor
    period); at/after the deadline the sweeper captures anyway —
    silence pays the worker. The response body's payment_pause block
    states this plainly. The worker may attach a counter-note
    (POST /jobs/<id>/dispute/note) as chargeback evidence only.

    Resolution before the deadline is via POST /jobs/<id>/force-settle
    (the existing payer-only exit). See EscrowService.dispute_job for
    the rules.
    """
    payer_id = require_payer(request, payer_id=hiring.paypal_id)
    body = _json_body()
    if body is None:
        return jsonify({"error": "request body must be a JSON object"}), 400
    try:
        claimed = _require_body_field(body, "payer")
        reason = _require_body_field(body, "reason")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if claimed != payer_id:
        return jsonify({"error": "payer field does not match the bearer token"}), 403
    try:
        job = escrow().get_job(job_id)
        if job.payer != payer_id:
            return jsonify({"error": "only the payer can dispute this job"}), 403
        result = escrow().dispute_job(job, payer=payer_id, reason=reason)
    except GuardRejection as e:
        return _guard_rejection_response(e)
    except EscrowError as e:
        return jsonify({"error": str(e)}), 409
    # v8.13: serialize the FRESH row, not the pre-dispute snapshot.
    # dispute_job re-reads and saves inside its lock; the `job` object
    # above is stale by construction. On stores that materialize new
    # instances per get(), serializing the snapshot would show
    # pre-dispute state next to the dispute result.
    # v8.14: full dict, not the public shape — this route is
    # payer-only, and the payer needs the pause fields
    # (needs_review, dispute_deadline) on the response itself, the
    # same way capture/void return to_dict().
    job = escrow().get_job(job_id)
    return jsonify({
        "job": job.to_dict(),
        "dispute": result,
        "payment_pause": {
            "review": False,
            "captures_at": result["dispute_deadline"],
            "detail": (
                "This pauses auto-capture until captures_at. "
                "Nobody reviews the reason, and it does not refund. "
                "Doing nothing pays the worker at that time. "
                "force_settle settles immediately: an active pause "
                "captures now and the worker is paid; otherwise an "
                "open hold is voided and the money returns to the "
                "payer; if a capture already landed, the worker "
                "keeps it."
            ),
        },
    }), 200


@app.route("/jobs/<job_id>/dispute/note", methods=["POST"])
@idempotent
def dispute_worker_note(job_id):
    """v8.14: worker's counter-note on an active payment pause.

    Agent-authenticated. Stored as chargeback evidence only — it never
    changes any outcome. The pause still expires on its deadline.
    """
    agent_id = require_agent(request, payer_id=hiring.paypal_id)
    body = _json_body()
    if body is None:
        return jsonify({"error": "request body must be a JSON object"}), 400
    try:
        claimed = _require_body_field(body, "agent")
        note = _require_body_field(body, "note")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if claimed != agent_id:
        return jsonify({"error": "agent field does not match the bearer token"}), 403
    try:
        job = escrow().get_job(job_id)
        result = escrow().set_pause_worker_note(
            job, agent=agent_id, note=note)
    except GuardRejection as e:
        return _guard_rejection_response(e)
    except EscrowError as e:
        return jsonify({"error": str(e)}), 409
    return jsonify(result), 200


@app.route("/admin/reconcile", methods=["POST"])
@idempotent
def admin_reconcile():
    """Payer-only admin endpoint: walk every AUTHORIZED/DELIVERED job
    and force a get_authorization + state update from PayPal, for use
    after a prolonged outage (dropped responses, a restart mid-flow).
    A job PayPal still reports CREATED stays open; one it reports
    CAPTURED is recorded captured (real id + amount from the linked
    captures when PayPal lists them); one it reports VOIDED is recorded
    voided. One job's failure never stops the rest. Returns the summary
    from EscrowService.reconcile_open_jobs: checked / updated /
    still_pending / errors."""
    require_payer(request, payer_id=hiring.paypal_id)
    return jsonify(escrow().reconcile_open_jobs())


# Phase 4 (property-testing game plan): reconciliation review routes.
# The nightly runner (scripts/reconcile_nightly.py) persists runs and
# findings in the store; these payer-only routes let a human review
# them. Follows the existing admin_reconcile pattern: require_payer
# raises AuthError -> 401/403 via the registered error handler.


@app.route("/admin/reconcile/runs", methods=["GET"])
def admin_reconcile_runs():
    """Payer-only: recent reconciliation runs, newest first.

    Query param: limit (default 20).
    """
    require_payer(request, payer_id=hiring.paypal_id)
    try:
        limit = int(request.args.get("limit", "20"))
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    runs = escrow().store.get_reconciliation_runs(limit=limit)
    return jsonify({"runs": runs})


@app.route("/admin/reconcile/findings", methods=["GET"])
def admin_reconcile_findings():
    """Payer-only: reconciliation findings, newest first.

    Query param: status (open | acknowledged | resolved; default open).
    """
    require_payer(request, payer_id=hiring.paypal_id)
    status = request.args.get("status", "open")
    if status not in ("open", "acknowledged", "resolved"):
        return jsonify(
            {"error": "status must be open, acknowledged, or resolved"}), 400
    findings = escrow().store.list_reconciliation_findings(status=status)
    return jsonify({"findings": findings})


def _recon_note_body():
    """The required note for ack/resolve — who looked, what they
    decided. The audit trail matters: a bare ack is not accepted."""
    body = request.get_json(silent=True) or {}
    note = body.get("note")
    if not isinstance(note, str) or not note.strip():
        return None, jsonify(
            {"error": "a non-empty note is required"}), 400
    return note.strip(), None, None


@app.route("/admin/reconcile/findings/<finding_id>/ack", methods=["POST"])
@idempotent
def admin_reconcile_ack(finding_id):
    """Payer-only: acknowledge an open finding (status -> acknowledged)."""
    require_payer(request, payer_id=hiring.paypal_id)
    note, err_body, err_status = _recon_note_body()
    if err_body is not None:
        return err_body, err_status
    finding = escrow().store.update_reconciliation_finding(
        finding_id, status="acknowledged", note=note)
    if finding is None:
        return jsonify({"error": "finding not found"}), 404
    return jsonify({"finding": finding})


@app.route("/admin/reconcile/findings/<finding_id>/resolve", methods=["POST"])
@idempotent
def admin_reconcile_resolve(finding_id):
    """Payer-only: resolve a finding (status -> resolved)."""
    require_payer(request, payer_id=hiring.paypal_id)
    note, err_body, err_status = _recon_note_body()
    if err_body is not None:
        return err_body, err_status
    import time
    finding = escrow().store.update_reconciliation_finding(
        finding_id, status="resolved", note=note,
        resolved_at=int(time.time()))
    if finding is None:
        return jsonify({"error": "finding not found"}), 404
    return jsonify({"finding": finding})


@app.route("/webhooks/paypal", methods=["POST"])
def paypal_webhook():
    """PayPal webhook listener (Fix 1, new reviewer): delegates to
    app.webhooks.handle_webhook, the single gateway for signature
    verification, dedupe, and routing.

    Raw request bytes (request.get_data()) are passed through untouched —
    parsing to a dict and re-serializing would break PayPal's signature,
    which covers the exact byte stream. The webhook id comes from env
    LAMPLIGHT_PAYPAL_WEBHOOK_ID; without it the endpoint is 503.
    handle_webhook returns {"http_status": ...} which this endpoint
    honors directly.
    """
    # v6.1: one env name, with the webhooks module's name as a fallback.
    webhook_id = (os.environ.get("LAMPLIGHT_PAYPAL_WEBHOOK_ID")
                  or os.environ.get("PAYPAL_WEBHOOK_ID"))
    if not webhook_id:
        return jsonify({"error": "LAMPLIGHT_PAYPAL_WEBHOOK_ID not set — "
                        "webhook verification is unavailable"}), 503
    raw_body = request.get_data()  # RAW BYTES — never a parsed dict
    headers = dict(request.headers)
    res = handle_webhook(
        escrow(),
        headers=headers,
        body=raw_body,
        webhook_id=webhook_id,
        idempotency_cache=_webhook_cache,
    )
    # v6.1: http_status is transport metadata — use it, don't leak it.
    status = res.get("http_status", 200)
    return jsonify({k: v for k, v in res.items() if k != "http_status"}), status


if __name__ == "__main__":
    # LOW #8 (Claude): fail fast on bad config — never serve traffic
    # with a missing or test intent secret.
    from app.dispatcher import validate_config
    validate_config()
    # v8.14: the single-process warning is for the JSON store only.
    # SQLite (the default jobs.db) is multi-process safe — printing
    # the warning for it was noise. Match make_store()'s selection:
    # LAMPLIGHT_STORE=json or a .json path means the legacy
    # JsonFileStore.
    _jf = _jobs_file()
    if os.environ.get("LAMPLIGHT_STORE", "").strip().lower() == "json" \
            or _jf.suffix == ".json":
        print(_single_process_warning(_jf), file=sys.stderr)
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port)

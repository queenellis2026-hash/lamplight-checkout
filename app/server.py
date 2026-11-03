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
admin/reconcile)
require a bearer token, and the caller's identity is derived from the token —
never from the request body and never from a default. The body must
still carry the identity field (``payer`` / ``agent``) and it must
agree with the credential; a missing field is 400, a wrong token is
401, a valid token for the wrong party is 403. This is demo-grade
shared-secret auth, not production identity.

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

import os
import pathlib
import sys
import threading

from flask import Flask, jsonify, request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.auth import AuthError, require_agent, require_payer  # noqa: E402
from app.dispatcher import GuardRejection, UnhandledTransition  # noqa: E402
from app.escrow import (EscrowError, EscrowService,  # noqa: E402
                        sanitize_paypal_record)
from app.hiring_agent import HiringAgent  # noqa: E402
from app.paypal_client import PayPalClient, PayPalConfigError, PayPalError  # noqa: E402
from app.receipt import build_receipt, to_public_receipt  # noqa: E402
from app.store import JsonFileStore, make_store  # noqa: E402
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
# v7.2: PayPal authorizations live at most 29 days; a longer ttl/deadline
# is meaningless.
MAX_SECONDS = 29 * 24 * 3600

hiring = HiringAgent()


def _paypal() -> PayPalClient:
    return PayPalClient()  # reads env vars; raises PayPalConfigError if unset


def _jobs_file() -> pathlib.Path:
    """Where jobs are persisted. Env override, else jobs.json in the
    project root (gitignored — it holds live job state, not source)."""
    override = os.environ.get("LAMPLIGHT_JOBS_FILE")
    return pathlib.Path(override) if override else ROOT / "jobs.json"


def _service() -> EscrowService:
    # Fix 5: SqliteStore when LAMPLIGHT_STORE=sqlite or the jobs path
    # ends in .db — safe for multi-process. Otherwise JsonFileStore
    # (single-process only, enforced by _refuse_multi_worker).
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
    is_sqlite = (store_type == "sqlite"
                 or jobs_path.endswith((".db", ".sqlite", ".sqlite3")))
    if not is_sqlite:
        _refuse_multi_worker()


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


@app.route("/jobs", methods=["POST"])
def create_job():
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
    # has no auth (held — demo scope), so an unbounded creation loop
    # would pile up orders and jobs without limit. Settled jobs do
    # not count: only QUOTED / AUTHORIZED / DELIVERED jobs hold money
    # or an order. 429 is the honest answer for a limit, not a 400.
    open_jobs = [j for j in svc.jobs.values()
                 if j.status in ("QUOTED", "AUTHORIZED", "DELIVERED")]
    if len(open_jobs) >= MAX_OPEN_JOBS:
        return jsonify({"error": "too many open jobs"}), 429
    job = svc.create_job(quote=quote, payer=hiring.paypal_id,
                         agent=worker["paypal_id"],
                         deadline_seconds=deadline_seconds)
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
    return jsonify({"job": job.to_public_dict(),
                    "receipt": to_public_receipt(receipt) if receipt else None})


@app.route("/jobs/<job_id>/authorize", methods=["POST"])
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
    return jsonify({"job": job.to_dict()})


@app.route("/jobs/<job_id>/deliver", methods=["POST"])
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
    return jsonify({"job": job.to_dict(), "delivery_hash": dh})


@app.route("/jobs/<job_id>/capture", methods=["POST"])
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
    return jsonify({"job": job.to_dict(), "receipt": _receipt_for(job)})


@app.route("/jobs/<job_id>/capture-partial", methods=["POST"])
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
    return jsonify({"job": job.to_dict(), "receipt": _receipt_for(job)})


@app.route("/jobs/<job_id>/void", methods=["POST"])
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
    return jsonify({"job": job.to_dict(), "receipt": _receipt_for(job)})


@app.route("/jobs/<job_id>/force-settle", methods=["POST"])
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
    return jsonify({"job": job.to_dict(), "receipt": _receipt_for(job)})


@app.route("/admin/reconcile", methods=["POST"])
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
    print(_single_process_warning(_jobs_file()), file=sys.stderr)
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port)

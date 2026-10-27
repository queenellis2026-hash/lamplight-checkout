"""Flask service for Lamplight Checkout.

Run:  .venv/bin/python -m app.server   (or: flask --app app.server run)
Env:   PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET, PAYPAL_BASE_URL (optional).
       With no credentials the service still starts; any endpoint that
       touches PayPal returns 503 with the sandbox-setup hint until the
       env vars are set. For a fully offline run use the mock server:
       see scripts/demo.py --mode mock.
       LAMPLIGHT_PAYER_TOKEN, LAMPLIGHT_AGENT_TOKENS (optional): bearer
       tokens for the mutating endpoints — see app/auth.py. Demo
       defaults apply when unset; replace them for any real deployment.
       LAMPLIGHT_JOBS_FILE (optional): where jobs are persisted —
       default jobs.json in the project root (see app/store.py).

Caller authentication (app/auth.py)
------------------------------------
The mutating endpoints (authorize, deliver, capture, void) require a
bearer token, and the caller's identity is derived from the token —
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
  POST /jobs/<id>/void         payer token: {payer} -> release hold after deadline.
                               Any `now` field in the body is ignored — the
                               deadline is always wall-clock time.

Jobs are durable (app/store.py): the server keeps them in a JSON file
(env LAMPLIGHT_JOBS_FILE, default jobs.json next to the server), so a
restart never loses a job or its PayPal ids. The delivery text is
verified on capture through the hiring agent's verify_delivery.
"""
from __future__ import annotations

import os
import pathlib
import sys

from flask import Flask, jsonify, request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.auth import AuthError, require_agent, require_payer  # noqa: E402
from app.escrow import EscrowError, EscrowService  # noqa: E402
from app.hiring_agent import HiringAgent  # noqa: E402
from app.paypal_client import PayPalClient, PayPalConfigError, PayPalError  # noqa: E402
from app.receipt import build_receipt  # noqa: E402
from app.store import JsonFileStore  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent

app = Flask(__name__)
hiring = HiringAgent()


def _paypal() -> PayPalClient:
    return PayPalClient()  # reads env vars; raises PayPalConfigError if unset


def _jobs_file() -> pathlib.Path:
    """Where jobs are persisted. Env override, else jobs.json in the
    project root (gitignored — it holds live job state, not source)."""
    override = os.environ.get("LAMPLIGHT_JOBS_FILE")
    return pathlib.Path(override) if override else ROOT / "jobs.json"


def _service() -> EscrowService:
    return EscrowService(_paypal(), store=JsonFileStore(_jobs_file()),
                         delivery_verifier=hiring.verify_delivery)


# The service instance lives on the app object so the test client and
# server share it — but the jobs themselves live in the durable store,
# so a process restart (a fresh service on the same file) loses nothing.
app.config.setdefault("escrow", None)


def escrow() -> EscrowService:
    if app.config["escrow"] is None:
        app.config["escrow"] = _service()
    return app.config["escrow"]


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
        outcome="released" if job.status == "CAPTURED" else "voided",
        delivery_hash=job.delivery_hash,
        capture_reconciled=job.capture_reconciled)


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
        if value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise ValueError(f"{name} must be a non-negative integer")


def _require_body_field(body: dict, field: str) -> str:
    """The identity field must be present and a non-empty string. No
    defaults: a missing field is a 400, never a silent assumption."""
    value = body.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} is required")
    return value


@app.route("/")
def index():
    return jsonify({
        "service": "lamplight-checkout/1",
        "description": "Escrow-style agent hiring on PayPal sandbox: "
                       "authorize a hold at the quote, capture on delivery, "
                       "void on timeout. Both sides keep a hash-stable receipt.",
        "endpoints": ["/jobs", "/jobs/<id>", "/jobs/<id>/authorize",
                      "/jobs/<id>/deliver", "/jobs/<id>/capture",
                      "/jobs/<id>/void"],
        "auth": "mutating endpoints require Authorization: Bearer "
                "(payer token for authorize/capture/void, agent token for "
                "deliver). See app/auth.py.",
    })


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
    return jsonify({"job": job.to_dict(), "quote": quote,
                    "worker": worker["agent_id"],
                    "paypal_order": order, "approve_url": approve}), 201


@app.route("/jobs", methods=["GET"])
def list_jobs():
    return jsonify({"jobs": [j.to_dict() for j in escrow().jobs.values()]})


@app.route("/jobs/<job_id>", methods=["GET"])
def get_job(job_id):
    try:
        job = escrow().get_job(job_id)
    except EscrowError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"job": job.to_dict(), "receipt": _receipt_for(job)})


@app.route("/jobs/<job_id>/authorize", methods=["POST"])
def authorize_job(job_id):
    # Only the payer may place the hold (the buyer already approved the
    # order in PayPal; this endpoint just triggers the authorize call).
    require_payer(request, payer_id=hiring.paypal_id)
    try:
        job = escrow().get_job(job_id)
        escrow().authorize(job)
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
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if claimed != agent_id:
        return jsonify({"error": "agent field does not match the bearer token"}), 403
    try:
        job = escrow().get_job(job_id)
        if job.agent != agent_id:
            return jsonify({"error": "only the worker agent can deliver this job"}), 403
        dh = escrow().submit_delivery(job, agent=agent_id,
                                      delivery_text=body.get("delivery_text", ""))
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
    except EscrowError as e:
        return jsonify({"error": str(e)}), 409
    return jsonify({"job": job.to_dict(), "receipt": _receipt_for(job)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port)

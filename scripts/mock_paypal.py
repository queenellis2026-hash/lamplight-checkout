"""Local mock PayPal sandbox for Lamplight Checkout.

Implements the same REST surface the real sandbox exposes, so the demo and
tests exercise the real PayPalClient code paths with zero network and zero
money:

  POST /v1/oauth2/token                        -> fake access token
  POST /v2/checkout/orders                     -> order, status CREATED
  GET  /v2/checkout/orders/{id}                -> order with current status
  POST /mock/approve/{id}                      -> MOCK ONLY: simulates the buyer
                                                  clicking the approve link.
                                                  (In live mode a human clicks
                                                  the real approve URL instead.)
  POST /v2/checkout/orders/{id}/authorize      -> authorization (order must be
                                                  APPROVED first, like live)
  GET  /v2/payments/authorizations/{id} — the authorization, including
       its linked captures (a "captures" list of COMPLETED captures, in
       creation order), so a client reconciling a lost capture response
       can recover the real capture id and amount from PayPal's state
       instead of recording an unauditable capture. Round 11: a
       PARTIALLY_CAPTURED authorization lists NO captures — that is
       what real PayPal reports, so a client reconciling one cannot
       recover the capture from the read and must replay the
       idempotency key instead. The capture is still linked
       internally (the mock needs it for remainder math); only the
       reported resource hides it.
  POST /v2/payments/authorizations/{id}/capture -> COMPLETED capture
       (round 8: a requested amount BELOW what remains of the hold is
       a partial capture — the authorization stays open, so the
       residual can be voided afterwards, the way real PayPal leaves
       a partially-captured authorization open. No amount, or an
       amount >= the remainder, is a full capture and the authorization
       becomes CAPTURED, exactly as before. Round 11: a partial
       capture now reports PARTIALLY_CAPTURED by default — the
       status real PayPal reports — instead of staying CREATED.)
       (round 9: a capture — partial or full — whose amount exceeds
       what remains of the hold is rejected with 422, the way real
       PayPal rejects an over-capture. The mock used to accept it and
       record two captures on one authorization.)
  POST /v2/payments/authorizations/{id}/void    -> VOIDED (allowed
       while the authorization is CREATED, including after a partial
       capture — that is how the residual is released. Round 9: also
       allowed while it is PARTIALLY_CAPTURED, the status real PayPal
       reports after a partial capture.)

IDs are deterministic (ORDER-0001, AUTH-0001, CAPTURE-0001) so demo runs
are repeatable. State lives in memory; restart to reset.

Run:  .venv/bin/python scripts/mock_paypal.py [--port 5001]

Test controls (MOCK ONLY — not part of PayPal's API):
  POST /mock/next-capture-status  {"status": "DECLINED"} — the next
      capture returns that status instead of COMPLETED (and does not
      change the authorization state). Lets tests prove the client
      refuses a non-COMPLETED capture.
  POST /mock/next-auth-status     {"status": "DENIED"} — the next
      authorization is created with that status instead of CREATED.
  POST /mock/drop-next-capture / POST /mock/drop-next-void — the next
      capture / void applies server-side (and is stored under its
      PayPal-Request-Id) but the HTTP response is dropped, simulating
      a lost response. The client must recover via idempotent retry.
      POST /mock/drop-next-partial-capture (round 11) — the same, but
      only a PARTIAL capture's response is dropped; a full capture's
      response is not. Lets tests prove the partial-capture lost-
      response matrix (capture, void, residual void) per leg.
  POST /mock/forget-authorization/{id} — delete an authorization from
      the mock's state, so GET authorization returns 404 — as if
      PayPal no longer knew it (vanished authorization). Lets tests
      prove the admin reconcile reports the error without mutating
      the job. Returns 404 when the id was already unknown.
  POST /mock/next-webhook-verification {"verification_status":
      "SUCCESS"|"FAILURE"} — the next verify-webhook-signature call
      returns that status instead of SUCCESS (one-shot). Lets tests
      prove both the valid- and invalid-signature paths.
      "ERROR" instead returns HTTP 500 (round 8): the verification
      could not be completed at all, so the client raises and the
      server answers 503 — the retry path the end-to-end webhook
      test proves.
  POST /mock/next-partial-status {"status": "PARTIALLY_CAPTURED"} —
      the next partial capture leaves the authorization at that
      status instead of the default (one-shot, round 9). Round 11:
      the default is now PARTIALLY_CAPTURED, so the control only
      matters for other statuses (e.g. forcing CREATED to prove the
      old path still works). Real PayPal reports PARTIALLY_CAPTURED
      after a partial capture; the mock used to keep it at CREATED,
      so reconcile never saw the real status. The capture is still
      linked to the authorization (the mock needs it for remainder
      math); only the reported status changes.
  POST /v1/notifications/verify-webhook-signature — PayPal's webhook
      signature check, mocked: returns {"verification_status":
      "SUCCESS"} unless the one-shot control below set FAILURE.
  POST /mock/send-webhook {"target_url", "event"} — MOCK ONLY:
      deliver a webhook event to a server, the way PayPal does: POST
      the event JSON with the five transmission headers. Returns the
      target's status and body. Lets a demo or an operator fire a
      webhook at a running server without PayPal.

Idempotency: the mock stores every authorize/capture/void result under
its PayPal-Request-Id header and replays the stored result when the
same key is retried — the same behaviour real PayPal documents.
"""
from __future__ import annotations

import argparse
import itertools
import socket as _socket
import time
from decimal import Decimal, InvalidOperation

from flask import Flask, jsonify, request

app = Flask(__name__)

_orders: dict[str, dict] = {}
_auths: dict[str, dict] = {}
_counter = itertools.count(1)

# PayPal-Request-Id -> (http status, json body), keyed by (path, key).
_idempotency: dict[tuple[str, str], tuple[int, dict]] = {}
# One-shot test controls (see the module docstring).
_next_capture_status: str | None = None
_next_auth_status: str | None = None
_next_partial_status: str | None = None
_drop_next_capture: bool = False
_drop_next_partial_capture: bool = False
_drop_next_void: bool = False
# One-shot webhook verification outcome (round 7): "SUCCESS" | "FAILURE".
# None means the default, SUCCESS.
_next_webhook_verification: str | None = None


def _next(prefix: str) -> str:
    return f"{prefix}-{next(_counter):04d}"


def _idempotency_key() -> str | None:
    return request.headers.get("PayPal-Request-Id")


def _replay(path: str):
    """If this request carries a PayPal-Request-Id already seen for this
    path, return the stored (status, body) instead of executing again."""
    key = _idempotency_key()
    if key:
        stored = _idempotency.get((path, key))
        if stored is not None:
            status, body = stored
            return jsonify(body), status
    return None


def _store(path: str, status: int, body: dict) -> None:
    key = _idempotency_key()
    if key:
        _idempotency[(path, key)] = (status, body)


def _drop_response() -> bool:
    """Abruptly close the client socket so the caller sees a dropped
    connection (no HTTP response at all). Returns True if the socket
    was closed. Falls back to False when the server socket is not
    exposed — the caller then returns a 500 instead (the capture/void
    has still been applied server-side, so the client must reconcile
    either way)."""
    sock = request.environ.get("werkzeug.socket")
    if sock is None:
        return False
    try:
        sock.shutdown(_socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass
    return True


@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "mock-paypal-sandbox"})


@app.route("/v1/oauth2/token", methods=["POST"])
def token():
    # The real sandbox checks Basic auth; the mock accepts any credentials.
    return jsonify({"scope": "mock", "access_token": "MOCK-ACCESS-TOKEN",
                    "token_type": "Bearer", "app_id": "MOCK-APP",
                    "expires_in": 32400, "nonce": "mock"})


@app.route("/v1/notifications/verify-webhook-signature", methods=["POST"])
def verify_webhook_signature():
    """Mock of PayPal's webhook signature check (round 7). The real
    endpoint verifies the transmission cryptographically; the mock
    answers from the one-shot control (default SUCCESS), so tests can
    prove both the valid- and invalid-signature paths. The request
    body shape is the same as the real one (auth_algo, cert_url,
    transmission_id, transmission_sig, transmission_time, webhook_id,
    webhook_event) — the mock just does not inspect it. "ERROR"
    (round 8) returns HTTP 500 instead: the verification could not be
    completed at all, so the client raises and the server answers 503
    (PayPal retries) — the path the end-to-end webhook test proves."""
    global _next_webhook_verification
    status = _next_webhook_verification or "SUCCESS"
    _next_webhook_verification = None
    if status == "ERROR":
        return jsonify({"name": "MOCK_VERIFY_ERROR",
                        "message": "webhook verification unavailable"}), 500
    return jsonify({"verification_status": status})


@app.route("/v2/checkout/orders", methods=["POST"])
def create_order():
    body = request.get_json(force=True, silent=True) or {}
    pu = (body.get("purchase_units") or [{}])[0]
    amount = pu.get("amount", {})
    oid = _next("ORDER")
    # Echo the requested payee (if any) so tests can prove the order
    # directs the capture to the worker; otherwise the merchant owns it.
    payee = pu.get("payee") or {"merchant_id": "MOCK-MERCHANT"}
    order = {
        "id": oid,
        "intent": body.get("intent", "AUTHORIZE"),
        "status": "CREATED",
        "purchase_units": [{
            "reference_id": "default",
            "amount": amount,
            "description": pu.get("description", ""),
            "custom_id": pu.get("custom_id", ""),
            "payee": payee,
        }],
        "create_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "links": [
            {"href": f"http://127.0.0.1:{request.host.split(':')[-1]}/mock/approve/{oid}",
             "rel": "approve", "method": "GET"},
            {"href": f"http://127.0.0.1:{request.host.split(':')[-1]}/v2/checkout/orders/{oid}",
             "rel": "self", "method": "GET"},
        ],
    }
    _orders[oid] = order
    return jsonify(order), 201


@app.route("/v2/checkout/orders/<order_id>", methods=["GET"])
def get_order(order_id):
    order = _orders.get(order_id)
    if order is None:
        return jsonify({"name": "RESOURCE_NOT_FOUND",
                        "message": f"order {order_id} not found"}), 404
    return jsonify(order)


@app.route("/mock/approve/<order_id>", methods=["POST", "GET"])
def mock_approve(order_id):
    """Simulate the buyer approving the order in the PayPal checkout page."""
    order = _orders.get(order_id)
    if order is None:
        return jsonify({"name": "RESOURCE_NOT_FOUND",
                        "message": f"order {order_id} not found"}), 404
    order["status"] = "APPROVED"
    order["payer"] = {"payer_id": "MOCK-BUYER",
                      "email_address": "buyer@lamplight.example"}
    return jsonify(order)


@app.route("/v2/checkout/orders/<order_id>/authorize", methods=["POST"])
def authorize_order(order_id):
    replayed = _replay(request.path)
    if replayed is not None:
        return replayed
    order = _orders.get(order_id)
    if order is None:
        return jsonify({"name": "RESOURCE_NOT_FOUND",
                        "message": f"order {order_id} not found"}), 404
    if order["status"] != "APPROVED":
        return jsonify({"name": "ORDER_NOT_APPROVED",
                        "message": "Order must be approved by the buyer before "
                                   "it can be authorized.",
                        "details": [{"issue": "ORDER_NOT_APPROVED"}]}), 422
    global _next_auth_status
    pu = order["purchase_units"][0]
    aid = _next("AUTH")
    auth = {
        "id": aid,
        "status": _next_auth_status or "CREATED",
        "amount": pu["amount"],
        "expiration_time": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                        time.gmtime(time.time() + 29 * 86400)),
        "create_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "links": [],
    }
    _next_auth_status = None
    _auths[aid] = auth
    order["status"] = "COMPLETED"
    result = {
        "id": order_id,
        "status": "COMPLETED",
        # Round 9: a real authorize response carries the buyer's payer
        # object and payment_source. The mock emits both so tests can
        # prove the service allowlists them out (app/escrow
        # sanitize_paypal_body) and they never reach a read or a receipt.
        "payer": order.get("payer") or {
            "payer_id": "MOCK-BUYER",
            "email_address": "buyer@lamplight.example",
            "name": {"given_name": "Mock", "surname": "Buyer"}},
        "payment_source": {
            "paypal": {"email_address": "buyer@lamplight.example",
                       "account_id": "MOCK-BUYER"}},
        "purchase_units": [{"payments": {"authorizations": [auth]},
                            # Round 10: real PayPal can return a
                            # shipping block here (buyer name + address)
                            # even for a NO_SHIPPING order — the
                            # service's allowlist must drop it, so the
                            # mock emits one and the tests prove it
                            # never reaches a GET or a receipt.
                            "shipping": {
                                "name": {"full_name": "Mock Buyer"},
                                "address": {
                                    "address_line_1": "1 Mock Street",
                                    "admin_area_2": "Mockville",
                                    "postal_code": "12345",
                                    "country_code": "US"}}}],
    }
    _store(request.path, 201, result)
    return jsonify(result), 201


@app.route("/v2/payments/authorizations/<auth_id>", methods=["GET"])
def get_authorization(auth_id):
    auth = _auths.get(auth_id)
    if auth is None:
        return jsonify({"name": "RESOURCE_NOT_FOUND",
                        "message": f"authorization {auth_id} not found"}), 404
    return jsonify(_authorization_view(auth))


def _authorization_view(auth: dict) -> dict:
    """The authorization resource as PayPal reports it (round 11): a
    PARTIALLY_CAPTURED authorization lists NO captures — real
    PayPal's resource omits them at that status, so a client
    reconciling one cannot recover the capture from the read and must
    replay the idempotency key instead. The capture is still linked
    internally (the mock needs it for remainder math); only the
    reported resource hides it."""
    view = dict(auth)
    if view.get("status") == "PARTIALLY_CAPTURED":
        view.pop("captures", None)
    return view


@app.route("/v2/payments/authorizations/<auth_id>/capture", methods=["POST"])
def capture_authorization(auth_id):
    replayed = _replay(request.path)
    if replayed is not None:
        return replayed
    auth = _auths.get(auth_id)
    if auth is None:
        return jsonify({"name": "RESOURCE_NOT_FOUND",
                        "message": f"authorization {auth_id} not found"}), 404
    if auth["status"] != "CREATED":
        return jsonify({"name": "AUTHORIZATION_ALREADY_CAPTURED_OR_VOIDED",
                        "message": f"authorization is {auth['status']}"}), 422
    global _next_capture_status, _next_partial_status, _drop_next_capture
    global _drop_next_partial_capture
    body = request.get_json(force=True, silent=True) or {}
    requested = body.get("amount") if isinstance(body.get("amount"), dict) else None
    # Round 9: reject an over-capture — a capture whose amount exceeds
    # what remains of the hold. The effective amount is the requested
    # one for a partial capture, the full authorized amount for a
    # no-amount capture (so a full capture after a partial capture is
    # also rejected: its amount exceeds the remainder). Real PayPal
    # rejects it; the mock used to accept it and record two captures
    # on one authorization. An unparseable amount falls back to the
    # old behaviour (treated as full) — the mock never crashes on a
    # bad amount.
    _requested_cents = (_cents(requested.get("value"))
                        if requested is not None else None)
    _remaining = _remaining_cents(auth)
    _effective_cents = (_requested_cents if _requested_cents is not None
                        else _cents((auth.get("amount") or {}).get("value")))
    if (_effective_cents is not None and _remaining is not None
            and _effective_cents > _remaining):
        return jsonify({"name": "AUTHORIZATION_AMOUNT_EXCEEDED",
                        "message": "capture amount exceeds the remaining "
                                   "authorized amount"}), 422
    if _next_capture_status is not None:
        # Test control: return a non-COMPLETED capture without changing
        # the authorization state, so the client must refuse it. The
        # amount is the requested one when the call asked for a
        # partial capture, else the full authorized amount.
        status = _next_capture_status
        _next_capture_status = None
        capture = {
            "id": _next("CAPTURE"),
            "status": status,
            "amount": requested or auth["amount"],
            "authorization_id": auth_id,
            "create_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        return jsonify(capture), 201
    # Partial vs full (round 8): a requested amount below what remains
    # of the hold is a partial capture — real PayPal leaves the
    # authorization open (status PARTIALLY_CAPTURED, round 11) so the
    # residual can be voided afterwards. No amount, or an amount >=
    # the remainder, is a full capture and the authorization becomes
    # CAPTURED, exactly as before. The capture is linked to its
    # authorization either way, so the internal remainder math sees
    # it — but GET authorization hides the list while the status is
    # PARTIALLY_CAPTURED, the way real PayPal does (see
    # _authorization_view).
    # Round 9: when the one-shot next-partial-status control is set,
    # a partial capture reports that status instead of the default.
    amount = requested or auth["amount"]
    partial = (requested is not None
               and _cents(requested.get("value")) is not None
               and _remaining_cents(auth) is not None
               and _cents(requested.get("value")) < _remaining_cents(auth))
    # Claude v8.5: honor final_capture — when the client sends
    # final_capture=true on a partial capture, real PayPal releases the
    # residual automatically and the authorization reads CAPTURED, not
    # PARTIALLY_CAPTURED. The mock used to ignore the flag, hiding the
    # real behavior from the tests.
    final_capture = body.get("final_capture") is True
    cid = _next("CAPTURE")
    if not partial:
        auth["status"] = "CAPTURED"
    elif final_capture:
        auth["status"] = "CAPTURED"
    elif _next_partial_status is not None:
        auth["status"] = _next_partial_status
        _next_partial_status = None
    else:
        # Round 11: real PayPal reports PARTIALLY_CAPTURED after a
        # partial capture — the default now matches the real API
        # instead of leaving the authorization at CREATED.
        auth["status"] = "PARTIALLY_CAPTURED"
    capture = {
        "id": cid,
        "status": "COMPLETED",
        "amount": amount,
        "authorization_id": auth_id,
        "create_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    # The capture is linked to its authorization, so the internal
    # remainder math sees it — the same shape the real API's
    # authorization resource carries for a fully captured
    # authorization. (Forced non-COMPLETED captures above are NOT
    # linked: they never moved money and the authorization stays
    # CREATED.)
    auth.setdefault("captures", []).append(capture)
    _store(request.path, 201, capture)
    if partial and _drop_next_partial_capture:
        # Test control (round 11): only a partial capture's response
        # is dropped — a full capture's response is not. The capture
        # applied server-side; the client must recover via idempotent
        # retry or reconcile.
        _drop_next_partial_capture = False
        if _drop_response():
            return jsonify({}), 201  # never reaches the client
        return jsonify({"name": "MOCK_DROPPED",
                        "message": "response dropped after capture"}), 500
    if _drop_next_capture:
        # Test control: the capture applied server-side, but the response
        # is dropped. The client must recover via idempotent retry.
        _drop_next_capture = False
        if _drop_response():
            return jsonify({}), 201  # never reaches the client
        return jsonify({"name": "MOCK_DROPPED",
                        "message": "response dropped after capture"}), 500
    return jsonify(capture), 201


def _cents(value) -> int | None:
    """Parse a PayPal decimal-string amount ("12.50") to cents, or
    None when it does not parse — the mock never crashes on a bad
    amount, it falls back to treating the capture as full."""
    if not isinstance(value, str):
        return None
    try:
        cents = Decimal(value) * 100
    except InvalidOperation:
        return None
    if cents != cents.to_integral_value():
        return None
    return int(cents)


def _remaining_cents(auth: dict) -> int | None:
    """What is left of an authorization's hold, in cents: the original
    amount minus every COMPLETED capture linked to it. None when any
    amount does not parse."""
    total = _cents((auth.get("amount") or {}).get("value"))
    if total is None:
        return None
    captured = 0
    for cap in auth.get("captures") or []:
        if isinstance(cap, dict) and cap.get("status") == "COMPLETED":
            c = _cents((cap.get("amount") or {}).get("value"))
            if c is None:
                return None
            captured += c
    return total - captured


@app.route("/v2/payments/authorizations/<auth_id>/void", methods=["POST"])
def void_authorization(auth_id):
    replayed = _replay(request.path)
    if replayed is not None:
        return replayed
    auth = _auths.get(auth_id)
    if auth is None:
        return jsonify({"name": "RESOURCE_NOT_FOUND",
                        "message": f"authorization {auth_id} not found"}), 404
    # Round 9: a partially captured authorization (PARTIALLY_CAPTURED)
    # can still be voided — that is how the residual is released.
    if auth["status"] not in ("CREATED", "PARTIALLY_CAPTURED"):
        return jsonify({"name": "AUTHORIZATION_ALREADY_CAPTURED_OR_VOIDED",
                        "message": f"authorization is {auth['status']}"}), 422
    global _drop_next_void
    auth["status"] = "VOIDED"
    _store(request.path, 200, {})
    if _drop_next_void:
        # Test control: the void applied server-side, but the response
        # is dropped. The client must recover via idempotent retry.
        _drop_next_void = False
        if _drop_response():
            return jsonify({}), 200  # never reaches the client
        return jsonify({"name": "MOCK_DROPPED",
                        "message": "response dropped after void"}), 500
    return jsonify({})


# -- mock-only test controls ---------------------------------------------------
@app.route("/mock/forget-authorization/<auth_id>", methods=["POST"])
def forget_authorization(auth_id):
    """MOCK ONLY: delete an authorization from the mock's state, so a
    later GET returns 404 — the authorization vanished from PayPal's
    side. Lets tests prove the admin reconcile reports the error
    without mutating the job (and one job's failure never stops the
    rest)."""
    if auth_id not in _auths:
        return jsonify({"name": "RESOURCE_NOT_FOUND",
                        "message": f"authorization {auth_id} not found"}), 404
    del _auths[auth_id]
    return jsonify({"forgotten": auth_id})


@app.route("/mock/next-capture-status", methods=["POST"])
def set_next_capture_status():
    global _next_capture_status
    body = request.get_json(force=True, silent=True) or {}
    _next_capture_status = body.get("status")
    return jsonify({"next_capture_status": _next_capture_status})


@app.route("/mock/next-auth-status", methods=["POST"])
def set_next_auth_status():
    global _next_auth_status
    body = request.get_json(force=True, silent=True) or {}
    _next_auth_status = body.get("status")
    return jsonify({"next_auth_status": _next_auth_status})


@app.route("/mock/next-partial-status", methods=["POST"])
def set_next_partial_status():
    """MOCK ONLY: the next partial capture leaves the authorization at
    this status instead of CREATED (one-shot, round 9). Lets tests
    prove reconcile treats PARTIALLY_CAPTURED like CREATED — the
    status real PayPal reports after a partial capture."""
    global _next_partial_status
    body = request.get_json(force=True, silent=True) or {}
    _next_partial_status = body.get("status")
    return jsonify({"next_partial_status": _next_partial_status})


@app.route("/mock/drop-next-capture", methods=["POST"])
def set_drop_next_capture():
    global _drop_next_capture
    _drop_next_capture = True
    return jsonify({"drop_next_capture": True})


@app.route("/mock/drop-next-partial-capture", methods=["POST"])
def set_drop_next_partial_capture():
    """MOCK ONLY (round 11): drop the response of the next PARTIAL
    capture only — a full capture's response is not dropped. Lets
    tests prove the partial-capture lost-response recovery (same-key
    replay, then reconcile replay) without also dropping a full
    capture's response."""
    global _drop_next_partial_capture
    _drop_next_partial_capture = True
    return jsonify({"drop_next_partial_capture": True})


@app.route("/mock/drop-next-void", methods=["POST"])
def set_drop_next_void():
    global _drop_next_void
    _drop_next_void = True
    return jsonify({"drop_next_void": True})


@app.route("/mock/next-webhook-verification", methods=["POST"])
def set_next_webhook_verification():
    """MOCK ONLY: the next verify-webhook-signature call returns this
    status instead of SUCCESS (one-shot). Lets tests prove both the
    valid- and invalid-signature paths."""
    global _next_webhook_verification
    body = request.get_json(force=True, silent=True) or {}
    _next_webhook_verification = body.get("verification_status")
    return jsonify({"next_webhook_verification": _next_webhook_verification})


def _webhook_headers(event_id: str) -> dict:
    """The five transmission headers PayPal puts on a webhook
    delivery. Values are mock-shaped (a fixed cert URL and signature)
    — the mock's verification control, not the headers, decides
    whether a delivery verifies."""
    return {
        "PAYPAL-TRANSMISSION-ID": f"MOCK-TRANSMISSION-{event_id}",
        "PAYPAL-TRANSMISSION-TIME": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "PAYPAL-CERT-URL": "https://api-m.sandbox.paypal.com/v1/"
                           "notifications/certs/CERT-123",
        "PAYPAL-AUTH-ALGO": "SHA256withRSA",
        "PAYPAL-TRANSMISSION-SIG": "MOCK-SIGNATURE",
    }


def send_webhook_event(target_url: str, event: dict) -> dict:
    """MOCK ONLY: deliver a webhook event to a server, the way PayPal
    does — POST the event JSON with the five transmission headers.
    Returns {"target_status", "target_body"}. An error status from the
    target (401/503/...) IS the delivery result, not a sender failure
    — it is reported, not raised — so a test can assert the server's
    503 retry answer through the sender. Raises only on transport
    failure (the target cannot be reached at all). Used by
    /mock/send-webhook below; tests import it (or post to the test
    client directly) to fire events at the service."""
    import json as _json
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        target_url, data=_json.dumps(event).encode(),
        headers={"Content-Type": "application/json",
                 **_webhook_headers(event.get("id", "UNKNOWN"))},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return {"target_status": resp.status,
                    "target_body": resp.read().decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        # The target answered with an error status: report it.
        return {"target_status": e.code,
                "target_body": e.read().decode("utf-8", "replace")}


@app.route("/mock/send-webhook", methods=["POST"])
def mock_send_webhook():
    """MOCK ONLY: {"target_url", "event"} — deliver a webhook event
    to a server. Lets a demo or an operator fire a webhook at a
    running server without PayPal. Returns the target's status and
    body, or 502 when the target cannot be reached."""
    body = request.get_json(force=True, silent=True) or {}
    target_url = body.get("target_url")
    event = body.get("event")
    if not target_url or not isinstance(event, dict):
        return jsonify({"error": "target_url and event are required"}), 400
    try:
        return jsonify(send_webhook_event(target_url, event))
    except Exception as e:  # noqa: BLE001 — surfaced as a 502, not hidden
        return jsonify({"error": str(e)}), 502


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5001)
    args = parser.parse_args()
    # Threaded so a dropped-response test (which closes the client
    # socket mid-request) cannot stall or kill the server.
    app.run(host="127.0.0.1", port=args.port, threaded=True)

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
       instead of recording an unauditable capture.
  POST /v2/payments/authorizations/{id}/capture -> COMPLETED capture
  POST /v2/payments/authorizations/{id}/void    -> VOIDED

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
  POST /mock/forget-authorization/{id} — delete an authorization from
      the mock's state, so GET authorization returns 404 — as if
      PayPal no longer knew it (vanished authorization). Lets tests
      prove the admin reconcile reports the error without mutating
      the job. Returns 404 when the id was already unknown.

Idempotency: the mock stores every authorize/capture/void result under
its PayPal-Request-Id header and replays the stored result when the
same key is retried — the same behaviour real PayPal documents.
"""
from __future__ import annotations

import argparse
import itertools
import socket as _socket
import time

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
_drop_next_capture: bool = False
_drop_next_void: bool = False


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
        "purchase_units": [{"payments": {"authorizations": [auth]}}],
    }
    _store(request.path, 201, result)
    return jsonify(result), 201


@app.route("/v2/payments/authorizations/<auth_id>", methods=["GET"])
def get_authorization(auth_id):
    auth = _auths.get(auth_id)
    if auth is None:
        return jsonify({"name": "RESOURCE_NOT_FOUND",
                        "message": f"authorization {auth_id} not found"}), 404
    return jsonify(auth)


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
    global _next_capture_status, _drop_next_capture
    if _next_capture_status is not None:
        # Test control: return a non-COMPLETED capture without changing
        # the authorization state, so the client must refuse it.
        status = _next_capture_status
        _next_capture_status = None
        capture = {
            "id": _next("CAPTURE"),
            "status": status,
            "amount": auth["amount"],
            "authorization_id": auth_id,
            "create_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        return jsonify(capture), 201
    body = request.get_json(force=True, silent=True) or {}
    amount = (body.get("amount") or auth["amount"])
    cid = _next("CAPTURE")
    auth["status"] = "CAPTURED"
    capture = {
        "id": cid,
        "status": "COMPLETED",
        "amount": amount,
        "authorization_id": auth_id,
        "create_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    # The capture is linked to its authorization, so GET authorization
    # exposes it — the same shape the real API's authorization resource
    # carries, and what a client reconciling a lost response reads.
    # (Forced non-COMPLETED captures above are NOT linked: they never
    # moved money and the authorization stays CREATED.)
    auth.setdefault("captures", []).append(capture)
    _store(request.path, 201, capture)
    if _drop_next_capture:
        # Test control: the capture applied server-side, but the response
        # is dropped. The client must recover via idempotent retry.
        _drop_next_capture = False
        if _drop_response():
            return jsonify({}), 201  # never reaches the client
        return jsonify({"name": "MOCK_DROPPED",
                        "message": "response dropped after capture"}), 500
    return jsonify(capture), 201


@app.route("/v2/payments/authorizations/<auth_id>/void", methods=["POST"])
def void_authorization(auth_id):
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


@app.route("/mock/drop-next-capture", methods=["POST"])
def set_drop_next_capture():
    global _drop_next_capture
    _drop_next_capture = True
    return jsonify({"drop_next_capture": True})


@app.route("/mock/drop-next-void", methods=["POST"])
def set_drop_next_void():
    global _drop_next_void
    _drop_next_void = True
    return jsonify({"drop_next_void": True})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5001)
    args = parser.parse_args()
    # Threaded so a dropped-response test (which closes the client
    # socket mid-request) cannot stall or kill the server.
    app.run(host="127.0.0.1", port=args.port, threaded=True)

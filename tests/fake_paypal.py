"""In-process fake PayPal client for property-based testing.

Phase 1 of the property-testing game plan: the subprocess mock
(scripts/mock_paypal.py) is too slow and too stateful for hundreds of
Hypothesis examples. Each example needs a *fresh, deterministic* PayPal.

FakePayPalClient subclasses PayPalClient and implements the same methods
against in-memory dicts:

    create_order / get_order / authorize_order /
    get_authorization / capture_authorization / void_authorization

Idempotency contract (Stripe behavior, stricter than the subprocess
mock): same PayPal-Request-Id + same params -> replay the recorded
response; same key + different params -> PayPalError(422). The mock
only replays on key match without checking params; the fake enforces
the full contract. tests/test_fake_conformance.py proves the fake and
the mock agree on the observable happy-path contract.

Hooks for later phases (present but unused in Phase 1):
    fail_next(operation, error) -- raise a programmed PayPalError once
    (Phase 3 chaos proxy builds on this pattern).

Call record: every call appends (operation, idempotency_key, params_fp,
was_replay) to self.calls, so tests can assert N-retries-one-effect.

One deliberate deviation: the fake exposes approve_order(order_id) as an
explicit step (the real flow needs the buyer to click approve). The
state machine calls it between create_order and authorize. The fake
does NOT auto-approve -- an authorize_order on a CREATED order raises
422 ORDER_NOT_APPROVED, exactly like the mock.
"""
from __future__ import annotations

import itertools
import json
import time

from app.paypal_client import (
    PayPalClient,
    PayPalError,
    amount_to_cents,
    cents_to_amount,
)


def _params_fingerprint(params: dict) -> str:
    """Stable fingerprint of call params for the same-key-different-params check."""
    return json.dumps(params, sort_keys=True, separators=(",", ":"))


class FakePayPalClient(PayPalClient):
    """In-memory PayPal double. Fresh per instance; fully deterministic."""

    def __init__(self):
        # Bypass PayPalClient.__init__ (no credentials needed for the fake).
        self.client_id = "fake"
        self.client_secret = "fake"
        self.base_url = "fake://localhost"
        self._token = "fake-token"
        self._token_expires_at = float("inf")
        # -- fake state ---------------------------------------------------
        self._orders: dict[str, dict] = {}
        self._auths: dict[str, dict] = {}
        # (operation, idempotency_key) -> (params_fp, response_dict)
        self._idempotency: dict[tuple[str, str], tuple[str, dict]] = {}
        # (operation, idempotency_key, params_fp, was_replay)
        self.calls: list[tuple[str, str | None, str, bool]] = []
        self._counter = itertools.count(1)
        # Phase 3 hook: operation -> PayPalError to raise once.
        self._fail_next: dict[str, PayPalError] = {}

    # -- helpers ----------------------------------------------------------
    def _next(self, prefix: str) -> str:
        return f"{prefix}-{next(self._counter):04d}"

    def _check_idempotency(self, operation: str,
                           request_id: str | None,
                           params: dict) -> dict | None:
        """Return the replayed response if (operation, key) was seen.

        Same key + different params -> PayPalError(422), per Stripe's
        contract (catches client key-reuse bugs). Records the call in
        self.calls either way.
        """
        fp = _params_fingerprint(params)
        if request_id:
            slot = self._idempotency.get((operation, request_id))
            if slot is not None:
                old_fp, response = slot
                if old_fp != fp:
                    self.calls.append((operation, request_id, fp, False))
                    raise PayPalError(
                        "idempotency key already used with different parameters",
                        status=422,
                        body={"name": "IDEMPOTENCY_KEY_IN_USE"},
                    )
                self.calls.append((operation, request_id, fp, True))
                # Return a copy so callers can't mutate the recorded response.
                return json.loads(json.dumps(response))
            self.calls.append((operation, request_id, fp, False))
        else:
            self.calls.append((operation, None, fp, False))
        return None

    def _store_idempotency(self, operation: str, request_id: str | None,
                           params: dict, response: dict) -> None:
        if request_id:
            self._idempotency[(operation, request_id)] = (
                _params_fingerprint(params), json.loads(json.dumps(response)))

    def _maybe_fail(self, operation: str) -> None:
        err = self._fail_next.pop(operation, None)
        if err is not None:
            raise err

    # -- Phase 3 hook ------------------------------------------------------
    def fail_next(self, operation: str, error: PayPalError) -> None:
        """Raise `error` on the next call to `operation`, once."""
        self._fail_next[operation] = error

    # -- orders ------------------------------------------------------------
    def create_order(self, *, amount_cents: int, currency: str = "USD",
                     description: str, custom_id: str,
                     payee_email: str | None = None,
                     return_url: str = "https://example.com/return",
                     cancel_url: str = "https://example.com/cancel") -> dict:
        oid = self._next("ORDER")
        order = {
            "id": oid,
            "status": "CREATED",
            "intent": "AUTHORIZE",
            "purchase_units": [{
                "amount": {"currency_code": currency,
                           "value": cents_to_amount(amount_cents)},
                "description": description[:127],
                "custom_id": custom_id[:127],
            }],
            "links": [{"href": f"fake://approve/{oid}",
                       "rel": "approve", "method": "GET"}],
        }
        self._orders[oid] = order
        return json.loads(json.dumps(order))

    def approve_order(self, order_id: str) -> dict:
        """Fake-only: simulate the buyer clicking the approve link."""
        order = self._orders.get(order_id)
        if order is None:
            raise PayPalError(f"order {order_id} not found", status=404)
        order["status"] = "APPROVED"
        return json.loads(json.dumps(order))

    def get_order(self, order_id: str) -> dict:
        order = self._orders.get(order_id)
        if order is None:
            raise PayPalError(f"order {order_id} not found", status=404)
        return json.loads(json.dumps(order))

    def authorize_order(self, order_id: str,
                        request_id: str | None = None) -> dict:
        params = {"order_id": order_id}
        replayed = self._check_idempotency("authorize_order", request_id, params)
        if replayed is not None:
            return replayed
        self._maybe_fail("authorize_order")
        order = self._orders.get(order_id)
        if order is None:
            raise PayPalError(f"order {order_id} not found", status=404)
        if order["status"] != "APPROVED":
            raise PayPalError(
                "Order must be approved by the buyer before authorize",
                status=422,
                body={"name": "ORDER_NOT_APPROVED"})
        pu = order["purchase_units"][0]
        aid = self._next("AUTH")
        auth = {
            "id": aid,
            "status": "CREATED",
            "amount": pu["amount"],
            "captures": [],
            "create_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._auths[aid] = auth
        order["status"] = "COMPLETED"
        # Same shape as the mock: the order response with the
        # authorization nested under purchase_units[0].payments.
        # The service's _extract_first_authorization handles it.
        response = {
            "id": order_id,
            "status": "COMPLETED",
            "purchase_units": [{"payments": {"authorizations": [auth]}}],
        }
        self._store_idempotency("authorize_order", request_id, params, response)
        return json.loads(json.dumps(response))

    # -- authorizations ----------------------------------------------------
    def get_authorization(self, authorization_id: str) -> dict:
        auth = self._auths.get(authorization_id)
        if auth is None:
            raise PayPalError(f"authorization {authorization_id} not found",
                              status=404)
        return json.loads(json.dumps(auth))

    def _remaining_cents(self, auth: dict) -> int:
        total = amount_to_cents(auth["amount"]["value"])
        captured = sum(amount_to_cents(c["amount"]["value"])
                       for c in auth.get("captures", []))
        return total - captured

    def capture_authorization(self, authorization_id: str,
                              amount_cents: int | None = None,
                              currency: str = "USD",
                              request_id: str | None = None,
                              final_capture: bool = False) -> dict:
        params = {"authorization_id": authorization_id,
                  "amount_cents": amount_cents, "currency": currency,
                  "final_capture": final_capture}
        replayed = self._check_idempotency("capture_authorization",
                                           request_id, params)
        if replayed is not None:
            return replayed
        self._maybe_fail("capture_authorization")
        auth = self._auths.get(authorization_id)
        if auth is None:
            raise PayPalError(f"authorization {authorization_id} not found",
                              status=404)
        if auth["status"] != "CREATED":
            raise PayPalError(
                f"authorization is {auth['status']}",
                status=422,
                body={"name": "AUTHORIZATION_ALREADY_CAPTURED_OR_VOIDED"})
        remaining = self._remaining_cents(auth)
        if amount_cents is not None and amount_cents > remaining:
            raise PayPalError(
                "capture amount exceeds the remaining authorized amount",
                status=422,
                body={"name": "AUTHORIZATION_AMOUNT_EXCEEDED"})
        partial = (amount_cents is not None and amount_cents < remaining)
        amount = ({"currency_code": currency,
                   "value": cents_to_amount(amount_cents)}
                  if amount_cents is not None else dict(auth["amount"]))
        cid = self._next("CAPTURE")
        capture = {
            "id": cid,
            "status": "COMPLETED",
            "amount": amount,
            "authorization_id": authorization_id,
            "create_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        auth["captures"].append(capture)
        # Match the mock: partial without final_capture leaves the auth
        # open (PARTIALLY_CAPTURED); anything else closes it (CAPTURED).
        if not partial or final_capture:
            auth["status"] = "CAPTURED"
        else:
            auth["status"] = "PARTIALLY_CAPTURED"
        self._store_idempotency("capture_authorization", request_id,
                                params, capture)
        return json.loads(json.dumps(capture))

    def void_authorization(self, authorization_id: str,
                           request_id: str | None = None) -> dict:
        params = {"authorization_id": authorization_id}
        replayed = self._check_idempotency("void_authorization",
                                           request_id, params)
        if replayed is not None:
            return replayed
        self._maybe_fail("void_authorization")
        auth = self._auths.get(authorization_id)
        if auth is None:
            raise PayPalError(f"authorization {authorization_id} not found",
                              status=404)
        if auth["status"] not in ("CREATED", "PARTIALLY_CAPTURED"):
            raise PayPalError(
                f"authorization is {auth['status']}",
                status=422,
                body={"name": "AUTHORIZATION_ALREADY_CAPTURED_OR_VOIDED"})
        auth["status"] = "VOIDED"
        # Match the mock (and real PayPal's 204): empty body on success.
        response: dict = {}
        self._store_idempotency("void_authorization", request_id,
                                params, response)
        return {}

    # -- test introspection -------------------------------------------------
    def executed_call_count(self, operation: str,
                            request_id: str | None) -> int:
        """How many times (operation, key) actually executed (not replayed)."""
        return sum(1 for op, key, _fp, was_replay in self.calls
                   if op == operation and key == request_id
                   and not was_replay)

    def reset(self) -> None:
        """Fresh PayPal for the next Hypothesis example."""
        self._orders.clear()
        self._auths.clear()
        self._idempotency.clear()
        self.calls.clear()
        self._fail_next.clear()
        self._counter = itertools.count(1)

"""PayPal REST client for Lamplight Checkout.

Talks to the PayPal sandbox (default) or live API (PAYPAL_BASE_URL) with
plain requests — no SDK, so every call is visible and auditable.

Flow (Orders API v2, intent=AUTHORIZE):
  create_order()      -> order id + buyer approve link (hold is NOT placed yet)
  get_order()         -> poll until status == "APPROVED" (buyer approved)
  authorize_order()   -> places the hold; returns authorization id
  capture_authorization() -> moves the held money to the worker agent
  void_authorization()    -> releases the hold back to the payer

Credentials come from environment variables only:
  PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET, PAYPAL_BASE_URL (optional,
  default https://api-m.sandbox.paypal.com). Nothing secret is ever written
  to a file. With no credentials set, the client raises PayPalConfigError —
  use scripts/demo.py --mode mock to run the full flow against the local
  mock PayPal server instead.
"""
from __future__ import annotations

import base64
import os
import time
from decimal import Decimal, InvalidOperation

import requests

DEFAULT_BASE_URL = "https://api-m.sandbox.paypal.com"


class PayPalError(Exception):
    """A PayPal API call failed; carries the HTTP status and PayPal's body."""

    def __init__(self, message: str, status: int | None = None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


class PayPalConfigError(PayPalError):
    """Credentials or base URL missing — nothing was sent to PayPal."""


def cents_to_amount(cents: int) -> str:
    """1250 -> "12.50". PayPal wants a decimal string with 2 places."""
    if cents < 0:
        raise ValueError("amount_cents must be >= 0")
    return f"{cents // 100}.{cents % 100:02d}"


def amount_to_cents(value) -> int:
    """"12.50" -> 1250. PayPal returns amounts as decimal strings;
    parse with Decimal so float error (0.10 + 0.20 style) never appears.
    Only strings are accepted (that is what PayPal returns), and only
    at most two decimal places — anything else raises ValueError."""
    if not isinstance(value, str):
        raise ValueError(f"bad PayPal amount: {value!r}")
    try:
        cents = Decimal(value) * 100
    except InvalidOperation as e:
        raise ValueError(f"bad PayPal amount: {value!r}") from e
    if cents != cents.to_integral_value():
        raise ValueError(f"bad PayPal amount: {value!r}")
    return int(cents)


def first_completed_capture(authorization: dict) -> dict | None:
    """The first COMPLETED capture linked to an authorization, if any.

    The authorization resource carries its captures (the mock does the
    same — see scripts/mock_paypal.py). The real sandbox may list none
    even for a CAPTURED authorization; the caller then keeps the
    reconciled-but-unobserved fallback (capture id "", flag set) rather
    than inventing an id."""
    for cap in authorization.get("captures") or []:
        if isinstance(cap, dict) and cap.get("status") == "COMPLETED":
            return cap
    return None


class PayPalClient:
    def __init__(self, client_id: str | None = None,
                 client_secret: str | None = None,
                 base_url: str | None = None):
        self.client_id = client_id or os.environ.get("PAYPAL_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("PAYPAL_CLIENT_SECRET")
        self.base_url = (base_url or os.environ.get("PAYPAL_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        if not self.client_id or not self.client_secret:
            raise PayPalConfigError(
                "PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET not set. "
                "Create a sandbox app at https://developer.paypal.com "
                "(Dashboard > My Apps & Credentials > Create App) and export "
                "the Client ID and Secret — see README.md 'Sandbox setup'.")
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # -- auth -----------------------------------------------------------------
    def get_access_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 30:
            return self._token
        try:
            resp = requests.post(
                f"{self.base_url}/v1/oauth2/token",
                auth=(self.client_id, self.client_secret),
                data={"grant_type": "client_credentials"},
                headers={"Accept": "application/json"},
                timeout=30,
            )
        except requests.RequestException as e:
            raise PayPalError(f"OAuth token request failed (transport): {e}")
        if resp.status_code != 200:
            raise PayPalError("OAuth token request failed",
                              resp.status_code, resp.text)
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires_at = time.time() + int(data.get("expires_in", 300))
        return self._token

    def _request(self, method: str, path: str, *,
                 request_id: str | None = None, **kwargs) -> dict:
        """One PayPal call. `request_id` sends PayPal-Request-Id — PayPal's
        idempotency key: retrying a call with the same key replays the
        original result instead of executing twice, which is how the escrow
        recovers when a response is lost after PayPal already acted."""
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.get_access_token()}"
        headers.setdefault("Content-Type", "application/json")
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        try:
            resp = requests.request(method, f"{self.base_url}{path}",
                                    headers=headers, timeout=30, **kwargs)
        except requests.RequestException as e:
            # Transport failure (dropped connection, timeout): the call may
            # still have executed server-side. Surface it as PayPalError so
            # callers can reconcile instead of assuming it did not happen.
            raise PayPalError(f"{method} {path} failed (transport): {e}")
        if resp.status_code in (200, 201):
            return resp.json() if resp.text else {}
        if resp.status_code == 204:
            return {}
        raise PayPalError(f"{method} {path} failed: {resp.text}",
                          resp.status_code, resp.text)

    # -- orders ---------------------------------------------------------------
    def create_order(self, *, amount_cents: int, currency: str = "USD",
                     description: str, custom_id: str,
                     payee_email: str | None = None,
                     return_url: str = "https://example.com/return",
                     cancel_url: str = "https://example.com/cancel") -> dict:
        """Create an AUTHORIZE order. Returns the order; the buyer must still
        approve it (see the approve link) before it can be authorized.

        `payee_email` is the worker agent's PayPal account: captured funds
        go to the payee, not to the merchant that owns the API credentials.
        Without it the capture lands in the merchant's account and the
        receipt's "agent paid" claim would be false. In live mode the
        payee must be a real PayPal account — the demo workers'
        @lamplight.example addresses are fictional, so a live run needs a
        real sandbox payee (see README 'Payee')."""
        purchase_unit: dict = {
            "amount": {"currency_code": currency,
                       "value": cents_to_amount(amount_cents)},
            "description": description[:127],
            "custom_id": custom_id[:127],
        }
        if payee_email:
            purchase_unit["payee"] = {"email_address": payee_email}
        body = {
            "intent": "AUTHORIZE",
            "purchase_units": [purchase_unit],
            "application_context": {
                "return_url": return_url,
                "cancel_url": cancel_url,
                "user_action": "CONTINUE",
            },
        }
        return self._request("POST", "/v2/checkout/orders", json=body)

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/v2/checkout/orders/{order_id}")

    @staticmethod
    def approve_link(order: dict) -> str | None:
        for link in order.get("links", []):
            if link.get("rel") == "approve":
                return link["href"]
        return None

    def authorize_order(self, order_id: str,
                        request_id: str | None = None) -> dict:
        """Place the hold. The order must be APPROVED first (buyer clicked
        the approve link). Returns the authorization object."""
        return self._request("POST", f"/v2/checkout/orders/{order_id}/authorize",
                             json={}, request_id=request_id)

    # -- authorizations --------------------------------------------------------
    def get_authorization(self, authorization_id: str) -> dict:
        return self._request("GET", f"/v2/payments/authorizations/{authorization_id}")

    def capture_authorization(self, authorization_id: str,
                              amount_cents: int | None = None,
                              currency: str = "USD",
                              request_id: str | None = None) -> dict:
        """Move held money to the worker agent. Omit amount_cents to capture
        the full authorized amount. `request_id` is PayPal's idempotency
        key: retrying with the same key replays the original capture
        instead of capturing twice."""
        body: dict = {}
        if amount_cents is not None:
            body["amount"] = {"currency_code": currency,
                              "value": cents_to_amount(amount_cents)}
        return self._request(
            "POST", f"/v2/payments/authorizations/{authorization_id}/capture",
            json=body, request_id=request_id)

    def void_authorization(self, authorization_id: str,
                           request_id: str | None = None) -> dict:
        """Release the hold back to the payer. The authorization must not
        already be captured. `request_id` is PayPal's idempotency key."""
        return self._request(
            "POST", f"/v2/payments/authorizations/{authorization_id}/void",
            json={}, request_id=request_id)

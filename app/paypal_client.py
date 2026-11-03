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

import os
import re
import time
from decimal import Decimal, InvalidOperation

import requests

DEFAULT_BASE_URL = "https://api-m.sandbox.paypal.com"


class PayPalError(Exception):
    """A PayPal API call failed; carries the HTTP status, PayPal's body,
    and the response headers (Claude Q2: the 429 Retry-After header
    tells us exactly how long to back off — never guess)."""

    def __init__(self, message: str, status: int | None = None, body=None,
                 headers: dict | None = None):
        super().__init__(message)
        self.status = status
        self.body = body
        # Response headers, lower-cased for case-insensitive lookup.
        self.headers: dict[str, str] = {
            str(k).lower(): str(v) for k, v in (headers or {}).items()}

    @property
    def retry_after_seconds(self) -> float | None:
        """PayPal's Retry-After hint on 429 (Claude Q2), else None."""
        raw = self.headers.get("retry-after")
        if raw is None:
            return None
        try:
            return max(0.0, float(str(raw).strip()))
        except (ValueError, TypeError):
            return None


class PayPalConfigError(PayPalError):
    """Credentials or base URL missing — nothing was sent to PayPal."""


def cents_to_amount(cents: int) -> str:
    """1250 -> "12.50". PayPal wants a decimal string with 2 places."""
    if cents < 0:
        raise ValueError("amount_cents must be >= 0")
    return f"{cents // 100}.{cents % 100:02d}"


# Round 10: the amount shape, checked before Decimal parses it.
# Plain digits, optionally a decimal point with one or two places —
# no sign, no exponent, no words ("Infinity", "NaN"), no empties.
_AMOUNT_RE = re.compile(r"(?P<int>\d+)(\.\d{1,2})?")


def amount_to_cents(value) -> int:
    """"12.50" -> 1250. PayPal returns amounts as decimal strings;
    parse with Decimal so float error (0.10 + 0.20 style) never appears.
    Only strings are accepted (that is what PayPal returns), and only
    at most two decimal places — anything else raises ValueError.
    Round 9 (Oct 28, 2026): negative and non-finite values are also
    rejected — "-5.00" used to return -500, and "Infinity" raised
    OverflowError (which _record_capture_amount does not catch), so a
    malformed PayPal response could escape after PayPal had already
    captured and leave the job unrecorded.
    Round 10 (Oct 28, 2026): the round-9 fix was incomplete —
    "1E+999999" raised decimal.Overflow, which is neither
    InvalidOperation nor ValueError, so it still escaped the callers
    (which catch only ValueError) and aborted reconcile_open_jobs.
    The value is now validated against a strict shape BEFORE Decimal
    ever sees it — plain digits with at most two decimals — so no
    exponent, sign, word or empty string can reach the parser, and
    ArithmeticError is caught as well, so the only failure this
    function can raise is ValueError. A 16+-digit integer part is
    also refused: no real PayPal amount is that large, and Decimal's
    default 28-digit context would silently round one."""
    if not isinstance(value, str):
        raise ValueError(f"bad PayPal amount: {value!r}")
    match = _AMOUNT_RE.fullmatch(value)
    if match is None or len(match.group("int")) > 15:
        raise ValueError(f"bad PayPal amount: {value!r}")
    try:
        cents = Decimal(value) * 100
    except (InvalidOperation, ArithmeticError) as e:
        raise ValueError(f"bad PayPal amount: {value!r}") from e
    if not cents.is_finite() or cents < 0:
        raise ValueError(f"bad PayPal amount: {value!r}")
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
                          resp.status_code, resp.text,
                          dict(resp.headers))

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
                # Round 10: this is a service, not a shipped good —
                # tell PayPal not to collect a shipping address, so no
                # buyer name/address ever enters the authorize response
                # in the first place. The recorded-response allowlist
                # (app/escrow.py) is the second layer: a shipping
                # block PayPal returns anyway is still dropped.
                "shipping_preference": "NO_SHIPPING",
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
                              request_id: str | None = None,
                              final_capture: bool = False) -> dict:
        """Move held money to the worker agent. Omit amount_cents to capture
        the full authorized amount. `request_id` is PayPal's idempotency
        key: retrying with the same key replays the original capture
        instead of capturing twice. `final_capture` tells PayPal this is
        the last capture against the authorization — any remaining held
        amount is released automatically (Claude v8.4 review: without it,
        a partial capture leaves the residual held on real PayPal)."""
        body: dict = {}
        if amount_cents is not None:
            body["amount"] = {"currency_code": currency,
                              "value": cents_to_amount(amount_cents)}
        if final_capture:
            body["final_capture"] = True
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

    # -- webhooks -------------------------------------------------------------
    def verify_webhook_signature(self, *, webhook_id: str,
                                 transmission_id: str,
                                 transmission_time: str,
                                 cert_url: str, auth_algo: str,
                                 transmission_sig: str,
                                 webhook_event: dict | bytes) -> bool:
        """Ask PayPal whether a webhook delivery is genuine
        (POST /v1/notifications/verify-webhook-signature). The five
        transmission values come from the delivery's headers, the
        webhook id identifies the webhook registered in the PayPal
        dashboard (env LAMPLIGHT_PAYPAL_WEBHOOK_ID), and the event is
        the delivery's JSON body. Returns True only when PayPal
        answers verification_status == "SUCCESS" — anything else
        (FAILURE, or a missing status) is treated as invalid, never
        as a pass. A transport or HTTP error raises PayPalError, so
        the caller can answer 503 (PayPal retries the delivery)
        instead of deciding on an unverified event.

        HIGH #1 (Claude): ``webhook_event`` MUST be the raw delivery
        bytes in production. PayPal's verification covers the exact
        original bytes — if we json.loads then re-serialize, key
        order/spacing/unicode escapes change and genuine deliveries
        fail verification intermittently. The raw bytes are spliced
        directly into the request body without re-serialization. A
        dict is accepted only for tests (it is serialized with
        sort_keys/separators for determinism, but this is NOT
        byte-identical to production)."""
        import json as _json
        if isinstance(webhook_event, (bytes, bytearray)):
            raw_event = bytes(webhook_event)
        else:
            # Test path only — NOT byte-identical to a real delivery.
            raw_event = _json.dumps(
                webhook_event, sort_keys=True,
                separators=(",", ":")).encode("utf-8")
        # Splice the raw event bytes directly into the request body —
        # never re-serialize (Claude HIGH #1).
        head = _json.dumps({
            "auth_algo": auth_algo,
            "cert_url": cert_url,
            "transmission_id": transmission_id,
            "transmission_sig": transmission_sig,
            "transmission_time": transmission_time,
            "webhook_id": webhook_id,
        }, sort_keys=True, separators=(",", ":"))
        # head ends with "}"; replace it with ,"webhook_event":<RAW>}
        body_bytes = (head[:-1] + ',"webhook_event":').encode("utf-8") \
            + raw_event + b"}"
        result = self._request(
            "POST", "/v1/notifications/verify-webhook-signature",
            data=body_bytes)
        return result.get("verification_status") == "SUCCESS"

"""Quote / invoice objects for Lamplight Checkout.

A Quote is the off-chain half of a payment: it tells the hiring agent exactly
what to hold, where, and by when. It is JSON-serialisable and hash-stable
(keys sorted, no timestamps inside the hash input except expiry).
Same design as Lamplight Pay (../hsk/a2a/quote.py), priced in USD cents for
PayPal instead of token smallest-units.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid

PROTOCOL = "lamplight-checkout/1"


def new_quote(*, service: str, price_cents: int, currency: str = "USD",
              network: str = "PayPal Sandbox", pay_to: str,
              ttl_seconds: int = 3600) -> dict:
    """Build a quote. `price_cents` is in USD cents (e.g. 1250 = $12.50)."""
    now = int(time.time())
    quote = {
        "protocol": PROTOCOL,
        "quote_id": str(uuid.uuid4()),
        "service": service,
        "price_cents": price_cents,
        "currency": currency,
        "network": network,
        "pay_to": pay_to,          # the worker agent's PayPal account (email or merchant id)
        "issued_at": now,
        "expires_at": now + ttl_seconds,
        "nonce": uuid.uuid4().hex[:16],
    }
    quote["quote_hash"] = _hash(quote)
    return quote


def _hash(quote: dict) -> str:
    payload = {k: v for k, v in quote.items() if k != "quote_hash"}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "0x" + hashlib.sha256(blob).hexdigest()


def verify_quote_hash(quote: dict) -> bool:
    """True if the quote's stored hash matches its contents (untampered)."""
    return quote.get("quote_hash") == _hash(quote)


def quote_expired(quote: dict, now: int | None = None) -> bool:
    effective_now = now if now is not None else int(time.time())
    return effective_now > quote["expires_at"]


def to_json(quote: dict) -> str:
    return json.dumps(quote, indent=2, sort_keys=True)

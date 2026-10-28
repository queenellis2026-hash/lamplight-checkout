"""Settlement receipts for Lamplight Checkout.

A Receipt is the audit record both agents keep after a job settles: who paid
whom, how much, for what, on PayPal, and the PayPal proof (order /
authorization / capture ids). It is hash-stable so either side can prove the
other agreed to the same facts. Same design as Lamplight Pay
(../hsk/a2a/receipt.py), with PayPal ids in place of chain/tx-hash fields.
"""
from __future__ import annotations

import hashlib
import json

PROTOCOL = "lamplight-checkout/1"


def build_receipt(*, job_id: str, quote_id: str, service: str, payer: str,
                  agent: str, currency: str, amount_cents: int,
                  network: str, paypal_order_id: str,
                  authorization_id: str, capture_id: str = "",
                  capture_amount_cents: int | None = None,
                  outcome: str, delivery_hash: str = "",
                  capture_reconciled: bool = False) -> dict:
    receipt = {
        "protocol": PROTOCOL,
        "type": "settlement-receipt",
        "job_id": job_id,
        "quote_id": quote_id,
        "service": service,
        "payer": payer,               # hiring agent's PayPal account
        "agent": agent,               # worker agent's PayPal account
        "currency": currency,
        "amount_cents": amount_cents,
        "network": network,
        "paypal_order_id": paypal_order_id,
        "authorization_id": authorization_id,
        "capture_id": capture_id,     # "" when the hold was voided, not captured
        "capture_amount_cents": capture_amount_cents,  # amount actually
            # captured, in cents; None when voided, or when the capture
            # was reconciled from an authorization that lists no capture
        "capture_reconciled": capture_reconciled,  # True when the capture was
            # recorded by reconciling PayPal's authorization state after the
            # capture response was lost. The capture id is the real one
            # when the authorization lists a COMPLETED capture; it is ""
            # only when it lists none, so a reconciled receipt with a real
            # id is fully auditable against the merchant account.
        "outcome": outcome,           # "released" | "voided" | "cancelled"
        "delivery_hash": delivery_hash,
    }
    blob = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    receipt["receipt_hash"] = "0x" + hashlib.sha256(blob).hexdigest()
    return receipt


def verify_receipt_hash(receipt: dict) -> bool:
    """True if the receipt's stored hash matches its contents (untampered)."""
    stored = receipt.get("receipt_hash")
    payload = {k: v for k, v in receipt.items() if k != "receipt_hash"}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return stored == "0x" + hashlib.sha256(blob).hexdigest()


def to_json(receipt: dict) -> str:
    return json.dumps(receipt, indent=2, sort_keys=True)

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
                  capture_ids: list | None = None,
                  capture_amount_cents: int | None = None,
                  voided_amount_cents: int | None = None,
                  outcome: str, delivery_hash: str = "",
                  capture_reconciled: bool = False,
                  settlement_method: str | None = None,
                  capture_status: str | None = None,
                  last_paypal_response: dict | None = None,
                  webhook_denial: dict | None = None) -> dict:
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
        # Gemini Q4: all capture ids (multi-capture jobs list each one).
        "capture_ids": list(capture_ids) if capture_ids else [],
        "capture_amount_cents": capture_amount_cents,  # amount actually
            # captured, in cents; None when voided, or when the capture
            # was reconciled from an authorization that lists no capture
        "voided_amount_cents": voided_amount_cents,  # (round 8) the
            # residual voided back to the payer by a partial capture:
            # captured + voided = the hold. None for a full capture or
            # a plain void. A "released" receipt with this set IS a
            # partial release — the split is auditable from the receipt
            # alone, without re-querying PayPal.
        "capture_reconciled": capture_reconciled,  # True when the capture was
            # recorded by reconciling PayPal's authorization state after the
            # capture response was lost. The capture id is the real one
            # when the authorization lists a COMPLETED capture; it is ""
            # only when it lists none, so a reconciled receipt with a real
            # id is fully auditable against the merchant account.
        "settlement_method": settlement_method,  # (round 31, B2) how the
            # settlement was established: "observed" (direct POST
            # response), "reconciled" (found via read), or
            # "force_settled" (payer-only force-settle). None on voids
            # and on receipts built before round 31.
        "capture_status": capture_status,  # (round 36) the recorded
            # capture's PayPal status: COMPLETED, REFUNDED, or
            # PARTIALLY_REFUNDED. A refunded capture settles the job
            # but must be visible as refunded without re-querying
            # PayPal. None on voids and on receipts built before
            # round 36.
        "last_paypal_response": last_paypal_response,  # the last
            # successful PayPal response on the job (envelope: operation
            # + status + id + raw body), so the receipt is auditable
            # without re-querying PayPal. None when no PayPal call has
            # succeeded yet (a receipt is only built for settled jobs,
            # so in practice it is the capture or void record).
        "webhook_denial": webhook_denial,  # (round 13) a capture
            # denial PayPal reported for this job ({"event_id",
            # "resource_id", "status": "DENIED"}), or None. The job
            # document already stored it — the receipt must carry it
            # too, or a denial disappears from the artifact the payer
            # actually verifies.
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


# Fields withheld from the receipt served on unauthenticated endpoints.
# The open GET /jobs/<id> endpoint returns the job's public dict plus the
# receipt — without this split, the receipt would leak the PayPal-side ids
# and account identifiers that Job.to_public_dict() deliberately hides.
_PUBLIC_RECEIPT_STRIP = frozenset({
    "payer", "agent",                 # PayPal account identifiers
    "paypal_order_id", "authorization_id",
    "capture_id", "capture_ids",      # PayPal-side money ids
    "last_paypal_response",           # raw PayPal body
    "webhook_denial",                 # PayPal event/resource ids
})


def to_public_receipt(receipt: dict) -> dict:
    """Public-safe subset of a settlement receipt.

    Drops PayPal-side ids, account identifiers, and the raw PayPal
    response body, then re-hashes over the public subset so
    verify_receipt_hash still holds on what was actually served.
    """
    public = {k: v for k, v in receipt.items()
              if k not in _PUBLIC_RECEIPT_STRIP and k != "receipt_hash"}
    blob = json.dumps(public, sort_keys=True, separators=(",", ":")).encode()
    public["receipt_hash"] = "0x" + hashlib.sha256(blob).hexdigest()
    return public

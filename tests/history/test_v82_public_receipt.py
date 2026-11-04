"""v8.2 reviewer finding (Grok Build): the public receipt undid the
public-dict fix.

The open GET /jobs/<id> endpoint serves Job.to_public_dict() (which hides
PayPal-side ids) — but it also served the FULL settlement receipt, which
carries paypal_order_id, authorization_id, capture_ids, the raw PayPal
response body, and the payer/agent account identifiers. The receipt
leaked everything the public dict hid.

Fix: to_public_receipt() strips the sensitive fields and re-hashes over
the public subset; the open endpoint serves it, authenticated endpoints
keep the full receipt.
"""
from app.receipt import (
    build_receipt,
    to_public_receipt,
    verify_receipt_hash,
)


def _full_receipt():
    return build_receipt(
        job_id="job-1", quote_id="q-1", service="audit",
        payer="payer@x.example", agent="agent@y.example",
        currency="USD", amount_cents=1250, network="PayPal Sandbox",
        paypal_order_id="ORDER-1", authorization_id="AUTH-1",
        capture_id="CAPTURE-1", capture_ids=["CAPTURE-1"],
        capture_amount_cents=1250, outcome="released",
        delivery_hash="0xabc",
        last_paypal_response={"op": "capture", "raw": {"id": "CAPTURE-1"}},
        webhook_denial={"event_id": "EVT-1"},
    )


def test_public_receipt_strips_paypal_ids_and_accounts():
    public = to_public_receipt(_full_receipt())
    for field in ("payer", "agent", "paypal_order_id", "authorization_id",
                  "capture_id", "capture_ids", "last_paypal_response",
                  "webhook_denial"):
        assert field not in public, field


def test_public_receipt_keeps_settlement_facts():
    public = to_public_receipt(_full_receipt())
    assert public["job_id"] == "job-1"
    assert public["outcome"] == "released"
    assert public["capture_amount_cents"] == 1250
    assert public["delivery_hash"] == "0xabc"


def test_public_receipt_hash_verifies_on_what_was_served():
    public = to_public_receipt(_full_receipt())
    assert public["receipt_hash"].startswith("0x")
    assert verify_receipt_hash(public) is True

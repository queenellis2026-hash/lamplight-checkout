"""Tests for the hash-stable quote and receipt modules."""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.quote import new_quote, verify_quote_hash, quote_expired
from app.receipt import build_receipt, verify_receipt_hash


def _receipt(**overrides):
    kw = dict(job_id="job1", quote_id="q1", service="audit",
              payer="payer@example", agent="agent@example", currency="USD",
              amount_cents=1250, network="PayPal Sandbox",
              paypal_order_id="ORDER-0001", authorization_id="AUTH-0001",
              outcome="released", delivery_hash="ab" * 32)
    kw.update(overrides)
    return build_receipt(**kw)


def test_quote_hash_is_stable_and_verifies():
    q = new_quote(service="audit", price_cents=1250, pay_to="a@example")
    assert verify_quote_hash(q)
    # Same inputs (minus nonce/uuid) can't be re-created identically, but the
    # hash must verify against the quote's own contents.
    assert q["quote_hash"].startswith("0x")


def test_quote_tamper_detected():
    q = new_quote(service="audit", price_cents=1250, pay_to="a@example")
    q["price_cents"] = 999999
    assert not verify_quote_hash(q)


def test_quote_expiry():
    q = new_quote(service="audit", price_cents=1250, pay_to="a@example",
                  ttl_seconds=10)
    assert not quote_expired(q, now=q["issued_at"] + 5)
    assert quote_expired(q, now=q["issued_at"] + 11)


def test_receipt_hash_is_stable_and_verifies():
    r = _receipt()
    assert verify_receipt_hash(r)
    # Same inputs -> same hash (deterministic).
    assert _receipt()["receipt_hash"] == r["receipt_hash"]


def test_receipt_tamper_detected():
    r = _receipt()
    r["amount_cents"] = 1
    assert not verify_receipt_hash(r)


def test_receipt_voided_has_no_capture_id():
    r = _receipt(outcome="voided", capture_id="")
    assert r["capture_id"] == ""
    assert verify_receipt_hash(r)


def test_receipt_reconciled_capture_flagged():
    """A capture recorded by reconciliation (response lost, capture id
    unobserved) is flagged on the receipt instead of silently carrying
    an empty capture id on a released receipt."""
    r = _receipt(outcome="released", capture_id="", capture_reconciled=True)
    assert r["capture_reconciled"] is True
    assert verify_receipt_hash(r)
    assert _receipt()["capture_reconciled"] is False


def test_receipt_records_capture_amount():
    """A released receipt carries the amount actually captured, and the
    hash still verifies."""
    r = _receipt(outcome="released", capture_id="CAPTURE-0003",
                 capture_amount_cents=1250)
    assert r["capture_amount_cents"] == 1250
    assert verify_receipt_hash(r)
    assert _receipt()["capture_amount_cents"] is None


def test_receipt_reconciled_with_real_capture_id():
    """A reconciled receipt now carries the REAL capture id and amount
    from the authorization's linked captures — fully auditable — with
    the reconciled flag still set."""
    r = _receipt(outcome="released", capture_id="CAPTURE-0003",
                 capture_amount_cents=1250, capture_reconciled=True)
    assert r["capture_id"] == "CAPTURE-0003"
    assert r["capture_amount_cents"] == 1250
    assert r["capture_reconciled"] is True
    assert verify_receipt_hash(r)

def test_receipt_carries_last_paypal_response():
    """A settled receipt carries the last PayPal response, so it is
    auditable without re-querying PayPal — and the hash still
    verifies. Without one the field is None (receipts built before
    round 4 have no audit record)."""
    response = {"operation": "capture", "status": "COMPLETED",
                "id": "CAPTURE-0003",
                "body": {"id": "CAPTURE-0003", "status": "COMPLETED",
                         "amount": {"currency_code": "USD",
                                    "value": "12.50"}}}
    r = _receipt(outcome="released", capture_id="CAPTURE-0003",
                 capture_amount_cents=1250,
                 last_paypal_response=response)
    assert r["last_paypal_response"] == response
    assert verify_receipt_hash(r)
    assert _receipt()["last_paypal_response"] is None

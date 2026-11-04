"""Finding 1 (v8.2) — reviewer's exact test specs.

For _capture_partial_with_reconcile(), regression tests for:
(1) PARTIALLY_CAPTURED + UNREAD
(2) PARTIALLY_CAPTURED + ORDER_UNREAD
(3) CAPTURED + ORDER_UNREAD
(4) CREATED + UNREAD during pending-intent retry
(5) an idempotency key that was persisted but never accepted by PayPal
(6) an authorization with multiple captures where the order cannot be read

EACH test asserts BOTH the returned error AND the actual number of
PayPal POST attempts (the replay must not fire on incomplete evidence).
"""
import pytest

from app.escrow import EscrowError


class CountingStub:
    """Wraps _StubPayPal to count capture POST attempts."""
    def __init__(self, stub):
        self._stub = stub
        self.capture_posts = 0

    def __getattr__(self, name):
        return getattr(self._stub, name)

    def capture_authorization(self, *args, **kwargs):
        self.capture_posts += 1
        return self._stub.capture_authorization(*args, **kwargs)


def _setup():
    from test_escrow_paypal import _StubPayPal, _stub_authorized_job
    stub = _StubPayPal()
    svc, job = _stub_authorized_job(stub)
    svc.submit_delivery(job, agent="agent@example", delivery_text="r")
    # The capture POST fails WITHOUT applying (transport, not lost response).
    stub.capture_applies = False
    stub.capture_raises = False
    counting = CountingStub(stub)
    svc.paypal = counting
    return stub, counting, svc, job


def _pending_key(svc, job, amount_cents=500):
    key = f"lamplight-capture-partial-{amount_cents}-{job.job_id}-attempt-0"
    svc._set_pending_capture(job, amount_cents, key)
    return key


def test_1_partially_captured_unread():
    """(1) PARTIALLY_CAPTURED + UNREAD: no replay, wedge, intent kept."""
    stub, counting, svc, job = _setup()
    key = _pending_key(svc, job)
    stub.auths["AUTH-STUB"]["status"] = "PARTIALLY_CAPTURED"
    # No captures key → UNREAD (order read fails too).
    stub.auths["AUTH-STUB"].pop("captures", None)
    stub.fail_get_order = True

    with pytest.raises(EscrowError, match="operator review"):
        svc._capture_partial_with_reconcile(job, 500)
    assert counting.capture_posts == 1, "expected only the initial retry, not the Round-11 replay"
    assert job.needs_review is True
    assert job.pending_capture_request_id == key


def test_2_partially_captured_order_unread():
    """(2) PARTIALLY_CAPTURED + ORDER_UNREAD: no replay, wedge."""
    stub, counting, svc, job = _setup()
    key = _pending_key(svc, job)
    stub.auths["AUTH-STUB"]["status"] = "PARTIALLY_CAPTURED"
    # Auth lists a capture, order unread → ORDER_UNREAD.
    stub.auths["AUTH-STUB"]["captures"] = [{
        "id": "CAP-1", "status": "COMPLETED",
        "amount": {"currency_code": "USD", "value": "5.00"}}]
    stub.fail_get_order = True

    with pytest.raises(EscrowError, match="operator review"):
        svc._capture_partial_with_reconcile(job, 500)
    assert counting.capture_posts == 1, "expected only the initial retry, not the Round-11 replay"
    assert job.needs_review is True
    assert "order could not be read" in job.needs_review_reason


def test_3_captured_order_unread():
    """(3) CAPTURED + ORDER_UNREAD: wedge, do NOT book as reconciled."""
    stub, counting, svc, job = _setup()
    key = _pending_key(svc, job)
    stub.auths["AUTH-STUB"]["status"] = "CAPTURED"
    stub.auths["AUTH-STUB"]["captures"] = [{
        "id": "CAP-1", "status": "COMPLETED",
        "amount": {"currency_code": "USD", "value": "12.50"}}]
    stub.fail_get_order = True

    with pytest.raises(EscrowError, match="operator review"):
        svc._capture_partial_with_reconcile(job, 500)
    assert counting.capture_posts == 1  # initial retry only
    assert job.needs_review is True
    # Not booked as reconciled.
    assert not getattr(job, "capture_reconciled", False)


def test_4_created_unread_during_retry():
    """(4) CREATED + UNREAD during pending-intent retry: no replay, wedge."""
    stub, counting, svc, job = _setup()
    key = _pending_key(svc, job)
    stub.auths["AUTH-STUB"]["status"] = "CREATED"
    # Ensure no captures key → UNREAD (auth has no captures list at all).
    stub.auths["AUTH-STUB"].pop("captures", None)
    stub.fail_get_order = True

    with pytest.raises(EscrowError, match="operator review"):
        svc._capture_partial_with_reconcile(job, 500)
    assert counting.capture_posts == 1, "expected only the initial retry, not the Round-11 replay"
    assert job.needs_review is True
    assert job.pending_capture_request_id == key


def test_5_key_persisted_never_accepted():
    """(5) Idempotency key persisted but never accepted by PayPal:
    PARTIALLY_CAPTURED (other capture) + UNREAD → no replay."""
    stub, counting, svc, job = _setup()
    key = _pending_key(svc, job)
    # The key was NEVER sent — not in seen_keys.
    assert key not in stub.seen_keys
    # Some OTHER capture caused PARTIALLY_CAPTURED.
    stub.auths["AUTH-STUB"]["status"] = "PARTIALLY_CAPTURED"
    stub.auths["AUTH-STUB"]["captures"] = []
    stub.fail_get_order = True

    with pytest.raises(EscrowError, match="operator review"):
        svc._capture_partial_with_reconcile(job, 500)
    # The replay must NOT fire — it would EXECUTE a new capture.
    assert counting.capture_posts == 1  # initial retry only
    assert key not in stub.seen_keys
    assert job.needs_review is True


def test_6_multiple_captures_order_unread():
    """(6) Multiple captures on auth, order unread → wedge, no booking."""
    stub, counting, svc, job = _setup()
    key = _pending_key(svc, job)
    stub.auths["AUTH-STUB"]["status"] = "PARTIALLY_CAPTURED"
    stub.auths["AUTH-STUB"]["captures"] = [
        {"id": "CAP-1", "status": "COMPLETED",
         "amount": {"currency_code": "USD", "value": "3.00"}},
        {"id": "CAP-2", "status": "COMPLETED",
         "amount": {"currency_code": "USD", "value": "2.00"}},
    ]
    stub.fail_get_order = True

    with pytest.raises(EscrowError, match="operator review"):
        svc._capture_partial_with_reconcile(job, 500)
    assert counting.capture_posts == 1  # initial retry only
    assert job.needs_review is True

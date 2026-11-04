"""Reviewer finding (v8.2): Job.to_dict() exposes internal operational detail.

to_dict() carries idempotency keys (pending_capture_request_id,
pending_void_request_id), internal review flags (needs_review*), payment
attempt counters, and PayPal-side ids — and the unauthenticated endpoints
(POST /jobs, GET /jobs, GET /jobs/<id>) returned it verbatim. The fix is a
separate public representation: to_public_dict() holds only the minimal
safe subset, and the three open endpoints use it. Authenticated endpoints
keep to_dict().
"""
from test_escrow_paypal import _StubPayPal, _stub_authorized_job

PUBLIC_FIELDS = {
    "job_id", "status", "service", "price_cents", "currency",
    "created_at", "deadline", "settled_at",
}

# Fields that must NEVER appear on the public endpoints.
INTERNAL_FIELDS = {
    "payer", "agent", "order_id", "authorization_id", "capture_id",
    "capture_ids", "pending_capture_request_id", "pending_void_request_id",
    "pending_capture_amount_cents", "needs_review", "needs_review_reason",
    "needs_review_amount_cents", "void_attempts", "capture_attempt",
    "authorize_attempted", "webhook_denial", "last_paypal_response",
    "settlement_method", "settlement_note",
}


def _job_with_internals():
    stub = _StubPayPal()
    svc, job = _stub_authorized_job(stub)
    # Simulate in-flight intent + review state so the internals exist.
    job.pending_capture_request_id = "intent-key-123"
    job.pending_void_request_id = "void-key-456"
    job.pending_capture_amount_cents = 500
    svc._set_review_flag(job, "test wedge", 1250)
    job.void_attempts = 2
    job.capture_attempt = 1
    return job


def test_public_dict_has_only_safe_fields():
    job = _job_with_internals()
    pub = job.to_public_dict()
    assert set(pub.keys()) == PUBLIC_FIELDS


def test_public_dict_leaks_nothing_internal():
    job = _job_with_internals()
    pub = job.to_public_dict()
    for field in INTERNAL_FIELDS:
        assert field not in pub, f"internal field leaked: {field}"


def test_to_dict_still_carries_internals_for_authenticated_callers():
    # The split is additive: authenticated endpoints keep the full view.
    job = _job_with_internals()
    full = job.to_dict()
    assert full["pending_capture_request_id"] == "intent-key-123"
    assert full["needs_review"] is True

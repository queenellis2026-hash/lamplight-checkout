"""v8.5 — Claude review round: tests for the two demo-claim fixes.

1. final_capture=True is sent on partial captures (so real PayPal
   releases the residual instead of leaving it held).
2. The sweeper loop calls check_inspection_deadlines() (auto-capture
   was code-complete but never wired in).
"""
import threading
import time

import pytest
import requests

# Reuse the mock-server + client fixtures from the main escrow test module.
from tests.test_escrow_paypal import mock_server  # noqa: F401
from tests.test_escrow_paypal import BASE


@pytest.fixture()
def client(mock_server):
    from app.paypal_client import PayPalClient
    return PayPalClient(client_id="mock", client_secret="<redacted>",
                        base_url=mock_server)


def test_partial_capture_sends_final_capture():
    """The PayPal capture POST for a partial capture carries
    final_capture=true."""
    from app.paypal_client import PayPalClient

    sent = {}

    class CaptureSpy(PayPalClient):
        def __init__(self):
            # Skip real init; we only need _request.
            pass

        def _request(self, method, path, json=None, request_id=None):
            sent["json"] = json
            return {"id": "CAPTURE-1", "status": "COMPLETED"}

    c = CaptureSpy()
    c.capture_authorization("AUTH-1", amount_cents=500, request_id="k1",
                            final_capture=True)
    assert sent["json"]["final_capture"] is True
    assert sent["json"]["amount"] == {"currency_code": "USD", "value": "5.00"}


def test_full_capture_omits_final_capture():
    """A full capture does not send final_capture (it's the default)."""
    from app.paypal_client import PayPalClient

    sent = {}

    class CaptureSpy(PayPalClient):
        def __init__(self):
            pass

        def _request(self, method, path, json=None, request_id=None):
            sent["json"] = json
            return {"id": "CAPTURE-1", "status": "COMPLETED"}

    c = CaptureSpy()
    c.capture_authorization("AUTH-1", request_id="k1")
    assert "final_capture" not in sent["json"]


def test_sweeper_loop_calls_inspection_deadlines():
    """The background sweeper loop invokes check_inspection_deadlines()."""
    from app import sweeper

    calls = []

    class FakeService:
        def check_inspection_deadlines(self):
            calls.append(1)
            return []

    # sweep_stale_intents needs a real service; stub it.
    orig = sweeper.sweep_stale_intents
    sweeper.sweep_stale_intents = lambda svc, thresh: None
    try:
        t = sweeper.start_sweeper_thread(FakeService(), interval_seconds=1)
        time.sleep(2.5)
        t.stop_event.set()
        t.join(timeout=5)
    finally:
        sweeper.sweep_stale_intents = orig
    assert len(calls) >= 1, "sweeper loop never called check_inspection_deadlines"


def test_capture_partial_passes_final_capture_true(client):
    """capture_partial() sends final_capture=True on the PayPal POST."""
    from tests.test_escrow_paypal import _delivered_job_for_partial

    svc, job = _delivered_job_for_partial(client)
    seen = {}
    orig = svc.paypal.capture_authorization

    def spy(auth_id, amount_cents=None, currency="USD", request_id=None,
            final_capture=False):
        seen["final_capture"] = final_capture
        return orig(auth_id, amount_cents=amount_cents, currency=currency,
                    request_id=request_id, final_capture=final_capture)

    svc.paypal.capture_authorization = spy
    try:
        svc.capture_partial(job, payer="payer@example",
                            delivery_text="report v1", amount_cents=800)
    finally:
        svc.paypal.capture_authorization = orig
    assert seen.get("final_capture") is True


def test_out_of_band_capture_flags_for_review(client):
    """A stray capture alongside the recorded one goes through the real
    _finish_partial_capture flow: the job settles CAPTURED (money is
    accounted for) but needs_review is True. Claude v8.8: the old test
    called the helper directly and never met the flag-clearing code."""
    from tests.test_escrow_paypal import _delivered_job_for_partial

    svc, job = _delivered_job_for_partial(client)
    # Do the partial capture normally first (records the $8 capture).
    svc.capture_partial(job, payer="payer@example",
                        delivery_text="report v1", amount_cents=800)
    # Reset to DELIVERED so we can re-run _finish_partial_capture with
    # a doctored auth read.
    job.status = "DELIVERED"
    job.settled_at = None
    recorded_id = job.capture_id
    assert recorded_id.startswith("CAPTURE-")

    # Stub get_authorization to return CAPTURED with the recorded $8
    # plus a stray $4.50.
    def fake_get_auth(auth_id):
        return {
            "id": auth_id,
            "status": "CAPTURED",
            "captures": [
                {"id": recorded_id,
                 "status": "COMPLETED",
                 "amount": {"currency_code": "USD", "value": "8.00"}},
                {"id": "CAPTURE-STRAY",
                 "status": "COMPLETED",
                 "amount": {"currency_code": "USD", "value": "4.50"}},
            ],
        }

    orig = svc.paypal.get_authorization
    svc.paypal.get_authorization = fake_get_auth
    try:
        svc._finish_partial_capture(job)
    finally:
        svc.paypal.get_authorization = orig

    # Settles (money accounted for) but flagged for review.
    assert job.status == "CAPTURED"
    assert job.needs_review is True
    assert "out-of-band" in (job.needs_review_reason or "")

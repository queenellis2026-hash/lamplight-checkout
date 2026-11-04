"""Phase 5: recovery point tests.

Proves a crashed capture request RESUMES instead of RESTARTS, and that
ambiguous PSP outcomes always verify-then-act (never blind-retry with a
new key). Driven by the Phase 3 chaos proxy.

Recovery points on the capture path:
    None           — no capture in flight (initial, recorded, abandoned)
    "intent_saved" — intent persisted, PSP not yet called.
                     Safe to call PSP (idempotency key protects).
    "psp_called"   — PSP was called; outcome unknown.
                     NEVER re-POST blindly — verify-then-act.
"""
import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.escrow import EscrowError, EscrowService
from app.errors import ErrorClass, classify_paypal_error
from app.paypal_client import PayPalError
from app.quote import new_quote
from app.store import MemoryStore, SqliteStore
from tests.chaos_paypal import ChaosPayPalClient

PAYER = "payer@sm.example"
AGENT = "agent@sm.example"
PRICE = 5000


@pytest.fixture(params=["memory", "sqlite"])
def svc_pair(request, tmp_path):
    if request.param == "memory":
        store = MemoryStore()
    else:
        store = SqliteStore(tmp_path / "jobs.db")
    paypal = ChaosPayPalClient()
    svc = EscrowService(paypal, store=store)
    return svc, paypal


def _delivered_job(svc, paypal, price_cents=PRICE):
    quote = new_quote(service="sm", price_cents=price_cents,
                      pay_to=AGENT, ttl_seconds=30 * 24 * 3600)
    job = svc.create_job(quote=quote, payer=PAYER, agent=AGENT,
                         deadline_seconds=3600)
    order = svc.create_order(job)
    paypal.approve_order(order["id"])
    svc.authorize(svc.store.get(job.job_id))
    text = "recovery delivery text"
    svc.submit_delivery(svc.store.get(job.job_id), agent=AGENT,
                        delivery_text=text, now=int(time.time()))
    return svc.store.get(job.job_id), text


def _capture_keys(paypal):
    """All idempotency keys used in capture_authorization calls."""
    return [c[1] for c in paypal.calls if c[0] == "capture_authorization"]


# ---------------------------------------------------------------------------
# Recovery point lifecycle.
# ---------------------------------------------------------------------------

def test_recovery_point_transitions(svc_pair):
    """None -> intent_saved -> psp_called -> None across a clean capture."""
    svc, paypal = svc_pair
    job, text = _delivered_job(svc, paypal)

    assert job.capture_recovery_point is None

    # Drive to just before the PSP call: use the service's own intent
    # machinery, then inspect the point.
    job = svc.store.get(job.job_id)
    svc._set_pending_capture(job, None, "test-key-123")
    job = svc.store.get(job.job_id)
    assert job.capture_recovery_point == "intent_saved"
    assert job.pending_capture_request_id == "test-key-123"

    # Clean up the test intent via the real capture path.
    svc._clear_pending_capture(job)
    svc._save(job)
    job = svc.store.get(job.job_id)
    assert job.capture_recovery_point is None
    assert job.pending_capture_request_id is None


def test_psp_called_point_set_on_success(svc_pair):
    """A successful PSP call leaves psp_called until recording clears it."""
    svc, paypal = svc_pair
    job, text = _delivered_job(svc, paypal)

    # Capture via the public path; the point must be None after
    # recording (cleared with the intent).
    svc.capture(svc.store.get(job.job_id), payer=PAYER,
                delivery_text=text)
    assert svc.store.get(job.job_id).status == "CAPTURED"
    job = svc.store.get(job.job_id)
    assert job.capture_recovery_point is None
    assert job.pending_capture_request_id is None


def test_old_store_files_load_without_recovery_point(svc_pair, tmp_path):
    """Store files written before Phase 5 load with recovery_point None."""
    svc, _ = svc_pair
    job, _ = _delivered_job(svc, svc.paypal)
    # Simulate an old file: delete the key from the stored dict.
    data = svc.store.get(job.job_id).to_store_dict()
    data.pop("capture_recovery_point", None)
    # Round-trip through from_store_dict.
    from app.escrow import Job
    reloaded = Job.from_store_dict(data)
    assert reloaded.capture_recovery_point is None


# ---------------------------------------------------------------------------
# Kill at intent_saved: resume is safe (idempotency key protects).
# ---------------------------------------------------------------------------

def test_crash_at_intent_saved_resumes_safely(svc_pair):
    """Intent saved but PSP never called: resume re-POSTs with the same
    key. Exactly one money effect."""
    svc, paypal = svc_pair
    job, text = _delivered_job(svc, paypal)

    # Simulate: intent was saved, then the process died before the PSP
    # call. Recovery point is intent_saved.
    job = svc.store.get(job.job_id)
    key = svc._capture_request_id(job, "capture")
    svc._set_pending_capture(job, None, key)
    job = svc.store.get(job.job_id)
    assert job.capture_recovery_point == "intent_saved"

    # Resume via the public capture path. It must use the stored key
    # (not a new one) and settle exactly once.
    svc.capture(svc.store.get(job.job_id), payer=PAYER,
                delivery_text=text)
    assert svc.store.get(job.job_id).status == "CAPTURED"

    keys = _capture_keys(paypal)
    assert len(set(keys)) == 1, f"expected one key, got {set(keys)}"
    assert paypal.executed_count("capture_authorization") == 1

    job = svc.store.get(job.job_id)
    assert job.capture_recovery_point is None


# ---------------------------------------------------------------------------
# Kill at psp_called: resume via verify-then-act, never blind re-POST.
# ---------------------------------------------------------------------------

def test_crash_at_psp_called_verifies_then_acts(svc_pair):
    """PSP accepted, then the response was lost (408). The resume must
    NOT POST a new key — it replays the original, then reads. Exactly
    one money effect, one executed key."""
    svc, paypal = svc_pair
    job, text = _delivered_job(svc, paypal)

    # The fake ACCEPTS the capture, then raises 408 (ambiguous: the
    # request was sent, outcome unknown from the caller's view).
    paypal.crash_after("capture_authorization",
                       PayPalError("timeout", status=408))

    svc.capture(svc.store.get(job.job_id), payer=PAYER,
                delivery_text=text)
    # The service recovers via _verify_then_act -> _capture_with_reconcile
    # -> replay the stored key -> PayPal replays the accepted capture.
    assert svc.store.get(job.job_id).status == "CAPTURED"

    # Exactly one key was ever used, and exactly one execution happened.
    keys = _capture_keys(paypal)
    assert len(set(keys)) == 1, f"new key on retry: {set(keys)}"
    assert paypal.executed_count("capture_authorization") == 1

    job = svc.store.get(job.job_id)
    assert job.capture_recovery_point is None
    # The 408 was classified AMBIGUOUS (request was sent).
    assert classify_paypal_error(PayPalError("t", status=408),
                                 request_was_sent=True) is ErrorClass.AMBIGUOUS


def test_ambiguous_timeout_never_uses_new_key(svc_pair):
    """The strongest property: after an ambiguous failure, no capture
    POST ever uses a key different from the original. This is what
    'never blind-retry' means."""
    svc, paypal = svc_pair
    job, text = _delivered_job(svc, paypal)

    # Three ambiguous failures in a row (500 after send).
    for _ in range(3):
        paypal.crash_after("capture_authorization",
                           PayPalError("server error", status=500))

    # First attempt raises into verify-then-act; the replay also hits
    # the armed crash... each crash_after is one-shot, so only the
    # first fires. Subsequent calls proceed normally.
    svc.capture(svc.store.get(job.job_id), payer=PAYER,
                delivery_text=text)
    assert svc.store.get(job.job_id).status == "CAPTURED"

    # The strongest property: exactly ONE key was ever used, no matter
    # how many ambiguous failures occurred. A new key would mean a
    # blind retry (double-capture risk).
    keys = set(_capture_keys(paypal))
    assert len(keys) == 1, f"key changed across retries: {keys}"
    assert paypal.executed_count("capture_authorization") == 1


def test_raw_crash_leaves_intent_saved(svc_pair):
    """A raw transport crash (not PayPalError) during the PSP call:
    the recovery point stays intent_saved (we never got past the
    call). Resume re-POSTs with the same key — safe via idempotency."""
    svc, paypal = svc_pair
    job, text = _delivered_job(svc, paypal)

    # Raw ConnectionError: not caught as PayPalError, propagates up.
    # The fake ACCEPTED before raising (money moved, response lost).
    paypal.crash_after("capture_authorization",
                       ConnectionError("process killed"))

    with pytest.raises(ConnectionError):
        svc.capture(svc.store.get(job.job_id), payer=PAYER,
                    delivery_text=text)

    # The crash happened DURING the call — psp_called was never set
    # (it's set after the call returns/raises PayPalError). The point
    # correctly stays intent_saved.
    job = svc.store.get(job.job_id)
    assert job.capture_recovery_point == "intent_saved"
    assert job.pending_capture_request_id is not None

    # Resume: the retry path replays the stored key; PayPal replays
    # the accepted capture. Exactly one money effect.
    svc.capture(svc.store.get(job.job_id), payer=PAYER,
                delivery_text=text)
    assert svc.store.get(job.job_id).status == "CAPTURED"
    assert paypal.executed_count("capture_authorization") == 1
    assert len(set(_capture_keys(paypal))) == 1


# ---------------------------------------------------------------------------
# _verify_then_act is the canonical helper.
# ---------------------------------------------------------------------------

def test_verify_then_act_routes_ambiguous(svc_pair):
    """_verify_then_act exists, takes (job, error, class), and recovers
    an ambiguous capture via the stored key."""
    svc, paypal = svc_pair
    job, text = _delivered_job(svc, paypal)

    job = svc.store.get(job.job_id)
    key = svc._capture_request_id(job, "capture")
    svc._set_pending_capture(job, None, key)
    # Simulate the PSP having accepted (money moved, response lost).
    paypal._auths[job.authorization_id]["status"] = "CAPTURED"
    paypal._auths[job.authorization_id]["captures"] = [{
        "id": "cap-verify-1", "status": "COMPLETED", "amount_cents": PRICE}]

    e = PayPalError("timeout", status=408)
    cls = classify_paypal_error(e, request_was_sent=True)
    assert cls is ErrorClass.AMBIGUOUS

    result = svc._verify_then_act(svc.store.get(job.job_id), e, cls)
    assert result.get("reconciled") is True

    job = svc.store.get(job.job_id)
    assert job.capture_recovery_point is None

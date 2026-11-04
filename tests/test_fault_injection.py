"""Phase 3: fault injection / chaos tests.

Proves the system survives the exact kill-windows from the postmortems
(deep dive §6): crash between PSP call and DB write, slow-success
timeouts, duplicate/out-of-order/concurrent webhooks, expired
idempotency keys mid-retry, 429s, and the dispute-flag/deadline crash
window (scenario 8) plus the clear-then-capture kill-window regression.

Vehicle: tests/chaos_paypal.py (ChaosPayPalClient over the Phase 1
fake). Every scenario runs on BOTH stores (MemoryStore, SqliteStore).

Game-plan rule: if a scenario fails because the code is wrong, that is
the phase working as intended — fix the code, don't weaken the
scenario.
"""
import pathlib
import sys
import threading
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.escrow import EscrowError, EscrowService
from app.paypal_client import PayPalError
from app.quote import new_quote
from app.store import MemoryStore, SqliteStore
from tests.chaos_paypal import ChaosPayPalClient

PAYER = "payer@sm.example"
AGENT = "agent@sm.example"
PRICE = 5000


@pytest.fixture(params=["memory", "sqlite"])
def svc_pair(request, tmp_path):
    """(EscrowService, ChaosPayPalClient) on each supported store."""
    if request.param == "memory":
        store = MemoryStore()
    else:
        store = SqliteStore(tmp_path / "jobs.db")
    paypal = ChaosPayPalClient()
    svc = EscrowService(paypal, store=store)
    return svc, paypal


def _delivered_job(svc, paypal, price_cents=PRICE):
    """Drive a job create -> order -> approve -> authorize -> deliver."""
    quote = new_quote(service="sm", price_cents=price_cents,
                      pay_to=AGENT, ttl_seconds=30 * 24 * 3600)
    job = svc.create_job(quote=quote, payer=PAYER, agent=AGENT,
                         deadline_seconds=3600)
    order = svc.create_order(job)
    paypal.approve_order(order["id"])
    svc.authorize(svc.store.get(job.job_id))
    text = "chaos delivery text"
    svc.submit_delivery(svc.store.get(job.job_id), agent=AGENT,
                        delivery_text=text, now=int(time.time()))
    return svc.store.get(job.job_id), text


def _executed_captures(paypal):
    return [c for c in paypal.calls
            if c[0] == "capture_authorization" and not c[3]]


def _pause_job(svc, job):
    """File a payer payment pause; returns (job_id, deadline, sweep_now)."""
    now0 = int(time.time())
    svc.dispute_job(svc.store.get(job.job_id), payer=PAYER,
                    reason="chaos pause", now=now0)
    jid = job.job_id
    deadline = svc.store.get(jid).dispute_deadline
    assert deadline is not None
    return jid, deadline, deadline + 60


# ---------------------------------------------------------------------------
# 1. Crash between the PSP capture call and our DB write.
# ---------------------------------------------------------------------------

def test_crash_between_psp_capture_and_db_write(svc_pair):
    """PayPal accepted the capture; our process died before recording it.

    Retry must verify-then-act: _retry_pre_read reads the authorization
    FIRST, finds the capture, and reconciles. Exactly one capture may
    ever execute; no blind re-POST under a new key.
    """
    svc, paypal = svc_pair
    job, text = _delivered_job(svc, paypal)
    paypal.crash_after("capture_authorization")  # accept, then ConnectionError

    with pytest.raises(ConnectionError):
        svc.capture(svc.store.get(job.job_id), payer=PAYER,
                    delivery_text=text)

    mid = svc.store.get(job.job_id)
    assert mid.status == "DELIVERED"  # not recorded yet
    key = mid.pending_capture_request_id
    assert key, "the intent must be stored before the PSP call"
    assert paypal.executed_call_count("capture_authorization", key) == 1

    # Retry: the read comes first, the capture is found, job settles.
    result = svc.capture(svc.store.get(job.job_id), payer=PAYER,
                         delivery_text=text)
    final = svc.store.get(job.job_id)
    assert final.status == "CAPTURED"
    assert result.get("reconciled") or final.capture_amount_cents == PRICE
    # Exactly one execution, ever, under a single key.
    assert len(_executed_captures(paypal)) == 1
    assert paypal.executed_keys("capture_authorization") == {key}


# ---------------------------------------------------------------------------
# 2. Slow-success timeout: the client gives up, the PSP succeeded.
# ---------------------------------------------------------------------------

def test_slow_success_timeout_no_blind_retry(svc_pair):
    """A 408 after PayPal accepted must not become a second capture.

    _capture_with_reconcile replays the STORED key (PayPal replays the
    original). The forbidden outcome is a capture POST under a NEW key.
    """
    svc, paypal = svc_pair
    job, text = _delivered_job(svc, paypal)
    paypal.crash_after(
        "capture_authorization",
        PayPalError("timeout after request sent", status=408))

    result = svc.capture(svc.store.get(job.job_id), payer=PAYER,
                         delivery_text=text)

    final = svc.store.get(job.job_id)
    assert final.status == "CAPTURED"
    executed = _executed_captures(paypal)
    assert len(executed) == 1, "the 408 recovery must replay, not re-execute"
    assert paypal.replayed_count("capture_authorization") >= 1


def test_slow_psp_still_settles_exactly_once(svc_pair):
    """A slow-but-successful PSP call settles once, no wedge, no dup."""
    svc, paypal = svc_pair
    job, text = _delivered_job(svc, paypal)
    paypal.slow_next("capture_authorization", 1.5)

    result = svc.capture(svc.store.get(job.job_id), payer=PAYER,
                         delivery_text=text)

    final = svc.store.get(job.job_id)
    assert final.status == "CAPTURED"
    assert len(_executed_captures(paypal)) == 1


# ---------------------------------------------------------------------------
# 3-5. Webhook delivery chaos.
# ---------------------------------------------------------------------------

def _auth_created_event(event_id, auth_id):
    return {"id": event_id, "event_type": "PAYMENT.AUTHORIZATION.CREATED",
            "resource": {"id": auth_id, "status": "CREATED"}}


def _capture_completed_event(event_id, auth_id, cap_id="CAP-EXT-1"):
    return {"id": event_id, "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {
                "id": cap_id, "status": "COMPLETED",
                "supplementary_data": {
                    "related_ids": {"authorization_id": auth_id}}}}


def test_duplicate_webhook_is_noop(svc_pair):
    svc, paypal = svc_pair
    job, _ = _delivered_job(svc, paypal)
    event = _auth_created_event("WH-DUP-1", job.authorization_id)

    r1 = svc.handle_webhook_event(event)
    before = svc.store.get(job.job_id).status
    r2 = svc.handle_webhook_event(dict(event))

    assert r1["action"] == "confirmed"
    assert r2["action"] == "duplicate"
    assert svc.store.get(job.job_id).status == before


def test_out_of_order_webhooks_settle_correctly(svc_pair):
    """CAPTURE.COMPLETED arrives before AUTHORIZATION.CREATED.

    The money moved out-of-band; the capture event must reconcile it
    (fresh read, not the payload), and the late auth event must not
    corrupt the settled job.
    """
    svc, paypal = svc_pair
    job, _ = _delivered_job(svc, paypal)
    auth_id = job.authorization_id
    # Out-of-band: the capture happened outside our flow.
    paypal.capture_authorization(auth_id)

    r_cap = svc.handle_webhook_event(
        _capture_completed_event("WH-OOO-1", auth_id))
    assert r_cap["action"] == "reconciled", r_cap
    mid = svc.store.get(job.job_id)
    assert mid.status == "CAPTURED"
    assert mid.capture_amount_cents == PRICE

    # Late authorization event: job already settled -> ignored, not corrupt.
    r_auth = svc.handle_webhook_event(
        _auth_created_event("WH-OOO-2", auth_id))
    assert r_auth["action"] == "ignored"
    final = svc.store.get(job.job_id)
    assert final.status == "CAPTURED"
    assert final.capture_amount_cents == PRICE


def test_concurrent_webhook_single_claim(svc_pair):
    """Two threads deliver the same event: exactly one wins the claim."""
    svc, paypal = svc_pair
    job, _ = _delivered_job(svc, paypal)
    event = _auth_created_event("WH-CONC-1", job.authorization_id)

    results = []
    def deliver():
        results.append(svc.handle_webhook_event(dict(event)))

    t1 = threading.Thread(target=deliver)
    t2 = threading.Thread(target=deliver)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    actions = sorted(r["action"] for r in results)
    assert actions.count("duplicate") == 1, actions
    assert "confirmed" in actions, actions
    # Single money effect: the job is untouched by the replay.
    assert svc.store.get(job.job_id).status == "DELIVERED"


# ---------------------------------------------------------------------------
# 6. Idempotency key expiry mid-retry.
# ---------------------------------------------------------------------------

def test_idempotency_key_expiry_mid_retry(svc_pair):
    """PayPal forgets the key but keeps the money state.

    A retry that blindly re-POSTs would double-execute. The service
    must read-first (_retry_pre_read): the read finds the capture and
    reconciles, so the forgotten key is never re-POSTed.
    """
    svc, paypal = svc_pair
    job, text = _delivered_job(svc, paypal)
    paypal.crash_after("capture_authorization")  # accept, then crash

    with pytest.raises(ConnectionError):
        svc.capture(svc.store.get(job.job_id), payer=PAYER,
                    delivery_text=text)

    paypal.forget_idempotency_keys()  # 24h expiry, mid-retry

    result = svc.capture(svc.store.get(job.job_id), payer=PAYER,
                         delivery_text=text)
    final = svc.store.get(job.job_id)
    assert final.status == "CAPTURED"
    executed = _executed_captures(paypal)
    assert len(executed) == 1, (
        "key expiry must not cause a second execution — the retry "
        "should have reconciled from the read")


# ---------------------------------------------------------------------------
# 7. 429 during capture, then success.
# ---------------------------------------------------------------------------

def test_rate_limit_then_success_single_effect(svc_pair):
    svc, paypal = svc_pair
    job, text = _delivered_job(svc, paypal)
    auth_id = job.authorization_id
    paypal.fail_next("capture_authorization",
                     PayPalError("rate limited", status=429))

    result = svc.capture(svc.store.get(job.job_id), payer=PAYER,
                         delivery_text=text)

    final = svc.store.get(job.job_id)
    assert final.status == "CAPTURED"
    # Money truth: PayPal recorded exactly one capture. (The 429
    # attempt reached PayPal but was not processed, so the replay
    # with the same key executed — once.)
    assert len(paypal._auths[auth_id]["captures"]) == 1
    # No blind retry: every capture POST used a single idempotency key.
    assert len(paypal.executed_keys("capture_authorization")) == 1
    assert final.capture_amount_cents == PRICE


# ---------------------------------------------------------------------------
# 8. Crash between dispute flag-set and deadline write (the v8.12 wedge).
# ---------------------------------------------------------------------------

def test_sweep_captures_null_deadline_pause(svc_pair):
    """A pause with needs_review set but no deadline must CAPTURE on
    sweep — never skip-forever (the v8.12 wedge)."""
    svc, paypal = svc_pair
    job, _ = _delivered_job(svc, paypal)
    now0 = int(time.time())
    svc.dispute_job(svc.store.get(job.job_id), payer=PAYER,
                    reason="chaos pause", now=now0)
    # Simulate the crash between flag-set and deadline-write (or the
    # v8.12 writer that set the flag and forgot the timestamp).
    j = svc.store.get(job.job_id)
    j.dispute_deadline = None
    svc.store.save(j)
    mid = svc.store.get(job.job_id)
    assert mid.needs_review is True and mid.dispute_deadline is None

    captured = svc.check_inspection_deadlines(now=now0 + 10)

    assert job.job_id in captured
    final = svc.store.get(job.job_id)
    assert final.status == "CAPTURED"
    assert final.needs_review is False


# ---------------------------------------------------------------------------
# Clear-then-capture kill-window regression (Grok's v8.13 finding).
# ---------------------------------------------------------------------------

def test_sweep_keeps_flag_set_until_paypal_accepts(svc_pair):
    """In the kill window — after the sweep's capture decision, before
    the PayPal call — needs_review must still be True, and a
    concurrent pause must 409 without moving the deadline.

    v8.14: the flag clears only in the same write as the terminal
    CAPTURED status. The old code cleared it first, reopening the
    ladder for a second pause in the gap.
    """
    svc, paypal = svc_pair
    job, _ = _delivered_job(svc, paypal)
    jid, deadline, sweep_now = _pause_job(svc, job)

    calls = {"n": 0}
    orig_capture = paypal.capture_authorization

    def spy(authorization_id, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # --- kill window: decision made, PayPal not yet called ---
            mid = svc.store.get(jid)
            assert mid.needs_review is True, (
                "flag must stay set until PayPal accepts")
            with pytest.raises(EscrowError):
                svc.dispute_job(svc.store.get(jid), payer=PAYER,
                                reason="second pause", now=sweep_now)
            assert svc.store.get(jid).dispute_deadline == deadline, (
                "the 409'd pause must not move the deadline")
        return orig_capture(authorization_id, **kwargs)

    paypal.capture_authorization = spy
    try:
        captured = svc.check_inspection_deadlines(now=sweep_now)
    finally:
        paypal.capture_authorization = orig_capture

    assert jid in captured
    final = svc.store.get(jid)
    assert final.status == "CAPTURED"
    assert final.needs_review is False
    assert len(_executed_captures(paypal)) == 1


def test_force_settle_keeps_pause_on_capture_error(svc_pair):
    """Grok review (Nov 2026): _force_settle_active_pause cleared the
    pause flag and the dispute timestamps BEFORE PayPal accepted —
    the same clear-then-capture pattern fixed in the sweep path
    (v8.14). _set_pending_capture saves inside _capture_core, so a
    failed capture left the pause gone while money was still in
    flight: no flag, no deadline, but a live capture intent.

    Fixed: force_settle now calls _capture_core with
    _sweep_dispute=True — the flag stays set until PayPal accepts
    and clears only in the same write as the terminal CAPTURED
    status. On failure the pause stands: flag set, deadline
    unmoved, and the payer (or the sweep at the deadline) retries.
    """
    svc, paypal = svc_pair
    job, _ = _delivered_job(svc, paypal)
    jid, deadline, sweep_now = _pause_job(svc, job)

    # Force-settle with PayPal exploding mid-capture (accepted, then
    # crash — the worst case: money may have moved).
    paypal.crash_after("capture_authorization")
    with pytest.raises(ConnectionError):
        svc.force_settle(svc.store.get(jid), payer=PAYER)

    mid = svc.store.get(jid)
    assert mid.status == "DELIVERED"
    assert mid.needs_review is True, (
        "pause flag must survive a failed force-settle")
    assert mid.dispute_deadline == deadline, (
        "deadline must not move on failure")
    assert mid.dispute_opened_at is not None, (
        "pause timestamps must survive a failed force-settle")

    # Retry captures exactly once; the flag clears with the terminal
    # status in the same write.
    result = svc.force_settle(svc.store.get(jid), payer=PAYER)
    final = svc.store.get(jid)
    assert final.status == "CAPTURED"
    assert final.needs_review is False
    assert final.capture_amount_cents == PRICE
    assert result["to_status"] == "CAPTURED"
    assert len(_executed_captures(paypal)) == 1

    # The sweep at the deadline is the other retry path — it must see
    # a settled job, not a second capture.
    captured = svc.check_inspection_deadlines(now=sweep_now)
    assert jid not in captured
    assert len(_executed_captures(paypal)) == 1


def test_sweep_retries_after_capture_error(svc_pair):
    """A capture error during the sweep leaves the flag set and the
    deadline in the past; the next sweep captures exactly once."""
    svc, paypal = svc_pair
    job, _ = _delivered_job(svc, paypal)
    jid, deadline, sweep_now = _pause_job(svc, job)

    # Sweep 1: PayPal explodes mid-capture (accepted, then crash).
    paypal.crash_after("capture_authorization")
    captured1 = svc.check_inspection_deadlines(now=sweep_now)

    mid = svc.store.get(jid)
    assert jid not in captured1
    assert mid.status == "DELIVERED"
    assert mid.needs_review is True, "flag stays set after capture error"
    assert mid.dispute_deadline is not None
    assert mid.dispute_deadline <= sweep_now, "deadline stays in the past"

    # Sweep 2: clean run captures once.
    captured2 = svc.check_inspection_deadlines(now=sweep_now + 1)
    final = svc.store.get(jid)
    assert jid in captured2
    assert final.status == "CAPTURED"
    assert final.needs_review is False
    assert len(_executed_captures(paypal)) == 1

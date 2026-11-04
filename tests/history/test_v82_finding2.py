"""Finding 2 (v8.2): terminal jobs stuck in operator review.

A CAPTURED/VOIDED job carrying needs_review has no recovery route
(_force_settle_legacy requires AUTHORIZED/DELIVERED). The new
clear_terminal_review_flag() lets the payer clear it after re-reading
PayPal — without re-settling money.
"""
import pytest

from app.escrow import EscrowService, EscrowError


def _settled_job_with_flag(status="CAPTURED"):
    from test_escrow_paypal import _StubPayPal, _stub_authorized_job
    stub = _StubPayPal()
    svc, job = _stub_authorized_job(stub)
    svc.submit_delivery(job, agent="agent@example", delivery_text="r")
    # Settle the job, then wedge it.
    job.status = status
    job.settled_at = 1234567890
    if status == "CAPTURED":
        job.capture_id = "CAP-1"
        job.capture_amount_cents = 1250
    svc._set_review_flag(job, "test wedge on terminal job", 1250)
    svc._save(job)
    # PayPal agrees with the terminal state.
    stub.auths["AUTH-STUB"]["status"] = (
        "CAPTURED" if status == "CAPTURED" else "VOIDED")
    return stub, svc, job


def test_clear_terminal_review_flag_captured():
    stub, svc, job = _settled_job_with_flag("CAPTURED")
    money_before = (job.status, job.capture_id, job.capture_amount_cents,
                    job.voided_amount_cents)
    result = svc.clear_terminal_review_flag(job, payer="payer@example")
    assert result["cleared"] is True
    assert result["status"] == "CAPTURED"
    got = svc.store.get(job.job_id)
    assert got.needs_review is False
    # Money state unchanged.
    money_after = (got.status, got.capture_id, got.capture_amount_cents,
                   got.voided_amount_cents)
    assert money_after == money_before


def test_clear_terminal_review_flag_voided():
    stub, svc, job = _settled_job_with_flag("VOIDED")
    result = svc.clear_terminal_review_flag(job, payer="payer@example")
    assert result["cleared"] is True
    got = svc.store.get(job.job_id)
    assert got.needs_review is False
    assert got.status == "VOIDED"


def test_clear_terminal_review_flag_rejects_non_payer():
    stub, svc, job = _settled_job_with_flag("CAPTURED")
    with pytest.raises(EscrowError, match="only the payer"):
        svc.clear_terminal_review_flag(job, payer="attacker@example")


def test_clear_terminal_review_flag_rejects_open_job():
    from test_escrow_paypal import _StubPayPal, _stub_authorized_job
    stub = _StubPayPal()
    svc, job = _stub_authorized_job(stub)
    svc.submit_delivery(job, agent="agent@example", delivery_text="r")
    svc._set_review_flag(job, "wedge on open job", 500)
    svc._save(job)
    with pytest.raises(EscrowError, match="for terminal jobs"):
        svc.clear_terminal_review_flag(job, payer="payer@example")


def test_force_settle_still_rejects_terminal():
    """_force_settle_legacy still refuses terminal jobs — the new path
    is the only exit."""
    stub, svc, job = _settled_job_with_flag("CAPTURED")
    with pytest.raises(EscrowError, match="cannot force-settle"):
        svc.force_settle(job, payer="payer@example")

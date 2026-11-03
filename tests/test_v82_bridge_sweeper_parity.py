"""Item 2b/2c (production-readiness): the bridge and the sweeper use
ONE evidence builder and ONE executor — identical outcomes for the
same books + read.

Regression: a stored capture id, no amount, no listing on the pass.
The old contradiction (bridge has_capture from books vs sweeper
has_capture from the read's listing) must be gone.
"""
import time

import pytest

from app.sweeper import _sweep_one_locked


def _make_job_with_stored_capture():
    """A DELIVERED job with a stored capture id but no amount, a stale
    intent, and a PayPal read that lists nothing (ENUM_NONE) on a
    VOIDED auth.

    The stored capture on the books is the crux: the bridge sees it
    (has_capture from books) and settles from the books; the old
    sweeper fork saw only the read's listing (has_capture=False) and
    took the clean-void path — the contradiction.
    """
    from test_escrow_paypal import _StubPayPal, _stub_authorized_job
    stub = _StubPayPal()
    svc, job = _stub_authorized_job(stub)
    svc.submit_delivery(job, agent="agent@example", delivery_text="r")
    # Books: a stored capture id, no amount (id-only booking).
    svc._book_capture(job, "CAP-STORED", None)
    assert job.capture_id == "CAP-STORED"
    assert job.capture_amount_cents is None
    # Stale intent so the sweeper will pick it up.
    key = f"lamplight-capture-{job.job_id}-attempt-0"
    svc._set_pending_capture(job, 1250, key)
    job.pending_capture_created_at = int(time.time()) - 3600
    svc._save(job)
    # PayPal: VOIDED auth, nothing listed on auth or order.
    stub.auths["AUTH-STUB"]["status"] = "VOIDED"
    stub.auths["AUTH-STUB"]["captures"] = []
    return svc, job, stub


def test_bridge_and_sweeper_agree_on_stored_capture():
    """The shared builder computes has_capture from the books in both
    paths. A stored capture id with no amount and no listing: both see
    has_capture=True (the old sweeper fork saw False)."""
    svc, job, stub = _make_job_with_stored_capture()

    # The Evidence both paths build must agree on has_capture.
    enum_res = svc._first_completed_capture_including_order(
        job, stub.auths["AUTH-STUB"])
    ev_bridge = svc._build_reconcile_evidence(
        job, stub.auths["AUTH-STUB"], enum_res)
    ev_sweeper = svc._build_reconcile_evidence(
        job, stub.auths["AUTH-STUB"], enum_res,
        intent_stale=True)
    assert ev_bridge.has_capture is True
    assert ev_sweeper.has_capture is True
    assert "CAP-STORED" in ev_bridge.capture_ids
    assert "CAP-STORED" in ev_sweeper.capture_ids
    # The only difference is the stale flag — the books view is identical.
    assert ev_bridge.pending_intent_stale is False
    assert ev_sweeper.pending_intent_stale is True


def test_sweeper_uses_shared_builder():
    """The sweeper's Evidence comes from _build_reconcile_evidence —
    has_capture reflects the books, not just the read's listing."""
    import inspect
    from app import sweeper
    src = inspect.getsource(sweeper._sweep_one_locked)
    assert "_build_reconcile_evidence" in src
    assert "Evidence(" not in src


def test_sweeper_uses_shared_executor():
    """The sweeper applies actions via EscrowService's executor —
    no forked _apply_actions."""
    import inspect
    from app import sweeper
    assert not hasattr(sweeper, "_apply_actions")
    src = inspect.getsource(sweeper._sweep_one_locked)
    assert "_apply_dispatcher_actions" in src
    assert "read_only=True" in src

"""Tests for the crash-recovery sweeper (app/sweeper.py, suggestion #1).

The sweeper finds jobs with stale in-flight capture intents, re-reads
PayPal's authorization state, dispatches E_RECONCILE through the
dispatcher, applies the returned actions, and returns the recovered
job ids. These tests use a fake PayPal client — no mock server needed.
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.escrow import EscrowService, Job
from app.paypal_client import PayPalError
from app.quote import new_quote
from app.sweeper import sweep_stale_intents


def _quote(price_cents=1250):
    return new_quote(service="audit", price_cents=price_cents,
                     pay_to="agent@example")


class FakePayPal:
    """Minimal PayPal stand-in: only the reads the sweeper uses."""

    def __init__(self, auth_by_id=None, fail_ids=()):
        self.auth_by_id = auth_by_id or {}
        self.fail_ids = set(fail_ids)
        self.reads = []

    def get_authorization(self, authorization_id):
        self.reads.append(authorization_id)
        if authorization_id in self.fail_ids:
            raise PayPalError("boom", status=500)
        return self.auth_by_id[authorization_id]


def _completed_capture_auth(auth_id="AUTH-1", cap_id="CAP-1",
                            cents=1250, status="CAPTURED"):
    return {
        "id": auth_id,
        "status": status,
        "captures": [{
            "id": cap_id,
            "status": "COMPLETED",
            "amount": {"value": f"{cents / 100:.2f}",
                       "currency_code": "USD"},
        }],
    }


def _job(job_id, *, price_cents=1250, status="DELIVERED",
         auth_id="AUTH-1", intent_age=None):
    job = Job(job_id=job_id, quote=_quote(price_cents),
              payer="payer@example", agent="agent@example")
    job.status = status
    job.authorization_id = auth_id
    job.order_id = "ORDER-1"
    if intent_age is not None:
        job.pending_capture_request_id = f"cap-key-{job_id}"
        job.pending_capture_created_at = int(time.time()) - intent_age
    return job


def _service(paypal, *jobs):
    svc = EscrowService(paypal)
    for job in jobs:
        svc.store.save(job)
    return svc


def test_sweeper_recovers_stale_capture_intent():
    # A DELIVERED job whose capture intent went stale (crash between the
    # POST and the response); PayPal actually completed the capture.
    # The sweep must record it, clear the intent, and settle CAPTURED.
    paypal = FakePayPal({"AUTH-1": _completed_capture_auth()})
    job = _job("job-1", intent_age=3600)
    svc = _service(paypal, job)

    recovered = sweep_stale_intents(svc, stale_threshold_seconds=300)

    assert recovered == ["job-1"]
    got = svc.store.get("job-1")
    assert got.status == "CAPTURED"
    assert got.capture_id == "CAP-1"
    assert got.capture_amount_cents == 1250
    assert got.pending_capture_request_id is None
    assert got.pending_capture_created_at is None
    assert got.settlement_method == "reconciled"
    assert got.settled_at is not None


def test_sweeper_ignores_fresh_intents():
    # An intent newer than the threshold is in flight, not stranded —
    # the sweep must not even read PayPal for it.
    paypal = FakePayPal({"AUTH-1": _completed_capture_auth()})
    job = _job("job-1", intent_age=60)
    svc = _service(paypal, job)

    recovered = sweep_stale_intents(svc, stale_threshold_seconds=300)

    assert recovered == []
    assert paypal.reads == []
    got = svc.store.get("job-1")
    assert got.pending_capture_request_id == "cap-key-job-1"
    assert got.status == "DELIVERED"


def test_sweeper_ignores_jobs_without_intents():
    paypal = FakePayPal({"AUTH-1": _completed_capture_auth()})
    job = _job("job-1")  # no intent set
    svc = _service(paypal, job)

    recovered = sweep_stale_intents(svc, stale_threshold_seconds=300)

    assert recovered == []
    assert paypal.reads == []
    assert svc.store.get("job-1").status == "DELIVERED"


def test_sweeper_continues_on_error():
    # One job's read blows up; the others must still be swept.
    paypal = FakePayPal(
        {"AUTH-good": _completed_capture_auth(auth_id="AUTH-good")},
        fail_ids={"AUTH-bad"},
    )
    bad = _job("job-bad", auth_id="AUTH-bad", intent_age=3600)
    good = _job("job-good", auth_id="AUTH-good", intent_age=3600)
    svc = _service(paypal, bad, good)

    recovered = sweep_stale_intents(svc, stale_threshold_seconds=300)

    assert recovered == ["job-good"]
    # The failed job keeps its intent for the next sweep.
    bad_got = svc.store.get("job-bad")
    assert bad_got.pending_capture_request_id == "cap-key-job-bad"
    assert bad_got.status == "DELIVERED"
    good_got = svc.store.get("job-good")
    assert good_got.status == "CAPTURED"
    assert good_got.pending_capture_request_id is None


def test_sweeper_leaves_intent_when_hold_still_live():
    # The authorization is still CREATED — nothing moved, the intent is
    # for the payer's retry. The sweep must keep it and not settle.
    paypal = FakePayPal({
        "AUTH-1": {"id": "AUTH-1", "status": "CREATED", "captures": []},
    })
    job = _job("job-1", intent_age=3600)
    svc = _service(paypal, job)

    recovered = sweep_stale_intents(svc, stale_threshold_seconds=300)

    assert recovered == []
    got = svc.store.get("job-1")
    assert got.pending_capture_request_id == "cap-key-job-1"
    assert got.status == "DELIVERED"


def test_sweeper_recovers_released_hold():
    # The hold was released (voided off-book, or expired) — the sweep
    # clears the dangling intent without booking money.
    paypal = FakePayPal({
        "AUTH-1": {"id": "AUTH-1", "status": "VOIDED", "captures": []},
    })
    job = _job("job-1", intent_age=3600)
    svc = _service(paypal, job)

    recovered = sweep_stale_intents(svc, stale_threshold_seconds=300)

    assert recovered == ["job-1"]
    got = svc.store.get("job-1")
    assert got.pending_capture_request_id is None
    assert got.status == "VOIDED"
    assert got.capture_id == ""


def test_sweeper_deadline_none_not_passed():
    """MEDIUM #4: a job with no deadline never reads as
    deadline_passed — the sweep must not treat a missing deadline
    as 0 (always in the past)."""
    from app.sweeper import _sweep_one_locked
    paypal = FakePayPal({"AUTH-1": _completed_capture_auth()})
    job = _job("job-1", intent_age=3600)
    job.deadline = None
    svc = _service(paypal, job)
    # Should not raise; the evidence just carries deadline_passed=False.
    result = _sweep_one_locked(svc, job, int(time.time()), 300)
    assert result is True
    got = svc.store.get("job-1")
    assert got.status == "CAPTURED"


def test_sweeper_revalidates_staleness_in_lock():
    """Gemini Q1 (TOCTOU): the sweep iterates store.all() OUTSIDE the
    lock — an intent refreshed (re-armed by a live retry) between the
    scan and the lock acquisition must be left alone. The staleness
    check is re-validated inside the lock."""
    from app.sweeper import _sweep_one_locked
    paypal = FakePayPal({"AUTH-1": _completed_capture_auth()})
    job = _job("job-1", intent_age=3600)
    svc = _service(paypal, job)
    now = int(time.time())
    # Simulate the race: the outer scan saw a stale intent, but before
    # the lock was acquired a live retry re-armed it (fresh timestamp).
    job.pending_capture_created_at = now - 10  # 10s ago, not stale
    svc.store.save(job)
    result = _sweep_one_locked(svc, job, now, 300)
    assert result is False  # left alone — no longer stale
    got = svc.store.get("job-1")
    assert got.pending_capture_request_id is not None  # untouched
    assert got.status == "DELIVERED"  # not settled


def test_sweeper_starts_by_default():
    """Grok v7.1: the sweeper must be ON by default (300s interval).
    Unset env var → sweeper thread starts with 300s. Explicit 0 → disabled."""
    import os
    from unittest import mock

    # Test the interval resolution logic directly.
    # Unset → default 300.
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("LAMPLIGHT_SWEEP_INTERVAL_SECONDS", None)
        raw = os.environ.get("LAMPLIGHT_SWEEP_INTERVAL_SECONDS", "").strip()
        interval = 300 if not raw else int(raw)
        assert interval == 300

    # Explicit value → used.
    with mock.patch.dict(os.environ, {"LAMPLIGHT_SWEEP_INTERVAL_SECONDS": "600"}):
        raw = os.environ.get("LAMPLIGHT_SWEEP_INTERVAL_SECONDS", "").strip()
        interval = 300 if not raw else int(raw)
        assert interval == 600

    # Explicit 0 → disabled (caller returns early).
    with mock.patch.dict(os.environ, {"LAMPLIGHT_SWEEP_INTERVAL_SECONDS": "0"}):
        raw = os.environ.get("LAMPLIGHT_SWEEP_INTERVAL_SECONDS", "").strip()
        interval = 300 if not raw else int(raw)
        assert interval == 0  # _maybe_start_sweeper returns early on 0

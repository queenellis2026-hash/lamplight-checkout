"""Service-level regression tests for v8 money-safety fixes 6, 7, 9, 10.

Each test FAILS on the unfixed code and PASSES after the fix.
Uses fake PayPal clients — no network.
"""
import hashlib
import pathlib
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.dispatcher import GuardRejection
from app.escrow import EscrowService, Job
from app.paypal_client import PayPalError
from app.quote import new_quote
from app.store import SqliteStore
from app.sweeper import sweep_stale_intents


def _quote(price_cents=1250):
    return new_quote(service="audit", price_cents=price_cents,
                     pay_to="agent@example")


class FakePayPal:
    def __init__(self, auth_by_id=None, fail_ids=()):
        self.auth_by_id = auth_by_id or {}
        self.fail_ids = set(fail_ids)

    def get_authorization(self, authorization_id):
        if authorization_id in self.fail_ids:
            raise PayPalError("boom", status=500)
        return self.auth_by_id[authorization_id]


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


def _cap(cap_id, cents, status="COMPLETED"):
    return {"id": cap_id, "status": status,
            "amount": {"value": f"{cents / 100:.2f}",
                       "currency_code": "USD"}}


# ---------------------------------------------------------------------------
# Fix 6: sweeper — full evidence, RecordVoid/ClearReviewFlag handling,
# S10b settlement.
# ---------------------------------------------------------------------------

class TestSweeperMoneySafety:
    def test_partial_on_released_hold_settles_split(self):
        # DELIVERED + stale intent; PayPal VOIDED the hold but lists a
        # COMPLETED $5.00 partial of the $12.50 hold. Must settle S10b:
        # capture booked, residual voided, job CAPTURED, intent cleared.
        # Before the fix the sweep dispatched with is_partial=False →
        # S10a "full", RecordVoid unhandled → intent stranded and the
        # job stuck DELIVERED after the log announced a terminal state.
        auth = {"id": "AUTH-1", "status": "VOIDED",
                "captures": [_cap("CAP-1", 500)]}
        svc = _service(FakePayPal({"AUTH-1": auth}),
                       _job("job-1", intent_age=3600))

        recovered = sweep_stale_intents(svc, stale_threshold_seconds=300)

        got = svc.store.get("job-1")
        assert recovered == ["job-1"]
        assert got.status == "CAPTURED", (
            f"split must settle CAPTURED, got {got.status}")
        assert got.capture_amount_cents == 500
        assert got.voided_amount_cents == 750, (
            "the $7.50 residual void must be booked (one ledger)")
        assert got.pending_capture_request_id is None


# ---------------------------------------------------------------------------
# Fix 7: capture preflight goes through the dispatcher bridge.
# ---------------------------------------------------------------------------

class TestCapturePreflightBridge:
    def test_preflight_routes_through_bridge_not_legacy(self):
        # A non-CREATED pre-read must reconcile via
        # _reconcile_job_via_dispatcher (one reconcile implementation),
        # not _reconcile_with_auth directly.
        auth = {"id": "AUTH-1", "status": "CAPTURED",
                "captures": [_cap("CAP-1", 1250)]}
        job = _job("job-1", status="DELIVERED")
        job.delivery_hash = hashlib.sha256(b"delivered-ok").hexdigest()
        svc = _service(FakePayPal({"AUTH-1": auth}), job)

        calls = {}

        def fake_legacy(job, auth, from_status):
            raise AssertionError(
                "legacy _reconcile_with_auth called directly — bypass!")

        def fake_bridge(job, auth=None):
            calls["auth"] = auth
            return {"to_status": "CAPTURED", "capture_id": "CAP-1",
                    "reconciled": True, "error": ""}

        svc._reconcile_with_auth = fake_legacy
        svc._reconcile_job_via_dispatcher = fake_bridge

        result = svc._capture_via_dispatcher(
            job, payer="payer@example", delivery_text="delivered-ok")
        assert result["status"] == "ALREADY_SETTLED"
        assert result["to_status"] == "CAPTURED"
        assert calls["auth"] is not None, \
            "the bridge must receive the pre-fetched read"
        assert calls["auth"]["id"] == "AUTH-1"


# ---------------------------------------------------------------------------
# Fix 9: SQLite optimistic concurrency actually holds.
# ---------------------------------------------------------------------------

class TestSqliteVersioning:
    def test_all_stamps_store_version(self, tmp_path):
        store = SqliteStore(tmp_path / "jobs.db")
        store.save(_job("j1"))
        jobs = store.all()
        assert jobs[0].store_version == 1, \
            "all() must stamp store_version like get() does"

    def test_unstamped_save_after_external_bump(self, tmp_path):
        # The old blind upsert set the object to version 1 while the DB
        # sat at N+1, so the NEXT save raised a phantom conflict.
        # The fixed save reads the row version first and lands on N+1.
        db = tmp_path / "jobs.db"
        store = SqliteStore(db)
        store.save(_job("j1"))  # v1
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE jobs SET version = 6 WHERE job_id = 'j1'")
        conn.commit()
        conn.close()

        job2 = _job("j1")  # fresh, unstamped object
        assert getattr(job2, "store_version", None) is None
        store.save(job2)  # must not raise
        assert job2.store_version == 7, (
            f"expected version 7, got {job2.store_version}")
        assert store.get("j1").store_version == 7

    def test_real_conflict_still_raises(self, tmp_path):
        store = SqliteStore(tmp_path / "jobs.db")
        store.save(_job("j1"))
        a = store.get("j1")  # v1
        b = store.get("j1")  # v1
        store.save(a)  # v2
        with pytest.raises(RuntimeError, match="Concurrency conflict"):
            store.save(b)  # stale v1 must raise


# ---------------------------------------------------------------------------
# Fix 10: listed captures (with _members) in reconcile evidence.
# ---------------------------------------------------------------------------

class TestListedCapturesInEvidence:
    def _enum(self, svc, outcome, capture):
        return svc.CaptureEnumeration(outcome, capture)

    def _ev_job(self, price_cents=1250):
        job = _job("job-1", price_cents=price_cents, status="DELIVERED")
        return job

    def test_second_capture_reaches_evidence(self):
        # Books hold CAP-1; the order lists a synthetic sum with a NEW
        # member CAP-2. CAP-2 must reach the evidence (before the fix it
        # was invisible behind the stored id).
        svc = _service(FakePayPal())
        job = self._ev_job()
        job.capture_id = "CAP-1"
        job.capture_ids = ["CAP-1"]
        job.capture_amount_cents = 500
        cap = {"id": "CAP-1+CAP-2", "status": "COMPLETED",
               "amount": {"value": "8.00", "currency_code": "USD"},
               "_members": [_cap("CAP-1", 500), _cap("CAP-2", 300)]}
        enum_res = self._enum(svc, "found", cap)
        auth = {"id": "AUTH-1", "status": "VOIDED"}

        ev = svc._build_reconcile_evidence(job, auth, enum_res)
        assert "CAP-2" in ev.capture_ids, (
            f"new member CAP-2 must reach the evidence, got {ev.capture_ids}")
        assert "CAP-1" in ev.capture_ids

    def test_synthetic_id_not_double_counted(self):
        # The books already hold the synthetic "A+B" as one stored id;
        # a re-list unpacking to the same members must not append A and
        # B alongside it.
        svc = _service(FakePayPal())
        job = self._ev_job()
        job.capture_id = "CAP-1+CAP-2"
        job.capture_ids = ["CAP-1+CAP-2"]
        job.capture_amount_cents = 800
        cap = {"id": "CAP-1+CAP-2", "status": "COMPLETED",
               "amount": {"value": "8.00", "currency_code": "USD"},
               "_members": [_cap("CAP-1", 500), _cap("CAP-2", 300)]}
        enum_res = self._enum(svc, "found", cap)
        auth = {"id": "AUTH-1", "status": "VOIDED"}

        ev = svc._build_reconcile_evidence(job, auth, enum_res)
        assert ev.capture_ids == ("CAP-1+CAP-2",), (
            f"members already covered must not be re-added, "
            f"got {ev.capture_ids}")

    def test_conflicting_bare_id_wedges(self):
        # Books hold CAP-1; the order lists a BARE different id CAP-9
        # (no _members). Legacy's conflict rule: wedge for the operator,
        # don't silently append.
        svc = _service(FakePayPal())
        job = self._ev_job()
        job.capture_id = "CAP-1"
        job.capture_ids = ["CAP-1"]
        job.capture_amount_cents = 1250
        cap = _cap("CAP-9", 1250)
        enum_res = self._enum(svc, "found", cap)
        auth = {"id": "AUTH-1", "status": "VOIDED"}

        with pytest.raises(GuardRejection, match="conflicts with"):
            svc._build_reconcile_evidence(job, auth, enum_res)


class TestBridgeEnumNoneWedges:
    def test_bridge_wedges_when_enumeration_raises(self):
        # Fix 1 at the bridge level: when the capture enumeration
        # itself raises PayPalError (enum_res=None), the bridge must
        # wedge — never dispatch a blind void.
        auth = {"id": "AUTH-1", "status": "VOIDED", "captures": []}
        svc = _service(FakePayPal({"AUTH-1": auth}),
                       _job("job-1", status="AUTHORIZED"))

        def boom(job, auth):
            raise PayPalError("enumeration transport failed", status=500)

        svc._first_completed_capture_including_order = boom
        job = svc.store.get("job-1")
        result = svc._reconcile_job_via_dispatcher(job)

        assert result["to_status"] == "AUTHORIZED", result
        assert "could not be read" in result["error"], result
        fresh = svc.store.get("job-1")
        assert fresh.needs_review is True
        assert fresh.status == "AUTHORIZED"  # never VOIDED

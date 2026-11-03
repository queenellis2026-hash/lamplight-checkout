"""Item 3 (production-readiness): atomic webhook deduplication.

Two concurrent deliveries of the same event id — exactly one processes.
The claim is a single conditional INSERT, atomic across processes.
"""
import threading
import time

import pytest


def _make_service():
    from test_escrow_paypal import _StubPayPal, _stub_authorized_job
    stub = _StubPayPal()
    svc, job = _stub_authorized_job(stub)
    # The webhook needs a job with an authorization.
    return svc, job, stub


def _webhook_event(job):
    return {
        "id": "WH-TEST-123",
        "event_type": "AUTHORIZATION.CREATED",
        "resource": {
            "id": job.authorization_id,
            "status": "CREATED",
        },
    }


def test_concurrent_deliveries_exactly_one_processes():
    """Two threads deliver the same event id concurrently. Exactly one
    must process it; the other must see duplicate/in-progress."""
    svc, job, stub = _make_service()
    event = _webhook_event(job)

    results = []
    barrier = threading.Barrier(2)

    def deliver():
        barrier.wait()  # start both at the same time
        results.append(svc.handle_webhook_event(dict(event)))

    t1 = threading.Thread(target=deliver)
    t2 = threading.Thread(target=deliver)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(results) == 2
    actions = sorted(r["action"] for r in results)
    # One processes (confirmed/reconciled/ignored), the other is duplicate.
    assert "duplicate" in actions, f"actions={actions}"
    # Exactly one non-duplicate.
    non_dup = [a for a in actions if a != "duplicate"]
    assert len(non_dup) == 1, f"actions={actions}"


def test_completed_event_never_reprocessed():
    svc, job, stub = _make_service()
    event = _webhook_event(job)

    r1 = svc.handle_webhook_event(dict(event))
    assert r1["action"] != "duplicate"
    r2 = svc.handle_webhook_event(dict(event))
    assert r2["action"] == "duplicate"


def test_failed_claim_can_be_retried():
    """A claim settled as failed can be re-claimed (retry)."""
    from app.store import MemoryStore
    store = MemoryStore()
    assert store.claim_webhook_event("E1") == "won"
    store.settle_webhook_event("E1", completed=False)
    assert store.claim_webhook_event("E1") == "retry"
    store.settle_webhook_event("E1", completed=True)
    assert store.claim_webhook_event("E1") == "duplicate"


def test_stale_claim_can_be_reclaimed():
    """A crashed claim (stale claimed/processing) can be re-claimed."""
    from app.store import MemoryStore
    store = MemoryStore()
    assert store.claim_webhook_event("E2") == "won"
    # Simulate a crash: backdate the claim past the timeout.
    state, _ = store._webhook_claims["E2"]
    store._webhook_claims["E2"] = (state, int(time.time()) - 400)
    assert store.claim_webhook_event("E2") == "retry"


def test_sqlite_claim_atomicity():
    """SqliteStore: concurrent claims from separate connections —
    exactly one wins."""
    import tempfile, os
    from app.store import SqliteStore
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store1 = SqliteStore(path)
        store2 = SqliteStore(path)
        results = []

        def claim1():
            results.append(("s1", store1.claim_webhook_event("E3")))

        def claim2():
            results.append(("s2", store2.claim_webhook_event("E3")))

        barrier = threading.Barrier(2)
        t1 = threading.Thread(target=lambda: (barrier.wait(), claim1()))
        t2 = threading.Thread(target=lambda: (barrier.wait(), claim2()))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        outcomes = sorted(r[1] for r in results)
        # One wins, the other sees in_progress (fresh claim).
        assert outcomes == ["in_progress", "won"], f"outcomes={outcomes}"
    finally:
        os.unlink(path)

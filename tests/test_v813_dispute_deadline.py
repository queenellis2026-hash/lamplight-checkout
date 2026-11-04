"""v8.13: dispute deadline — Grok's review of v8.12 found the dispute
endpoint froze a DELIVERED authorization with no timer ("dispute then
disappear" was the dominant strategy; the only exit was the payer's
own force_settle). v8.13 gives every dispute a deadline:
dispute_deadline = min(now + 72h, honor_period_end - 1h margin).
At/after the deadline the sweeper resolves the expired dispute and
captures through the normal idempotent path — silence pays the worker.

Also covered here:
- the dispute route serializes the FRESH row, not the pre-dispute
  snapshot (Grok's stale-object finding);
- the process-wide Create Order token bucket (abuse guard for the
  unauthenticated POST /jobs);
- the boot-time legacy-JSON-book guard;
- two-store parity: the full dispute lifecycle behaves identically on
  MemoryStore and SqliteStore.
"""
import os
import pathlib
import subprocess
import sys
import time

import pytest
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.auth import DEMO_AGENT_TOKENS, DEMO_PAYER_TOKEN
from app.escrow import EscrowError, EscrowService, Job
from app.paypal_client import PayPalClient
from app.quote import new_quote
from app.server import (_check_legacy_json_book, _CreateOrderBudget,
                        app as flask_app, hiring as _hiring)
from app.store import JsonFileStore, MemoryStore, SqliteStore

MOCK_PORT = 5004
BASE = f"http://127.0.0.1:{MOCK_PORT}"

PAYER = "payer@example"
AGENT = "agent@example"
PAYER_H = {"Authorization": f"Bearer {DEMO_PAYER_TOKEN}"}


@pytest.fixture(scope="module")
def mock_server():
    proc = subprocess.Popen(
        [sys.executable, str(pathlib.Path(__file__).resolve().parent.parent
                             / "scripts" / "mock_paypal.py"),
         "--port", str(MOCK_PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if requests.get(f"{BASE}/health", timeout=2).status_code == 200:
                break
        except requests.RequestException:
            time.sleep(0.3)
    yield BASE
    proc.terminate()


@pytest.fixture()
def paypal_client(mock_server):
    return PayPalClient(client_id="mock", client_secret="mock",
                        base_url=mock_server)


@pytest.fixture(params=["memory", "sqlite"])
def svc(request, paypal_client, tmp_path):
    """EscrowService on MemoryStore AND on a temp SqliteStore — every
    lifecycle test below runs on both, and the behaviors must match."""
    if request.param == "memory":
        store = MemoryStore()
    else:
        store = SqliteStore(tmp_path / "jobs.db")
    return EscrowService(paypal_client, store=store)


def _quote(price_cents=1250):
    return new_quote(service="audit", price_cents=price_cents,
                     pay_to="agent@example")


def _delivered(svc, paypal_client):
    """Approved + authorized + delivered job, re-read fresh from the store."""
    job = svc.create_job(quote=_quote(), payer=PAYER, agent=AGENT,
                         deadline_seconds=3600)
    order = svc.create_order(job)
    requests.post(f"{BASE}/mock/approve/{order['id']}",
                  timeout=10).raise_for_status()
    svc.authorize(job)
    svc.submit_delivery(job, agent=AGENT, delivery_text="report v1")
    fresh = svc.store.get(job.job_id)
    assert fresh.status == "DELIVERED"
    return fresh


# ---------------------------------------------------------------------------
# 1. The dispute carries a deadline
# ---------------------------------------------------------------------------

def test_dispute_sets_opened_at_and_deadline(svc, paypal_client):
    t0 = int(time.time())
    job = _delivered(svc, paypal_client)
    assert job.authorized_at is not None
    result = svc.dispute_job(job, payer=PAYER, reason="work is wrong", now=t0)
    assert result["dispute_opened_at"] == t0
    # Deadline = min(t0 + 72h, authorized_at + 72h - 1h margin).
    # authorized_at ~= t0 (just authorized), so the honor clamp binds.
    expected_honor = job.authorized_at + 72 * 3600 - 3600
    assert result["dispute_deadline"] == min(t0 + 72 * 3600, expected_honor)
    assert result["dispute_deadline"] > t0
    fresh = svc.store.get(job.job_id)
    assert fresh.needs_review is True
    assert fresh.dispute_opened_at == t0
    assert fresh.dispute_deadline == result["dispute_deadline"]


def test_dispute_deadline_clamped_to_honor(svc, paypal_client):
    """An old authorization shortens the review window to the honor limit."""
    t0 = int(time.time())
    job = _delivered(svc, paypal_client)
    job.authorized_at = t0 - 60 * 3600  # authorized 60h ago
    svc._save(job)
    result = svc.dispute_job(job, payer=PAYER, reason="late dispute", now=t0)
    # honor_limit = t0 - 60h + 72h - 1h = t0 + 11h; remaining 11h >= 6h min.
    assert result["dispute_deadline"] == t0 + 11 * 3600


def test_dispute_rejected_when_honor_nearly_expired(svc, paypal_client):
    """A dispute that cannot cover a minimum review window is refused —
    those jobs must settle or void now, not enter review."""
    t0 = int(time.time())
    job = _delivered(svc, paypal_client)
    job.authorized_at = t0 - 68 * 3600  # only ~3h of honor left (< 6h min)
    svc._save(job)
    with pytest.raises(EscrowError, match="insufficient authorization time"):
        svc.dispute_job(job, payer=PAYER, reason="too late", now=t0)
    fresh = svc.store.get(job.job_id)
    assert fresh.needs_review is False
    assert fresh.dispute_deadline is None


# ---------------------------------------------------------------------------
# 2. The sweeper: skip before the deadline, capture at/after it
# ---------------------------------------------------------------------------

def test_sweep_skips_dispute_before_deadline(svc, paypal_client):
    t0 = int(time.time())
    job = _delivered(svc, paypal_client)
    svc.dispute_job(job, payer=PAYER, reason="not what I asked for", now=t0)
    # One hour into the review window: the sweep must not touch it.
    assert svc.check_inspection_deadlines(now=t0 + 3600) == []
    fresh = svc.store.get(job.job_id)
    assert fresh.status == "DELIVERED"
    assert fresh.needs_review is True


def test_sweep_captures_at_dispute_deadline_exactly_once(svc, paypal_client):
    """The core v8.13 property: an unresolved dispute does not wedge
    the job — at the deadline the sweeper captures, once, and the
    next sweep is a no-op."""
    t0 = int(time.time())
    job = _delivered(svc, paypal_client)
    result = svc.dispute_job(job, payer=PAYER, reason="not what I asked for",
                             now=t0)
    deadline = result["dispute_deadline"]
    # Past the deadline: the sweep resolves the dispute and captures.
    captured = svc.check_inspection_deadlines(now=deadline + 1)
    assert captured == [job.job_id]
    fresh = svc.store.get(job.job_id)
    assert fresh.status == "CAPTURED"
    assert fresh.needs_review is False
    assert fresh.capture_id  # money moved exactly once, on the record
    # The dispute timestamps survive as history.
    assert fresh.dispute_opened_at == t0
    assert fresh.dispute_deadline == deadline
    # Next sweep: no-op. No double capture.
    assert svc.check_inspection_deadlines(now=deadline + 2) == []
    again = svc.store.get(job.job_id)
    assert again.capture_id == fresh.capture_id


def test_legacy_dispute_without_deadline_treated_as_expired(svc, paypal_client):
    """v8.14: a v8.12 dispute row (needs_review, no dispute_deadline
    persisted) is treated as already expired — captured,
    not skipped forever. A flag with no deadline is not sacred."""
    job = _delivered(svc, paypal_client)
    job.inspection_deadline = int(time.time()) - 10
    job.needs_review = True
    job.needs_review_reason = "v8.12 dispute"
    job.dispute_opened_at = None
    job.dispute_deadline = None
    svc._save(job)
    # The sweep captures it (mock PayPal); the flag stays set until success.
    captured = svc.check_inspection_deadlines(now=int(time.time()) + 10)
    assert captured == [job.job_id]
    fresh = svc.store.get(job.job_id)
    assert fresh.status == "CAPTURED"
    assert fresh.needs_review is False


# ---------------------------------------------------------------------------
# 3. The exit: dispute -> force_settle settles exactly once
# ---------------------------------------------------------------------------

def test_dispute_then_force_settle_exactly_once(svc, paypal_client):
    """Grok's required test: the wedge has a working exit, and the
    exit cannot double-settle."""
    t0 = int(time.time())
    job = _delivered(svc, paypal_client)
    svc.dispute_job(job, payer=PAYER, reason="please just settle it", now=t0)
    result = svc.force_settle(svc.store.get(job.job_id), payer=PAYER)
    assert result["to_status"] in ("CAPTURED", "VOIDED")
    fresh = svc.store.get(job.job_id)
    assert fresh.status == result["to_status"]
    assert fresh.needs_review is False
    # Second force_settle: refused — the job is no longer wedged.
    with pytest.raises(EscrowError):
        svc.force_settle(svc.store.get(job.job_id), payer=PAYER)
    # And the money state did not move twice.
    final = svc.store.get(job.job_id)
    assert final.status == result["to_status"]
    assert final.capture_id == fresh.capture_id


# ---------------------------------------------------------------------------
# 4. The route serializes the fresh row (stale-snapshot fix)
# ---------------------------------------------------------------------------

@pytest.fixture()
def http_client(mock_server):
    flask_app.config["escrow"] = EscrowService(
        PayPalClient(client_id="mock", client_secret="mock",
                     base_url=mock_server))
    flask_app.config["TESTING"] = True
    flask_app.config["BYPASS_ORDER_BUDGET"] = True  # v8.14: the
    # Create Order budget is no longer bypassed by TESTING alone
    # (Grok latest) — fixtures opt in explicitly so the
    # budget-path tests can drive the real bucket.
    return flask_app.test_client()


def _http_delivered_job(client):
    body = client.post("/jobs",
                       json={"brief": "audit the top-20 holders, budget $50"})
    assert body.status_code == 201, body.get_json()
    data = body.get_json()
    job_id = data["job"]["job_id"]
    assert requests.post(
        f"{BASE}/mock/approve/{data['paypal_order']['id']}",
        timeout=10).status_code == 200
    r = client.post(f"/jobs/{job_id}/authorize", headers=PAYER_H)
    assert r.status_code == 200, r.get_json()
    r = client.post(f"/jobs/{job_id}/deliver",
                    json={"agent": "auditor-01@lamplight.example",
                          "delivery_text": "audit report: top-20 holders"},
                    headers={"Authorization":
                             "Bearer " + DEMO_AGENT_TOKENS["auditor-01@lamplight.example"]})
    assert r.status_code == 200, r.get_json()
    return job_id


def test_dispute_route_returns_fresh_row(http_client):
    """Grok: the route serialized the caller's pre-dispute snapshot.
    Pin the fix: get_job must be re-read after dispute_job, and the
    response job must match a genuinely fresh read."""
    svc = flask_app.config["escrow"]
    calls = []
    orig_get = svc.get_job

    def spy(job_id):
        calls.append(job_id)
        return orig_get(job_id)

    svc.get_job = spy
    try:
        job_id = _http_delivered_job(http_client)
        # The route derives the payer id from the bearer <redacted> (the
        # hiring agent's PayPal id), not from our test constant.
        r = http_client.post(f"/jobs/{job_id}/dispute",
                             json={"payer": _hiring.paypal_id,
                                   "reason": "stale?"},
                             headers=PAYER_H)
        assert r.status_code == 200, r.get_json()
    finally:
        svc.get_job = orig_get
    # Pre-dispute read + post-dispute re-read: the route must not
    # serialize the snapshot it entered with.
    # v8.14: the route returns the full dict (payer-only route, like
    # capture/void), not the public shape.
    assert calls.count(job_id) >= 2
    assert r.get_json()["job"] == orig_get(job_id).to_dict()


# ---------------------------------------------------------------------------
# 5. Create Order token bucket
# ---------------------------------------------------------------------------

def test_order_budget_allows_burst_then_refills():
    now = [1000.0]

    def clock():
        return now[0]

    bucket = _CreateOrderBudget(2, _clock=clock)
    assert bucket.take() is True
    assert bucket.take() is True
    assert bucket.take() is False  # burst exhausted
    now[0] += 30.0  # half a minute: 1 token refills
    assert bucket.take() is True
    assert bucket.take() is False
    now[0] += 60.0  # full minute: back to capacity
    assert bucket.take() is True
    assert bucket.take() is True
    assert bucket.take() is False


def test_order_budget_never_exceeds_capacity():
    now = [0.0]
    bucket = _CreateOrderBudget(3, _clock=lambda: now[0])
    now[0] += 3600.0  # an hour idle: still capped at 3
    assert bucket.take() is True
    assert bucket.take() is True
    assert bucket.take() is True
    assert bucket.take() is False


# ---------------------------------------------------------------------------
# 6. Boot guard: never strand a live JSON book on an empty SQLite db
# ---------------------------------------------------------------------------

def _write_json_job(path, job_id="JOB-1"):
    store = JsonFileStore(path)
    job = Job(job_id=job_id, quote=new_quote(service="audit",
                                             price_cents=1250,
                                             pay_to="agent@example"),
              payer=PAYER, agent=AGENT, deadline_seconds=3600)
    store.save(job)


def test_boot_refuses_stranded_json_book(tmp_path, monkeypatch):
    """jobs.json live, no jobs.db, no env config -> RuntimeError."""
    _write_json_job(tmp_path / "jobs.json")
    monkeypatch.delenv("LAMPLIGHT_JOBS_FILE", raising=False)
    monkeypatch.delenv("LAMPLIGHT_STORE", raising=False)
    with pytest.raises(RuntimeError, match="refusing to boot"):
        _check_legacy_json_book(root=tmp_path)


def test_boot_allows_when_db_has_jobs(tmp_path, monkeypatch):
    """Both books present with jobs in the db -> boot proceeds."""
    _write_json_job(tmp_path / "jobs.json")
    db_store = SqliteStore(tmp_path / "jobs.db")
    job = Job(job_id="JOB-1", quote=new_quote(service="audit",
                                              price_cents=1250,
                                              pay_to="agent@example"),
              payer=PAYER, agent=AGENT, deadline_seconds=3600)
    db_store.save(job)
    monkeypatch.delenv("LAMPLIGHT_JOBS_FILE", raising=False)
    monkeypatch.delenv("LAMPLIGHT_STORE", raising=False)
    _check_legacy_json_book(root=tmp_path)  # no raise


def test_boot_allows_empty_json_book(tmp_path, monkeypatch):
    """jobs.json exists but is empty -> nothing to strand, boot proceeds."""
    JsonFileStore(tmp_path / "jobs.json")  # creates the file, no jobs
    monkeypatch.delenv("LAMPLIGHT_JOBS_FILE", raising=False)
    monkeypatch.delenv("LAMPLIGHT_STORE", raising=False)
    _check_legacy_json_book(root=tmp_path)  # no raise


def test_boot_allows_explicit_config(tmp_path, monkeypatch):
    """Explicit LAMPLIGHT_JOBS_FILE -> operator chose; no refusal."""
    _write_json_job(tmp_path / "jobs.json")
    monkeypatch.setenv("LAMPLIGHT_JOBS_FILE", str(tmp_path / "jobs.json"))
    monkeypatch.delenv("LAMPLIGHT_STORE", raising=False)
    _check_legacy_json_book(root=tmp_path)  # no raise


def test_boot_allows_no_json_book(tmp_path, monkeypatch):
    """Fresh deploy, no jobs.json at all -> boot proceeds."""
    monkeypatch.delenv("LAMPLIGHT_JOBS_FILE", raising=False)
    monkeypatch.delenv("LAMPLIGHT_STORE", raising=False)
    _check_legacy_json_book(root=tmp_path)  # no raise

"""v8.14: Grok's v8.13 review fixes, applied exactly.

1. The flag stays SET until PayPal accepts. _resolve_expired_dispute
   audits only — no flag clear, no status change. dispute_job 409s
   while needs_review is set: that is the ladder-blocker. _capture_core
   takes _sweep_dispute=True for the sweep path, bypassing the review
   guard and clearing the flag in the same write as CAPTURED. On PayPal
   failure the flag stays set and the deadline stays in the past, so
   the next sweep retries.

2. Fail closed on honor math: _honor_limit() returns None when
   authorized_at is unknown, and dispute_job refuses. Honor is three
   days from authorization (not the 29-day lifetime).

3. Honest labeling: the route stays /dispute, but the response carries
   effect="payment_pause", resolves_by="capture", and a payment_pause
   block stating plainly what this does. The worker can attach a
   counter-note as chargeback evidence only.
"""
import pathlib
import subprocess
import sys
import time

import pytest
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.auth import DEMO_AGENT_TOKENS, DEMO_PAYER_TOKEN
from app.escrow import EscrowError, EscrowService
from app.paypal_client import PayPalClient
from app.quote import new_quote
from app.server import app as flask_app, hiring as _hiring
from app.store import MemoryStore, SqliteStore

MOCK_PORT = 5005
BASE = f"http://127.0.0.1:{MOCK_PORT}"

PAYER = "payer@example"
AGENT = "agent@example"
PAYER_H = {"Authorization": f"Bearer {DEMO_PAYER_TOKEN}"}
AGENT_H = {"Authorization": f"Bearer {DEMO_AGENT_TOKENS['auditor-01@lamplight.example']}"}


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
    return PayPalClient(client_id="mock", client_secret="<redacted>",
                        base_url=mock_server)


@pytest.fixture(params=["memory", "sqlite"])
def svc(request, paypal_client, tmp_path):
    if request.param == "memory":
        store = MemoryStore()
    else:
        store = SqliteStore(tmp_path / "jobs.db")
    return EscrowService(paypal_client, store=store)


def _quote(price_cents=1250):
    return new_quote(service="audit", price_cents=price_cents,
                     pay_to="agent@example")


def _delivered(svc, paypal_client):
    job = svc.create_job(quote=_quote(), payer=PAYER, agent=AGENT,
                         deadline_seconds=3600)
    order = svc.create_order(job)
    requests.post(f"{BASE}/mock/approve/{order['id']}",
                  timeout=10).raise_for_status()
    svc.authorize(job)
    svc.submit_delivery(job, agent=AGENT, delivery_text="report v1")
    fresh = svc.store.get(job.job_id)
    assert fresh.status == "DELIVERED"
    assert fresh.authorized_at is not None
    return fresh


# ---------------------------------------------------------------------------
# 1. The flag stays set until PayPal accepts (no clear-then-capture gap)
# ---------------------------------------------------------------------------

def test_expired_pause_keeps_flag_until_capture(svc, paypal_client):
    """Grok §1: _resolve_expired_dispute audits only. The flag stays
    set; a concurrent dispute_job 409s on it."""
    t0 = int(time.time())
    job = _delivered(svc, paypal_client)
    result = svc.dispute_job(job, payer=PAYER, reason="pause me", now=t0)
    deadline = result["dispute_deadline"]
    # Drive the resolve without capturing: call it directly.
    j = svc.store.get(job.job_id)
    with svc._delivery_lock(j.job_id):
        fresh = svc.store.get(j.job_id)
        svc._resolve_expired_dispute(fresh, now=deadline + 1)
    mid = svc.store.get(job.job_id)
    # Flag STILL SET, status STILL DELIVERED. No gap.
    assert mid.needs_review is True
    assert mid.status == "DELIVERED"
    # A second dispute is refused — the ladder is blocked.
    with pytest.raises(EscrowError, match="already under review"):
        svc.dispute_job(mid, payer=PAYER, reason="second", now=deadline + 2)
    assert svc.store.get(job.job_id).dispute_deadline == deadline


def test_sweep_capture_clears_flag_on_success(svc, paypal_client):
    """The sweep's expired-pause capture clears needs_review in the
    same write as CAPTURED."""
    t0 = int(time.time())
    job = _delivered(svc, paypal_client)
    result = svc.dispute_job(job, payer=PAYER, reason="pause", now=t0)
    deadline = result["dispute_deadline"]
    captured = svc.check_inspection_deadlines(now=deadline + 1)
    assert captured == [job.job_id]
    fresh = svc.store.get(job.job_id)
    assert fresh.status == "CAPTURED"
    assert fresh.needs_review is False
    assert fresh.capture_id


def test_failed_capture_leaves_flag_for_retry(svc, paypal_client):
    """On PayPal failure the flag stays set and the deadline stays in
    the past — the next sweep retries."""
    t0 = int(time.time())
    job = _delivered(svc, paypal_client)
    result = svc.dispute_job(job, payer=PAYER, reason="pause", now=t0)
    deadline = result["dispute_deadline"]
    # Break the PayPal capture.
    orig = svc.paypal.capture_authorization
    svc.paypal.capture_authorization = lambda *a, **k: (_ for _ in ()).throw(
        Exception("mock paypal down"))
    try:
        captured = svc.check_inspection_deadlines(now=deadline + 1)
    finally:
        svc.paypal.capture_authorization = orig
    assert captured == []
    fresh = svc.store.get(job.job_id)
    # Flag still set, deadline still in the past, still DELIVERED.
    assert fresh.needs_review is True
    assert fresh.status == "DELIVERED"
    assert fresh.dispute_deadline == deadline
    # Next sweep (PayPal back): captures once.
    captured = svc.check_inspection_deadlines(now=deadline + 60)
    assert captured == [job.job_id]
    assert svc.store.get(job.job_id).status == "CAPTURED"


def test_legacy_null_deadline_captures(svc, paypal_client):
    """A flagged job with no deadline (v8.12 row, or any writer that
    forgot the timestamp) is treated as expired — captured, not
    skipped forever."""
    job = _delivered(svc, paypal_client)
    job.inspection_deadline = int(time.time()) - 10
    job.needs_review = True
    job.needs_review_reason = "v8.12 dispute"
    job.dispute_opened_at = None
    job.dispute_deadline = None
    svc._save(job)
    captured = svc.check_inspection_deadlines(now=int(time.time()) + 10)
    assert captured == [job.job_id]
    assert svc.store.get(job.job_id).status == "CAPTURED"


# ---------------------------------------------------------------------------
# 2. Fail closed: _honor_limit
# ---------------------------------------------------------------------------

def test_pause_refused_without_authorized_at(svc, paypal_client):
    job = _delivered(svc, paypal_client)
    job.authorized_at = None
    svc._save(job)
    with pytest.raises(EscrowError, match="honor period is unknown"):
        svc.dispute_job(job, payer=PAYER, reason="pause?",
                        now=int(time.time()))
    assert svc.store.get(job.job_id).needs_review is False


def test_pause_refused_when_authorized_29_days_ago(svc, paypal_client):
    """Honor is 3 days from authorization, not the 29-day lifetime."""
    t0 = int(time.time())
    job = _delivered(svc, paypal_client)
    job.authorized_at = t0 - 29 * 24 * 3600
    svc._save(job)
    with pytest.raises(EscrowError, match="insufficient authorization"):
        svc.dispute_job(job, payer=PAYER, reason="too late", now=t0)


def test_pause_deadline_uses_honor_not_flat_72h(svc, paypal_client):
    """authorized_at 1h ago: deadline = authorized_at + 3d − 1h, which
    is EARLIER than now + 72h. The honor clamp binds, not the flat window."""
    t0 = int(time.time())
    job = _delivered(svc, paypal_client)
    job.authorized_at = t0 - 3600  # 1h ago
    svc._save(job)
    result = svc.dispute_job(job, payer=PAYER, reason="pause", now=t0)
    expected = (t0 - 3600) + 3 * 24 * 3600 - 3600
    assert result["dispute_deadline"] == expected
    assert expected < t0 + 72 * 3600  # honor clamp bound, not the flat 72h


def test_honor_limit_helper(svc, paypal_client):
    job = _delivered(svc, paypal_client)
    assert svc._honor_limit(job) == (
        job.authorized_at + 3 * 24 * 3600 - 3600)
    job.authorized_at = None
    assert svc._honor_limit(job) is None


# ---------------------------------------------------------------------------
# 3. Honest labeling: effect, resolves_by, payment_pause block
# ---------------------------------------------------------------------------

@pytest.fixture()
def http_client(mock_server):
    flask_app.config["escrow"] = EscrowService(
        PayPalClient(client_id="mock", client_secret="<redacted>",
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


def test_dispute_response_labels_payment_pause(http_client):
    """Grok §3: the route stays /dispute, but the body says what it is."""
    job_id = _http_delivered_job(http_client)
    r = http_client.post(f"/jobs/{job_id}/dispute",
                         json={"payer": _hiring.paypal_id,
                               "reason": "not what I wanted"},
                         headers=PAYER_H)
    assert r.status_code == 200, r.get_json()
    data = r.get_json()
    assert "dispute" in data
    d = data["dispute"]
    assert d["effect"] == "payment_pause"
    assert d["resolves_by"] == "capture"
    assert d["dispute_opened_at"] is not None
    assert d["dispute_deadline"] is not None
    pp = data["payment_pause"]
    assert pp["review"] is False
    assert pp["captures_at"] == d["dispute_deadline"]
    detail = pp["detail"]
    assert "Nobody reviews the reason" in detail
    assert "does not refund" in detail
    assert "pays the worker" in detail
    # The response text is explicit about force_settle's cases: an
    # active pause captures now and the worker is paid; otherwise an
    # open hold is voided and the money returns to the payer; if a
    # capture already landed, the worker keeps it. The old text said
    # only "force_settle pays the worker now" with no void
    # clarification — a payer who believed it would never press the
    # only button that stops payment on an open hold.
    assert "force_settle pays the worker now" not in detail
    assert "voided" in detail
    assert "worker keeps it" in detail


def test_worker_note_stored_as_evidence(http_client):
    job_id = _http_delivered_job(http_client)
    r = http_client.post(f"/jobs/{job_id}/dispute",
                         json={"payer": _hiring.paypal_id,
                               "reason": "looks wrong"},
                         headers=PAYER_H)
    assert r.status_code == 200
    deadline = r.get_json()["dispute"]["dispute_deadline"]
    r = http_client.post(f"/jobs/{job_id}/dispute/note",
                         json={"agent": "auditor-01@lamplight.example",
                               "note": "delivered exactly per the brief"},
                         headers=AGENT_H)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["note_set"] is True
    svc = flask_app.config["escrow"]
    captured = svc.check_inspection_deadlines(now=deadline + 1)
    assert captured == [job_id]
    fresh = svc.get_job(job_id)
    assert fresh.status == "CAPTURED"
    assert fresh.pause_worker_note == "delivered exactly per the brief"


def test_worker_note_requires_agent_token(http_client):
    job_id = _http_delivered_job(http_client)
    r = http_client.post(f"/jobs/{job_id}/dispute",
                         json={"payer": _hiring.paypal_id, "reason": "x"},
                         headers=PAYER_H)
    assert r.status_code == 200
    r = http_client.post(f"/jobs/{job_id}/dispute/note",
                         json={"agent": "auditor-01@lamplight.example",
                               "note": "payer pretending"},
                         headers=PAYER_H)
    assert r.status_code == 403

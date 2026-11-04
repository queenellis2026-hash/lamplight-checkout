"""Phase 4 (property-testing game plan): reconciliation system tests.

The nightly runner compares the books against fresh PayPal reads,
persists runs + findings, and alerts only on NEW divergences. These
tests drive it with the Phase 1 FakePayPalClient (in-process,
deterministic) — no subprocess mock needed.
"""

import json
import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.auth import DEMO_PAYER_TOKEN  # noqa: E402
from app.escrow import EscrowService  # noqa: E402
from app.quote import new_quote  # noqa: E402
from app.store import MemoryStore, SqliteStore  # noqa: E402
from app.server import app as flask_app  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from fake_paypal import FakePayPalClient  # noqa: E402

PAYER = "payer@sm.example"
AGENT = "agent@sm.example"
PAYER_H = {"Authorization": f"Bearer {DEMO_PAYER_TOKEN}"}


def _svc():
    fake = FakePayPalClient()
    svc = EscrowService(fake, store=MemoryStore())
    return svc, fake


def _drive_to_captured(svc, fake, price_cents=5000):
    """Drive one job create -> order -> authorize -> deliver -> capture."""
    quote = new_quote(service="sm", price_cents=price_cents,
                      pay_to="agent@sm.example",
                      ttl_seconds=30 * 24 * 3600)
    job = svc.create_job(quote=quote, payer=PAYER, agent=AGENT,
                         deadline_seconds=3600)
    order = svc.create_order(job)
    fake.approve_order(order["id"])
    svc.authorize(svc.store.get(job.job_id))
    text = "reconciliation test delivery"
    svc.submit_delivery(svc.store.get(job.job_id), agent=AGENT,
                        delivery_text=text, now=int(time.time()))
    svc.capture(svc.store.get(job.job_id), payer=PAYER, delivery_text=text)
    job = svc.store.get(job.job_id)
    assert job.status == "CAPTURED", job.status
    return job


def test_runner_flags_divergence_and_alerts_once(tmp_path):
    """Book says CAPTURED, PayPal says VOIDED -> one finding, one alert."""
    svc, fake = _svc()
    job = _drive_to_captured(svc, fake)
    # Seed the divergence on the PayPal side only.
    fake._auths[job.authorization_id]["status"] = "VOIDED"

    alert_log = tmp_path / "alerts.jsonl"
    summary = svc.run_scheduled_reconciliation(alert_log_path=str(alert_log))

    assert summary["status"] == "divergent"
    assert summary["jobs_checked"] == 1
    assert summary["divergent_count"] == 1
    assert summary["new_findings"] == 1
    assert summary["alerts_sent"] == 1

    findings = svc.store.list_reconciliation_findings(status="open")
    assert len(findings) == 1
    f = findings[0]
    assert f["job_id"] == job.job_id
    assert f["issue_type"] == "status_mismatch"
    assert "VOIDED" in f["detail"] and "CAPTURED" in f["detail"]
    assert f["status"] == "open"
    assert f["resolved_at"] is None

    lines = alert_log.read_text().strip().split("\n")
    assert len(lines) == 1
    alert = json.loads(lines[0])
    assert alert["job_id"] == job.job_id
    assert alert["issue_type"] == "status_mismatch"
    assert alert["severity"] == "high"
    assert alert["run_id"] == summary["run_id"]

    runs = svc.store.get_reconciliation_runs()
    assert len(runs) == 1
    assert runs[0]["run_id"] == summary["run_id"]
    assert runs[0]["status"] == "divergent"
    assert runs[0]["jobs_checked"] == 1
    assert runs[0]["divergent_count"] == 1
    assert runs[0]["finished_at"] >= runs[0]["started_at"]


def test_alert_dedupe_no_second_alert(tmp_path):
    """The same divergence on the next run alerts nothing new."""
    svc, fake = _svc()
    job = _drive_to_captured(svc, fake)
    fake._auths[job.authorization_id]["status"] = "VOIDED"

    alert_log = tmp_path / "alerts.jsonl"
    first = svc.run_scheduled_reconciliation(alert_log_path=str(alert_log))
    assert first["alerts_sent"] == 1

    second = svc.run_scheduled_reconciliation(alert_log_path=str(alert_log))
    assert second["status"] == "divergent"
    assert second["new_findings"] == 0
    assert second["alerts_sent"] == 0

    # Still exactly one open finding, one alert line.
    assert len(svc.store.list_reconciliation_findings(status="open")) == 1
    assert len(alert_log.read_text().strip().split("\n")) == 1


def test_resolve_reopens_alerting(tmp_path):
    """Resolving a finding lets the next run alert again if it recurs."""
    svc, fake = _svc()
    job = _drive_to_captured(svc, fake)
    fake._auths[job.authorization_id]["status"] = "VOIDED"

    alert_log = tmp_path / "alerts.jsonl"
    svc.run_scheduled_reconciliation(alert_log_path=str(alert_log))
    f = svc.store.list_reconciliation_findings(status="open")[0]
    svc.store.update_reconciliation_finding(
        f["finding_id"], status="resolved", note="ops looked, re-checking",
        resolved_at=int(time.time()))

    third = svc.run_scheduled_reconciliation(alert_log_path=str(alert_log))
    assert third["new_findings"] == 1
    assert third["alerts_sent"] == 1
    assert len(alert_log.read_text().strip().split("\n")) == 2


def test_paypal_read_failure_is_error_not_divergence(tmp_path):
    """A failed PayPal read is an error, not a divergence — absence of
    evidence is not evidence of divergence (game plan locks this in)."""
    svc, fake = _svc()
    job = _drive_to_captured(svc, fake)
    # PayPal "forgets" the authorization: get_authorization 404s.
    del fake._auths[job.authorization_id]

    alert_log = tmp_path / "alerts.jsonl"
    summary = svc.run_scheduled_reconciliation(alert_log_path=str(alert_log))

    assert summary["status"] == "error"
    assert summary["divergent_count"] == 0
    assert summary["read_failures"] == 1
    assert summary["new_findings"] == 1

    findings = svc.store.list_reconciliation_findings(status="open")
    assert len(findings) == 1
    assert findings[0]["issue_type"] == "paypal_read_failed"
    assert findings[0]["job_id"] == job.job_id

    runs = svc.store.get_reconciliation_runs()
    assert runs[0]["status"] == "error"


def test_clean_run_is_ok(tmp_path):
    """No divergence -> ok status, no findings, no alerts."""
    svc, fake = _svc()
    _drive_to_captured(svc, fake)

    alert_log = tmp_path / "alerts.jsonl"
    summary = svc.run_scheduled_reconciliation(alert_log_path=str(alert_log))

    assert summary["status"] == "ok"
    assert summary["divergent_count"] == 0
    assert summary["new_findings"] == 0
    assert summary["alerts_sent"] == 0
    assert svc.store.list_reconciliation_findings(status="open") == []
    assert not alert_log.exists()
    assert svc.store.get_reconciliation_runs()[0]["status"] == "ok"


def _flask_client(svc):
    flask_app.config["escrow"] = svc
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


def test_admin_routes_ack_resolve(tmp_path):
    """Payer-only admin review: list, ack, resolve — with required notes."""
    svc, fake = _svc()
    job = _drive_to_captured(svc, fake)
    fake._auths[job.authorization_id]["status"] = "VOIDED"
    alert_log = tmp_path / "alerts.jsonl"
    svc.run_scheduled_reconciliation(alert_log_path=str(alert_log))
    client = _flask_client(svc)

    # Unauthenticated -> 401.
    assert client.get("/admin/reconcile/findings").status_code == 401

    r = client.get("/admin/reconcile/findings", headers=PAYER_H)
    assert r.status_code == 200
    findings = r.get_json()["findings"]
    assert len(findings) == 1
    fid = findings[0]["finding_id"]

    r = client.get("/admin/reconcile/runs", headers=PAYER_H)
    assert r.status_code == 200
    assert len(r.get_json()["runs"]) == 1

    # Note is required.
    r = client.post(f"/admin/reconcile/findings/{fid}/ack",
                    json={}, headers=PAYER_H)
    assert r.status_code == 400

    r = client.post(f"/admin/reconcile/findings/{fid}/ack",
                    json={"note": "seen, investigating"}, headers=PAYER_H)
    assert r.status_code == 200
    assert r.get_json()["finding"]["status"] == "acknowledged"
    assert r.get_json()["finding"]["note"] == "seen, investigating"

    r = client.get("/admin/reconcile/findings?status=acknowledged",
                   headers=PAYER_H)
    assert len(r.get_json()["findings"]) == 1

    r = client.post(f"/admin/reconcile/findings/{fid}/resolve",
                    json={"note": "paypal corrected"}, headers=PAYER_H)
    assert r.status_code == 200
    body = r.get_json()["finding"]
    assert body["status"] == "resolved"
    assert body["resolved_at"] is not None

    r = client.get("/admin/reconcile/findings?status=open", headers=PAYER_H)
    assert r.get_json()["findings"] == []

    # Unknown finding -> 404.
    r = client.post("/admin/reconcile/findings/does-not-exist/ack",
                    json={"note": "x"}, headers=PAYER_H)
    assert r.status_code == 404


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_recon_store_roundtrip(tmp_path, store_kind):
    """The reconciliation tables work on both stores (parity)."""
    if store_kind == "sqlite":
        store = SqliteStore(tmp_path / "recon.db")
    else:
        store = MemoryStore()

    assert store.get_reconciliation_runs() == []
    assert store.list_reconciliation_findings() == []

    store.record_reconciliation_run({
        "run_id": "run-1", "started_at": 1000, "finished_at": 1005,
        "jobs_checked": 3, "divergent_count": 1, "status": "divergent"})
    runs = store.get_reconciliation_runs()
    assert len(runs) == 1 and runs[0]["run_id"] == "run-1"

    store.record_reconciliation_finding({
        "finding_id": "f-1", "run_id": "run-1", "job_id": "job-1",
        "issue_type": "amount_mismatch", "detail": "books=500 paypal=400",
        "first_seen": 1001, "status": "open", "resolved_at": None,
        "note": None})
    assert store.find_open_finding("job-1", "amount_mismatch")["finding_id"] == "f-1"
    assert store.find_open_finding("job-1", "status_mismatch") is None
    assert store.get_reconciliation_finding("f-1")["detail"] == \
        "books=500 paypal=400"

    updated = store.update_reconciliation_finding(
        "f-1", status="acknowledged", note="looking")
    assert updated["status"] == "acknowledged"
    assert updated["note"] == "looking"
    assert store.find_open_finding("job-1", "amount_mismatch") is None
    assert len(store.list_reconciliation_findings(status="open")) == 0
    assert len(store.list_reconciliation_findings()) == 1
    assert store.update_reconciliation_finding(
        "missing", status="resolved", note="x") is None

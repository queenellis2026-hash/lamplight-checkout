"""Tests for the Flask service, run against the local mock PayPal sandbox.

Mutating endpoints require a bearer token (app/auth.py): the payer token
for authorize/capture/void, the worker's own token for deliver. The demo
defaults are imported from app.auth so the tests always use the same
tokens the server does.
"""
import pathlib
import subprocess
import sys
import time

import pytest
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.auth import DEMO_AGENT_TOKENS, DEMO_PAYER_TOKEN
from app.escrow import EscrowService
from app.paypal_client import PayPalClient, PayPalError
from app.server import app as flask_app

MOCK_PORT = 5003
BASE = f"http://127.0.0.1:{MOCK_PORT}"

PAYER = "ellis@lamplight.example"  # hiring agent's PayPal id
AUDITOR = "auditor-01@lamplight.example"
WRITER = "writer-02@lamplight.example"

PAYER_H = {"Authorization": f"Bearer {DEMO_PAYER_TOKEN}"}


def _agent_headers(agent_id: str) -> dict:
    return {"Authorization": f"Bearer {DEMO_AGENT_TOKENS[agent_id]}"}


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
def client(mock_server):
    flask_app.config["escrow"] = EscrowService(
        PayPalClient(client_id="mock", client_secret="mock", base_url=mock_server))
    flask_app.config["TESTING"] = True
    flask_app.config["BYPASS_ORDER_BUDGET"] = True  # v8.14: the
    # Create Order budget is no longer bypassed by TESTING alone
    # (Grok latest) — fixtures opt in explicitly so the
    # budget-path tests can drive the real bucket.
    return flask_app.test_client()


def _create_job(client, brief="audit the top-20 holders, budget $50", **kw):
    body = {"brief": brief}
    body.update(kw)
    r = client.post("/jobs", json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def _approve_and_authorize(client, job_id, order_id):
    assert requests.post(f"{BASE}/mock/approve/{order_id}",
                         timeout=10).status_code == 200
    r = client.post(f"/jobs/{job_id}/authorize", headers=PAYER_H)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["job"]["status"] == "AUTHORIZED"


def _delivered_job(client):
    """A job in DELIVERED state (auditor worker), ready to capture."""
    body = _create_job(client)
    job_id = body["job"]["job_id"]
    _approve_and_authorize(client, job_id, body["paypal_order"]["id"])
    r = client.post(f"/jobs/{job_id}/deliver",
                    json={"agent": AUDITOR,
                          "delivery_text": "audit report: top-20 holders"},
                    headers=_agent_headers(AUDITOR))
    assert r.status_code == 200, r.get_json()
    return job_id


def test_full_flow_over_http(client):
    # Create job -> quote + order.
    body = _create_job(client)
    job_id = body["job"]["job_id"]
    order_id = body["paypal_order"]["id"]
    assert body["approve_url"]

    _approve_and_authorize(client, job_id, order_id)

    # Capture before delivery must be refused.
    r = client.post(f"/jobs/{job_id}/capture",
                    json={"payer": PAYER,
                          "delivery_text": "audit report: top-20 holders"},
                    headers=PAYER_H)
    assert r.status_code == 409

    # Deliver, then capture.
    r = client.post(f"/jobs/{job_id}/deliver",
                    json={"agent": AUDITOR,
                          "delivery_text": "audit report: top-20 holders"},
                    headers=_agent_headers(AUDITOR))
    assert r.status_code == 200, r.get_json()
    r = client.post(f"/jobs/{job_id}/capture",
                    json={"payer": PAYER,
                          "delivery_text": "audit report: top-20 holders"},
                    headers=PAYER_H)
    assert r.status_code == 200, r.get_json()
    receipt = r.get_json()["receipt"]
    assert receipt["outcome"] == "released"
    assert receipt["capture_id"].startswith("CAPTURE-")
    assert receipt["receipt_hash"].startswith("0x")


def test_void_flow_over_http(client):
    body = _create_job(client, brief="write a research summary, budget $20",
                       deadline_seconds=1)
    job_id = body["job"]["job_id"]
    order_id = body["paypal_order"]["id"]
    _approve_and_authorize(client, job_id, order_id)
    time.sleep(1.5)
    r = client.post(f"/jobs/{job_id}/void", json={"payer": PAYER},
                    headers=PAYER_H)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["receipt"]["outcome"] == "voided"
    assert r.get_json()["receipt"]["capture_id"] == ""


# ---------------------------------------------------------------------------
# Post-review fixes (Oct 27, 2026): bearer auth, no identity defaults,
# input validation, no orphaned jobs.
# ---------------------------------------------------------------------------

def test_missing_auth_rejected(client):
    """No bearer token at all: 401, even with a correct payer field."""
    job_id = _delivered_job(client)
    r = client.post(f"/jobs/{job_id}/capture", json={"payer": PAYER})
    assert r.status_code == 401


def test_wrong_auth_rejected(client):
    """An unknown bearer token: 401."""
    job_id = _delivered_job(client)
    r = client.post(f"/jobs/{job_id}/capture", json={"payer": PAYER},
                    headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401


def test_missing_payer_field_rejected(client):
    """Valid payer token but no payer field in the body: 400. There is
    no default — an empty body is never the payer."""
    job_id = _delivered_job(client)
    r = client.post(f"/jobs/{job_id}/capture", json={}, headers=PAYER_H)
    assert r.status_code == 400


def test_missing_agent_field_rejected(client):
    """Valid agent token but no agent field in the body: 400."""
    body = _create_job(client)
    job_id = body["job"]["job_id"]
    _approve_and_authorize(client, job_id, body["paypal_order"]["id"])
    r = client.post(f"/jobs/{job_id}/deliver",
                    json={"delivery_text": "audit report"},
                    headers=_agent_headers(AUDITOR))
    assert r.status_code == 400


def test_body_identity_mismatch_rejected(client):
    """Valid payer token but a payer field naming someone else: 403.
    The credential wins; the body must agree with it."""
    job_id = _delivered_job(client)
    r = client.post(f"/jobs/{job_id}/capture",
                    json={"payer": "someone-else@example"}, headers=PAYER_H)
    assert r.status_code == 403


def test_agent_cannot_capture(client):
    """A worker agent's token on the payer-only capture endpoint: 403."""
    job_id = _delivered_job(client)
    r = client.post(f"/jobs/{job_id}/capture", json={"payer": AUDITOR},
                    headers=_agent_headers(AUDITOR))
    assert r.status_code == 403


def test_payer_cannot_deliver(client):
    """The payer's token on the agent-only deliver endpoint: 403."""
    body = _create_job(client)
    job_id = body["job"]["job_id"]
    _approve_and_authorize(client, job_id, body["paypal_order"]["id"])
    r = client.post(f"/jobs/{job_id}/deliver",
                    json={"agent": AUDITOR, "delivery_text": "audit report"},
                    headers=PAYER_H)
    assert r.status_code == 403


def test_wrong_agent_cannot_deliver(client):
    """A different worker's token delivering an auditor job: 403."""
    body = _create_job(client)
    job_id = body["job"]["job_id"]
    _approve_and_authorize(client, job_id, body["paypal_order"]["id"])
    r = client.post(f"/jobs/{job_id}/deliver",
                    json={"agent": WRITER, "delivery_text": "audit report"},
                    headers=_agent_headers(WRITER))
    assert r.status_code == 403


def test_authorize_requires_payer_token(client):
    """Authorize without a token: 401. With an agent token: 403."""
    body = _create_job(client)
    job_id = body["job"]["job_id"]
    requests.post(f"{BASE}/mock/approve/{body['paypal_order']['id']}",
                  timeout=10).raise_for_status()
    assert client.post(f"/jobs/{job_id}/authorize").status_code == 401
    assert client.post(f"/jobs/{job_id}/authorize",
                       headers=_agent_headers(AUDITOR)).status_code == 403


@pytest.mark.parametrize("payload,desc", [
    ({"brief": "audit the holders, budget $50", "ttl_seconds": "abc"},
     "non-numeric ttl"),
    ({"brief": "audit the holders, budget $50", "ttl_seconds": -5},
     "negative ttl"),
    ({"brief": "audit the holders, budget $50", "deadline_seconds": -1},
     "negative deadline"),
    ({"brief": "audit the holders, budget $50", "deadline_seconds": "soon"},
     "non-numeric deadline"),
    ({"brief": "audit the holders, budget $50", "ttl_seconds": True},
     "boolean ttl"),
    ({"brief": 123}, "non-string brief"),
    ({"brief": ""}, "empty brief"),
])
def test_create_job_bad_inputs_rejected(client, payload, desc):
    r = client.post("/jobs", json=payload)
    assert r.status_code == 400, (desc, r.status_code, r.get_json())


def test_create_job_list_body_rejected(client):
    """A JSON list body must be a clean 400, not a 500 AttributeError."""
    r = client.post("/jobs", json=["not", "an", "object"])
    assert r.status_code == 400


class _FailingPayPal:
    """PayPal client whose order creation always fails."""

    def create_order(self, **kw):
        raise PayPalError("order creation failed")


def test_create_order_failure_leaves_no_job(client):
    """If create_order fails, no orphaned QUOTED job with no order id
    is left in the store."""
    flask_app.config["escrow"] = EscrowService(_FailingPayPal())
    r = client.post("/jobs", json={"brief": "audit the holders, budget $50"})
    assert r.status_code == 503, r.get_json()
    assert client.get("/jobs").get_json() == {"jobs": []}

# ---------------------------------------------------------------------------
# Round 2 fixes (Oct 27, 2026): delivery-hash verification in capture,
# durable job store, forged `now` never accepted from the request path.
# ---------------------------------------------------------------------------

def test_capture_missing_delivery_text_rejected(client):
    """Capture with no delivery_text field: 400. Verification needs the
    original text — there is no default and no silent assumption."""
    job_id = _delivered_job(client)
    r = client.post(f"/jobs/{job_id}/capture", json={"payer": PAYER},
                    headers=PAYER_H)
    assert r.status_code == 400, r.get_json()


def test_capture_wrong_delivery_text_rejected(client):
    """Capture with a delivery text that does not hash to the delivered
    work: 409, and the job stays DELIVERED — the money never moves."""
    job_id = _delivered_job(client)
    r = client.post(f"/jobs/{job_id}/capture",
                    json={"payer": PAYER,
                          "delivery_text": "different work entirely"},
                    headers=PAYER_H)
    assert r.status_code == 409, r.get_json()
    assert "delivery verification failed" in r.get_json()["error"]
    assert client.get(f"/jobs/{job_id}").get_json()["job"]["status"] == "DELIVERED"


def test_void_forged_now_ignored(client):
    """A forged future `now` sent with the void request must not void
    early: the server never takes a timestamp from the request path,
    so the deadline is still evaluated against wall-clock time."""
    body = _create_job(client, deadline_seconds=3600)
    job_id = body["job"]["job_id"]
    _approve_and_authorize(client, job_id, body["paypal_order"]["id"])
    r = client.post(f"/jobs/{job_id}/void",
                    json={"payer": PAYER, "now": 9999999999},
                    headers=PAYER_H)
    assert r.status_code == 409, r.get_json()
    assert "deadline not reached" in r.get_json()["error"]
    assert client.get(f"/jobs/{job_id}").get_json()["job"]["status"] == "AUTHORIZED"


def test_restart_round_trip_over_http(client, mock_server, tmp_path):
    """The whole flow across a simulated server restart, over HTTP:
    create + authorize, swap in a fresh service on the same store file
    (a new process would do the same), then deliver and capture — the
    job, its hold and its hash are all still there."""
    from app.server import hiring as server_hiring
    from app.store import JsonFileStore

    path = tmp_path / "jobs.json"

    def fresh_service():
        return EscrowService(
            PayPalClient(client_id="mock", client_secret="mock",
                         base_url=mock_server),
            store=JsonFileStore(path),
            delivery_verifier=server_hiring.verify_delivery)

    flask_app.config["escrow"] = fresh_service()
    body = _create_job(client)
    job_id = body["job"]["job_id"]
    _approve_and_authorize(client, job_id, body["paypal_order"]["id"])

    # Simulate the restart: a brand-new service on the same file.
    flask_app.config["escrow"] = fresh_service()
    assert client.get(f"/jobs/{job_id}").get_json()["job"]["status"] == "AUTHORIZED"

    r = client.post(f"/jobs/{job_id}/deliver",
                    json={"agent": AUDITOR,
                          "delivery_text": "audit report: top-20 holders"},
                    headers=_agent_headers(AUDITOR))
    assert r.status_code == 200, r.get_json()
    r = client.post(f"/jobs/{job_id}/capture",
                    json={"payer": PAYER,
                          "delivery_text": "audit report: top-20 holders"},
                    headers=PAYER_H)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["receipt"]["outcome"] == "released"


# ---------------------------------------------------------------------------
# Round 3 fixes (Oct 27, 2026): /admin/reconcile endpoint, real capture
# id + amount on reconcile, single-process startup warning.
# ---------------------------------------------------------------------------

def _job_authorization_id(client, job_id):
    # Internal test helper: reads the authorization id from the service
    # directly, NOT through GET /jobs/<id> — that endpoint now returns
    # the public representation (to_public_dict), which deliberately
    # omits PayPal-side ids and idempotency keys.
    svc = flask_app.config["escrow"]
    return svc.get_job(job_id).authorization_id


def _job_internal(job_id):
    """Full internal job state, for test assertions only.

    Reads from the service directly — the open HTTP endpoints now
    return the public representation, so tests that need internal
    fields (capture_id, last_paypal_response, webhook_denial) use this.
    """
    return flask_app.config["escrow"].get_job(job_id)


def _capture_directly(auth_id):
    """Capture an authorization straight through the mock, bypassing
    the escrow — as if the money moved outside the normal capture call
    (the case /admin/reconcile exists to recover)."""
    r = requests.post(
        f"{BASE}/v2/payments/authorizations/{auth_id}/capture",
        headers={"PayPal-Request-Id": "direct-test-capture"}, json={},
        timeout=10)
    assert r.status_code == 201, r.text
    return r.json()


def _void_directly(auth_id):
    r = requests.post(
        f"{BASE}/v2/payments/authorizations/{auth_id}/void",
        headers={"PayPal-Request-Id": "direct-test-void"}, json={},
        timeout=10)
    assert r.status_code == 200, r.text
    return r.json()


def test_admin_reconcile_requires_payer_token(client):
    """No token: 401. Agent token: 403. The admin endpoint is payer-only."""
    body = _create_job(client)
    job_id = body["job"]["job_id"]
    _approve_and_authorize(client, job_id, body["paypal_order"]["id"])
    assert client.post("/admin/reconcile").status_code == 401
    assert client.post("/admin/reconcile",
                       headers=_agent_headers(AUDITOR)).status_code == 403


def test_admin_reconcile_no_open_jobs(client):
    """Nothing open: checked 0, everything else empty."""
    r = client.post("/admin/reconcile", headers=PAYER_H)
    assert r.status_code == 200, r.get_json()
    assert r.get_json() == {"checked": 0, "updated": [],
                            "still_pending": [], "errors": [],
                            "needs_review": []}


def test_admin_reconcile_still_pending_when_created(client):
    """An AUTHORIZED job PayPal still reports CREATED is checked and
    left alone."""
    body = _create_job(client)
    job_id = body["job"]["job_id"]
    _approve_and_authorize(client, job_id, body["paypal_order"]["id"])
    r = client.post("/admin/reconcile", headers=PAYER_H)
    assert r.status_code == 200, r.get_json()
    summary = r.get_json()
    assert summary["checked"] == 1
    assert summary["updated"] == []
    assert summary["still_pending"] == [job_id]
    assert summary["errors"] == []
    assert client.get(f"/jobs/{job_id}").get_json()["job"]["status"] == "AUTHORIZED"


def test_admin_reconcile_captures_delivered_job(client):
    """A DELIVERED job whose hold was captured outside the normal call
    is recorded CAPTURED with the real capture id and amount — and the
    receipt is released, not voided."""
    job_id = _delivered_job(client)
    auth_id = _job_authorization_id(client, job_id)
    direct = _capture_directly(auth_id)
    assert direct["status"] == "COMPLETED"
    # The escrow still thinks the job is DELIVERED — it never saw this.
    assert client.get(f"/jobs/{job_id}").get_json()["job"]["status"] == "DELIVERED"
    r = client.post("/admin/reconcile", headers=PAYER_H)
    assert r.status_code == 200, r.get_json()
    summary = r.get_json()
    assert summary["checked"] == 1
    assert summary["updated"] == [{"job_id": job_id, "from": "DELIVERED",
                                   "to": "CAPTURED",
                                   "capture_id": direct["id"]}]
    assert summary["still_pending"] == []
    got = client.get(f"/jobs/{job_id}").get_json()
    assert got["job"]["status"] == "CAPTURED"
    internal = _job_internal(job_id)
    assert internal.capture_id == direct["id"]
    assert internal.capture_amount_cents == 1250
    assert internal.capture_reconciled is True
    # The open endpoint serves the PUBLIC receipt: outcome and amounts
    # are visible, PayPal-side ids are not.
    assert got["receipt"]["outcome"] == "released"
    assert "capture_id" not in got["receipt"]
    assert "paypal_order_id" not in got["receipt"]
    assert "authorization_id" not in got["receipt"]
    assert got["receipt"]["capture_amount_cents"] == 1250
    assert got["receipt"]["capture_reconciled"] is True


def test_admin_reconcile_voids_authorized_job(client):
    """An AUTHORIZED job whose hold was voided outside the normal call
    is recorded VOIDED."""
    body = _create_job(client)
    job_id = body["job"]["job_id"]
    _approve_and_authorize(client, job_id, body["paypal_order"]["id"])
    auth_id = _job_authorization_id(client, job_id)
    _void_directly(auth_id)
    r = client.post("/admin/reconcile", headers=PAYER_H)
    assert r.status_code == 200, r.get_json()
    summary = r.get_json()
    assert summary["updated"] == [{"job_id": job_id, "from": "AUTHORIZED",
                                   "to": "VOIDED", "capture_id": ""}]
    got = client.get(f"/jobs/{job_id}").get_json()
    assert got["job"]["status"] == "VOIDED"
    assert got["receipt"]["outcome"] == "voided"


def test_single_process_warning_names_jobs_file(tmp_path):
    """The startup warning names the jobs file and the single-process
    limit — an operator running two servers on one file sees it."""
    from app.server import _single_process_warning
    path = tmp_path / "jobs.json"
    warning = _single_process_warning(path)
    assert str(path) in warning
    assert "SINGLE-PROCESS ONLY" in warning
    assert "clobber" in warning

# ---------------------------------------------------------------------------
# Round 4 fixes (Oct 27, 2026): 404 vanished-authorization test, last
# PayPal response persisted on the job (and on the receipt).
# ---------------------------------------------------------------------------

def test_admin_reconcile_vanished_authorization_reports_error(client):
    """A job whose authorization PayPal no longer knows (404, vanished
    authorization) is reported in the admin summary's errors — and the
    job is NOT mutated: it stays AUTHORIZED, still pending, with the
    same authorize record it had. One job's failure never stops the
    rest, and a failed call never overwrites the last good response."""
    body = _create_job(client)
    job_id = body["job"]["job_id"]
    _approve_and_authorize(client, job_id, body["paypal_order"]["id"])
    auth_id = _job_authorization_id(client, job_id)
    before = client.get(f"/jobs/{job_id}").get_json()["job"]
    assert before["status"] == "AUTHORIZED"
    assert _job_internal(job_id).last_paypal_response["operation"] == "authorize"
    # PayPal forgets the authorization (mock-only control).
    r = requests.post(f"{BASE}/mock/forget-authorization/{auth_id}",
                      timeout=10)
    assert r.status_code == 200, r.text
    assert r.json() == {"forgotten": auth_id}
    # And the mock really 404s on it now.
    assert requests.get(f"{BASE}/v2/payments/authorizations/{auth_id}",
                        timeout=10).status_code == 404

    r = client.post("/admin/reconcile", headers=PAYER_H)
    assert r.status_code == 200, r.get_json()
    summary = r.get_json()
    assert summary["checked"] == 1
    assert summary["updated"] == []
    assert summary["still_pending"] == [job_id]
    assert len(summary["errors"]) == 1
    assert summary["errors"][0]["job_id"] == job_id
    assert "RESOURCE_NOT_FOUND" in summary["errors"][0]["error"]
    # The job is byte-for-byte what it was: same status, same ids,
    # same last PayPal response — the 404 changed nothing.
    after = client.get(f"/jobs/{job_id}").get_json()["job"]
    assert after == before


def test_admin_reconcile_vanished_authorization_reports_error_for_delivered(client):
    """Same vanished-authorization guarantee for a DELIVERED job: the
    admin summary reports RESOURCE_NOT_FOUND and the job is
    byte-for-byte unchanged — DELIVERED, same delivery hash, same
    authorize record. Both open states are pinned against
    mutation-on-error (round 5)."""
    job_id = _delivered_job(client)
    auth_id = _job_authorization_id(client, job_id)
    before = client.get(f"/jobs/{job_id}").get_json()["job"]
    assert before["status"] == "DELIVERED"
    internal = _job_internal(job_id)
    assert internal.delivery_hash
    assert internal.last_paypal_response["operation"] == "authorize"
    # PayPal forgets the authorization (mock-only control).
    r = requests.post(f"{BASE}/mock/forget-authorization/{auth_id}",
                      timeout=10)
    assert r.status_code == 200, r.text
    assert r.json() == {"forgotten": auth_id}
    # And the mock really 404s on it now.
    assert requests.get(f"{BASE}/v2/payments/authorizations/{auth_id}",
                        timeout=10).status_code == 404

    r = client.post("/admin/reconcile", headers=PAYER_H)
    assert r.status_code == 200, r.get_json()
    summary = r.get_json()
    assert summary["checked"] == 1
    assert summary["updated"] == []
    assert summary["still_pending"] == [job_id]
    assert len(summary["errors"]) == 1
    assert summary["errors"][0]["job_id"] == job_id
    assert "RESOURCE_NOT_FOUND" in summary["errors"][0]["error"]
    # The job is byte-for-byte what it was: same status, same delivery
    # hash, same last PayPal response — the 404 changed nothing.
    after = client.get(f"/jobs/{job_id}").get_json()["job"]
    assert after == before


def test_job_view_exposes_last_paypal_response(client):
    """The public job view (to_public_dict) must NOT expose the audit
    record or PayPal-side ids; the service still keeps them for
    operators (via to_dict / internal state)."""
    job_id = _delivered_job(client)
    got = client.get(f"/jobs/{job_id}").get_json()["job"]
    # Public view: minimal safe subset only.
    assert "last_paypal_response" not in got
    assert "authorization_id" not in got
    assert "capture_id" not in got
    assert "pending_capture_request_id" not in got
    assert "needs_review" not in got
    # The service still records what PayPal said (operator visibility).
    record = _job_internal(job_id).last_paypal_response
    assert record["operation"] == "authorize"
    assert record["status"] == "CREATED"
    assert record["id"] == _job_internal(job_id).authorization_id
    r = client.post(f"/jobs/{job_id}/capture",
                    json={"payer": PAYER,
                          "delivery_text": "audit report: top-20 holders"},
                    headers=PAYER_H)
    assert r.status_code == 200, r.get_json()
    internal = _job_internal(job_id)
    assert internal.last_paypal_response["operation"] == "capture"
    assert internal.last_paypal_response["status"] == "COMPLETED"
    assert internal.last_paypal_response["id"] == internal.capture_id


# ---------------------------------------------------------------------------
# Round 7 (Oct 28, 2026): POST /webhooks/paypal — PayPal tells the
# service when money moves. Signature verified via PayPal's
# verify-webhook-signature API (mocked); events are reconciled from
# PayPal before any state change and deduped by event id.
# ---------------------------------------------------------------------------

@pytest.fixture()
def webhook_env(monkeypatch):
    monkeypatch.setenv("LAMPLIGHT_PAYPAL_WEBHOOK_ID", "MOCK-WEBHOOK-ID")


def _webhook_headers(event_id: str) -> dict:
    """The five transmission headers PayPal puts on a webhook delivery
    (same shape as scripts/mock_paypal.py _webhook_headers)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "PAYPAL-TRANSMISSION-ID": f"MOCK-TRANSMISSION-{event_id}",
        "PAYPAL-TRANSMISSION-TIME": now,
        "PAYPAL-CERT-URL": "https://api-m.sandbox.paypal.com/v1/"
                           "notifications/certs/CERT-123",
        "PAYPAL-AUTH-ALGO": "SHA256withRSA",
        "PAYPAL-TRANSMISSION-SIG": "MOCK-SIGNATURE",
    }


def _webhook_event(event_type, auth_id, event_id, *, capture_id=None,
                   capture_status="COMPLETED", amount="12.50"):
    """A PayPal-shaped webhook event. Authorization events name the
    authorization directly (resource.id); capture events name it in
    supplementary_data.related_ids.authorization_id, the way PayPal
    surfaces the link."""
    if event_type.startswith("PAYMENT.AUTHORIZATION."):
        resource = {"id": auth_id, "status": "CREATED"}
        resource_type = "authorization"
    else:
        resource = {
            "id": capture_id or "CAPTURE-9999",
            "status": capture_status,
            "amount": {"currency_code": "USD", "value": amount},
            "supplementary_data": {"related_ids": {"authorization_id": auth_id}},
        }
        resource_type = "capture"
    return {
        "id": event_id, "event_version": "1.0",
        "create_time": "2026-10-28T05:00:00Z",
        "resource_type": resource_type,
        "event_type": event_type,
        "summary": f"{event_type} for {auth_id}",
        "resource": resource,
    }


def _post_webhook(client, event, verification="SUCCESS"):
    """Post a webhook event to the service with the transmission
    headers, setting the mock's one-shot verification outcome first
    (consumed by the verify call the endpoint makes)."""
    requests.post(f"{BASE}/mock/next-webhook-verification",
                  json={"verification_status": verification},
                  timeout=10).raise_for_status()
    return client.post("/webhooks/paypal", json=event,
                       headers=_webhook_headers(event["id"]))


def test_webhook_capture_completed_settles_delivered_job(client, webhook_env):
    """A CAPTURE.COMPLETED webhook settles a DELIVERED job whose hold
    was captured outside the normal call: the job is recorded CAPTURED
    with the real capture id and amount, and the receipt is released."""
    job_id = _delivered_job(client)
    auth_id = _job_authorization_id(client, job_id)
    direct = _capture_directly(auth_id)
    assert direct["status"] == "COMPLETED"
    assert client.get(f"/jobs/{job_id}").get_json()["job"]["status"] == "DELIVERED"
    event = _webhook_event("PAYMENT.CAPTURE.COMPLETED", auth_id, "WH-HTTP-1",
                           capture_id=direct["id"])
    r = _post_webhook(client, event)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["status"] == "ok", r.get_json()
    got = client.get(f"/jobs/{job_id}").get_json()
    assert got["job"]["status"] == "CAPTURED"
    internal = _job_internal(job_id)
    assert internal.capture_id == direct["id"]
    assert internal.capture_amount_cents == 1250
    assert internal.capture_reconciled is True
    assert got["receipt"]["outcome"] == "released"
    # Public receipt: no PayPal-side ids.
    assert "capture_id" not in got["receipt"]


def test_webhook_authorization_voided_voids_authorized_job(client, webhook_env):
    """An AUTHORIZATION.VOIDED webhook voids an AUTHORIZED job whose
    hold was voided outside the normal call."""
    body = _create_job(client)
    job_id = body["job"]["job_id"]
    _approve_and_authorize(client, job_id, body["paypal_order"]["id"])
    auth_id = _job_authorization_id(client, job_id)
    _void_directly(auth_id)
    event = _webhook_event("PAYMENT.AUTHORIZATION.VOIDED", auth_id, "WH-HTTP-2")
    r = _post_webhook(client, event)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["status"] == "ok", r.get_json()
    got = client.get(f"/jobs/{job_id}").get_json()
    assert got["job"]["status"] == "VOIDED"
    assert got["receipt"]["outcome"] == "voided"


def test_webhook_capture_denied_stamps_denial_on_delivered_job(client, webhook_env):
    """Grok v7.1: a PAYMENT.CAPTURE.DENIED webhook on a DELIVERED job
    must stamp job.webhook_denial and leave the job DELIVERED (open).
    The gateway now routes through EscrowService.handle_webhook_event
    instead of reconcile_job, which never recorded denials."""
    job_id = _delivered_job(client)
    auth_id = _job_authorization_id(client, job_id)
    # The job is DELIVERED before the denial.
    assert client.get(f"/jobs/{job_id}").get_json()["job"]["status"] == "DELIVERED"
    event = _webhook_event("PAYMENT.CAPTURE.DENIED", auth_id, "WH-HTTP-DENY-1",
                           capture_status="DENIED")
    r = _post_webhook(client, event)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["status"] == "ok", r.get_json()
    got = client.get(f"/jobs/{job_id}").get_json()
    # Job stays DELIVERED (open) — a denial never settles.
    assert got["job"]["status"] == "DELIVERED"
    # The denial is stamped on the job (internal state, not public).
    denial = _job_internal(job_id).webhook_denial
    assert denial is not None
    assert denial["status"] == "DENIED"


def test_webhook_authorization_created_confirms_hold(client, webhook_env):
    """An AUTHORIZATION.CREATED webhook on an AUTHORIZED job: the
    gateway reconciles via fresh read, confirming the hold is still
    there. No state change."""
    body = _create_job(client)
    job_id = body["job"]["job_id"]
    _approve_and_authorize(client, job_id, body["paypal_order"]["id"])
    auth_id = _job_authorization_id(client, job_id)
    event = _webhook_event("PAYMENT.AUTHORIZATION.CREATED", auth_id, "WH-HTTP-3")
    r = _post_webhook(client, event)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["status"] == "ok", r.get_json()
    got = client.get(f"/jobs/{job_id}").get_json()["job"]
    assert got["status"] == "AUTHORIZED"


def test_webhook_capture_denied_leaves_job_open(client, webhook_env):
    """A CAPTURE.DENIED webhook leaves the job open — the denial is
    recorded via reconcile, and the hold is still there."""
    job_id = _delivered_job(client)
    auth_id = _job_authorization_id(client, job_id)
    event = _webhook_event("PAYMENT.CAPTURE.DENIED", auth_id, "WH-HTTP-4",
                           capture_id="CAPTURE-DENIED-1",
                           capture_status="DENIED")
    r = _post_webhook(client, event)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["status"] == "ok", r.get_json()
    got = client.get(f"/jobs/{job_id}").get_json()["job"]
    assert got["status"] == "DELIVERED"


def test_webhook_invalid_signature_rejected(client, webhook_env):
    """A webhook whose signature PayPal says is invalid: 400, and the
    job is untouched — nothing is decided on an unverified event."""
    job_id = _delivered_job(client)
    auth_id = _job_authorization_id(client, job_id)
    event = _webhook_event("PAYMENT.CAPTURE.COMPLETED", auth_id, "WH-HTTP-5",
                           capture_id="CAPTURE-9999")
    r = _post_webhook(client, event, verification="FAILURE")
    assert r.status_code == 400, r.get_json()
    assert client.get(f"/jobs/{job_id}").get_json()["job"]["status"] == "DELIVERED"


def test_webhook_missing_signature_headers_rejected(client, webhook_env):
    """A webhook with no transmission headers at all: 400 — the
    signature cannot be verified, so nothing is processed."""
    job_id = _delivered_job(client)
    auth_id = _job_authorization_id(client, job_id)
    event = _webhook_event("PAYMENT.CAPTURE.COMPLETED", auth_id, "WH-HTTP-6",
                           capture_id="CAPTURE-9999")
    r = client.post("/webhooks/paypal", json=event)
    assert r.status_code == 400, r.get_json()
    assert client.get(f"/jobs/{job_id}").get_json()["job"]["status"] == "DELIVERED"


def test_webhook_replayed_event_is_noop(client, webhook_env):
    """The same event id delivered twice over HTTP: the second
    delivery is a duplicate no-op, and the job is byte-for-byte what
    the first delivery left it."""
    job_id = _delivered_job(client)
    auth_id = _job_authorization_id(client, job_id)
    direct = _capture_directly(auth_id)
    event = _webhook_event("PAYMENT.CAPTURE.COMPLETED", auth_id, "WH-HTTP-7",
                           capture_id=direct["id"])
    first = _post_webhook(client, event)
    assert first.status_code == 200, first.get_json()
    assert first.get_json()["status"] == "ok", first.get_json()
    before = client.get(f"/jobs/{job_id}").get_json()
    second = _post_webhook(client, event)
    assert second.status_code == 200, second.get_json()
    assert second.get_json()["status"] == "duplicate", second.get_json()
    assert client.get(f"/jobs/{job_id}").get_json() == before


def test_webhook_unknown_event_type_ignored(client, webhook_env):
    """An event type the service does not understand: 200, status
    unknown, no state change — the unknown type is logged and the
    job is untouched."""
    body = _create_job(client)
    job_id = body["job"]["job_id"]
    _approve_and_authorize(client, job_id, body["paypal_order"]["id"])
    event = {"id": "WH-HTTP-8", "event_version": "1.0",
             "create_time": "2026-10-28T05:00:00Z",
             "resource_type": "sale", "event_type": "PAYMENT.SALE.COMPLETED",
             "summary": "a sale completed",
             "resource": {"id": "SALE-1", "status": "COMPLETED"}}
    r = _post_webhook(client, event)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["status"] == "unknown", r.get_json()
    assert client.get(f"/jobs/{job_id}").get_json()["job"]["status"] == "AUTHORIZED"


def test_webhook_unknown_authorization_left_pending(client, webhook_env):
    """An event naming an authorization no job carries: 200, status
    unknown, logged and left pending — no crash, no state change, and
    the admin reconcile endpoint remains the fallback for it."""
    event = _webhook_event("PAYMENT.CAPTURE.COMPLETED", "AUTH-UNKNOWN",
                           "WH-HTTP-9", capture_id="CAPTURE-9999")
    r = _post_webhook(client, event)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["status"] == "unknown", r.get_json()
    assert client.get("/jobs").get_json() == {"jobs": []}


def test_webhook_capture_completed_when_auth_still_created_is_ignored(client, webhook_env):
    """A CAPTURE.COMPLETED event whose authorization PayPal still
    reports CREATED is spurious/premature: the read contradicts the
    event, the hold is still there, and the job stays DELIVERED —
    the event body alone never settles a job."""
    job_id = _delivered_job(client)
    auth_id = _job_authorization_id(client, job_id)
    event = _webhook_event("PAYMENT.CAPTURE.COMPLETED", auth_id, "WH-HTTP-10",
                           capture_id="CAPTURE-9999")
    r = _post_webhook(client, event)
    assert r.status_code == 200, r.get_json()
    # The gateway reconciles via fresh read; a premature event yields
    # unknown/no-op, not a settlement.
    assert r.get_json()["status"] in ("unknown", "ok"), r.get_json()
    assert client.get(f"/jobs/{job_id}").get_json()["job"]["status"] == "DELIVERED"


def test_webhook_requires_webhook_id_configured(client, monkeypatch):
    """Without LAMPLIGHT_PAYPAL_WEBHOOK_ID the endpoint is 503: the
    signature cannot be verified, so no event is accepted."""
    monkeypatch.delenv("LAMPLIGHT_PAYPAL_WEBHOOK_ID", raising=False)
    event = _webhook_event("PAYMENT.CAPTURE.COMPLETED", "AUTH-1", "WH-HTTP-11",
                           capture_id="CAPTURE-9999")
    r = client.post("/webhooks/paypal", json=event,
                    headers=_webhook_headers(event["id"]))
    assert r.status_code == 503, r.get_json()
    assert "LAMPLIGHT_PAYPAL_WEBHOOK_ID" in r.get_json()["error"]


def test_webhook_missing_event_id_rejected(client, webhook_env):
    """An event body with no id: the new gateway uses the transmission
    ID (header) for dedupe, not the event id — so this is processed
    normally, not rejected. The resource id is extracted from the
    resource object."""
    event = {"event_type": "PAYMENT.CAPTURE.COMPLETED",
             "resource": {"id": "CAPTURE-1"}}
    r = client.post("/webhooks/paypal", json=event,
                    headers=_webhook_headers("WH-HTTP-12"))
    # 200 with unknown (no job has CAPTURE-1) — not a 400 rejection.
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["status"] == "unknown", r.get_json()

# ---------------------------------------------------------------------------
# Round 8 (Oct 28, 2026): POST /jobs/<id>/capture-partial, and the
# end-to-end webhook integration test — a full delivery through the
# mock sender into a short-lived REAL Flask server process.
# ---------------------------------------------------------------------------

def test_capture_partial_over_http(client):
    """Capture $8.00 of a $12.50 hold over HTTP with final_capture=True:
    200, receipt released with the split (captured 800 + voided 450 =
    the hold), job CAPTURED, and the mock authorization ends CAPTURED
    (residual auto-released, no void POST)."""
    job_id = _delivered_job(client)
    r = client.post(f"/jobs/{job_id}/capture-partial",
                    json={"payer": PAYER,
                          "delivery_text": "audit report: top-20 holders",
                          "amount_cents": 800},
                    headers=PAYER_H)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["job"]["status"] == "CAPTURED"
    assert body["job"]["capture_amount_cents"] == 800
    assert body["job"]["voided_amount_cents"] == 450
    receipt = body["receipt"]
    assert receipt["outcome"] == "released"
    assert receipt["capture_id"].startswith("CAPTURE-")
    assert receipt["capture_amount_cents"] == 800
    assert receipt["voided_amount_cents"] == 450
    assert receipt["receipt_hash"].startswith("0x")
    auth_id = _job_authorization_id(client, job_id)
    auth = requests.get(f"{BASE}/v2/payments/authorizations/{auth_id}",
                        timeout=10).json()
    assert auth["status"] == "CAPTURED"
    assert len(auth["captures"]) == 1
    assert auth["captures"][0]["amount"] == {"currency_code": "USD",
                                             "value": "8.00"}


def test_capture_partial_full_amount_degenerates_over_http(client):
    """An amount equal to the full hold over HTTP is a normal capture:
    200, receipt released with the full amount and voided None."""
    job_id = _delivered_job(client)
    r = client.post(f"/jobs/{job_id}/capture-partial",
                    json={"payer": PAYER,
                          "delivery_text": "audit report: top-20 holders",
                          "amount_cents": 1250},
                    headers=PAYER_H)
    assert r.status_code == 200, r.get_json()
    receipt = r.get_json()["receipt"]
    assert receipt["outcome"] == "released"
    assert receipt["capture_amount_cents"] == 1250
    assert receipt["voided_amount_cents"] is None


@pytest.mark.parametrize("amount_cents,expected", [
    ("800", 400),      # non-integer amount: bad input
    (None, 400),       # missing amount: bad input
    (True, 400),       # boolean is not an integer amount
    (0, 409),          # zero: semantic refusal from the service
    (-50, 409),        # negative: semantic refusal
    (1251, 409),       # more than the hold: semantic refusal
])
def test_capture_partial_bad_amounts_over_http(client, amount_cents, expected):
    """Bad amounts over HTTP: a non-integer or missing amount is a 400
    (bad input); zero, negative, or over-hold is a 409 (the service
    refuses the operation) — and the job stays DELIVERED either way."""
    job_id = _delivered_job(client)
    body = {"payer": PAYER, "delivery_text": "audit report: top-20 holders"}
    if amount_cents is not None:
        body["amount_cents"] = amount_cents
    r = client.post(f"/jobs/{job_id}/capture-partial", json=body,
                    headers=PAYER_H)
    assert r.status_code == expected, r.get_json()
    assert client.get(f"/jobs/{job_id}").get_json()["job"]["status"] == "DELIVERED"


def test_capture_partial_wrong_delivery_text_over_http(client):
    """A partial capture with the wrong delivery text: 409, job stays
    DELIVERED, money never moves."""
    job_id = _delivered_job(client)
    r = client.post(f"/jobs/{job_id}/capture-partial",
                    json={"payer": PAYER,
                          "delivery_text": "different work entirely",
                          "amount_cents": 800},
                    headers=PAYER_H)
    assert r.status_code == 409, r.get_json()
    assert "delivery verification failed" in r.get_json()["error"]
    assert client.get(f"/jobs/{job_id}").get_json()["job"]["status"] == "DELIVERED"


def test_capture_partial_requires_payer_token(client):
    """No token: 401. Agent token: 403. The partial-capture endpoint
    is payer-only, like the full capture."""
    job_id = _delivered_job(client)
    body = {"payer": PAYER, "delivery_text": "audit report: top-20 holders",
            "amount_cents": 800}
    assert client.post(f"/jobs/{job_id}/capture-partial",
                       json=body).status_code == 401
    assert client.post(f"/jobs/{job_id}/capture-partial", json=body,
                       headers=_agent_headers(AUDITOR)).status_code == 403


def _free_port() -> int:
    """An ephemeral localhost port, for the live-server subprocess."""
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_webhook_end_to_end_through_mock_sender_into_live_server(mock_server, tmp_path):
    """The full webhook delivery end-to-end, over real HTTP: a real
    Flask server process (short-lived, its own jobs file, pointed at
    the mock PayPal) runs the whole flow — create, approve, authorize,
    deliver — then the money moves outside the service (direct mock
    capture) and the mock sender delivers the CAPTURE.COMPLETED event
    to the live server with PayPal's five transmission headers. The
    server verifies the signature against the mock verify endpoint
    and the job settles CAPTURED. Then a second delivery whose
    verification cannot be completed at all (the mock answers 500)
    gets a 503 — PayPal retries — and the job is untouched by the
    unverified delivery.

    Correction to the brief: a verification that returns FAILURE is a
    401 (the signature is invalid — already covered by
    test_webhook_invalid_signature_rejected), not a 503. The 503 is
    for verification that cannot be completed, and this test proves
    that path with the mock's ERROR control."""
    import json as _json
    import os
    import subprocess as _subprocess

    root = pathlib.Path(__file__).resolve().parent.parent
    live_port = _free_port()
    live = f"http://127.0.0.1:{live_port}"
    env = dict(os.environ)
    env.update({
        "PORT": str(live_port),
        "LAMPLIGHT_JOBS_FILE": str(tmp_path / "jobs.json"),
        "LAMPLIGHT_PAYPAL_WEBHOOK_ID": "MOCK-WEBHOOK-ID",
        "PAYPAL_CLIENT_ID": "mock",
        "PAYPAL_CLIENT_SECRET": "mock",
        "PAYPAL_BASE_URL": mock_server,
        # LOW #8: production refuses "test-secret" — the live server
        # test uses a dedicated non-test value.
        "DISPATCHER_INTENT_SECRET": "live-server-test-secret-abc123",
    })
    proc = _subprocess.Popen(
        [sys.executable, "-m", "app.server"], cwd=str(root), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                if requests.get(f"{live}/", timeout=2).status_code == 200:
                    break
            except requests.RequestException:
                time.sleep(0.3)
        else:
            raise RuntimeError("live server did not come up")

        # The whole flow over real HTTP against the live server.
        # v8.14: the storefront gate is live here (not TESTING mode) —
        # the payer token passes the gate.
        r = requests.post(f"{live}/jobs",
                          json={"brief": "audit the top-20 holders, budget $50"},
                          headers=PAYER_H,
                          timeout=10)
        assert r.status_code == 201, r.text
        body = r.json()
        job_id = body["job"]["job_id"]
        order_id = body["paypal_order"]["id"]
        requests.post(f"{BASE}/mock/approve/{order_id}",
                      timeout=10).raise_for_status()
        r = requests.post(f"{live}/jobs/{job_id}/authorize", headers=PAYER_H,
                          timeout=10)
        assert r.status_code == 200, r.text
        # The authenticated authorize response carries the full job view.
        auth_id = r.json()["job"]["authorization_id"]
        r = requests.post(
            f"{live}/jobs/{job_id}/deliver",
            json={"agent": AUDITOR,
                  "delivery_text": "audit report: top-20 holders"},
            headers=_agent_headers(AUDITOR), timeout=10)
        assert r.status_code == 200, r.text

        # The money moves outside the service (direct mock capture).
        direct = _capture_directly(auth_id)
        assert direct["status"] == "COMPLETED"
        assert requests.get(f"{live}/jobs/{job_id}",
                            timeout=10).json()["job"]["status"] == "DELIVERED"

        # The mock sender delivers the event to the live server, the
        # way PayPal does — five transmission headers and all.
        event = _webhook_event("PAYMENT.CAPTURE.COMPLETED", auth_id,
                               "WH-E2E-1", capture_id=direct["id"])
        r = requests.post(f"{BASE}/mock/send-webhook",
                          json={"target_url": f"{live}/webhooks/paypal",
                                "event": event}, timeout=10)
        assert r.status_code == 200, r.text
        delivered = r.json()
        assert delivered["target_status"] == 200, delivered
        assert _json.loads(delivered["target_body"])["status"] == "ok"
        got = requests.get(f"{live}/jobs/{job_id}", timeout=10).json()
        assert got["job"]["status"] == "CAPTURED"
        assert "capture_id" not in got["job"]  # public view hides it
        assert got["receipt"]["outcome"] == "released"

        # Verification that cannot be completed: the mock answers 500,
        # the server answers 503, and the job is untouched. (Claude v5
        # review: the settled-job check now runs AFTER signature
        # verification, so even a settled job with unverifiable
        # delivery gets 503 — a forged webhook on a settled job must
        # not get a free 200 + dedupe-cache pollution.)
        requests.post(f"{BASE}/mock/next-webhook-verification",
                      json={"verification_status": "ERROR"},
                      timeout=10).raise_for_status()
        event2 = _webhook_event("PAYMENT.AUTHORIZATION.VOIDED", auth_id,
                                "WH-E2E-2")
        r = requests.post(f"{BASE}/mock/send-webhook",
                          json={"target_url": f"{live}/webhooks/paypal",
                                "event": event2}, timeout=10)
        assert r.status_code == 200, r.text
        delivered = r.json()
        # Verification runs before the settled check: unverifiable
        # delivery → 503 even though the job is already CAPTURED.
        # The 503-on-verification-failure path is also covered in
        # test_webhooks.py unit tests.
        assert delivered["target_status"] == 503, delivered
        assert requests.get(f"{live}/jobs/{job_id}",
                            timeout=10).json()["job"]["status"] == "CAPTURED"
    finally:
        proc.terminate()


# ---------------------------------------------------------------------------
# Round 9 (Oct 28, 2026): Claude final-gate fixes — buyer-data stripping
# on reads and receipts, non-string delivery text, PARTIALLY_CAPTURED
# webhook handling, and the escrow() first-request lock.
# ---------------------------------------------------------------------------

def test_buyer_payload_stripped_from_reads_and_receipts(client):
    """Claude finding 2 (Medium): the mock authorize response now
    carries a payer block and payment_source (the way real PayPal
    does). Neither may appear in a GET response or in a receipt —
    the service strips them before recording, and again on the way
    out over HTTP. The buyer email must not appear anywhere in the
    capture response body.
    Round 10: the mock authorize response also carries a shipping
    block (buyer name + address) — the round-9 denylist let that
    through. The allowlist drops it too: no shipping key, no buyer
    name and no street address anywhere in the GET or the receipt."""
    import json

    job_id = _delivered_job(client)
    got = client.get(f"/jobs/{job_id}").get_json()["job"]
    # The public view carries no PayPal record at all (to_public_dict).
    assert "last_paypal_response" not in got
    assert "Mock Buyer" not in json.dumps(got)
    assert "1 Mock Street" not in json.dumps(got)
    # The stored record itself is stripped of buyer PII at record time.
    record = _job_internal(job_id).last_paypal_response
    assert record["operation"] == "authorize"
    assert "payer" not in record["body"]
    assert "payment_source" not in record["body"]
    assert "shipping" not in record["body"]
    r = client.post(f"/jobs/{job_id}/capture",
                    json={"payer": PAYER,
                          "delivery_text": "audit report: top-20 holders"},
                    headers=PAYER_H)
    assert r.status_code == 200, r.get_json()
    receipt = r.get_json()["receipt"]
    assert "payer" not in receipt["last_paypal_response"]["body"]
    assert "payment_source" not in receipt["last_paypal_response"]["body"]
    assert "shipping" not in receipt["last_paypal_response"]["body"]
    assert "buyer@lamplight.example" not in json.dumps(r.get_json())
    assert "Mock Buyer" not in json.dumps(r.get_json())
    assert "1 Mock Street" not in json.dumps(r.get_json())


def test_deliver_non_string_delivery_text_rejected(client):
    """Claude finding 4 (Low): POST /deliver with a JSON number as
    delivery_text returns 400, not a 500 — and the job is left
    AUTHORIZED, unchanged."""
    body = _create_job(client)
    job_id = body["job"]["job_id"]
    _approve_and_authorize(client, job_id, body["paypal_order"]["id"])
    r = client.post(f"/jobs/{job_id}/deliver",
                    json={"agent": AUDITOR, "delivery_text": 123},
                    headers=_agent_headers(AUDITOR))
    assert r.status_code == 400, r.get_json()
    assert client.get(f"/jobs/{job_id}").get_json()["job"]["status"] == "AUTHORIZED"


def test_webhook_partially_captured_authorization_surfaces_wedge_error(client, webhook_env):
    """Round 14 (Claude round-13 Low 2), HTTP webhook path: a partial
    capture applied outside the normal call leaves the authorization
    PARTIALLY_CAPTURED — and, like real PayPal, the mock lists no
    captures on it. The gateway's reconcile fails (no recorded capture,
    none in flight), so it answers 503 — PayPal retries the delivery
    later when the data may be consistent. The job stays DELIVERED."""
    job_id = _delivered_job(client)
    auth_id = _job_authorization_id(client, job_id)
    requests.post(f"{BASE}/mock/next-partial-status",
                  json={"status": "PARTIALLY_CAPTURED"},
                  timeout=10).raise_for_status()
    r = requests.post(
        f"{BASE}/v2/payments/authorizations/{auth_id}/capture",
        headers={"PayPal-Request-Id": "direct-partial-9"},
        json={"amount": {"currency_code": "USD", "value": "3.00"}},
        timeout=10)
    assert r.status_code == 201, r.text
    assert requests.get(f"{BASE}/v2/payments/authorizations/{auth_id}",
                        timeout=10).json()["status"] == "PARTIALLY_CAPTURED"
    event = _webhook_event("PAYMENT.CAPTURE.COMPLETED", auth_id,
                           "WH-PC-HTTP-1", capture_id=r.json()["id"])
    resp = _post_webhook(client, event)
    # 200: reconcile surfaced a wedge (definitive business outcome, not a
    # transport failure). Marked as processed to avoid PayPal's 25x retry
    # storm — the review flag records the wedge for operator review.
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["status"] == "ok", body
    # Grok v7.1: gateway now uses handle_webhook_event; the wedge
    # surfaces in body["error"], not body["reconcile"]["error"].
    assert "PARTIALLY" in body.get("error", ""), body
    assert client.get(f"/jobs/{job_id}").get_json()["job"]["status"] == "DELIVERED"


def test_escrow_singleton_under_concurrent_first_requests(client, mock_server,
                                                          tmp_path, monkeypatch):
    """Claude finding 6 (Low): two simultaneous first requests build
    one service, not two — escrow() is lock-guarded (round 9).
    Deterministic for the fixed code (every thread gets the identical
    object); as a regression test it is weak — without the lock the
    race window is tiny and the test would usually pass anyway — but
    it pins the property the fix guarantees."""
    import threading

    from app.server import escrow as escrow_fn

    monkeypatch.setenv("PAYPAL_CLIENT_ID", "mock")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "mock")
    monkeypatch.setenv("PAYPAL_BASE_URL", mock_server)
    monkeypatch.setenv("LAMPLIGHT_JOBS_FILE", str(tmp_path / "jobs.json"))
    flask_app.config["escrow"] = None
    try:
        barrier = threading.Barrier(4)
        results: list = []

        def build():
            barrier.wait(timeout=10)
            results.append(escrow_fn())

        threads = [threading.Thread(target=build) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert len(results) == 4, results
        assert all(s is results[0] for s in results)
    finally:
        flask_app.config["escrow"] = None


# ---------------------------------------------------------------------------
# Round 10 (Oct 28, 2026): Claude final-gate re-review of round 9 —
# NEEDS WORK, six findings. Finding 5's cheap half (body cap, open
# jobs cap) lives here; token auth on these endpoints is held.
# ---------------------------------------------------------------------------

def test_oversized_request_body_rejected(client, monkeypatch):
    """Claude finding 5 (Low/Medium): the unauthenticated endpoints
    had no body cap. Flask now answers 413 for a body over
    MAX_CONTENT_LENGTH instead of parsing an unbounded one. The cap
    is shrunk for the test so the check does not need a real
    megabyte."""
    monkeypatch.setitem(flask_app.config, "MAX_CONTENT_LENGTH", 100)
    r = client.post("/jobs", json={"brief": "x" * 200})
    assert r.status_code == 413, r.status_code


def test_open_jobs_cap_refuses_new_jobs(client, monkeypatch):
    """Claude finding 5 (Low/Medium): creating a job makes a PayPal
    order and rewrites the whole jobs file, and POST /jobs has no
    auth — an unbounded creation loop would pile up orders and jobs
    without limit. Past the cap the endpoint answers 429, and the
    jobs it refused were never created. The cap is shrunk for the
    test; settled jobs do not count toward it."""
    import app.server as server_module

    monkeypatch.setattr(server_module, "MAX_OPEN_JOBS", 2)
    _create_job(client)
    _create_job(client)
    r = client.post("/jobs", json={"brief": "audit the top-20 holders, budget $50"})
    assert r.status_code == 429, r.get_json()
    assert r.get_json()["error"] == "too many open jobs"
    assert len(client.get("/jobs").get_json()["jobs"]) == 2


def test_expired_quotes_not_counted_toward_open_jobs_cap(client, monkeypatch):
    """Claude finding 2 (Medium), round 11: QUOTED jobs whose quote
    has expired do not count toward MAX_OPEN_JOBS — and they are
    deleted when seen, so the cap can never be filled by dead
    negotiations. Two jobs created with an immediately-expiring
    quote, then a third job after they expire: 201, and the expired
    jobs are gone from GET /jobs."""
    import app.server as server_module

    monkeypatch.setattr(server_module, "MAX_OPEN_JOBS", 2)
    _create_job(client, ttl_seconds=0)
    _create_job(client, ttl_seconds=0)
    time.sleep(1.1)  # both quotes expire (expires_at == creation second)
    r = client.post("/jobs", json={"brief": "audit the top-20 holders, budget $50"})
    assert r.status_code == 201, r.get_json()
    jobs = client.get("/jobs").get_json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == r.get_json()["job"]["job_id"]


def test_partial_capture_dropped_response_recovers_over_http(client):
    """Round 11 mock control: /mock/drop-next-partial-capture drops
    only a partial capture's response; the service recovers via its
    idempotent retry and settles the job on the split."""
    job_id = _delivered_job(client)
    assert requests.post(f"{BASE}/mock/drop-next-partial-capture",
                         timeout=10).status_code == 200
    r = client.post(f"/jobs/{job_id}/capture-partial",
                    json={"payer": PAYER,
                          "delivery_text": "audit report: top-20 holders",
                          "amount_cents": 800},
                    headers=PAYER_H)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["job"]["status"] == "CAPTURED"
    assert body["job"]["capture_amount_cents"] == 800
    assert body["job"]["voided_amount_cents"] == 450


# ---------------------------------------------------------------------------
# Round 20 (Oct 30, 2026): POST /jobs/<id>/force-settle — payer-only exit
# from a needs_review wedge.
# ---------------------------------------------------------------------------

def test_force_settle_requires_payer_token(client):
    body = _create_job(client, brief="write a research summary, budget $20")
    job_id = body["job"]["job_id"]
    r = client.post(f"/jobs/{job_id}/force-settle", json={"payer": PAYER})
    assert r.status_code == 401


def test_force_settle_rejects_wrong_payer_field(client):
    body = _create_job(client, brief="write a research summary, budget $20")
    job_id = body["job"]["job_id"]
    r = client.post(f"/jobs/{job_id}/force-settle",
                    json={"payer": "mallory@example"}, headers=PAYER_H)
    assert r.status_code == 403


def test_force_settle_rejects_non_wedged_job(client):
    body = _create_job(client, brief="write a research summary, budget $20")
    job_id = body["job"]["job_id"]
    order_id = body["paypal_order"]["id"]
    _approve_and_authorize(client, job_id, order_id)
    r = client.post(f"/jobs/{job_id}/force-settle", json={"payer": PAYER},
                    headers=PAYER_H)
    assert r.status_code == 409
    assert "not wedged" in r.get_json()["error"]


def test_force_settle_flow_over_http(client):
    """Round 20: the payer exits a wedge over HTTP — 200, settled,
    flag cleared, receipt served."""
    from app.server import app as flask_app
    body = _create_job(client, brief="write a research summary, budget $20")
    job_id = body["job"]["job_id"]
    order_id = body["paypal_order"]["id"]
    _approve_and_authorize(client, job_id, order_id)
    svc = flask_app.config["escrow"]
    job = svc.get_job(job_id)
    job.needs_review = True
    job.needs_review_reason = "test wedge"
    svc._save(job)
    r = client.post(f"/jobs/{job_id}/force-settle", json={"payer": PAYER},
                    headers=PAYER_H)
    assert r.status_code == 200, r.get_json()
    data = r.get_json()
    assert data["job"]["status"] == "VOIDED"
    assert data["job"]["needs_review"] is False
    assert data["job"]["needs_review_reason"] is None
    assert data["receipt"]["outcome"] == "voided"


def test_boot_refuses_multi_worker():
    """Grok v7.1: the server must REFUSE (raise) on multi-worker config,
    not just warn. The JSON store is single-process only."""
    import os
    from unittest import mock
    from app import server as srv

    # WEB_CONCURRENCY=2 → refuse.
    with mock.patch.dict(os.environ, {"WEB_CONCURRENCY": "2"}):
        with pytest.raises(RuntimeError, match="refusing to boot with multiple workers"):
            srv._refuse_multi_worker()

    # GUNICORN_WORKERS=4 → refuse.
    with mock.patch.dict(os.environ, {"GUNICORN_WORKERS": "4"}):
        with pytest.raises(RuntimeError, match="refusing to boot with multiple workers"):
            srv._refuse_multi_worker()

    # Single worker → OK.
    with mock.patch.dict(os.environ, {"WEB_CONCURRENCY": "1"}):
        srv._refuse_multi_worker()  # should not raise

    # Unset → OK.
    with mock.patch.dict(os.environ, {}, clear=False):
        for var in ("WEB_CONCURRENCY", "GUNICORN_WORKERS", "LAMPLIGHT_WORKERS"):
            os.environ.pop(var, None)
        srv._refuse_multi_worker()  # should not raise


# ---------------------------------------------------------------------------
# v8.12 (Nov 3, 2026): POST /jobs/<id>/dispute — payer-filed dispute,
# the missing half of the 48h inspection window.
# ---------------------------------------------------------------------------

def test_dispute_requires_payer_token(client):
    job_id = _delivered_job(client)
    r = client.post(f"/jobs/{job_id}/dispute",
                    json={"payer": PAYER, "reason": "work is wrong"})
    assert r.status_code == 401


def test_dispute_rejects_wrong_payer_field(client):
    job_id = _delivered_job(client)
    r = client.post(f"/jobs/{job_id}/dispute",
                    json={"payer": "mallory@example", "reason": "work is wrong"},
                    headers=PAYER_H)
    assert r.status_code == 403


def test_dispute_requires_reason(client):
    job_id = _delivered_job(client)
    r = client.post(f"/jobs/{job_id}/dispute",
                    json={"payer": PAYER}, headers=PAYER_H)
    assert r.status_code == 400
    r = client.post(f"/jobs/{job_id}/dispute",
                    json={"payer": PAYER, "reason": "  "}, headers=PAYER_H)
    assert r.status_code == 409  # empty reason rejected by the service


def test_dispute_rejects_non_delivered_job(client):
    body = _create_job(client, brief="write a research summary, budget $20")
    job_id = body["job"]["job_id"]
    _approve_and_authorize(client, job_id, body["paypal_order"]["id"])
    # AUTHORIZED, not DELIVERED — nothing to dispute yet.
    r = client.post(f"/jobs/{job_id}/dispute",
                    json={"payer": PAYER, "reason": "too slow"},
                    headers=PAYER_H)
    assert r.status_code == 409
    assert "DELIVERED" in r.get_json()["error"]


def test_dispute_flow_over_http(client):
    """v8.12: the payer disputes a DELIVERED job — 200, needs_review set,
    auto-capture blocked, and the reason is on the record."""
    from app.server import app as flask_app
    job_id = _delivered_job(client)
    r = client.post(f"/jobs/{job_id}/dispute",
                    json={"payer": PAYER, "reason": "delivery does not match the brief"},
                    headers=PAYER_H)
    assert r.status_code == 200, r.get_json()
    data = r.get_json()
    assert data["dispute"]["needs_review"] is True
    assert data["dispute"]["reason"] == "delivery does not match the brief"
    # to_public_dict() deliberately excludes needs_review (v8.2 F3) —
    # verify the flag on the stored job instead.
    # The sweeper's auto-capture must skip this job.
    svc = flask_app.config["escrow"]
    job = svc.get_job(job_id)
    assert job.needs_review is True
    assert "payer pause" in job.needs_review_reason


def test_dispute_rejects_double_dispute(client):
    job_id = _delivered_job(client)
    r = client.post(f"/jobs/{job_id}/dispute",
                    json={"payer": PAYER, "reason": "first"},
                    headers=PAYER_H)
    assert r.status_code == 200
    r = client.post(f"/jobs/{job_id}/dispute",
                    json={"payer": PAYER, "reason": "second"},
                    headers=PAYER_H)
    assert r.status_code == 409
    assert "already under review" in r.get_json()["error"]


def test_dispute_blocks_auto_capture(client):
    """v8.13: a disputed job is skipped by check_inspection_deadlines
    WHILE the dispute is under review — the point of the endpoint.
    (v8.12 asserted the skip was forever; v8.13 gives the dispute a
    deadline, after which the sweeper captures. See
    tests/test_v813_dispute_deadline.py for the deadline behavior.)"""
    import time
    from app.server import app as flask_app
    job_id = _delivered_job(client)
    svc = flask_app.config["escrow"]
    job = svc.get_job(job_id)
    # Force the inspection deadline into the past.
    job.inspection_deadline = 1
    svc._save(job)
    # Dispute it.
    r = client.post(f"/jobs/{job_id}/dispute",
                    json={"payer": PAYER, "reason": "not what I asked for"},
                    headers=PAYER_H)
    assert r.status_code == 200
    # One hour into the review window the sweep must not capture it.
    captured = svc.check_inspection_deadlines(now=int(time.time()) + 3600)
    assert job_id not in captured
    assert svc.get_job(job_id).status == "DELIVERED"


# ---------------------------------------------------------------------------
# v8.14 (Grok latest, fix 2): Create Order budget is checked BEFORE the
# job row is written; take() is locked; the suite no longer bypasses
# via app.testing.
# ---------------------------------------------------------------------------

def test_order_budget_429_writes_no_row(client, monkeypatch):
    """A 429 from the Create Order budget must not leave an orphaned
    QUOTED job with no order. The old order (create row, then check)
    wrote one orphan per rejected request, and orphans counted toward
    MAX_OPEN_JOBS until the quote expired."""
    import app.server as srv
    # Fresh 1-token bucket, already spent.
    fresh = srv._CreateOrderBudget(1)
    assert fresh.take() is True
    monkeypatch.setattr(srv, "_order_budget", fresh)
    # Drive the real bucket for this test (the fixture bypasses it).
    monkeypatch.setitem(flask_app.config, "BYPASS_ORDER_BUDGET", False)
    r = client.post("/jobs",
                    json={"brief": "audit the top-20 holders, budget $50"})
    assert r.status_code == 429, r.get_json()
    assert r.headers.get("Retry-After") == "60"
    svc = flask_app.config["escrow"]
    orphans = [j for j in svc.jobs.values()
               if j.status == "QUOTED" and not j.order_id]
    assert orphans == []


def test_order_budget_take_is_thread_safe():
    """take()'s check-then-act raced: two concurrent requests could
    both see tokens >= 1 and both spend. With the lock and a frozen
    clock, 200 takes across 10 threads grant exactly capacity."""
    import threading
    import app.server as srv
    budget = srv._CreateOrderBudget(5, _clock=lambda: 0.0)
    granted = []
    granted_lock = threading.Lock()

    def worker():
        local = 0
        for _ in range(20):
            if budget.take():
                local += 1
        with granted_lock:
            granted.append(local)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(granted) == 5


# ---------------------------------------------------------------------------
# v8.14 (Grok latest): operator exits on the API; authenticated reads.
# ---------------------------------------------------------------------------

def test_clear_review_flag_route_is_payer_gated(client):
    """POST /jobs/<id>/clear-review-flag exists and requires the payer."""
    data = _create_job(client)
    job_id = data["job"]["job_id"]
    # No token -> 401.
    r = client.post(f"/jobs/{job_id}/clear-review-flag",
                    json={"payer": PAYER})
    assert r.status_code == 401, r.get_json()
    # Payer token, but the job is not flagged -> 409, not 404/500.
    r = client.post(f"/jobs/{job_id}/clear-review-flag",
                    json={"payer": PAYER}, headers=PAYER_H)
    assert r.status_code == 409, r.get_json()
    assert "not flagged" in r.get_json()["error"]


def test_cancel_pending_capture_route_is_payer_gated(client):
    """POST /jobs/<id>/cancel-pending-capture exists and requires the payer."""
    data = _create_job(client)
    job_id = data["job"]["job_id"]
    # No token -> 401.
    r = client.post(f"/jobs/{job_id}/cancel-pending-capture",
                    json={"payer": PAYER})
    assert r.status_code == 401, r.get_json()
    # Payer token, but no capture is pending -> 409.
    r = client.post(f"/jobs/{job_id}/cancel-pending-capture",
                    json={"payer": PAYER}, headers=PAYER_H)
    assert r.status_code == 409, r.get_json()
    assert "no capture is pending" in r.get_json()["error"]


def test_get_job_authenticated_sees_pause_fields(client):
    """Grok latest, improvement 1: after a pause, the payer's own
    GET /jobs/<id> shows needs_review/dispute_deadline; the public
    view omits them."""
    job_id = _delivered_job(client)
    r = client.post(f"/jobs/{job_id}/dispute",
                    json={"payer": PAYER, "reason": "not what I asked for"},
                    headers=PAYER_H)
    assert r.status_code == 200, r.get_json()
    # Authenticated as the payer: pause fields present.
    r = client.get(f"/jobs/{job_id}", headers=PAYER_H)
    assert r.status_code == 200, r.get_json()
    job = r.get_json()["job"]
    assert job["needs_review"] is True
    assert job["dispute_deadline"] is not None
    # Unauthenticated: public shape, pause fields omitted.
    r = client.get(f"/jobs/{job_id}")
    assert r.status_code == 200, r.get_json()
    job = r.get_json()["job"]
    assert "needs_review" not in job
    assert "dispute_deadline" not in job

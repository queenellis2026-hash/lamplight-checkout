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
    return client.get(f"/jobs/{job_id}").get_json()["job"]["authorization_id"]


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
                            "still_pending": [], "errors": []}


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
    assert got["job"]["capture_id"] == direct["id"]
    assert got["job"]["capture_amount_cents"] == 1250
    assert got["job"]["capture_reconciled"] is True
    assert got["receipt"]["outcome"] == "released"
    assert got["receipt"]["capture_id"] == direct["id"]
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
    assert before["last_paypal_response"]["operation"] == "authorize"
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
    assert before["delivery_hash"]
    assert before["last_paypal_response"]["operation"] == "authorize"
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
    """GET /jobs/<id> exposes the audit record: authorize after the
    hold, capture after the release — so an operator can audit what
    PayPal said without re-querying it."""
    job_id = _delivered_job(client)
    got = client.get(f"/jobs/{job_id}").get_json()["job"]
    record = got["last_paypal_response"]
    assert record["operation"] == "authorize"
    assert record["status"] == "CREATED"
    assert record["id"] == got["authorization_id"]
    r = client.post(f"/jobs/{job_id}/capture",
                    json={"payer": PAYER,
                          "delivery_text": "audit report: top-20 holders"},
                    headers=PAYER_H)
    assert r.status_code == 200, r.get_json()
    got = client.get(f"/jobs/{job_id}").get_json()
    assert got["job"]["last_paypal_response"]["operation"] == "capture"
    assert got["job"]["last_paypal_response"]["status"] == "COMPLETED"
    assert got["job"]["last_paypal_response"]["id"] == got["job"]["capture_id"]
    assert got["receipt"]["last_paypal_response"]["operation"] == "capture"
    assert got["receipt"]["last_paypal_response"]["id"] == got["job"]["capture_id"]

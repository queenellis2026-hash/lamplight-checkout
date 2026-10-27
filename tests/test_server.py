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
    r = client.post(f"/jobs/{job_id}/capture", json={"payer": PAYER},
                    headers=PAYER_H)
    assert r.status_code == 409

    # Deliver, then capture.
    r = client.post(f"/jobs/{job_id}/deliver",
                    json={"agent": AUDITOR,
                          "delivery_text": "audit report: top-20 holders"},
                    headers=_agent_headers(AUDITOR))
    assert r.status_code == 200, r.get_json()
    r = client.post(f"/jobs/{job_id}/capture", json={"payer": PAYER},
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

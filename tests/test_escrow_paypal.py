"""Tests for the PayPal client and the escrow state machine, run against
the local mock PayPal sandbox (scripts/mock_paypal.py)."""
import os
import pathlib
import subprocess
import sys
import time

import pytest
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.escrow import EscrowService, EscrowError
from app.hiring_agent import HiringAgent
from app.paypal_client import PayPalClient, PayPalError
from app.quote import new_quote

MOCK_PORT = 5002
BASE = f"http://127.0.0.1:{MOCK_PORT}"


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
    return PayPalClient(client_id="mock", client_secret="mock",
                        base_url=mock_server)


def _quote(price_cents=1250):
    return new_quote(service="audit", price_cents=price_cents,
                     pay_to="agent@example")


def _approved_authorized_job(client, price_cents=1250, deadline_seconds=3600):
    svc = EscrowService(client)
    job = svc.create_job(quote=_quote(price_cents), payer="payer@example",
                         agent="agent@example", deadline_seconds=deadline_seconds)
    order = svc.create_order(job)
    requests.post(f"{BASE}/mock/approve/{order['id']}", timeout=10).raise_for_status()
    svc.authorize(job)
    return svc, job


def test_oauth_token_from_mock(client):
    assert client.get_access_token() == "MOCK-ACCESS-TOKEN"


def test_authorize_requires_approval(client):
    svc = EscrowService(client)
    job = svc.create_job(quote=_quote(), payer="payer@example",
                         agent="agent@example")
    svc.create_order(job)
    with pytest.raises(EscrowError, match="authorize failed"):
        svc.authorize(job)


def test_happy_path_capture(client):
    svc, job = _approved_authorized_job(client)
    assert job.status == "AUTHORIZED"
    svc.submit_delivery(job, agent="agent@example", delivery_text="report v1")
    assert job.status == "DELIVERED"
    svc.capture(job, payer="payer@example")
    assert job.status == "CAPTURED"
    assert job.capture_id.startswith("CAPTURE-")


def test_capture_before_delivery_refused(client):
    svc, job = _approved_authorized_job(client)
    with pytest.raises(EscrowError, match="cannot capture"):
        svc.capture(job, payer="payer@example")


def test_double_capture_refused(client):
    svc, job = _approved_authorized_job(client)
    svc.submit_delivery(job, agent="agent@example", delivery_text="report v1")
    svc.capture(job, payer="payer@example")
    with pytest.raises(EscrowError, match="cannot capture"):
        svc.capture(job, payer="payer@example")


def test_only_agent_can_deliver(client):
    svc, job = _approved_authorized_job(client)
    with pytest.raises(EscrowError, match="only the worker agent"):
        svc.submit_delivery(job, agent="someone-else@example",
                            delivery_text="report v1")


def test_only_payer_can_capture(client):
    svc, job = _approved_authorized_job(client)
    svc.submit_delivery(job, agent="agent@example", delivery_text="report v1")
    with pytest.raises(EscrowError, match="only the payer"):
        svc.capture(job, payer="someone-else@example")


def test_void_before_deadline_refused(client):
    svc, job = _approved_authorized_job(client, deadline_seconds=3600)
    with pytest.raises(EscrowError, match="deadline not reached"):
        svc.void_after_deadline(job, payer="payer@example")


def test_void_after_deadline(client):
    svc, job = _approved_authorized_job(client, deadline_seconds=0)
    time.sleep(1)
    svc.void_after_deadline(job, payer="payer@example")
    assert job.status == "VOIDED"


def test_void_after_delivery_refused(client):
    # Delivery before the deadline, then void: refused because the job is
    # DELIVERED, not AUTHORIZED (the void path needs AUTHORIZED).
    svc, job = _approved_authorized_job(client, deadline_seconds=3600)
    svc.submit_delivery(job, agent="agent@example", delivery_text="report v1")
    with pytest.raises(EscrowError, match="cannot void"):
        svc.void_after_deadline(job, payer="payer@example")


def test_hiring_agent_budget_guard():
    hiring = HiringAgent()
    with pytest.raises(ValueError, match="budget"):
        hiring.negotiate("audit the holders, budget $1")


def test_hiring_agent_negotiate_and_verify():
    hiring = HiringAgent()
    quote, worker = hiring.negotiate("audit the top-20 holders, budget $50")
    assert quote["price_cents"] == 1250
    assert worker["agent_id"] == "auditor-01"
    dh = hiring.delivery_hash("report v1")
    assert dh == hiring.delivery_hash("report v1")
    assert dh != hiring.delivery_hash("report v2")


# ---------------------------------------------------------------------------
# Post-review fixes (Oct 27, 2026): payee, result statuses, quote expiry,
# delivery deadline, idempotent recovery, now=0, delivery lock.
# ---------------------------------------------------------------------------

def test_create_order_sets_payee(client):
    """The order's purchase_units payee must be the worker's pay_to
    account, so the capture pays the worker — not the merchant."""
    svc = EscrowService(client)
    job = svc.create_job(quote=_quote(), payer="payer@example",
                         agent="agent@example")
    order = svc.create_order(job)
    assert order["purchase_units"][0]["payee"] == {"email_address": "agent@example"}


@pytest.mark.parametrize("status", ["DECLINED", "PENDING", "FAILED"])
def test_capture_non_completed_status_rejected(client, status):
    """A capture that PayPal did not COMPLETE must never settle the job."""
    svc, job = _approved_authorized_job(client)
    svc.submit_delivery(job, agent="agent@example", delivery_text="report v1")
    requests.post(f"{BASE}/mock/next-capture-status", json={"status": status},
                  timeout=10).raise_for_status()
    with pytest.raises(EscrowError, match="capture not completed"):
        svc.capture(job, payer="payer@example")
    assert job.status == "DELIVERED"
    assert job.capture_id == ""


@pytest.mark.parametrize("status", ["DENIED", "PENDING"])
def test_authorize_non_created_status_rejected(client, status):
    """An authorization PayPal did not CREATE must never place the hold."""
    svc = EscrowService(client)
    job = svc.create_job(quote=_quote(), payer="payer@example",
                         agent="agent@example")
    order = svc.create_order(job)
    requests.post(f"{BASE}/mock/approve/{order['id']}", timeout=10).raise_for_status()
    requests.post(f"{BASE}/mock/next-auth-status", json={"status": status},
                  timeout=10).raise_for_status()
    with pytest.raises(EscrowError, match="authorization not created"):
        svc.authorize(job)
    assert job.status == "QUOTED"
    assert job.authorization_id == ""


def test_authorize_expired_quote_rejected(client):
    """A quote that expired before authorize is refused, even though the
    order was created while it was still live."""
    svc = EscrowService(client)
    quote = new_quote(service="audit", price_cents=1250,
                      pay_to="agent@example", ttl_seconds=0)
    job = svc.create_job(quote=quote, payer="payer@example",
                         agent="agent@example")
    order = svc.create_order(job)
    requests.post(f"{BASE}/mock/approve/{order['id']}", timeout=10).raise_for_status()
    time.sleep(1.1)  # now > expires_at (ttl was 0)
    with pytest.raises(EscrowError, match="quote expired"):
        svc.authorize(job)
    assert job.status == "QUOTED"


def test_delivery_after_deadline_rejected(client):
    """A delivery at or after the deadline is refused, so it can never
    block the payer's void by stranding the job in DELIVERED."""
    svc, job = _approved_authorized_job(client, deadline_seconds=0)
    time.sleep(1.1)
    with pytest.raises(EscrowError, match="delivery deadline passed"):
        svc.submit_delivery(job, agent="agent@example", delivery_text="report v1")
    assert job.status == "AUTHORIZED"


def test_void_now_zero_uses_now(client):
    """void_after_deadline(now=0) must mean epoch 0, not 'now'. With a
    deadline far in the future, now=0 is before it and the void is refused."""
    svc, job = _approved_authorized_job(client, deadline_seconds=3600)
    job.deadline = 10 ** 9
    with pytest.raises(EscrowError, match="deadline not reached"):
        svc.void_after_deadline(job, payer="payer@example", now=0)


def test_concurrent_deliveries_exactly_once(client):
    """Two deliveries racing the same job: exactly one succeeds, the
    other is refused — the delivery hash is set once, never overwritten."""
    import threading

    svc, job = _approved_authorized_job(client)
    barrier = threading.Barrier(2)
    results: list = []

    def deliver():
        barrier.wait(timeout=10)
        try:
            results.append(svc.submit_delivery(
                job, agent="agent@example", delivery_text="report v1"))
        except EscrowError as e:
            results.append(e)

    threads = [threading.Thread(target=deliver) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    successes = [r for r in results if isinstance(r, str)]
    failures = [r for r in results if isinstance(r, EscrowError)]
    assert len(successes) == 1, results
    assert len(failures) == 1, results
    assert job.status == "DELIVERED"
    assert job.delivery_hash == successes[0]


def test_capture_response_lost_recovers_via_idempotent_retry(client):
    """The mock applies the capture then drops the connection. The
    capture must still succeed — the retry replays the original result
    under the same PayPal-Request-Id — and the job must end CAPTURED,
    not stuck in DELIVERED with every retry failing."""
    svc, job = _approved_authorized_job(client)
    svc.submit_delivery(job, agent="agent@example", delivery_text="report v1")
    requests.post(f"{BASE}/mock/drop-next-capture", timeout=10).raise_for_status()
    result = svc.capture(job, payer="payer@example")
    assert result["status"] == "COMPLETED"
    assert job.status == "CAPTURED"
    assert job.capture_id.startswith("CAPTURE-")
    assert not job.capture_reconciled  # recovered by replay, id observed
    with pytest.raises(EscrowError, match="cannot capture"):
        svc.capture(job, payer="payer@example")


def test_void_response_lost_recovers_via_idempotent_retry(client):
    """The mock applies the void then drops the connection. The void
    must still succeed via the idempotent retry."""
    svc, job = _approved_authorized_job(client, deadline_seconds=0)
    time.sleep(1.1)
    requests.post(f"{BASE}/mock/drop-next-void", timeout=10).raise_for_status()
    result = svc.void_after_deadline(job, payer="payer@example")
    assert result == {}
    assert job.status == "VOIDED"
    with pytest.raises(EscrowError, match="cannot void"):
        svc.void_after_deadline(job, payer="payer@example")


class _StubPayPal:
    """In-memory PayPal stand-in for lost-response tests where the HTTP
    mock cannot help: every capture/void applies server-side and then
    the response is lost (always raises). get_authorization reports
    the real state, which is what the escrow reconciles from."""

    def __init__(self):
        self.auths: dict[str, dict] = {}
        # When False, the call fails cleanly without applying server-side.
        self.capture_applies = True
        self.void_applies = True

    def create_order(self, *, amount_cents, currency="USD", description,
                     custom_id, payee_email=None, **kw):
        return {"id": "ORDER-STUB", "status": "CREATED",
                "purchase_units": [{"payee": {"email_address": payee_email}}],
                "links": []}

    def authorize_order(self, order_id, request_id=None):
        auth = {"id": "AUTH-STUB", "status": "CREATED"}
        self.auths["AUTH-STUB"] = auth
        return {"id": order_id, "status": "COMPLETED",
                "purchase_units": [{"payments": {"authorizations": [auth]}}]}

    def get_authorization(self, authorization_id):
        return self.auths[authorization_id]

    def capture_authorization(self, authorization_id, amount_cents=None,
                              currency="USD", request_id=None):
        if self.capture_applies:
            self.auths[authorization_id]["status"] = "CAPTURED"
        raise PayPalError("capture failed (transport): connection dropped")

    def void_authorization(self, authorization_id, request_id=None):
        if self.void_applies:
            self.auths[authorization_id]["status"] = "VOIDED"
        raise PayPalError("void failed (transport): connection dropped")


def _stub_authorized_job(stub):
    svc = EscrowService(stub)
    job = svc.create_job(quote=_quote(), payer="payer@example",
                         agent="agent@example", deadline_seconds=3600)
    svc.create_order(job)
    svc.authorize(job)
    return svc, job


def test_capture_response_lost_reconciles_from_authorization():
    """Capture applies server-side, every response is lost, and the
    retry also fails. Reconciling from the authorization state must
    still record the capture (flagged reconciled — the capture id was
    never observed) so a receipt can be produced."""
    stub = _StubPayPal()
    svc, job = _stub_authorized_job(stub)
    svc.submit_delivery(job, agent="agent@example", delivery_text="report v1")
    result = svc.capture(job, payer="payer@example")
    assert result["status"] == "COMPLETED"
    assert job.status == "CAPTURED"
    assert job.capture_id == ""
    assert job.capture_reconciled is True


def test_void_response_lost_reconciles_from_authorization():
    """Void applies server-side, every response is lost. Reconciling
    from the authorization state must still record the void."""
    stub = _StubPayPal()
    svc, job = _stub_authorized_job(stub)
    job.deadline = 0  # deadline already passed
    result = svc.void_after_deadline(job, payer="payer@example")
    assert result == {}
    assert job.status == "VOIDED"


def test_void_after_capture_rejected_via_reconcile():
    """The void call fails cleanly and the authorization is already
    CAPTURED: the void must be refused, not recorded as a void."""
    stub = _StubPayPal()
    stub.void_applies = False
    svc, job = _stub_authorized_job(stub)
    job.deadline = 0
    stub.auths["AUTH-STUB"]["status"] = "CAPTURED"
    with pytest.raises(EscrowError, match="already captured"):
        svc.void_after_deadline(job, payer="payer@example")
    assert job.status == "AUTHORIZED"

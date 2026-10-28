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

from app.escrow import EscrowService, EscrowError, Job
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
    svc.capture(job, payer="payer@example", delivery_text="report v1")
    assert job.status == "CAPTURED"
    assert job.capture_id.startswith("CAPTURE-")


def test_capture_before_delivery_refused(client):
    svc, job = _approved_authorized_job(client)
    with pytest.raises(EscrowError, match="cannot capture"):
        svc.capture(job, payer="payer@example", delivery_text="report v1")


def test_double_capture_refused(client):
    svc, job = _approved_authorized_job(client)
    svc.submit_delivery(job, agent="agent@example", delivery_text="report v1")
    svc.capture(job, payer="payer@example", delivery_text="report v1")
    with pytest.raises(EscrowError, match="cannot capture"):
        svc.capture(job, payer="payer@example", delivery_text="report v1")


def test_only_agent_can_deliver(client):
    svc, job = _approved_authorized_job(client)
    with pytest.raises(EscrowError, match="only the worker agent"):
        svc.submit_delivery(job, agent="someone-else@example",
                            delivery_text="report v1")


def test_only_payer_can_capture(client):
    svc, job = _approved_authorized_job(client)
    svc.submit_delivery(job, agent="agent@example", delivery_text="report v1")
    with pytest.raises(EscrowError, match="only the payer"):
        svc.capture(job, payer="someone-else@example", delivery_text="report v1")


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
        svc.capture(job, payer="payer@example", delivery_text="report v1")
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
    result = svc.capture(job, payer="payer@example", delivery_text="report v1")
    assert result["status"] == "COMPLETED"
    assert job.status == "CAPTURED"
    assert job.capture_id.startswith("CAPTURE-")
    assert not job.capture_reconciled  # recovered by replay, id observed
    with pytest.raises(EscrowError, match="cannot capture"):
        svc.capture(job, payer="payer@example", delivery_text="report v1")


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
    the real state, which is what the escrow reconciles from.

    ``record_capture`` (default True): when a capture applies, the
    authorization resource lists it in ``captures`` — the real shape.
    Set False to simulate an authorization that reports CAPTURED but
    lists no capture (the reconciled-but-unobserved fallback).
    ``fail_get_authorization``: get_authorization raises instead of
    answering, for the reconcile-error path."""

    def __init__(self):
        self.auths: dict[str, dict] = {}
        # When False, the call fails cleanly without applying server-side.
        self.capture_applies = True
        self.void_applies = True
        self.record_capture = True
        self.fail_get_authorization = False
        self._last_amount_cents = 1250

    def create_order(self, *, amount_cents, currency="USD", description,
                     custom_id, payee_email=None, **kw):
        self._last_amount_cents = amount_cents
        return {"id": "ORDER-STUB", "status": "CREATED",
                "purchase_units": [{"payee": {"email_address": payee_email}}],
                "links": []}

    def authorize_order(self, order_id, request_id=None):
        auth = {"id": "AUTH-STUB", "status": "CREATED"}
        self.auths["AUTH-STUB"] = auth
        return {"id": order_id, "status": "COMPLETED",
                "purchase_units": [{"payments": {"authorizations": [auth]}}]}

    def get_authorization(self, authorization_id):
        if self.fail_get_authorization:
            raise PayPalError("get_authorization failed (transport)")
        return self.auths[authorization_id]

    def capture_authorization(self, authorization_id, amount_cents=None,
                              currency="USD", request_id=None):
        if self.capture_applies:
            self.auths[authorization_id]["status"] = "CAPTURED"
            if self.record_capture:
                from app.paypal_client import cents_to_amount
                capture = {
                    "id": "CAPTURE-STUB", "status": "COMPLETED",
                    "amount": {"currency_code": currency,
                               "value": cents_to_amount(
                                   amount_cents if amount_cents is not None
                                   else self._last_amount_cents)},
                    "authorization_id": authorization_id,
                }
                self.auths[authorization_id].setdefault(
                    "captures", []).append(capture)
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
    still record the capture — now with the REAL capture id and amount
    from the authorization's linked captures, flagged reconciled — so
    the receipt is fully auditable instead of carrying an empty id."""
    stub = _StubPayPal()
    svc, job = _stub_authorized_job(stub)
    svc.submit_delivery(job, agent="agent@example", delivery_text="report v1")
    result = svc.capture(job, payer="payer@example", delivery_text="report v1")
    assert result["status"] == "COMPLETED"
    assert job.status == "CAPTURED"
    assert job.capture_id == "CAPTURE-STUB"
    assert job.capture_amount_cents == 1250
    assert job.capture_reconciled is True


def test_capture_reconcile_falls_back_when_no_capture_listed():
    """Same lost-response path, but the authorization reports CAPTURED
    and lists NO capture: the old fallback stands — capture id "" with
    the reconciled flag, amount unknown, documented as unauditable."""
    stub = _StubPayPal()
    stub.record_capture = False
    svc, job = _stub_authorized_job(stub)
    svc.submit_delivery(job, agent="agent@example", delivery_text="report v1")
    result = svc.capture(job, payer="payer@example", delivery_text="report v1")
    assert result["status"] == "COMPLETED"
    assert job.status == "CAPTURED"
    assert job.capture_id == ""
    assert job.capture_amount_cents is None
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

# ---------------------------------------------------------------------------
# Round 2 fixes (Oct 27, 2026): delivery-hash verification in capture,
# durable job store, forged `now` never accepted from the request path.
# Grok's Low (double-delivery race) was already fixed in round 1 by the
# per-job delivery lock — test_concurrent_deliveries_exactly_once above
# covers it; no new work here.
# ---------------------------------------------------------------------------

def test_capture_empty_delivery_text_rejected(client):
    """Capture with no delivery text is refused: verification needs the
    original text, and an empty string can never hash to the delivery."""
    svc, job = _approved_authorized_job(client)
    svc.submit_delivery(job, agent="agent@example", delivery_text="report v1")
    with pytest.raises(EscrowError, match="delivery text required"):
        svc.capture(job, payer="payer@example", delivery_text="")
    assert job.status == "DELIVERED"
    assert job.capture_id == ""


def test_capture_wrong_delivery_text_rejected(client):
    """Capture with a delivery text whose hash does not match the
    delivered work is refused — the money never moves on unverified
    content, even when the caller is the payer."""
    svc, job = _approved_authorized_job(client)
    svc.submit_delivery(job, agent="agent@example", delivery_text="report v1")
    with pytest.raises(EscrowError, match="delivery verification failed"):
        svc.capture(job, payer="payer@example", delivery_text="report v2")
    assert job.status == "DELIVERED"
    assert job.capture_id == ""


def test_capture_calls_injected_verifier(client):
    """The injected delivery verifier is what runs: a verifier that
    refuses makes capture fail, and one that accepts makes it succeed
    even for text that does not match the delivery hash. This is the
    hook the server wires to the hiring agent's verify_delivery."""
    svc, job = _approved_authorized_job(client)
    svc.submit_delivery(job, agent="agent@example", delivery_text="report v1")
    calls: list = []

    def refusing(job_, text):
        calls.append((job_.job_id, text))
        return False

    svc.delivery_verifier = refusing
    with pytest.raises(EscrowError, match="delivery verification failed"):
        svc.capture(job, payer="payer@example", delivery_text="report v1")
    assert calls == [(job.job_id, "report v1")]
    assert job.status == "DELIVERED"

    svc.delivery_verifier = lambda job_, text: True
    result = svc.capture(job, payer="payer@example", delivery_text="anything")
    assert result["status"] == "COMPLETED"
    assert job.status == "CAPTURED"


def test_capture_uses_hiring_agent_verifier(client):
    """The server wires HiringAgent.verify_delivery as the verifier, so
    capture accepts the exact delivered text and refuses anything else."""
    hiring = HiringAgent()
    svc, job = _approved_authorized_job(client)
    svc.delivery_verifier = hiring.verify_delivery
    svc.submit_delivery(job, agent="agent@example", delivery_text="report v1")
    with pytest.raises(EscrowError, match="delivery verification failed"):
        svc.capture(job, payer="payer@example", delivery_text="report v2")
    result = svc.capture(job, payer="payer@example", delivery_text="report v1")
    assert result["status"] == "COMPLETED"
    assert job.status == "CAPTURED"


def _restart_service(client, path, **kw):
    """A fresh EscrowService on the same store file — a simulated
    process restart: new objects, same durable state."""
    from app.store import JsonFileStore
    return EscrowService(client, store=JsonFileStore(path), **kw)


def test_job_survives_restart_and_captures(client, tmp_path):
    """Create, authorize and deliver, then 'restart' (a fresh service
    on the same store file): the job must still be there, DELIVERED,
    with its authorization id and delivery hash intact — and capture
    must still work on it."""
    import json
    from app.store import JsonFileStore

    path = tmp_path / "jobs.json"
    svc1 = EscrowService(client, store=JsonFileStore(path))
    job = svc1.create_job(quote=_quote(), payer="payer@example",
                          agent="agent@example", deadline_seconds=3600)
    order = svc1.create_order(job)
    requests.post(f"{BASE}/mock/approve/{order['id']}", timeout=10).raise_for_status()
    svc1.authorize(job)
    svc1.submit_delivery(job, agent="agent@example", delivery_text="report v1")

    # The store file itself holds the full job, quote included.
    stored = json.loads(path.read_text())["jobs"][job.job_id]
    assert stored["status"] == "DELIVERED"
    assert stored["authorization_id"] == job.authorization_id
    assert stored["quote"]["price_cents"] == 1250
    assert stored["quote"]["pay_to"] == "agent@example"

    svc2 = _restart_service(client, path)
    reloaded = svc2.get_job(job.job_id)
    assert reloaded.status == "DELIVERED"
    assert reloaded.authorization_id == job.authorization_id
    assert reloaded.delivery_hash == job.delivery_hash
    result = svc2.capture(reloaded, payer="payer@example",
                          delivery_text="report v1")
    assert result["status"] == "COMPLETED"
    assert reloaded.status == "CAPTURED"

    # And the capture survives a second restart too.
    svc3 = _restart_service(client, path)
    assert svc3.get_job(job.job_id).status == "CAPTURED"


def test_job_survives_restart_and_voids(client, tmp_path):
    """An AUTHORIZED job whose deadline passes must still be voidable
    after a restart — the hold is not stranded by a process crash."""
    from app.store import JsonFileStore

    path = tmp_path / "jobs.json"
    svc1 = EscrowService(client, store=JsonFileStore(path))
    job = svc1.create_job(quote=_quote(), payer="payer@example",
                          agent="agent@example", deadline_seconds=0)
    order = svc1.create_order(job)
    requests.post(f"{BASE}/mock/approve/{order['id']}", timeout=10).raise_for_status()
    svc1.authorize(job)
    time.sleep(1.1)

    svc2 = _restart_service(client, path)
    reloaded = svc2.get_job(job.job_id)
    assert reloaded.status == "AUTHORIZED"
    result = svc2.void_after_deadline(reloaded, payer="payer@example")
    assert result == {}
    assert reloaded.status == "VOIDED"


def test_quoted_job_survives_restart(client, tmp_path):
    """A job that only has an order (QUOTED, not yet authorized) must
    survive a restart with its order id intact, and authorize after it."""
    from app.store import JsonFileStore

    path = tmp_path / "jobs.json"
    svc1 = EscrowService(client, store=JsonFileStore(path))
    job = svc1.create_job(quote=_quote(), payer="payer@example",
                          agent="agent@example")
    order = svc1.create_order(job)
    requests.post(f"{BASE}/mock/approve/{order['id']}", timeout=10).raise_for_status()

    svc2 = _restart_service(client, path)
    reloaded = svc2.get_job(job.job_id)
    assert reloaded.status == "QUOTED"
    assert reloaded.order_id == order["id"]
    svc2.authorize(reloaded)
    assert reloaded.status == "AUTHORIZED"


def test_store_rejects_corrupt_file(tmp_path):
    """A corrupt store file fails loudly at startup instead of silently
    dropping jobs."""
    from app.store import JsonFileStore

    path = tmp_path / "jobs.json"
    path.write_text("{not valid json")
    with pytest.raises(ValueError, match="corrupt"):
        JsonFileStore(path)

# ---------------------------------------------------------------------------
# Round 3 fixes (Oct 27, 2026): real capture id + amount on reconcile,
# reconcile admin endpoint (service level), single-process warning.
# Held: SQLite/Postgres store, partial capture, webhook listener.
# ---------------------------------------------------------------------------

def test_amount_to_cents():
    """PayPal decimal strings parse to exact cents — no float error.
    Only strings with at most two decimal places are accepted."""
    from app.paypal_client import amount_to_cents
    assert amount_to_cents("12.50") == 1250
    assert amount_to_cents("0.99") == 99
    assert amount_to_cents("12.5") == 1250
    assert amount_to_cents("0") == 0
    with pytest.raises(ValueError, match="bad PayPal amount"):
        amount_to_cents("abc")
    with pytest.raises(ValueError, match="bad PayPal amount"):
        amount_to_cents(None)
    with pytest.raises(ValueError, match="bad PayPal amount"):
        amount_to_cents(1250)  # an int is not a PayPal amount string
    with pytest.raises(ValueError, match="bad PayPal amount"):
        amount_to_cents("12.505")  # more than two decimal places


def test_first_completed_capture():
    """Picks the first COMPLETED capture, skips non-completed ones,
    returns None when the authorization lists none."""
    from app.paypal_client import first_completed_capture
    assert first_completed_capture({}) is None
    assert first_completed_capture({"captures": []}) is None
    auth = {"captures": [
        {"id": "C-DECLINED", "status": "DECLINED"},
        {"id": "C-FIRST", "status": "COMPLETED",
         "amount": {"currency_code": "USD", "value": "12.50"}},
        {"id": "C-SECOND", "status": "COMPLETED",
         "amount": {"currency_code": "USD", "value": "12.50"}},
    ]}
    assert first_completed_capture(auth)["id"] == "C-FIRST"
    assert first_completed_capture({"captures": "not-a-list"}) is None


def test_normal_capture_records_amount(client):
    """The happy path records the captured amount on the job, not just
    the id — the receipt carries it, so it is auditable."""
    svc, job = _approved_authorized_job(client)
    svc.submit_delivery(job, agent="agent@example", delivery_text="report v1")
    svc.capture(job, payer="payer@example", delivery_text="report v1")
    assert job.status == "CAPTURED"
    assert job.capture_id.startswith("CAPTURE-")
    assert job.capture_amount_cents == 1250
    assert job.capture_reconciled is False


def test_mock_authorization_lists_linked_captures(client):
    """After a capture, GET authorization exposes the capture in its
    `captures` list — the shape the reconcile path reads."""
    svc, job = _approved_authorized_job(client)
    svc.submit_delivery(job, agent="agent@example", delivery_text="report v1")
    svc.capture(job, payer="payer@example", delivery_text="report v1")
    r = requests.get(f"{BASE}/v2/payments/authorizations/"
                     f"{job.authorization_id}", timeout=10)
    assert r.status_code == 200
    auth = r.json()
    assert auth["status"] == "CAPTURED"
    captures = auth.get("captures", [])
    assert len(captures) == 1
    assert captures[0]["id"] == job.capture_id
    assert captures[0]["status"] == "COMPLETED"
    assert captures[0]["amount"] == {"currency_code": "USD", "value": "12.50"}


def test_reconcile_open_jobs_still_pending_when_created():
    """An AUTHORIZED job PayPal still reports CREATED is checked but
    left alone — it is still pending, not an error."""
    stub = _StubPayPal()
    svc, job = _stub_authorized_job(stub)
    summary = svc.reconcile_open_jobs()
    assert summary == {"checked": 1, "updated": [],
                       "still_pending": [job.job_id], "errors": []}
    assert svc.get_job(job.job_id).status == "AUTHORIZED"


def test_reconcile_open_jobs_captures_delivered_job():
    """A DELIVERED job whose authorization PayPal reports CAPTURED (the
    capture happened outside the normal call) is recorded CAPTURED with
    the real id + amount and the reconciled flag."""
    stub = _StubPayPal()
    svc, job = _stub_authorized_job(stub)
    svc.submit_delivery(job, agent="agent@example", delivery_text="report v1")
    stub.auths["AUTH-STUB"]["status"] = "CAPTURED"
    stub.auths["AUTH-STUB"]["captures"] = [{
        "id": "CAPTURE-OUTSIDE", "status": "COMPLETED",
        "amount": {"currency_code": "USD", "value": "12.50"},
        "authorization_id": "AUTH-STUB"}]
    summary = svc.reconcile_open_jobs()
    assert summary["checked"] == 1
    assert summary["still_pending"] == []
    assert summary["errors"] == []
    assert summary["updated"] == [{"job_id": job.job_id, "from": "DELIVERED",
                                   "to": "CAPTURED",
                                   "capture_id": "CAPTURE-OUTSIDE"}]
    reloaded = svc.get_job(job.job_id)
    assert reloaded.status == "CAPTURED"
    assert reloaded.capture_id == "CAPTURE-OUTSIDE"
    assert reloaded.capture_amount_cents == 1250
    assert reloaded.capture_reconciled is True
    assert reloaded.settled_at is not None


def test_reconcile_open_jobs_voids_authorized_job():
    """An AUTHORIZED job PayPal reports VOIDED is recorded VOIDED."""
    stub = _StubPayPal()
    svc, job = _stub_authorized_job(stub)
    stub.auths["AUTH-STUB"]["status"] = "VOIDED"
    summary = svc.reconcile_open_jobs()
    assert summary["updated"] == [{"job_id": job.job_id, "from": "AUTHORIZED",
                                   "to": "VOIDED", "capture_id": ""}]
    assert svc.get_job(job.job_id).status == "VOIDED"


def test_reconcile_open_jobs_captured_without_listed_capture():
    """CAPTURED with no linked capture: recorded CAPTURED with "" id
    and unknown amount — the reconciled fallback, not a crash."""
    stub = _StubPayPal()
    svc, job = _stub_authorized_job(stub)
    stub.auths["AUTH-STUB"]["status"] = "CAPTURED"
    summary = svc.reconcile_open_jobs()
    assert summary["updated"][0]["to"] == "CAPTURED"
    assert summary["updated"][0]["capture_id"] == ""
    reloaded = svc.get_job(job.job_id)
    assert reloaded.capture_id == ""
    assert reloaded.capture_amount_cents is None
    assert reloaded.capture_reconciled is True


def test_reconcile_open_jobs_skips_settled_jobs():
    """CAPTURED/VOIDED jobs are not counted — only open jobs are checked."""
    stub = _StubPayPal()
    svc, job = _stub_authorized_job(stub)
    svc.submit_delivery(job, agent="agent@example", delivery_text="report v1")
    svc.capture(job, payer="payer@example", delivery_text="report v1")
    assert job.status == "CAPTURED"
    summary = svc.reconcile_open_jobs()
    assert summary == {"checked": 0, "updated": [],
                       "still_pending": [], "errors": []}


def test_reconcile_open_jobs_reports_paypal_errors():
    """One job's get_authorization failure is reported and counted
    still pending — and never stops the rest."""
    stub = _StubPayPal()
    svc, job = _stub_authorized_job(stub)
    stub.fail_get_authorization = True
    summary = svc.reconcile_open_jobs()
    assert summary["checked"] == 1
    assert summary["updated"] == []
    assert summary["still_pending"] == [job.job_id]
    assert len(summary["errors"]) == 1
    assert summary["errors"][0]["job_id"] == job.job_id
    assert svc.get_job(job.job_id).status == "AUTHORIZED"


def test_reconcile_job_refuses_settled_job():
    """reconcile_job on a settled job reports 'job is not open' and
    changes nothing."""
    stub = _StubPayPal()
    svc, job = _stub_authorized_job(stub)
    svc.submit_delivery(job, agent="agent@example", delivery_text="report v1")
    svc.capture(job, payer="payer@example", delivery_text="report v1")
    result = svc.reconcile_job(job)
    assert result["updated"] is False
    assert result["error"] == "job is not open"
    assert svc.get_job(job.job_id).status == "CAPTURED"


def test_old_store_file_without_capture_amount_loads(tmp_path):
    """A jobs.json written before round 3 (no capture_amount_cents
    field) still loads — the amount is simply unknown."""
    from app.store import JsonFileStore
    import json

    path = tmp_path / "jobs.json"
    job_data = {
        "job_id": "oldjob1",
        "quote": {"quote_id": "q1", "service": "audit",
                  "price_cents": 1250, "pay_to": "agent@example",
                  "issued_at": 0, "expires_at": 10 ** 9,
                  "quote_hash": "0x00"},
        "payer": "payer@example", "agent": "agent@example",
        "created_at": 0, "deadline": 10 ** 9, "status": "CAPTURED",
        "order_id": "ORDER-OLD", "authorization_id": "AUTH-OLD",
        "capture_id": "CAPTURE-OLD", "delivery_hash": "ab",
        "settled_at": 1, "capture_reconciled": False,
    }
    path.write_text(json.dumps({"jobs": {"oldjob1": job_data}}))
    store = JsonFileStore(path)
    job = store.get("oldjob1")
    assert job.status == "CAPTURED"
    assert job.capture_id == "CAPTURE-OLD"
    assert job.capture_amount_cents is None

# ---------------------------------------------------------------------------
# Round 4 fixes (Oct 27, 2026): last PayPal response persisted on the job
# (audit without re-querying PayPal), 404 vanished-authorization test.
# Held: SQLite/Postgres store, partial capture, webhook listener.
# ---------------------------------------------------------------------------

def test_authorize_records_last_paypal_response(client):
    """After authorize, the job carries the authorize response — the
    envelope (operation + status + id + raw body), so an operator can
    audit what PayPal said without re-querying it."""
    svc, job = _approved_authorized_job(client)
    record = job.last_paypal_response
    assert record["operation"] == "authorize"
    assert record["status"] == "CREATED"
    assert record["id"] == job.authorization_id
    auth = record["body"]["purchase_units"][0]["payments"]["authorizations"][0]
    assert auth["id"] == job.authorization_id
    assert auth["status"] == "CREATED"


def test_capture_records_last_paypal_response(client):
    """After capture, the last response is the capture result — the
    record the receipt carries, so the release is auditable."""
    svc, job = _approved_authorized_job(client)
    svc.submit_delivery(job, agent="agent@example", delivery_text="report v1")
    svc.capture(job, payer="payer@example", delivery_text="report v1")
    record = job.last_paypal_response
    assert record["operation"] == "capture"
    assert record["status"] == "COMPLETED"
    assert record["id"] == job.capture_id
    assert record["body"]["amount"] == {"currency_code": "USD",
                                        "value": "12.50"}
    assert record["body"]["authorization_id"] == job.authorization_id


def test_void_records_last_paypal_response(client):
    """After void, the last response is the void record. The void call
    returns no body (204), so the envelope's operation + status + id
    are the audit trail — that is why the field is an envelope, not
    the raw body alone."""
    svc, job = _approved_authorized_job(client)
    job.deadline = 0  # deadline already passed
    svc.void_after_deadline(job, payer="payer@example")
    record = job.last_paypal_response
    assert record["operation"] == "void"
    assert record["status"] == "VOIDED"
    assert record["id"] == job.authorization_id
    assert record["body"] == {}


def test_reconcile_records_last_paypal_response():
    """A reconcile read is itself a successful PayPal response: the
    record is the authorization body (operation "reconcile"), whether
    the job settles from it or stays pending."""
    stub = _StubPayPal()
    svc, job = _stub_authorized_job(stub)
    svc.submit_delivery(job, agent="agent@example", delivery_text="report v1")
    stub.auths["AUTH-STUB"]["status"] = "CAPTURED"
    stub.auths["AUTH-STUB"]["captures"] = [{
        "id": "CAPTURE-OUTSIDE", "status": "COMPLETED",
        "amount": {"currency_code": "USD", "value": "12.50"},
        "authorization_id": "AUTH-STUB"}]
    svc.reconcile_open_jobs()
    record = job.last_paypal_response
    assert record["operation"] == "reconcile"
    assert record["status"] == "CAPTURED"
    assert record["id"] == "AUTH-STUB"
    assert record["body"]["captures"][0]["id"] == "CAPTURE-OUTSIDE"

    # A still-pending (CREATED) reconcile records the read too.
    stub2 = _StubPayPal()
    svc2, job2 = _stub_authorized_job(stub2)
    svc2.reconcile_open_jobs()
    record2 = job2.last_paypal_response
    assert record2["operation"] == "reconcile"
    assert record2["status"] == "CREATED"
    assert record2["id"] == "AUTH-STUB"
    assert job2.status == "AUTHORIZED"


def test_failed_reconcile_keeps_last_good_response():
    """A failed get_authorization never overwrites the last good
    record: the job keeps its authorize response, stays AUTHORIZED,
    and the error is reported instead."""
    stub = _StubPayPal()
    svc, job = _stub_authorized_job(stub)
    before = job.last_paypal_response
    assert before["operation"] == "authorize"
    stub.fail_get_authorization = True
    summary = svc.reconcile_open_jobs()
    assert summary["checked"] == 1
    assert summary["still_pending"] == [job.job_id]
    assert len(summary["errors"]) == 1
    assert summary["errors"][0]["job_id"] == job.job_id
    reloaded = svc.get_job(job.job_id)
    assert reloaded.status == "AUTHORIZED"
    assert reloaded.last_paypal_response == before


def test_old_store_file_without_last_response_loads(tmp_path):
    """A jobs.json written before round 4 (no last_paypal_response
    field) still loads — the audit record is simply absent. And a job
    that has one survives a JsonFileStore round trip intact."""
    from app.store import JsonFileStore
    import json

    path = tmp_path / "jobs.json"
    job_data = {
        "job_id": "oldjob1",
        "quote": {"quote_id": "q1", "service": "audit",
                  "price_cents": 1250, "pay_to": "agent@example",
                  "issued_at": 0, "expires_at": 10 ** 9,
                  "quote_hash": "0x00"},
        "payer": "payer@example", "agent": "agent@example",
        "created_at": 0, "deadline": 10 ** 9, "status": "CAPTURED",
        "order_id": "ORDER-OLD", "authorization_id": "AUTH-OLD",
        "capture_id": "CAPTURE-OLD", "delivery_hash": "ab",
        "settled_at": 1, "capture_reconciled": False,
    }
    path.write_text(json.dumps({"jobs": {"oldjob1": job_data}}))
    store = JsonFileStore(path)
    job = store.get("oldjob1")
    assert job.capture_amount_cents is None
    assert job.last_paypal_response is None

    # Round trip: a recorded response survives save + reload.
    job.last_paypal_response = {
        "operation": "capture", "status": "COMPLETED", "id": "CAPTURE-OLD",
        "body": {"id": "CAPTURE-OLD", "status": "COMPLETED"}}
    store.save(job)
    reloaded = JsonFileStore(path).get("oldjob1")
    assert reloaded.last_paypal_response == job.last_paypal_response

# ---------------------------------------------------------------------------
# Round 5 fixes (Oct 27, 2026): size/shape guard on the recorded PayPal
# response envelope; one shared record+save helper for reconcile reads;
# vanished-authorization test for DELIVERED jobs. Held: SQLite/Postgres
# store, partial capture, webhook listener.
# ---------------------------------------------------------------------------

def _guard_job():
    """A bare Job for calling _record_paypal_response directly."""
    return Job(job_id="guard1", quote=_quote(), payer="payer@example",
               agent="agent@example")


def test_guard_keeps_normal_response_unchanged():
    """A normal PayPal response — ids, status, amount, captures list,
    a couple of links — records fully and unchanged. The guard is
    size-driven: it only trims when the body is over the cap."""
    job = _guard_job()
    body = {
        "id": "AUTH-1", "status": "CREATED",
        "purchase_units": [{"payments": {"authorizations": [
            {"id": "AUTH-1", "status": "CREATED"}]}}],
        "links": [{"href": "https://example/approve", "rel": "approve",
                   "method": "GET"},
                  {"href": "https://example/self", "rel": "self",
                   "method": "GET"}],
    }
    EscrowService._record_paypal_response(job, operation="authorize",
                                          status="CREATED", id_="AUTH-1",
                                          body=body)
    assert job.last_paypal_response["body"] == body


def test_guard_trims_huge_links_array():
    """A response with a huge links array records trimmed: the links
    array is cut to its first entry, the audit fields (id, status,
    amount) survive, and the recorded body fits the cap."""
    import json

    job = _guard_job()
    body = {
        "id": "AUTH-1", "status": "CREATED",
        "amount": {"currency_code": "USD", "value": "12.50"},
        "links": [{"href": f"https://example/payments/{i:04d}",
                   "rel": "self", "method": "GET"} for i in range(100)],
    }
    assert len(json.dumps(body).encode()) > 4096  # the test is really over the cap
    EscrowService._record_paypal_response(job, operation="authorize",
                                          status="CREATED", id_="AUTH-1",
                                          body=body)
    recorded = job.last_paypal_response["body"]
    assert len(recorded["links"]) == 1
    assert recorded["links"][0]["href"] == "https://example/payments/0000"
    assert recorded["id"] == "AUTH-1"
    assert recorded["status"] == "CREATED"
    assert recorded["amount"] == {"currency_code": "USD", "value": "12.50"}
    assert len(json.dumps(recorded).encode()) <= 4096


def test_guard_trims_oversized_nested_object():
    """A response with an oversized nested object records trimmed:
    long strings are cut, the audit fields survive, and the recorded
    body fits the cap."""
    import json

    job = _guard_job()
    body = {
        "id": "CAP-1", "status": "COMPLETED",
        "amount": {"currency_code": "USD", "value": "12.50"},
        "details": {"note": "x" * 10000,
                    "nested": {"deep": "y" * 5000}},
    }
    EscrowService._record_paypal_response(job, operation="capture",
                                          status="COMPLETED", id_="CAP-1",
                                          body=body)
    recorded = job.last_paypal_response["body"]
    assert recorded["id"] == "CAP-1"
    assert recorded["status"] == "COMPLETED"
    assert recorded["amount"] == {"currency_code": "USD", "value": "12.50"}
    assert len(recorded["details"]["note"]) <= 512
    assert len(recorded["details"]["nested"]["deep"]) <= 512
    assert len(json.dumps(recorded).encode()) <= 4096


def test_guard_drops_largest_keys_when_still_oversized():
    """When trimming alone is not enough, the largest top-level keys
    are dropped first (deterministic order) until the body fits — the
    small audit keys survive, and the recorded body fits the cap."""
    import json

    job = _guard_job()
    body = {"id": "AUTH-1", "status": "CREATED"}
    for i in range(20):
        body[f"blob_{i:02d}"] = "z" * 500
    EscrowService._record_paypal_response(job, operation="authorize",
                                          status="CREATED", id_="AUTH-1",
                                          body=body)
    recorded = job.last_paypal_response["body"]
    assert recorded["id"] == "AUTH-1"
    assert recorded["status"] == "CREATED"
    assert len(recorded) < len(body)  # some keys were dropped
    assert len(json.dumps(recorded).encode()) <= 4096


def test_guard_never_raises_on_pathological_body():
    """A pathological body degrades to a trimmed record, never an
    exception — a bad audit record must never break a settlement.
    Unserializable values and circular references both land on the
    marker; a non-dict body records as an empty body."""
    job = _guard_job()
    # A set is not JSON-serializable.
    EscrowService._record_paypal_response(
        job, operation="authorize", status="CREATED", id_="AUTH-1",
        body={"id": "AUTH-1", "weird": {1, 2, 3}})
    assert job.last_paypal_response["body"] == {"_trimmed": True}
    # A circular reference.
    circular = {"id": "AUTH-1"}
    circular["self"] = circular
    EscrowService._record_paypal_response(
        job, operation="authorize", status="CREATED", id_="AUTH-1",
        body=circular)
    assert job.last_paypal_response["body"] == {"_trimmed": True}
    # A non-dict body records as an empty body (never raises).
    EscrowService._record_paypal_response(
        job, operation="authorize", status="CREATED", id_="AUTH-1",
        body=["not", "a", "dict"])
    assert job.last_paypal_response["body"] == {}


def test_reconcile_reads_survive_reload_for_all_open_states(tmp_path):
    """All three reconcile branches record the successful read through
    the one shared helper and persist it: a CREATED, a CAPTURED and a
    VOIDED reconcile read each survive a store reload with the
    operation "reconcile" record intact."""
    from app.store import JsonFileStore

    for auth_status, expected_job_status in (
            ("CREATED", "AUTHORIZED"),
            ("CAPTURED", "CAPTURED"),
            ("VOIDED", "VOIDED")):
        path = tmp_path / f"jobs-{auth_status}.json"
        stub = _StubPayPal()
        svc = EscrowService(stub, store=JsonFileStore(path))
        job = svc.create_job(quote=_quote(), payer="payer@example",
                             agent="agent@example", deadline_seconds=3600)
        svc.create_order(job)
        svc.authorize(job)
        stub.auths["AUTH-STUB"]["status"] = auth_status
        if auth_status == "CAPTURED":
            stub.auths["AUTH-STUB"]["captures"] = [{
                "id": "CAPTURE-OUTSIDE", "status": "COMPLETED",
                "amount": {"currency_code": "USD", "value": "12.50"},
                "authorization_id": "AUTH-STUB"}]
        summary = svc.reconcile_open_jobs()
        assert summary["checked"] == 1, auth_status
        reloaded = JsonFileStore(path).get(job.job_id)
        assert reloaded.status == expected_job_status, auth_status
        record = reloaded.last_paypal_response
        assert record["operation"] == "reconcile", auth_status
        assert record["status"] == auth_status, auth_status
        assert record["id"] == "AUTH-STUB", auth_status


def test_failed_reconcile_keeps_delivered_job_untouched():
    """Same failed-reconcile guarantee for a DELIVERED job: the error
    is reported, the job stays DELIVERED with its delivery hash and
    its authorize record intact — both open states are pinned against
    mutation-on-error."""
    import copy

    stub = _StubPayPal()
    svc, job = _stub_authorized_job(stub)
    svc.submit_delivery(job, agent="agent@example", delivery_text="report v1")
    before_record = copy.deepcopy(job.last_paypal_response)
    assert before_record["operation"] == "authorize"
    before_hash = job.delivery_hash
    stub.fail_get_authorization = True
    summary = svc.reconcile_open_jobs()
    assert summary["checked"] == 1
    assert summary["still_pending"] == [job.job_id]
    assert len(summary["errors"]) == 1
    reloaded = svc.get_job(job.job_id)
    assert reloaded.status == "DELIVERED"
    assert reloaded.delivery_hash == before_hash
    assert reloaded.last_paypal_response == before_record

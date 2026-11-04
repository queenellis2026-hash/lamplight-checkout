"""Conformance: FakePayPalClient agrees with scripts/mock_paypal.py.

Phase 1 game plan: the state machine proves properties of the fake.
If the fake disagrees with the real mock on edge behavior, the machine
proves properties of the wrong system. These scripted scenarios run
against BOTH doubles and assert identical observable outcomes.

The fake is stricter on one point by design: same idempotency key +
different params -> 422 (Stripe's contract). The mock replays on key
alone. That case is tested against the fake only.
"""
import pathlib
import subprocess
import sys
import time

import pytest
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.paypal_client import PayPalClient, PayPalError
from tests.fake_paypal import FakePayPalClient

MOCK_PORT = 5011
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
def mock_client(mock_server):
    return PayPalClient(client_id="mock", client_secret="mock",
                        base_url=mock_server)


@pytest.fixture()
def fake_client():
    return FakePayPalClient()


def _approve_mock(order_id):
    requests.post(f"{BASE}/mock/approve/{order_id}",
                  timeout=10).raise_for_status()


def _extract_auth(authorize_result):
    """Pull the authorization from the order-shaped authorize response."""
    pus = authorize_result.get("purchase_units") or []
    return (pus[0].get("payments") or {}).get("authorizations")[0]


def _happy_path(client, approve):
    """create -> approve -> authorize -> capture. Returns (auth, capture)."""
    order = client.create_order(amount_cents=2500, currency="USD",
                                description="conformance",
                                custom_id="conf-1")
    assert order["status"] == "CREATED"
    approve(order["id"])
    result = client.authorize_order(order["id"], request_id="req-auth-1")
    auth = _extract_auth(result)
    assert auth["status"] == "CREATED"
    auth_id = auth["id"]
    cap = client.capture_authorization(auth_id, request_id="req-cap-1")
    assert cap["status"] == "COMPLETED"
    assert cap["amount"]["value"] == "25.00"
    auth_after = client.get_authorization(auth_id)
    assert auth_after["status"] == "CAPTURED"
    return auth, cap


def test_happy_path_agrees(mock_client, fake_client):
    _, mock_cap = _happy_path(mock_client, _approve_mock)
    _, fake_cap = _happy_path(fake_client,
                              lambda oid: fake_client.approve_order(oid))
    assert mock_cap["status"] == fake_cap["status"] == "COMPLETED"
    assert mock_cap["amount"]["value"] == fake_cap["amount"]["value"]


def test_partial_capture_then_void_agrees(mock_client, fake_client):
    def scenario(client, approve):
        order = client.create_order(amount_cents=2500, currency="USD",
                                    description="partial",
                                    custom_id="conf-2")
        approve(order["id"])
        auth = _extract_auth(client.authorize_order(order["id"], request_id="req-auth-2"))
        auth_id = auth["id"]
        cap = client.capture_authorization(auth_id, amount_cents=1000,
                                           request_id="req-cap-2")
        assert cap["amount"]["value"] == "10.00"
        mid = client.get_authorization(auth_id)
        assert mid["status"] == "PARTIALLY_CAPTURED"
        client.void_authorization(auth_id, request_id="req-void-2")
        after = client.get_authorization(auth_id)
        assert after["status"] == "VOIDED"
        return mid["status"], after["status"]

    mock_mid, mock_void = scenario(mock_client, _approve_mock)
    fake_mid, fake_void = scenario(fake_client,
                                   lambda oid: fake_client.approve_order(oid))
    assert (mock_mid, mock_void) == (fake_mid, fake_void)


def test_idempotent_retry_replays(mock_client, fake_client):
    def scenario(client, approve):
        order = client.create_order(amount_cents=2500, currency="USD",
                                    description="idem",
                                    custom_id="conf-3")
        approve(order["id"])
        auth = _extract_auth(client.authorize_order(order["id"], request_id="req-auth-3"))
        auth_id = auth["id"]
        cap1 = client.capture_authorization(auth_id, request_id="req-cap-3")
        cap2 = client.capture_authorization(auth_id, request_id="req-cap-3")
        assert cap1["id"] == cap2["id"], "retry must replay, not re-execute"
        return cap1["id"]

    mock_id = scenario(mock_client, _approve_mock)
    fake_id = scenario(fake_client,
                       lambda oid: fake_client.approve_order(oid))
    assert mock_id and fake_id  # both produced a stable capture id


def test_unapproved_authorize_rejected(mock_client, fake_client):
    """authorize_order on a CREATED (unapproved) order -> 422 on both."""
    for client, approve in ((mock_client, lambda oid: None),
                            (fake_client, lambda oid: None)):
        order = client.create_order(amount_cents=2500, currency="USD",
                                    description="unapproved",
                                    custom_id="conf-4")
        approve(order["id"])
        with pytest.raises(PayPalError) as exc_info:
            client.authorize_order(order["id"], request_id="req-auth-4")
        assert exc_info.value.status == 422


def test_over_capture_rejected(mock_client, fake_client):
    """Capture above the remaining hold -> 422 on both."""
    def scenario(client, approve):
        order = client.create_order(amount_cents=2500, currency="USD",
                                    description="overcap",
                                    custom_id="conf-5")
        approve(order["id"])
        auth = _extract_auth(client.authorize_order(order["id"], request_id="req-auth-5"))
        with pytest.raises(PayPalError) as exc_info:
            client.capture_authorization(auth["id"], amount_cents=99999,
                                         request_id="req-cap-5")
        assert exc_info.value.status == 422

    scenario(mock_client, _approve_mock)
    scenario(fake_client, lambda oid: fake_client.approve_order(oid))


def test_fake_rejects_key_reuse_with_different_params(fake_client):
    """The fake enforces Stripe's contract: same key + different params
    -> 422. (The mock replays on key alone; the fake is stricter by
    design — documented in fake_paypal.py.)"""
    order = fake_client.create_order(amount_cents=2500, currency="USD",
                                     description="keyreuse",
                                     custom_id="conf-6")
    fake_client.approve_order(order["id"])
    auth = _extract_auth(fake_client.authorize_order(order["id"], request_id="req-auth-6"))
    fake_client.capture_authorization(auth["id"], amount_cents=1000,
                                      request_id="req-cap-6")
    with pytest.raises(PayPalError) as exc_info:
        fake_client.capture_authorization(auth["id"], amount_cents=1500,
                                          request_id="req-cap-6")
    assert exc_info.value.status == 422
    assert exc_info.value.body["name"] == "IDEMPOTENCY_KEY_IN_USE"

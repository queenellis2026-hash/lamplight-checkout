"""v8.14 storefront gate: POST /jobs and GET /jobs require a shared-secret
bearer token (demo-grade, honest about it).

- No token -> 401
- Wrong token -> 401
- Storefront token -> pass
- Payer token -> pass (already-authed callers not broken)
- Agent token -> pass
- TESTING config bypasses the gate (the rest of the suite relies on this)

Env: LAMPLIGHT_DEMO_AUTH=1 is set by conftest, so the demo storefront
token default is active.
"""
from __future__ import annotations

import os

import pytest

from app import server as server_mod
from app.auth import (DEMO_AGENT_TOKENS, DEMO_PAYER_TOKEN,
                      DEMO_STOREFRONT_TOKEN, require_storefront_token,
                      storefront_token)
from app.server import app as flask_app


@pytest.fixture()
def live_client(monkeypatch):
    """Test client with TESTING off so the gate actually runs.

    Sets LAMPLIGHT_STORE=memory so the v8.13 legacy-JSON boot guard
    (which correctly refuses to strand the repo's live jobs.json) does
    not fire — the gate is what we are testing here, not the guard.
    """
    monkeypatch.setenv("LAMPLIGHT_STORE", "memory")
    flask_app.config["TESTING"] = False
    flask_app.testing = False
    # v8.14: the Create Order budget is no longer bypassed by TESTING
    # (Grok latest) — these gate tests are not budget tests, so opt
    # out explicitly to keep them deterministic.
    flask_app.config["BYPASS_ORDER_BUDGET"] = True
    c = flask_app.test_client()
    yield c
    flask_app.config["TESTING"] = True
    flask_app.testing = False
    flask_app.config.pop("BYPASS_ORDER_BUDGET", None)


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --- auth-level unit tests ----------------------------------------------

def test_storefront_token_demo_default():
    assert os.environ.get("LAMPLIGHT_DEMO_AUTH") == "1"
    assert storefront_token() == DEMO_STOREFRONT_TOKEN


def test_storefront_token_env_override(monkeypatch):
    monkeypatch.setenv("LAMPLIGHT_DEMO_TOKEN", "my-real-secret")
    assert storefront_token() == "my-real-secret"


def test_require_storefront_token_accepts_storefront():
    class R:
        headers = {"Authorization": f"Bearer {DEMO_STOREFRONT_TOKEN}"}
    require_storefront_token(R())  # no raise


def test_require_storefront_token_accepts_payer():
    class R:
        headers = {"Authorization": f"Bearer {DEMO_PAYER_TOKEN}"}
    require_storefront_token(R())  # no raise


def test_require_storefront_token_accepts_agent():
    agent_token = next(iter(DEMO_AGENT_TOKENS.values()))
    class R:
        headers = {"Authorization": f"Bearer {agent_token}"}
    require_storefront_token(R())  # no raise


def test_require_storefront_token_rejects_unknown():
    from app.auth import AuthError
    class R:
        headers = {"Authorization": "Bearer nope-wrong-token"}
    with pytest.raises(AuthError) as ei:
        require_storefront_token(R())
    assert ei.value.status == 401


def test_require_storefront_token_rejects_missing():
    from app.auth import AuthError
    class R:
        headers = {}
    with pytest.raises(AuthError) as ei:
        require_storefront_token(R())
    assert ei.value.status == 401


# --- integration tests through the Flask app ---------------------------

def test_post_jobs_no_token_401(live_client):
    r = live_client.post("/jobs", json={"brief": "audit, budget $50"})
    assert r.status_code == 401, r.get_json()


def test_post_jobs_wrong_token_401(live_client):
    r = live_client.post("/jobs", json={"brief": "audit, budget $50"},
                         headers=_h("wrong"))
    assert r.status_code == 401


def test_get_jobs_no_token_401(live_client):
    r = live_client.get("/jobs")
    assert r.status_code == 401


def test_get_jobs_wrong_token_401(live_client):
    r = live_client.get("/jobs", headers=_h("wrong"))
    assert r.status_code == 401


def test_post_jobs_storefront_token_passes_gate(live_client):
    # 201 if the PayPal order succeeds, 503 if not — either way, NOT 401.
    # (No mock PayPal running here; the gate is what we are testing.)
    r = live_client.post("/jobs", json={"brief": "audit, budget $50"},
                         headers=_h(DEMO_STOREFRONT_TOKEN))
    assert r.status_code in (201, 503), r.get_json()
    assert r.status_code != 401


def test_post_jobs_payer_token_passes_gate(live_client):
    r = live_client.post("/jobs", json={"brief": "audit, budget $50"},
                         headers=_h(DEMO_PAYER_TOKEN))
    assert r.status_code in (201, 503), r.get_json()
    assert r.status_code != 401


def test_get_jobs_storefront_token_passes_gate(live_client):
    # The gate passed (not 401). The service behind it may 503 without
    # PayPal credentials — that is not the gate's concern.
    r = live_client.get("/jobs", headers=_h(DEMO_STOREFRONT_TOKEN))
    assert r.status_code != 401, r.get_json()


def test_testing_bypass_still_works():
    # The rest of the suite calls these endpoints with no headers under
    # TESTING=True — the gate must not fire there (no 401).
    flask_app.config["TESTING"] = True
    c = flask_app.test_client()
    r = c.get("/jobs")
    assert r.status_code != 401, r.get_json()

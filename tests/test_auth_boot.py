"""Item 1 (production-readiness): demo credentials must not be live defaults.

The server must REFUSE TO BOOT unless LAMPLIGHT_PAYER_TOKEN and
LAMPLIGHT_AGENT_TOKENS are set to real values — same pattern as the
DISPATCHER_INTENT_SECRET boot check. Demo mode only via explicit opt-in
LAMPLIGHT_DEMO_AUTH=1 (never default).
"""
import json
import os

import pytest

from app import auth


def _clean_env(monkeypatch):
    for var in ("LAMPLIGHT_PAYER_TOKEN", "LAMPLIGHT_AGENT_TOKENS",
                "LAMPLIGHT_DEMO_TOKEN", "LAMPLIGHT_DEMO_AUTH"):
        monkeypatch.delenv(var, raising=False)


def test_boot_refuses_unset_tokens_without_optin(monkeypatch):
    """No tokens + no opt-in → validate_auth_config raises (fail closed)."""
    _clean_env(monkeypatch)
    with pytest.raises(RuntimeError, match="refusing to start"):
        auth.validate_auth_config()


def test_payer_token_raises_without_config(monkeypatch):
    """payer_token() must not silently return the demo default."""
    _clean_env(monkeypatch)
    with pytest.raises(RuntimeError, match="LAMPLIGHT_PAYER_TOKEN"):
        auth.payer_token()


def test_agent_tokens_raises_without_config(monkeypatch):
    _clean_env(monkeypatch)
    with pytest.raises(RuntimeError, match="LAMPLIGHT_AGENT_TOKENS"):
        auth.agent_tokens()


def test_demo_optin_allows_demo_defaults(monkeypatch):
    """Explicit LAMPLIGHT_DEMO_AUTH=1 keeps the demo working (tests use this)."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("LAMPLIGHT_DEMO_AUTH", "1")
    auth.validate_auth_config()  # must not raise
    assert auth.payer_token() == auth.DEMO_PAYER_TOKEN
    assert auth.agent_tokens() == dict(auth.DEMO_AGENT_TOKENS)


def test_real_tokens_pass_boot(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("LAMPLIGHT_PAYER_TOKEN", "prod-payer-secret-xyz")
    monkeypatch.setenv("LAMPLIGHT_AGENT_TOKENS", json.dumps(
        {"agent-1@example.com": "prod-agent-secret-abc"}))
    # v8.14: the storefront gate token is also required at boot.
    monkeypatch.setenv("LAMPLIGHT_DEMO_TOKEN", "prod-storefront-secret-123")
    auth.validate_auth_config()  # must not raise
    assert auth.payer_token() == "prod-payer-secret-xyz"
    assert auth.agent_tokens() == {"agent-1@example.com": "prod-agent-secret-abc"}
    assert auth.storefront_token() == "prod-storefront-secret-123"


def test_storefront_token_raises_without_config(monkeypatch):
    """storefront_token() must not silently return the demo default."""
    _clean_env(monkeypatch)
    with pytest.raises(RuntimeError, match="LAMPLIGHT_DEMO_TOKEN"):
        auth.storefront_token()


def test_demo_optin_allows_storefront_default(monkeypatch):
    """Explicit LAMPLIGHT_DEMO_AUTH=1 keeps the demo working."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("LAMPLIGHT_DEMO_AUTH", "1")
    assert auth.storefront_token() == auth.DEMO_STOREFRONT_TOKEN


def test_demo_values_via_env_are_rejected(monkeypatch):
    """Copy-pasting the demo tokens into env must not pass the boot check."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("LAMPLIGHT_PAYER_TOKEN", auth.DEMO_PAYER_TOKEN)
    monkeypatch.setenv("LAMPLIGHT_AGENT_TOKENS", json.dumps(
        dict(auth.DEMO_AGENT_TOKENS)))
    with pytest.raises(RuntimeError, match="demo"):
        auth.validate_auth_config()


def test_authenticate_rejects_demo_token_without_optin(monkeypatch):
    """With real tokens configured, the demo token must not authenticate."""

    class Req:
        headers = {"Authorization": "Bearer demo-payer-token"}

    _clean_env(monkeypatch)
    monkeypatch.setenv("LAMPLIGHT_PAYER_TOKEN", "prod-payer-secret-xyz")
    monkeypatch.setenv("LAMPLIGHT_AGENT_TOKENS", json.dumps(
        {"agent-1@example.com": "prod-agent-secret-abc"}))
    with pytest.raises(auth.AuthError):
        auth.authenticate(Req(), payer_id="payer@example.com")


def test_server_boot_check_calls_auth_validation(monkeypatch):
    """_boot_check() must include the auth validation (not just the secret)."""
    import app.server as srv

    _clean_env(monkeypatch)
    # Bypass the testing-mode early return and the multi-worker check;
    # we only assert the auth gate is wired into _boot_check.
    monkeypatch.setattr(srv.app, "testing", False)
    monkeypatch.setattr("app.dispatcher.validate_config", lambda: None)
    monkeypatch.setattr(srv, "_refuse_multi_worker", lambda: None)
    with pytest.raises(RuntimeError, match="refusing to start"):
        srv._boot_check()

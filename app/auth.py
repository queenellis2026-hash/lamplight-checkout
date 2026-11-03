"""Demo-grade caller authentication for the Lamplight Checkout server.

The escrow state machine (app/escrow.py) already checks "only the payer"
and "only the worker agent" — but over HTTP those checks are meaningless
if the caller can simply *claim* to be the payer or the agent in the
request body. This module closes that hole: mutating endpoints require a
bearer token, and the caller's identity is derived from the token, never
from the body or a default.

How it works
------------
* The payer holds one token (env ``LAMPLIGHT_PAYER_TOKEN``).
* Each worker agent holds its own token (env ``LAMPLIGHT_AGENT_TOKENS``,
  a JSON object mapping the agent's PayPal id to its token).
* The server reads ``Authorization: Bearer <token>`` and returns
  ``("payer", <payer paypal id>)`` or ``("agent", <agent paypal id>)``.
  A missing or unknown token is 401; a valid token for the wrong role is
  403. The request body may still carry a ``payer`` / ``agent`` field,
  but it must agree with the credential — the credential wins.

Demo-only, and honest about it
------------------------------
This is shared-secret auth, not production identity: anyone holding a
token can act as that party. The demo defaults below are NEVER live
defaults — the server refuses to boot unless real tokens are configured
via the env vars, or the explicit opt-in ``LAMPLIGHT_DEMO_AUTH=1`` is
set (local demo and test use only). For any real deployment, set real
tokens via the env vars (never in a file) and replace this with proper
identity (OAuth/JWT or request signatures) before exposing the server.
The demo defaults are deliberately obvious so nobody mistakes them for
secrets — and supplying them via env does not pass the boot check.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os

# Demo-only defaults. Obvious on purpose — they are NEVER live defaults:
# payer_token() / agent_tokens() raise unless real tokens are configured
# via env vars or the explicit demo opt-in below is set. See
# validate_auth_config(), called from app.server._boot_check().
DEMO_PAYER_TOKEN = "demo-payer-token"
DEMO_AGENT_TOKENS = {
    "auditor-01@lamplight.example": "demo-agent-auditor-01",
    "writer-02@lamplight.example": "demo-agent-writer-02",
    "coder-03@lamplight.example": "demo-agent-coder-03",
}

# Explicit demo-mode opt-in. The ONLY way the demo defaults above are
# usable. Never defaulted — production must set real tokens instead.
DEMO_AUTH_OPT_IN_ENV = "LAMPLIGHT_DEMO_AUTH"


def demo_auth_enabled() -> bool:
    """True only when the operator explicitly opted into demo auth."""
    return os.environ.get(DEMO_AUTH_OPT_IN_ENV) == "1"


class AuthError(Exception):
    """Authentication/authorization failure; carries the HTTP status."""

    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def _is_demo_value(value: str) -> bool:
    """True if a configured token is one of the published demo defaults."""
    if value == DEMO_PAYER_TOKEN:
        return True
    return value in DEMO_AGENT_TOKENS.values()


def payer_token() -> str:
    """The payer's bearer token.

    Real token from ``LAMPLIGHT_PAYER_TOKEN``; the demo default only when
    the explicit demo opt-in (``LAMPLIGHT_DEMO_AUTH=1``) is set. Raises
    RuntimeError otherwise — we refuse to authenticate callers against
    a publicly-known demo token.
    """
    configured = os.environ.get("LAMPLIGHT_PAYER_TOKEN")
    if configured and not _is_demo_value(configured):
        return configured
    if demo_auth_enabled():
        return DEMO_PAYER_TOKEN
    raise RuntimeError(
        "LAMPLIGHT_PAYER_TOKEN must be set to a real token — refusing to "
        "start with the demo default. Set LAMPLIGHT_DEMO_AUTH=1 only for "
        "local demo use (see validate_auth_config())."
    )


def agent_tokens() -> dict[str, str]:
    """Agent paypal_id -> bearer token.

    Real mapping from ``LAMPLIGHT_AGENT_TOKENS`` (JSON); the demo defaults
    only when the explicit demo opt-in (``LAMPLIGHT_DEMO_AUTH=1``) is set.
    Raises RuntimeError otherwise.
    """
    raw = os.environ.get("LAMPLIGHT_AGENT_TOKENS")
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            cleaned = {str(k): str(v) for k, v in parsed.items()}
            if cleaned and not any(_is_demo_value(v)
                                   for v in cleaned.values()):
                return cleaned
            # Empty mapping or demo values smuggled in via env — fall
            # through to the refusal below (demo values only with opt-in).
    if demo_auth_enabled():
        return dict(DEMO_AGENT_TOKENS)
    raise RuntimeError(
        "LAMPLIGHT_AGENT_TOKENS must be set to a real JSON mapping — "
        "refusing to start with the demo defaults. Set "
        "LAMPLIGHT_DEMO_AUTH=1 only for local demo use (see "
        "validate_auth_config())."
    )


def validate_auth_config() -> None:
    """Fail fast at startup if caller auth is not securely configured.

    Call once during application boot, before serving traffic (see
    app.server._boot_check). Raises RuntimeError unless real tokens are
    configured via LAMPLIGHT_PAYER_TOKEN / LAMPLIGHT_AGENT_TOKENS, or the
    explicit demo opt-in LAMPLIGHT_DEMO_AUTH=1 is set. The published demo
    defaults are refused even when supplied via env — they are public.
    """
    if demo_auth_enabled():
        return
    payer_token()
    agent_tokens()


def authenticate(request, *, payer_id: str) -> tuple[str, str]:
    """Identify the caller from its bearer token.

    Returns ("payer", payer_id) or ("agent", agent_paypal_id).
    Raises AuthError(401) for a missing/unknown token.
    """
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise AuthError("missing Authorization: Bearer token", 401)
    token = header[len("Bearer "):].strip()
    if not token:
        raise AuthError("missing Authorization: Bearer token", 401)
    if hmac.compare_digest(token, payer_token()):
        return ("payer", payer_id)
    for agent_id, agent_token in agent_tokens().items():
        if hmac.compare_digest(token, agent_token):
            return ("agent", agent_id)
    raise AuthError("unknown bearer token", 401)


def require_payer(request, *, payer_id: str) -> str:
    """The caller must be the payer. Returns the payer id. 401/403 on failure."""
    role, identity = authenticate(request, payer_id=payer_id)
    if role != "payer":
        raise AuthError("this endpoint requires the payer", 403)
    return identity


def require_agent(request, *, payer_id: str) -> str:
    """The caller must be a worker agent. Returns the agent's paypal id."""
    role, identity = authenticate(request, payer_id=payer_id)
    if role != "agent":
        raise AuthError("this endpoint requires a worker agent", 403)
    return identity

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
token can act as that party, and the demo defaults below let the demo
run out of the box. For any real deployment, set real tokens via the
env vars (never in a file) and replace this with proper identity
(OAuth/JWT or request signatures) before exposing the server. The demo
defaults are deliberately obvious so nobody mistakes them for secrets.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os

# Demo-only defaults. Obvious on purpose — set real tokens via env vars
# for anything beyond a local demo.
DEMO_PAYER_TOKEN = "demo-payer-token"
DEMO_AGENT_TOKENS = {
    "auditor-01@lamplight.example": "demo-agent-auditor-01",
    "writer-02@lamplight.example": "demo-agent-writer-02",
    "coder-03@lamplight.example": "demo-agent-coder-03",
}


class AuthError(Exception):
    """Authentication/authorization failure; carries the HTTP status."""

    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def payer_token() -> str:
    """The payer's bearer token (env override, else the demo default)."""
    return os.environ.get("LAMPLIGHT_PAYER_TOKEN") or DEMO_PAYER_TOKEN


def agent_tokens() -> dict[str, str]:
    """Agent paypal_id -> bearer token (env JSON override, else demo defaults)."""
    raw = os.environ.get("LAMPLIGHT_AGENT_TOKENS")
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items()}
        return {}
    return dict(DEMO_AGENT_TOKENS)


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


def token_fingerprint(token: str) -> str:
    """Short non-secret fingerprint for logs (first 4 chars of a hash)."""
    return hashlib.sha256(token.encode()).hexdigest()[:8]

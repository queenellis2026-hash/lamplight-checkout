"""Phase 2 CI gate: every mutating POST route has an idempotency contract.

Enumerates app.url_map for POST routes and asserts each one is either in
the contract registry (tests/test_idempotency_contracts.py:
IDEMPOTENCY_CONTRACT_ROUTES) or in the explicit skip list below.

A new POST route without a registered contract test FAILS this gate —
the contract is opt-out by explicit decision, never by omission.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.server import app as flask_app
from tests.test_idempotency_contracts import IDEMPOTENCY_CONTRACT_ROUTES

# POST routes that are deliberately NOT under the Idempotency-Key
# contract, with the reason. Everything else must be registered.
SKIP_ROUTES = {
    # Webhooks carry PayPal's own idempotency (signature + the
    # webhook_event_claims lifecycle in app/store.py). The client is
    # PayPal, not us — an Idempotency-Key header from PayPal is not
    # part of the webhook contract.
    "/webhooks/paypal": "PayPal webhook idempotency via event claims",
    # Test-only proxy that forwards to the mock PayPal subprocess.
    # Not part of the API surface.
    "/mock/<path:subpath>": "test mock proxy, not API surface",
}


def _post_rules():
    rules = set()
    for rule in flask_app.url_map.iter_rules():
        if "POST" in rule.methods:
            rules.add(str(rule.rule))
    return rules


def test_all_post_routes_have_contracts():
    post_rules = _post_rules()
    registered = set(IDEMPOTENCY_CONTRACT_ROUTES)
    skipped = set(SKIP_ROUTES)
    missing = post_rules - registered - skipped
    assert not missing, (
        "POST routes without an idempotency contract test: "
        f"{sorted(missing)}. Add the route to IDEMPOTENCY_CONTRACT_ROUTES "
        "in tests/test_idempotency_contracts.py (with setup/request/effect "
        "scenario), or to SKIP_ROUTES here with a written reason.")


def test_registry_has_no_stale_entries():
    """The registry must not list routes that no longer exist — a stale
    entry hides a renamed route from the gate."""
    post_rules = _post_rules()
    registered = set(IDEMPOTENCY_CONTRACT_ROUTES)
    stale = registered - post_rules
    assert not stale, (
        f"contract registry lists non-existent routes: {sorted(stale)}")


def test_skip_list_has_no_stale_entries():
    post_rules = _post_rules()
    skipped = set(SKIP_ROUTES)
    stale = skipped - post_rules
    assert not stale, (
        f"skip list references non-existent routes: {sorted(stale)}")


def test_every_skipped_route_documents_a_reason():
    for rule, reason in SKIP_ROUTES.items():
        assert isinstance(reason, str) and len(reason) >= 10, (
            f"skip entry for {rule} needs a real reason, not {reason!r}")

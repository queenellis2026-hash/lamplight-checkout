
"""Test session fixtures for the PayPal escrow dispatcher.

Sets a deterministic test secret for DISPATCHER_INTENT_SECRET so that
intent-key generation works in tests. Production MUST set its own
per-deployment secret (see app.dispatcher.validate_config()).
"""
import os

os.environ.setdefault(
    "DISPATCHER_INTENT_SECRET",
    "test-only-secret-not-for-production-use",
)

# v8.2 (production-readiness item 1): the app refuses to boot on the
# demo bearer tokens unless this explicit opt-in is set. The test suite
# is demo use by design, so opt in here — production must set real
# LAMPLIGHT_PAYER_TOKEN / LAMPLIGHT_AGENT_TOKENS instead.
os.environ.setdefault("LAMPLIGHT_DEMO_AUTH", "1")

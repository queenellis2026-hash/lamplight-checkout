"""Tests for app/webhooks.py — the §4.7 webhook gateway."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.webhooks import (
    PAYPAL_TO_DISPATCHER,
    WebhookIdempotencyCache,
    handle_webhook,
    translate_paypal_event,
    verify_paypal_signature,
)


def _now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _headers(**overrides):
    hdrs = {
        "PAYPAL-AUTH-ALGO": "SHA256withRSA",
        "PAYPAL-TRANSMISSION-SIG": "MOCK-SIGNATURE",
        "PAYPAL-CERT-URL": "https://api-m.sandbox.paypal.com/v1/"
                           "notifications/certs/CERT-123",
        "PAYPAL-TRANSMISSION-ID": "TRANS-1",
        "PAYPAL-TRANSMISSION-TIME": _now_iso(),
    }
    hdrs.update(overrides)
    return hdrs


def _body(event_type="PAYMENT.AUTHORIZATION.CREATED",
          auth_id="AUTH-1"):
    return json.dumps({
        "id": "WH-EVENT-1",
        "event_type": event_type,
        "resource": {"id": auth_id, "status": "CREATED"},
    }).encode("utf-8")


# ---------------------------------------------------------------------------
# Signature validation
# ---------------------------------------------------------------------------

def test_verify_valid_headers():
    """All headers present, valid structure → True."""
    assert verify_paypal_signature(_headers(), _body(), "MOCK-WEBHOOK-ID") is True


def test_verify_missing_header():
    """Missing PAYPAL-TRANSMISSION-SIG → False."""
    hdrs = _headers()
    del hdrs["PAYPAL-TRANSMISSION-SIG"]
    assert verify_paypal_signature(hdrs, _body(), "MOCK-WEBHOOK-ID") is False


def test_verify_bad_cert_domain():
    """Cert URL not on paypal.com → False."""
    hdrs = _headers(**{"PAYPAL-CERT-URL": "https://evil.example.com/cert.pem"})
    assert verify_paypal_signature(hdrs, _body(), "MOCK-WEBHOOK-ID") is False


def test_verify_stale_timestamp():
    """Transmission time older than 72 hours → False (Claude v5 review:
    widened from 1 hour so PayPal's retries survive an outage)."""
    old = (datetime.now(timezone.utc) - timedelta(hours=73)).isoformat().replace("+00:00", "Z")
    hdrs = _headers(**{"PAYPAL-TRANSMISSION-TIME": old})
    assert verify_paypal_signature(hdrs, _body(), "MOCK-WEBHOOK-ID") is False


def test_verify_empty_body():
    """Empty body → False."""
    assert verify_paypal_signature(_headers(), b"", "MOCK-WEBHOOK-ID") is False


def test_verify_header_case_insensitive():
    """Header names match case-insensitively (HTTP semantics)."""
    hdrs = {k.lower(): v for k, v in _headers().items()}
    assert verify_paypal_signature(hdrs, _body(), "MOCK-WEBHOOK-ID") is True


# ---------------------------------------------------------------------------
# Idempotency cache
# ---------------------------------------------------------------------------

def test_idempotency_duplicate():
    """Same transmission ID twice → second is duplicate."""
    cache = WebhookIdempotencyCache()
    assert cache.is_duplicate("TRANS-9") is False
    assert cache.is_duplicate("TRANS-9") is True


def test_idempotency_distinct_ids():
    """Different transmission IDs are not duplicates."""
    cache = WebhookIdempotencyCache()
    assert cache.is_duplicate("TRANS-A") is False
    assert cache.is_duplicate("TRANS-B") is False


# ---------------------------------------------------------------------------
# Event translation
# ---------------------------------------------------------------------------

def test_translate_known_events():
    """All 5 mapped events translate correctly."""
    assert translate_paypal_event("PAYMENT.AUTHORIZATION.CREATED") == "WH_AUTH_CREATED"
    assert translate_paypal_event("PAYMENT.AUTHORIZATION.VOIDED") == "WH_AUTH_VOIDED"
    assert translate_paypal_event("PAYMENT.CAPTURE.COMPLETED") == "WH_CAP_COMPLETED"
    assert translate_paypal_event("PAYMENT.CAPTURE.DENIED") == "WH_CAP_DENIED"
    # v8.14: refunds route to the capture-hint (fresh-read reconcile).
    assert translate_paypal_event("PAYMENT.CAPTURE.REFUNDED") == "WH_CAP_COMPLETED"
    assert set(PAYPAL_TO_DISPATCHER) == {
        "PAYMENT.AUTHORIZATION.CREATED",
        "PAYMENT.AUTHORIZATION.VOIDED",
        "PAYMENT.CAPTURE.COMPLETED",
        "PAYMENT.CAPTURE.DENIED",
        "PAYMENT.CAPTURE.REFUNDED",
    }


def test_translate_unknown():
    """Unmapped event → WH_UNKNOWN_TYPE."""
    assert translate_paypal_event("PAYMENT.SALE.REFUNDED") == "WH_UNKNOWN_TYPE"
    assert translate_paypal_event("") == "WH_UNKNOWN_TYPE"


# ---------------------------------------------------------------------------
# Gateway handler
# ---------------------------------------------------------------------------

class _FakeService:
    """Minimal service duck-typed on the webhooks.py protocol."""

    def __init__(self, jobs=None, *, fail_reconcile=False):
        self.jobs = jobs or {}
        self.reconciled = []
        self.fail_reconcile = fail_reconcile

    def find_job_by_resource_id(self, resource_id):
        return self.jobs.get(resource_id)

    def job_is_settled(self, job):
        return bool(job.get("settled"))

    def reconcile_job(self, job):
        if self.fail_reconcile:
            raise RuntimeError("paypal 500")
        self.reconciled.append(job["job_id"])
        return {"job_id": job["job_id"], "updated": True, "error": ""}

    def handle_webhook_event(self, event):
        """Grok v7.1: the gateway now calls this instead of reconcile_job.
        Default: successful reconcile outcome."""
        if self.fail_reconcile:
            raise RuntimeError("paypal 500")
        self.reconciled.append(event.get("id", "unknown"))
        return {"event_id": event.get("id", ""),
                "event_type": event.get("event_type", ""),
                "job_id": "", "action": "reconciled", "error": ""}


def _handle(service, *, headers=None, body=None, transmission_id="TRANS-WH-1",
            allow_unverified=True):
    # allow_unverified=True: these tests use a fake service with no
    # PayPal client — they exercise the routing/reconcile flow, not
    # signature verification (Claude HIGH #2: production defaults to
    # fail-closed).
    hdrs = _headers(**{"PAYPAL-TRANSMISSION-ID": transmission_id})
    if headers is not None:
        hdrs = headers
    return handle_webhook(service, hdrs,
                          body if body is not None else _body(),
                          "MOCK-WEBHOOK-ID", WebhookIdempotencyCache(),
                          allow_unverified=allow_unverified)


def test_handle_ok_triggers_reconcile():
    """Valid webhook for a live job → ok, and the service reconciles
    (fresh PayPal read + E_RECONCILE) instead of trusting the payload."""
    job = {"job_id": "JOB-1", "settled": False}
    service = _FakeService({"AUTH-1": job})
    result = _handle(service)
    assert result["status"] == "ok"
    assert result["event"] == "WH_AUTH_CREATED"
    assert result["resource_id"] == "AUTH-1"
    assert service.reconciled == ["WH-EVENT-1"]


def test_handle_duplicate_webhook():
    """Duplicate → {"status": "duplicate"}, no dispatch on the retry."""
    job = {"job_id": "JOB-1", "settled": False}
    service = _FakeService({"AUTH-1": job})
    cache = WebhookIdempotencyCache()
    first = handle_webhook(service, _headers(), _body(), "MOCK-WEBHOOK-ID",
                           cache, allow_unverified=True)
    assert first["status"] == "ok"
    assert first["http_status"] == 200
    second = handle_webhook(service, _headers(), _body(), "MOCK-WEBHOOK-ID",
                            cache, allow_unverified=True)
    assert second == {"status": "duplicate",
                      "event": "WH_DUPLICATE",
                      "transmission_id": "TRANS-1",
                      "http_status": 200}
    assert service.reconciled == ["WH-EVENT-1"]  # no second dispatch


def test_handle_invalid_signature():
    """Bad headers → {"status": "invalid"}."""
    service = _FakeService()
    hdrs = _headers()
    del hdrs["PAYPAL-TRANSMISSION-SIG"]
    result = handle_webhook(service, hdrs, _body(), "MOCK-WEBHOOK-ID",
                            WebhookIdempotencyCache())
    assert result["status"] == "invalid"


def test_handle_unknown_event_type():
    """Unmapped PayPal event → unknown / WH_UNKNOWN_TYPE, no dispatch."""
    service = _FakeService({"AUTH-1": {"job_id": "JOB-1"}})
    result = _handle(service, body=_body("PAYMENT.SALE.REFUNDED"))
    assert result["status"] == "unknown"
    assert result["event"] == "WH_UNKNOWN_TYPE"
    assert service.reconciled == []


def test_handle_no_matching_job():
    """No job for the resource id → unknown / WH_NO_JOB."""
    service = _FakeService({})
    result = _handle(service)
    assert result["status"] == "unknown"
    assert result["event"] == "WH_NO_JOB"


def test_handle_settled_job():
    """Settled job → ok / WH_JOB_SETTLED, no reconcile."""
    service = _FakeService({"AUTH-1": {"job_id": "JOB-1", "settled": True}})
    result = _handle(service)
    assert result["status"] == "ok"
    assert result["event"] == "WH_JOB_SETTLED"
    assert service.reconciled == []


def test_handle_read_failure():
    """Failed fresh PayPal read → error / WH_READ_FAIL with http 503
    (HIGH #1: PayPal retries), and the transmission id is NOT marked —
    the retry must be processed, not dropped as a duplicate."""
    service = _FakeService({"AUTH-1": {"job_id": "JOB-1"}},
                           fail_reconcile=True)
    cache = WebhookIdempotencyCache()
    hdrs = _headers(**{"PAYPAL-TRANSMISSION-ID": "TRANS-RETRY-1"})
    result = handle_webhook(service, hdrs, _body(), "MOCK-WEBHOOK-ID", cache,
                            allow_unverified=True)
    assert result["status"] == "error"
    assert result["event"] == "WH_READ_FAIL"
    assert result["http_status"] == 503
    # The id was NOT marked: the retry is accepted, not a duplicate.
    assert cache.seen("TRANS-RETRY-1") is False
    service.fail_reconcile = False
    retry = handle_webhook(service, hdrs, _body(), "MOCK-WEBHOOK-ID", cache,
                           allow_unverified=True)
    assert retry["status"] == "ok"
    assert retry["http_status"] == 200
    assert service.reconciled == ["WH-EVENT-1"]


def test_handle_capture_event_uses_related_auth_id():
    """Capture events name the authorization in
    supplementary_data.related_ids.authorization_id."""
    job = {"job_id": "JOB-2", "settled": False}
    service = _FakeService({"AUTH-2": job})
    body = json.dumps({
        "id": "WH-EVENT-2",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {
            "id": "CAP-1", "status": "COMPLETED",
            "supplementary_data": {"related_ids": {"authorization_id": "AUTH-2"}},
        },
    }).encode("utf-8")
    result = _handle(service, body=body, transmission_id="TRANS-WH-2")
    assert result["status"] == "ok"
    assert result["event"] == "WH_CAP_COMPLETED"
    assert service.reconciled == ["WH-EVENT-2"]


# ---------------------------------------------------------------------------
# HIGH #1: cache bounds (TTL + max entries)
# ---------------------------------------------------------------------------

def test_cache_ttl_evicts_old_entries():
    """Entries older than the TTL are evicted — the cache cannot grow
    forever."""
    import time as _time
    cache = WebhookIdempotencyCache(ttl_seconds=60, max_entries=10000)
    old = _time.monotonic() - 120  # 2 minutes ago > 60s TTL
    cache.mark("TRANS-OLD", now=old)
    cache.mark("TRANS-NEW")
    assert cache.seen("TRANS-OLD") is False  # evicted
    assert cache.seen("TRANS-NEW") is True


def test_cache_max_entries_evicts_oldest():
    """Past max_entries, the oldest-marked entry is evicted first."""
    import time as _time
    cache = WebhookIdempotencyCache(ttl_seconds=3600, max_entries=3)
    base = _time.monotonic()
    cache.mark("T1", now=base)
    cache.mark("T2", now=base + 1)
    cache.mark("T3", now=base + 2)
    cache.mark("T4", now=base + 3)  # evicts T1
    assert cache.seen("T1") is False
    assert cache.seen("T2") is True
    assert cache.seen("T4") is True
    assert len(cache) == 3


def test_cache_peek_does_not_record():
    """seen() is a pure peek — it never marks."""
    cache = WebhookIdempotencyCache()
    assert cache.seen("TRANS-X") is False
    assert cache.seen("TRANS-X") is False  # still not recorded
    cache.mark("TRANS-X")
    assert cache.seen("TRANS-X") is True


# ---------------------------------------------------------------------------
# HIGH #2: production verification via PayPal API
# ---------------------------------------------------------------------------

class _FakePaypalVerify:
    """Fake PayPal client exposing verify_webhook_signature."""

    def __init__(self, *, result=True, fail=False):
        self.result = result
        self.fail = fail
        self.calls = []

    def verify_webhook_signature(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            from app.paypal_client import PayPalError
            raise PayPalError("paypal 500", status=500)
        return self.result


class _FakeServiceWithPaypal(_FakeService):
    def __init__(self, jobs=None, *, paypal=None, **kwargs):
        super().__init__(jobs, **kwargs)
        self.paypal = paypal


def _handle_with_paypal(service, paypal, *, transmission_id="TRANS-V-1",
                        body=None, allow_unverified=False):
    hdrs = _headers(**{"PAYPAL-TRANSMISSION-ID": transmission_id})
    service.paypal = paypal
    return handle_webhook(service, hdrs, body if body is not None else _body(),
                          "MOCK-WEBHOOK-ID",
                          WebhookIdempotencyCache(),
                          allow_unverified=allow_unverified)


def test_verify_api_success_passes():
    """PayPal API says SUCCESS → delivery processed."""
    job = {"job_id": "JOB-1", "settled": False}
    service = _FakeServiceWithPaypal({"AUTH-1": job})
    result = _handle_with_paypal(service, _FakePaypalVerify(result=True))
    assert result["status"] == "ok"
    assert result["http_status"] == 200
    assert service.reconciled == ["WH-EVENT-1"]


def test_verify_api_failure_rejected():
    """PayPal API says FAILURE → invalid (400), no dispatch."""
    job = {"job_id": "JOB-1", "settled": False}
    service = _FakeServiceWithPaypal({"AUTH-1": job})
    result = _handle_with_paypal(service, _FakePaypalVerify(result=False))
    assert result["status"] == "invalid"
    assert result["http_status"] == 400
    assert service.reconciled == []


def test_verify_api_transport_error_retries():
    """PayPal API unreachable → error (503) so PayPal retries; the
    transmission id is NOT marked."""
    job = {"job_id": "JOB-1", "settled": False}
    service = _FakeServiceWithPaypal({"AUTH-1": job})
    cache = WebhookIdempotencyCache()
    hdrs = _headers(**{"PAYPAL-TRANSMISSION-ID": "TRANS-V-2"})
    service.paypal = _FakePaypalVerify(fail=True)
    result = handle_webhook(service, hdrs, _body(), "MOCK-WEBHOOK-ID", cache)
    assert result["status"] == "error"
    assert result["event"] == "WH_VERIFY_FAIL"
    assert result["http_status"] == 503
    assert cache.seen("TRANS-V-2") is False


def test_verify_passes_raw_event_body():
    """The RAW delivery bytes are what PayPal verifies — never a
    re-serialized dict (Claude HIGH #1). The client splices the exact
    bytes into the verify request."""
    import json
    job = {"job_id": "JOB-1", "settled": False}
    service = _FakeServiceWithPaypal({"AUTH-1": job})
    paypal = _FakePaypalVerify(result=True)
    raw_body = _body()
    _handle_with_paypal(service, paypal, body=raw_body)
    assert len(paypal.calls) == 1
    call = paypal.calls[0]
    assert call["webhook_id"] == "MOCK-WEBHOOK-ID"
    assert call["transmission_id"] == "TRANS-V-1"
    # Raw bytes, byte-identical to the delivery (not a re-serialized dict).
    assert isinstance(call["webhook_event"], (bytes, bytearray))
    assert bytes(call["webhook_event"]) == bytes(raw_body)
    # And the bytes still parse to the expected event.
    parsed = json.loads(bytes(call["webhook_event"]))
    assert parsed["event_type"] == "PAYMENT.AUTHORIZATION.CREATED"
    assert parsed["resource"]["id"] == "AUTH-1"


def test_webhook_id_missing_raises():
    """No webhook id (arg or env) → ValueError with a clear message."""
    import os
    service = _FakeService()
    old = os.environ.pop("PAYPAL_WEBHOOK_ID", None)
    try:
        with pytest.raises(ValueError, match="PAYPAL_WEBHOOK_ID"):
            handle_webhook(service, _headers(), _body(), None,
                           WebhookIdempotencyCache())
    finally:
        if old is not None:
            os.environ["PAYPAL_WEBHOOK_ID"] = old


def test_no_client_without_allow_unverified_is_503():
    """Claude HIGH #2: fail CLOSED. A service with no PayPal client
    (and no allow_unverified=True) answers 503 — never falls back to
    the structural check alone."""
    job = {"job_id": "JOB-1", "settled": False}
    service = _FakeService({"AUTH-1": job})
    assert not hasattr(service, "paypal")
    result = handle_webhook(service, _headers(), _body(), "MOCK-WEBHOOK-ID",
                            WebhookIdempotencyCache(),
                            allow_unverified=False)
    assert result["status"] == "error"
    assert result["event"] == "WH_VERIFY_UNAVAILABLE"
    assert result["http_status"] == 503


def test_no_client_with_allow_unverified_proceeds():
    """allow_unverified=True (tests only) lets a clientless service
    through on the structural check."""
    job = {"job_id": "JOB-1", "settled": False}
    service = _FakeService({"AUTH-1": job})
    result = _handle(service, allow_unverified=True)
    assert result["status"] == "ok"


def test_webhook_id_from_env():
    """PAYPAL_WEBHOOK_ID env var is used when no arg is passed."""
    import os
    job = {"job_id": "JOB-1", "settled": False}
    service = _FakeServiceWithPaypal({"AUTH-1": job})
    paypal = _FakePaypalVerify(result=True)
    service.paypal = paypal
    os.environ["PAYPAL_WEBHOOK_ID"] = "ENV-WEBHOOK-ID"
    try:
        result = handle_webhook(service, _headers(), _body(), None,
                                WebhookIdempotencyCache())
    finally:
        del os.environ["PAYPAL_WEBHOOK_ID"]
    assert result["status"] == "ok"
    assert paypal.calls[0]["webhook_id"] == "ENV-WEBHOOK-ID"


def test_all_results_carry_http_status():
    """Every handle_webhook result carries an http_status for the
    wiring layer."""
    service = _FakeService()
    cache = WebhookIdempotencyCache()
    # invalid
    bad = dict(_headers())
    del bad["PAYPAL-TRANSMISSION-SIG"]
    r = handle_webhook(service, bad, _body(), "W", cache)
    assert r["http_status"] == 400
    # unknown event type
    r = handle_webhook(service, _headers(), _body("PAYMENT.SALE.X"),
                       "W", WebhookIdempotencyCache())
    assert r["http_status"] == 200
    # no job
    r = handle_webhook(service, _headers(), _body(), "W",
                       WebhookIdempotencyCache())
    assert r["http_status"] == 200


def test_reconcile_business_error_returns_200_not_503():
    """Gemini Finding 1: a reconcile that returns {"error": ...} for a
    definitive business outcome (not a transport failure) must NOT
    trigger a 503 retry storm. Only transport errors 503."""
    class _BusinessErrorService(_FakeService):
        def handle_webhook_event(self, event):
            self.reconciled.append(event.get("id", "unknown"))
            # Definitive outcome: capture pending, not a transport failure.
            # Grok v7.1: action "reconciled" maps to 200 (definitive).
            return {"event_id": event.get("id", ""),
                    "event_type": event.get("event_type", ""),
                    "job_id": "JOB-1", "action": "reconciled",
                    "error": "capture pending but the authorization is "
                             "CREATED... retry the capture to replay the key"}

    job = {"job_id": "JOB-1", "settled": False}
    service = _BusinessErrorService({"AUTH-1": job})
    cache = WebhookIdempotencyCache()
    result = handle_webhook(service, _headers(), _body(), "MOCK-WEBHOOK-ID",
                            cache, allow_unverified=True)
    # 200, not 503 — PayPal must NOT retry a definitive outcome.
    assert result["http_status"] == 200
    assert result["status"] == "ok"
    # The id IS marked, so a genuine retry is deduped.
    assert cache.seen("TRANS-1")


def test_reconcile_transport_error_returns_503():
    """Transport failures (PayPalError during the fresh read) DO merit
    a 503 so PayPal retries. The id is NOT marked."""
    class _TransportErrorService(_FakeService):
        def handle_webhook_event(self, event):
            self.reconciled.append(event.get("id", "unknown"))
            # Grok v7.1: action "pending" maps to 503 (not definitive).
            return {"event_id": event.get("id", ""),
                    "event_type": event.get("event_type", ""),
                    "job_id": "JOB-1", "action": "pending",
                    "error": "PayPal API timeout"}

    job = {"job_id": "JOB-1", "settled": False}
    service = _TransportErrorService({"AUTH-1": job})
    cache = WebhookIdempotencyCache()
    result = handle_webhook(service, _headers(), _body(), "MOCK-WEBHOOK-ID",
                            cache, allow_unverified=True)
    assert result["http_status"] == 503
    assert result["status"] == "error"
    # NOT marked — the retry must be processed.
    assert not cache.seen("TRANS-1")


def test_reconcile_unconfirmed_intent_not_acked_stays_retryable():
    """Grok fix #1: a reconcile that returns intent_kept_unconfirmed or
    awaiting (the read lagged — a later re-delivery's read may catch up)
    must NOT be acked. The transmission id stays unmarked and PayPal
    gets 503 so it redelivers. Only definitive business outcomes
    (wedge, settled, no-op) get 200 + mark."""
    class _LaggingReadService(_FakeService):
        def handle_webhook_event(self, event):
            self.reconciled.append(event.get("id", "unknown"))
            # Lagging read: capture in flight, not confirmed on the books.
            # Grok v7.1: action "pending" maps to 503 (not definitive).
            return {"event_id": event.get("id", ""),
                    "event_type": event.get("event_type", ""),
                    "job_id": "JOB-1", "action": "pending",
                    "error": "capture pending but the authorization is "
                             "CREATED... retry the capture to replay the key"}

    job = {"job_id": "JOB-1", "settled": False}
    service = _LaggingReadService({"AUTH-1": job})
    cache = WebhookIdempotencyCache()

    # First delivery → 503, NOT marked.
    result = handle_webhook(service, _headers(), _body(), "MOCK-WEBHOOK-ID",
                            cache, allow_unverified=True)
    assert result["http_status"] == 503
    assert result["status"] == "error"
    assert not cache.seen("TRANS-1")

    # Second delivery with the SAME transmission id is NOT deduped —
    # the reconcile runs again (the redelivery's fresh read may now
    # confirm the capture).
    result2 = handle_webhook(service, _headers(), _body(), "MOCK-WEBHOOK-ID",
                             cache, allow_unverified=True)
    assert result2["http_status"] == 503
    assert service.reconciled == ["WH-EVENT-1", "WH-EVENT-1"]


def test_reconcile_awaiting_flag_not_acked():
    """Grok fix #1 (second flag): "awaiting" (capture pending, waiting
    on webhook or payer retry) is also non-definitive — 503, unmarked,
    redeliverable. Detection is by structured boolean flag, never by
    substring-matching the error string."""
    class _AwaitingService(_FakeService):
        def handle_webhook_event(self, event):
            self.reconciled.append(event.get("id", "unknown"))
            # Grok v7.1: action "pending" maps to 503 (not definitive).
            return {"event_id": event.get("id", ""),
                    "event_type": event.get("event_type", ""),
                    "job_id": "JOB-1", "action": "pending",
                    "error": "capture pending and a non-terminal capture "
                             "is listed — awaiting the webhook or a payer "
                             "retry"}

    job = {"job_id": "JOB-1", "settled": False}
    service = _AwaitingService({"AUTH-1": job})
    cache = WebhookIdempotencyCache()
    result = handle_webhook(service, _headers(), _body(), "MOCK-WEBHOOK-ID",
                            cache, allow_unverified=True)
    assert result["http_status"] == 503
    assert result["status"] == "error"
    assert not cache.seen("TRANS-1")
    # Retry is processed, not deduped.
    result2 = handle_webhook(service, _headers(), _body(), "MOCK-WEBHOOK-ID",
                             cache, allow_unverified=True)
    assert result2["http_status"] == 503
    assert service.reconciled == ["WH-EVENT-1", "WH-EVENT-1"]

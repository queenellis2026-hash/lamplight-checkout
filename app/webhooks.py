"""PayPal webhook gateway — §4.7 routing layer.

Webhooks are HINTS, not commands (see dispatcher.py §4.7): money never
moves on a webhook payload alone. This module validates an incoming
PayPal webhook delivery, dedupes repeat deliveries, translates the
PayPal event name into a dispatcher webhook event, and — for a live,
unsettled job — triggers a fresh PayPal read + E_RECONCILE through the
service, exactly as the dispatcher cells demand ("webhook is hint —
caller issues E_RECONCILE").

The four dispatchable events (§4.5) never go straight to
transition(): the gateway calls service.reconcile_job(job), which does
a fresh PayPal read and dispatches E_RECONCILE with read evidence.
The six routing-layer events (§4.7 — WH_NO_JOB, WH_NO_AUTH_ID,
WH_UNKNOWN_TYPE, WH_JOB_SETTLED, WH_DUPLICATE, WH_READ_FAIL) are
produced here and NEVER reach transition(); they are returned in the
result dict so the HTTP layer can answer PayPal with 200.

Signature verification (HIGH #2, Claude): when the service exposes a
PayPal client (``service.paypal`` with ``verify_webhook_signature``),
the delivery is verified against PayPal's
/v1/notifications/verify-webhook-signature API with the RAW delivery
bytes spliced into the request (never re-serialized — Claude HIGH
#1). The structural header check alone is NOT trusted in production.
When no client is available, the gateway answers 503 unless the
caller passes ``allow_unverified=True`` (tests only) — it never fails
open. A transport failure during verification returns 503 so PayPal
retries the delivery.

Dedupe (HIGH #1): a transmission id is marked seen ONLY after the
reconcile succeeds. A failed fresh read (WH_READ_FAIL) leaves the id
unmarked and answers 503, so PayPal's retry is processed instead of
being dropped as a duplicate.

Service protocol (duck-typed; EscrowService satisfies the reconcile
half — adapt find_job_by_resource_id / job_is_settled at the wiring
site):
    service.find_job_by_resource_id(resource_id) -> job | None
    service.job_is_settled(job) -> bool
    service.reconcile_job(job) -> dict   # fresh PayPal read + E_RECONCILE
                                         # dispatch; raises on read failure
    service.paypal                       # optional: PayPalClient with
                                         # verify_webhook_signature
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
from collections import OrderedDict
from datetime import datetime, timezone

from app.dispatcher import Event

# ---------------------------------------------------------------------------
# Signature validation
# ---------------------------------------------------------------------------

REQUIRED_HEADERS = (
    "PAYPAL-AUTH-ALGO",
    "PAYPAL-TRANSMISSION-SIG",
    "PAYPAL-CERT-URL",
    "PAYPAL-TRANSMISSION-ID",
    "PAYPAL-TRANSMISSION-TIME",
)

#: Timestamp must be within this many seconds of now. Widened from 1
#: hour to 72 hours (Claude v5 review): PayPal retries keep the original
#: transmission time, and an outage longer than an hour would make the
#: "503 so PayPal redelivers" design stop working. Signature
#: verification + idempotency cache are the real protections.
MAX_TRANSMISSION_AGE_SECONDS = 72 * 3600
# A transmission may not be dated meaningfully in the future (clock skew only).
MAX_FUTURE_SKEW_SECONDS = 300

#: Environment variable carrying the PayPal webhook id (HIGH #2).
WEBHOOK_ID_ENV_VAR = "PAYPAL_WEBHOOK_ID"


def _normalize_headers(headers: dict) -> dict:
    """HTTP headers are case-insensitive — normalize to upper case."""
    return {str(k).upper(): v for k, v in headers.items()}


def _parse_transmission_time(value: str) -> datetime | None:
    """Parse a PayPal ISO-8601 transmission time; None when unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        ts = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _cert_host_is_paypal(cert_url: str) -> bool:
    """The cert URL must be a paypal.com (or sandbox.paypal.com) domain."""
    try:
        host = (urllib.parse.urlsplit(cert_url).hostname or "").lower()
    except (ValueError, AttributeError):
        return False
    return host == "paypal.com" or host.endswith(".paypal.com")


def verify_paypal_signature(headers: dict, body: bytes,
                            webhook_id: str) -> bool:
    """Validate a PayPal webhook delivery's signature.

    Checks the five transmission headers: PAYPAL-AUTH-ALGO,
    PAYPAL-TRANSMISSION-SIG, PAYPAL-CERT-URL, PAYPAL-TRANSMISSION-ID,
    PAYPAL-TRANSMISSION-TIME.

    For now this implements STRUCTURAL validation only: all headers
    present and non-empty, cert URL on a paypal.com domain, and the
    transmission timestamp within 72 hours of now (widened from 1 hour
    so PayPal's retries — which keep the original timestamp — survive
    an outage; the signature check + idempotency cache are the real
    gates). The body must be non-empty and the configured webhook id
    must be a non-empty string (it is part of the signed payload in
    the full check).
    """
    hdrs = _normalize_headers(headers)
    for name in REQUIRED_HEADERS:
        if not hdrs.get(name):
            return False
    if not isinstance(webhook_id, str) or not webhook_id.strip():
        return False
    if isinstance(body, str):
        body = body.encode("utf-8")
    if not isinstance(body, (bytes, bytearray)) or not body:
        return False
    if not _cert_host_is_paypal(str(hdrs["PAYPAL-CERT-URL"])):
        return False
    sent_at = _parse_transmission_time(str(hdrs["PAYPAL-TRANSMISSION-TIME"]))
    if sent_at is None:
        return False
    age = (datetime.now(timezone.utc) - sent_at).total_seconds()
    return -MAX_FUTURE_SKEW_SECONDS <= age <= MAX_TRANSMISSION_AGE_SECONDS


def _resolve_webhook_id(webhook_id: str | None) -> str:
    """The PayPal webhook id: explicit arg wins, else the
    PAYPAL_WEBHOOK_ID env var (HIGH #2). Raises ValueError with a
    clear message when neither is set — failing closed is better
    than verifying against an empty id."""
    resolved = (webhook_id or "").strip() or os.environ.get(
        WEBHOOK_ID_ENV_VAR, "").strip()
    if not resolved:
        raise ValueError(
            f"webhook verification needs a webhook id: pass webhook_id "
            f"or set the {WEBHOOK_ID_ENV_VAR} environment variable")
    return resolved


def verify_webhook_delivery(paypal_client, headers: dict, body: bytes,
                            webhook_id: str) -> bool:
    """Verify a webhook delivery against PayPal's API (HIGH #2,
    Gemini Q2, Claude HIGH #1).

    Calls the client's verify_webhook_signature (POST
    /v1/notifications/verify-webhook-signature) with the RAW delivery
    bytes — never parsed and re-serialized (Claude HIGH #1: PayPal's
    check covers the exact original bytes; re-serialization changes
    key order/spacing and breaks genuine deliveries).

    Returns True only when the client reports verification_status ==
    "SUCCESS" (compared explicitly — bool(dict) is always True, so a
    bare truthiness check would never fail). A transport/HTTP error
    raises (the caller answers 503 so PayPal retries) — an unverified
    delivery is never treated as valid.
    """
    hdrs = _normalize_headers(headers)
    if isinstance(body, str):
        body = body.encode("utf-8")
    raw = bytes(body)  # keep the exact delivery bytes (HIGH #1)
    result = paypal_client.verify_webhook_signature(
        webhook_id=webhook_id,
        transmission_id=str(hdrs.get("PAYPAL-TRANSMISSION-ID", "")),
        transmission_time=str(hdrs.get("PAYPAL-TRANSMISSION-TIME", "")),
        cert_url=str(hdrs.get("PAYPAL-CERT-URL", "")),
        auth_algo=str(hdrs.get("PAYPAL-AUTH-ALGO", "")),
        transmission_sig=str(hdrs.get("PAYPAL-TRANSMISSION-SIG", "")),
        webhook_event=raw,
    )
    # Explicit comparison — bool(dict) is always True (Claude HIGH #2).
    if isinstance(result, dict):
        return result.get("verification_status") == "SUCCESS"
    return bool(result) is True


# ---------------------------------------------------------------------------
# Idempotency cache
# ---------------------------------------------------------------------------

#: Default: remember a transmission id for 24h (HIGH #1). PayPal
#: retries a delivery for up to 3 days; 24h covers the retry window
#: for the deliveries that matter (live jobs) without growing forever.
DEFAULT_IDEMPOTENCY_TTL_SECONDS = 24 * 3600

#: Default cap on remembered ids (HIGH #1). Past this, the oldest
#: entries are evicted first (LRU-ish by insertion time).
DEFAULT_IDEMPOTENCY_MAX_ENTRIES = 10_000


class WebhookIdempotencyCache:
    """Drop duplicate webhook deliveries by PAYPAL-TRANSMISSION-ID.

    PayPal retries deliveries it has not seen acknowledged; the cache
    makes retries a no-op instead of a double reconcile.

    HIGH #1: entries expire after ``ttl_seconds`` and the cache is
    capped at ``max_entries`` (oldest evicted first) — the old
    unbounded set grew forever. Callers must use the peek/mark
    protocol: ``seen()`` checks WITHOUT recording; ``mark()`` records
    only after the delivery was fully processed. A delivery whose
    reconcile failed is never marked, so PayPal's retry is processed
    instead of being dropped as a duplicate.

    Single-process memory only — a multi-worker deployment needs a
    shared store (same caveat as the jobs file, see app/server.py).

    LOW #6 (Claude): OrderedDict for O(1) LRU eviction (the old
    min()-scan was O(N) per eviction) + a threading lock so
    concurrent webhook workers don't corrupt the cache.
    """

    def __init__(self, ttl_seconds: float = DEFAULT_IDEMPOTENCY_TTL_SECONDS,
                 max_entries: int = DEFAULT_IDEMPOTENCY_MAX_ENTRIES):
        self._ttl = ttl_seconds
        self._max = max_entries
        # transmission_id -> monotonic timestamp of when it was marked.
        # OrderedDict: insertion order = oldest first; mark() moves to
        # end so popitem(last=False) evicts the true oldest.
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def _evict(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        cutoff = now - self._ttl
        # OrderedDict is insertion-ordered: pop from the left while
        # entries are expired, then while over capacity. O(1) amortized.
        while self._seen:
            _first_key, first_ts = next(iter(self._seen.items()))
            if first_ts < cutoff:
                self._seen.popitem(last=False)
            else:
                break
        while len(self._seen) > self._max:
            self._seen.popitem(last=False)

    def seen(self, transmission_id: str,
             now: float | None = None) -> bool:
        """True if this transmission id was already marked — WITHOUT
        recording it. Use before processing; call mark() only on
        success."""
        with self._lock:
            self._evict(now)
            return transmission_id in self._seen

    def mark(self, transmission_id: str,
             now: float | None = None) -> None:
        """Record a transmission id as processed."""
        now = time.monotonic() if now is None else now
        with self._lock:
            self._evict(now)
            self._seen[transmission_id] = now
            # Move to end: most-recently-marked = last evicted.
            self._seen.move_to_end(transmission_id)

    def is_duplicate(self, transmission_id: str) -> bool:
        """Legacy check-and-set (kept for backward compat). Prefer the
        seen()/mark() protocol in new code."""
        if self.seen(transmission_id):
            return True
        self.mark(transmission_id)
        return False

    def __len__(self) -> int:
        with self._lock:
            self._evict()
            return len(self._seen)


# ---------------------------------------------------------------------------
# Event translation
# ---------------------------------------------------------------------------

#: PayPal event name → dispatcher webhook event (§4.5).
PAYPAL_TO_DISPATCHER = {
    "PAYMENT.AUTHORIZATION.CREATED": Event.WH_AUTH_CREATED.value,
    "PAYMENT.AUTHORIZATION.VOIDED": Event.WH_AUTH_VOIDED.value,
    "PAYMENT.CAPTURE.COMPLETED": Event.WH_CAP_COMPLETED.value,
    "PAYMENT.CAPTURE.DENIED": Event.WH_CAP_DENIED.value,
    # v8.14: a refund is a capture-lifecycle event. It used to fall
    # through as WH_UNKNOWN_TYPE — answered 200 BEFORE signature
    # verification, so PayPal never retried and the books could stay
    # CAPTURED after a refund until someone ran reconcile. These are
    # HINTS, not commands (webhook policy §4.7): the gateway answers
    # with a fresh-read reconcile, which already understands
    # REFUNDED/PARTIALLY_REFUNDED. Map to the capture-hint so the
    # refund reaches that path.
    "PAYMENT.CAPTURE.REFUNDED": Event.WH_CAP_COMPLETED.value,
}


def translate_paypal_event(paypal_event_name: str) -> str:
    """Map a PayPal event name to a dispatcher webhook event.

    Returns Event.WH_UNKNOWN_TYPE for anything unmapped (a routing-layer
    event — it never reaches transition()).
    """
    return PAYPAL_TO_DISPATCHER.get(paypal_event_name,
                                    Event.WH_UNKNOWN_TYPE.value)


# ---------------------------------------------------------------------------
# Resource id extraction
# ---------------------------------------------------------------------------

def _resource_id_from_event(event: dict) -> str | None:
    """The PayPal-side id a webhook event refers to, or None when the
    event carries none. Authorization events name the authorization
    directly (resource.id); capture events name it in the resource's
    supplementary_data.related_ids (with fallback spellings, the same
    way PayPal surfaces the link). Mirrors the extraction the old
    EscrowService.handle_webhook_event path used."""
    resource = event.get("resource") or {}
    if not isinstance(resource, dict):
        return None
    if str(event.get("event_type", "")).startswith("PAYMENT.AUTHORIZATION."):
        auth_id = resource.get("id")
        return auth_id if isinstance(auth_id, str) and auth_id else None
    supp = resource.get("supplementary_data") or {}
    if isinstance(supp, dict):
        related = supp.get("related_ids") or {}
        if isinstance(related, dict):
            auth_id = related.get("authorization_id")
            if isinstance(auth_id, str) and auth_id:
                return auth_id
        auth_id = supp.get("authorization_id")
        if isinstance(auth_id, str) and auth_id:
            return auth_id
    auth_id = resource.get("authorization_id")
    return auth_id if isinstance(auth_id, str) and auth_id else None


# ---------------------------------------------------------------------------
# Gateway handler
# ---------------------------------------------------------------------------

# Semaphore bounding concurrent PayPal verification calls (Claude Q1):
# a forged-webhook flood must not exhaust worker threads on expensive
# verification round-trips. Cheap local checks run first; only
# deliveries that survive them reach verification.
_VERIFY_SEMAPHORE = threading.Semaphore(10)

#: Timeout for a single PayPal verification call (Claude Q1).
_VERIFY_TIMEOUT_SECONDS = 5.0


def handle_webhook(service, headers: dict, body: bytes,
                   webhook_id: str | None,
                   idempotency_cache: WebhookIdempotencyCache,
                   *, allow_unverified: bool = False) -> dict:
    """Process an incoming PayPal webhook delivery.

    ``allow_unverified``: tests only. When False (production default),
    a missing PayPal client (or one without verify_webhook_signature)
    answers 503 — it NEVER falls back to the structural check alone
    (Claude HIGH #2: fail closed, not open).

    Returns {"status", "event", "http_status", ...} where status is one
    of "ok" | "duplicate" | "invalid" | "unknown" | "error", and
    http_status is the HTTP code the wiring layer should answer with:
      invalid    bad signature headers / unparseable body / failed
                 PayPal verification -> 400 (do not retry)
      duplicate  transmission id already seen (no work done) -> 200
      unknown    webhook accepted but no reconcile triggered —
                 "event" names the routing-layer outcome
                 (WH_UNKNOWN_TYPE, WH_NO_AUTH_ID, WH_NO_JOB) -> 200
      error      could not verify (PayPal API unreachable, no client
                 and not allow_unverified, semaphore timeout) or the
                 fresh read failed (WH_READ_FAIL / WH_VERIFY_FAIL) ->
                 503 so PayPal retries the delivery
      ok         webhook accepted; "event" names the dispatcher event
                 (WH_JOB_SETTLED when the job was already settled,
                 otherwise the translated event that triggered
                 E_RECONCILE) -> 200

    Check order (Claude Q1, amended v5 review): cheap local checks
    FIRST — parse, event type, resource id, job lookup — and only then
    the expensive PayPal signature verification. A forged flood with
    random ids is rejected cheaply before burning a verification call.
    The settled-job check comes AFTER verification: it marks the
    transmission id, so a forged webhook on a settled job must not
    get a free 200 + cache pollution.

    HIGH #1: the transmission id is marked seen ONLY after the
    reconcile succeeds. A failed read leaves it unmarked and answers
    503, so PayPal's retry is processed instead of dropped.

    Steps:
      1. Resolve webhook id -> ValueError when missing.
      2. Structural header check -> invalid (400).
      3. Parse the body JSON -> invalid (400) when unparseable.
      4. Peek the dedupe cache -> duplicate (200), no dispatch.
      5. Translate the PayPal event name; unmapped -> unknown (200).
      6. Extract resource id; missing -> WH_NO_AUTH_ID (200).
      7. Job lookup; no job -> WH_NO_JOB (200).
      8. Verify via PayPal API (semaphore + timeout) -> invalid (400)
         on FAILURE, error (503) on transport failure or when no
         client is available and not allow_unverified.
      9. Settled check; settled -> WH_JOB_SETTLED, mark id, ok (200).
         AFTER verification — marking the id on an unverified
         delivery lets a forged webhook pollute the dedupe cache.
     10. Reconcile via service.reconcile_job() (fresh PayPal read +
         E_RECONCILE). Failed read -> WH_READ_FAIL, error (503), id
         NOT marked.
     11. On success mark the transmission id and return ok (200).
    """
    resolved_webhook_id = _resolve_webhook_id(webhook_id)

    if not verify_paypal_signature(headers, body, resolved_webhook_id):
        return {"status": "invalid", "event": None, "http_status": 400}

    hdrs = _normalize_headers(headers)
    transmission_id = str(hdrs["PAYPAL-TRANSMISSION-ID"])

    try:
        event = json.loads(body)
    except (ValueError, TypeError):
        return {"status": "invalid", "event": None, "http_status": 400}
    if not isinstance(event, dict):
        return {"status": "invalid", "event": None, "http_status": 400}

    # HIGH #1: peek — do NOT record yet. A failed delivery must stay
    # retryable.
    if idempotency_cache.seen(transmission_id):
        return {"status": "duplicate",
                "event": Event.WH_DUPLICATE.value,
                "transmission_id": transmission_id,
                "http_status": 200}

    # Cheap checks before the expensive verification (Claude Q1).
    paypal_name = str(event.get("event_type", ""))
    translated = translate_paypal_event(paypal_name)
    if translated == Event.WH_UNKNOWN_TYPE.value:
        return {"status": "unknown",
                "event": Event.WH_UNKNOWN_TYPE.value,
                "paypal_event_type": paypal_name,
                "http_status": 200}

    resource_id = _resource_id_from_event(event)
    if not resource_id:
        return {"status": "unknown",
                "event": Event.WH_NO_AUTH_ID.value,
                "paypal_event_type": paypal_name,
                "http_status": 200}

    job = service.find_job_by_resource_id(resource_id)
    if job is None:
        return {"status": "unknown",
                "event": Event.WH_NO_JOB.value,
                "resource_id": resource_id,
                "http_status": 200}

    # HIGH #2 (Claude): fail CLOSED. A missing client — or one without
    # verify_webhook_signature — answers 503 unless the caller passed
    # allow_unverified=True (tests only). Never fall back to the
    # structural check alone in production.
    paypal_client = getattr(service, "paypal", None)
    can_verify = (
        paypal_client is not None
        and hasattr(paypal_client, "verify_webhook_signature"))
    if not can_verify and not allow_unverified:
        return {"status": "error",
                "event": "WH_VERIFY_UNAVAILABLE",
                "transmission_id": transmission_id,
                "http_status": 503,
                "error": "no PayPal client for webhook verification"}
    if can_verify:
        # Semaphore + timeout (Claude Q1): bound the expensive call.
        acquired = _VERIFY_SEMAPHORE.acquire(
            timeout=_VERIFY_TIMEOUT_SECONDS)
        if not acquired:
            return {"status": "error",
                    "event": "WH_VERIFY_FAIL",
                    "transmission_id": transmission_id,
                    "http_status": 503,
                    "error": "verification semaphore timeout"}
        try:
            verified = verify_webhook_delivery(
                paypal_client, headers, body, resolved_webhook_id)
        except Exception as exc:  # noqa: BLE001 — transport failure
            # Cannot verify: answer 503 so PayPal retries; the id is
            # NOT marked.
            return {"status": "error",
                    "event": "WH_VERIFY_FAIL",
                    "transmission_id": transmission_id,
                    "http_status": 503,
                    "error": str(exc)}
        finally:
            _VERIFY_SEMAPHORE.release()
        if not verified:
            return {"status": "invalid",
                    "event": None,
                    "transmission_id": transmission_id,
                    "http_status": 400}

    # Claude v5 review: the settled-job early return must come AFTER
    # signature verification. Before this fix, a forged webhook on a
    # settled job got 200 and the attacker's transmission id was marked
    # in the dedupe cache. The cheap checks above (parse, event type,
    # resource id, job lookup) stay before verification — they never
    # mark the cache — but anything that marks must verify first.
    if service.job_is_settled(job):
        idempotency_cache.mark(transmission_id)
        return {"status": "ok",
                "event": Event.WH_JOB_SETTLED.value,
                "resource_id": resource_id,
                "http_status": 200}

    # Section 4.7: the webhook is a hint — money only moves after a
    # fresh PayPal read. reconcile_job() performs that read and
    # dispatches E_RECONCILE with read evidence; the webhook payload
    # itself is never trusted for state changes.
    # Grok v7.1 review: unify the webhook stacks. The gateway does
    # signature verification and routing; the state-changing logic
    # lives in EscrowService.handle_webhook_event, which handles
    # denials (job.webhook_denial), durable event-id dedupe, and
    # fresh PayPal reads. Calling reconcile_job here bypassed all of
    # that — a PAYMENT.CAPTURE.DENIED left the job DELIVERED with no
    # denial stamped.
    try:
        summary = service.handle_webhook_event(event)
    except Exception as exc:  # noqa: BLE001 — a failed read must not 500
        # Grok v7.1: distinguish transport failures (503, retry) from
        # definitive business outcomes (200, mark).
        # DEFAULT: 503 (transport failure, retryable). Only return 200
        # for clearly definitive outcomes like a wedge.
        err_str = str(exc)
        is_definitive = "operator review needed" in err_str.lower()
        if not is_definitive:
            # HIGH #1: do NOT mark the id — answer 503 so PayPal retries.
            return {"status": "error",
                    "event": Event.WH_READ_FAIL.value,
                    "resource_id": resource_id,
                    "transmission_id": transmission_id,
                    "http_status": 503,
                    "error": err_str}
        # Definitive business outcome (wedge) — mark and answer 200.
        idempotency_cache.mark(transmission_id)
        return {"status": "ok",
                "event": translated,
                "resource_id": resource_id,
                "transmission_id": transmission_id,
                "webhook_action": "wedge",
                "http_status": 200,
                "error": err_str}

    action = summary.get("action", "")
    if action == "duplicate":
        idempotency_cache.mark(transmission_id)
        return {"status": "duplicate",
                "event": Event.WH_DUPLICATE.value,
                "transmission_id": transmission_id,
                "http_status": 200}
    if action == "rejected":
        return {"status": "invalid",
                "event": None,
                "transmission_id": transmission_id,
                "http_status": 400,
                "error": summary.get("error", "")}
    if action == "pending":
        # PayPal's state could not be read — 503 so PayPal retries.
        # Do NOT mark the transmission id.
        # EXCEPT: a wedge ("operator review needed") is definitive —
        # the state WAS read, the outcome is "needs operator". 200.
        err = summary.get("error", "")
        if "operator review needed" in err.lower():
            idempotency_cache.mark(transmission_id)
            return {"status": "ok",
                    "event": translated,
                    "resource_id": resource_id,
                    "transmission_id": transmission_id,
                    "webhook_action": "wedge",
                    "http_status": 200,
                    "error": err}
        return {"status": "error",
                "event": Event.WH_READ_FAIL.value,
                "resource_id": resource_id,
                "transmission_id": transmission_id,
                "http_status": 503,
                "error": err}
    # confirmed, reconciled, denial_recorded, ignored, unknown —
    # definitive outcomes. Mark the transmission id and answer 200.
    idempotency_cache.mark(transmission_id)
    return {"status": "ok",
            "event": translated,
            "resource_id": resource_id,
            "transmission_id": transmission_id,
            "webhook_action": action,
            "http_status": 200}


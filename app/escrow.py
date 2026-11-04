"""Escrow state machine for Lamplight Checkout.

One Job is one hire, tracked through the same states as the on-chain
LamplightEscrow contract (../arc/contracts/LamplightEscrow.sol), with
PayPal authorizations playing the role of the on-chain lock:

  QUOTED -> AUTHORIZED -> DELIVERED -> CAPTURED   (happy path: agent paid)
  QUOTED -> AUTHORIZED -> VOIDED                  (timeout: hold released)

Rules (same spirit as the contract):
  * capture requires DELIVERED and caller == payer, AND the original
    delivery text, verified against the recorded hash before any
    PayPal call — an unverified delivery can never release funds
  * void requires AUTHORIZED, NOT delivered, and now >= deadline,
    evaluated against wall-clock time (the ``now`` parameter is a
    test-only hook; the server never passes a client-supplied time)
  * a settled job (CAPTURED or VOIDED) can never move again
  * deliver requires AUTHORIZED and caller == agent, exactly once,
    and only before the deadline
  * authorize requires a live (unexpired) quote
  * capture/authorize only settle on a PayPal result with the right
    status (COMPLETED capture, CREATED authorization) — a DECLINED or
    DENIED result never settles the job
  * capture/void are idempotent per job (a stable PayPal-Request-Id):
    if the response is lost after PayPal acted, the retry replays the
    original result instead of executing twice; if even the retry
    fails, the job is reconciled from PayPal's actual authorization
    state before giving up, so money that moved is never left
    unrecorded — the real capture id and amount are recovered from
    the authorization's linked captures when PayPal lists them (only
    when it lists none does the job keep capture id "" with the
    reconciled flag, documented as unauditable)
  * jobs live in a pluggable store (app/store.py) and every state
    change is saved, so a restart never loses a job or its PayPal ids
  * the last successful PayPal response is persisted on the job
    (last_paypal_response) at every authorize / capture / void and on
    every reconcile read, so an operator can audit what PayPal said
    without re-querying it; a failed call never overwrites the last
    good record. The recorded body is capped (4 KB, links trimmed —
    round 5) so a future PayPal response change cannot bloat the
    durable store; normal responses record fully and unchanged.
  * POST /admin/reconcile (server.py) walks open jobs and forces the
    same reconciliation from PayPal, for use after a prolonged outage.
    Every reconcile branch records its successful read through one
    helper (_record_reconcile_read — round 5), so the "always persist
    a successful read" rule is expressed once.
  * POST /webhooks/paypal (server.py, round 7) lets PayPal tell the
    service when money moves instead of waiting for an operator to
    poll /admin/reconcile: handle_webhook_event verifies nothing
    itself (the endpoint verifies the signature first), then reads
    the named authorization from PayPal and lets reconcile_job decide
    the state — the event body alone never settles a job. A capture
    denial is recorded on the job and leaves it open; an authorization
    creation is confirmed without overwriting the authorize record
    (the dedupe record proves the event arrived). Processed event ids
    are persisted in the store, so a replayed delivery is a no-op
    even after a restart — except when PayPal's state could genuinely
    not be read (transport failure / 5xx), in which case the event is
    deliberately NOT marked, so a later re-delivery can still settle
    it. The admin reconcile endpoint remains the fallback for events
    that name a job PayPal no longer knows, or an authorization the
    service has never seen.
  * capture_partial (round 8) releases part of the hold and voids the
    rest in one move: same delivery verification as the full capture,
    amount > 0 and <= the hold, one receipt showing the split
    (captured + voided residual). A failed residual void never
    reports settled — the partial capture is recorded, the job stays
    DELIVERED, and the payer retries with the same amount (capture
    skipped, void only) or the admin reconcile finishes it when the
    void did apply: reconcile_job records a partially-captured job
    whose authorization PayPal reports VOIDED as CAPTURED with the
    voided residual, never as VOIDED. The webhook event-id set is
    capped at 1000, oldest first evicted (app/store.py).
  * round 9: capture, capture_partial and void_after_deadline run
    under the per-job lock (the one submit_delivery already uses), so
    two concurrent settlement calls on one job cannot both pass the
    status check; buyer data (payer, payment_source) is stripped from
    PayPal response bodies before recording, and again on the way out
    over HTTP; a PARTIALLY_CAPTURED authorization is treated like
    CREATED by reconcile; job ids are full UUID hex with a
    collision check on create.
  * round 10: the round-9 buyer-data strip was a denylist, so a
    shipping block still leaked — recorded responses are now an
    allowlist (ids, status, amounts, timestamps, linked captures /
    authorizations reduced to the same), and orders ask PayPal for
    NO_SHIPPING; reconcile_job runs under the same per-job lock as
    the settlement paths (the webhook and admin paths inherit it),
    and capture() sets capture_reconciled explicitly from its own
    result instead of leaving a flag a concurrent reconcile set;
    a capture denial is kept in its own field instead of
    overwriting the authorize record; the capture_partial
    idempotency key carries the amount.
  * round 11: the amount-in-key fix above created its own hole — a
    lost-response partial capture could be followed by a
    different-amount retry (a NEW capture) or a full capture, both
    over-capturing. Every capture now records its intent (amount +
    idempotency key) on the job BEFORE the PayPal call; while an
    intent is pending, a different amount and a full capture are
    refused, and a same-amount retry replays the intent's key. A
    PARTIALLY_CAPTURED authorization that lists no captures is
    recovered by replaying the pending key (capture_partial's
    reconcile path and reconcile_job both), and a COMPLETED capture
    for a different amount is recorded as returned, never refused
    with nothing recorded. A full capture's returned amount is
    checked against the hold and logged when it exceeds it. The
    open-jobs cap no longer counts expired QUOTED jobs (they are
    swept when seen), and authorize runs under the per-job lock
    with the job re-read inside it.
  * round 34 / v2: authorize() and sweep_expired_quotes() adopt-then-delegate —
    an adopted authorization is saved (authorization_id, AUTHORIZED, persisted
    PayPal response) before _reconcile_with_auth decides the state, so a
    terminal authorization never strands a job in QUOTED. A released hold
    (VOIDED/EXPIRED/DENIED) with no captures listed anywhere books VOIDED;
    with captures listed it settles CAPTURED. (The "DENIED never settles" rule
    above covers a fresh authorize attempt PayPal declines — no hold was ever
    placed, so the job stays QUOTED. It does not cover recovering an
    already-placed hold PayPal later reports as released: the hold died, and
    the books must say so.)
  * round 35 / v2: REFUNDED and PARTIALLY_REFUNDED captures count as completed
    in the capture enumeration (round-34 Claude item 1) — they are terminal
    and mean money moved. Leaving them in the ambiguous bucket wedged jobs
    permanently, because force_settle refuses ambiguous reads.
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import threading
import time
import uuid

from .paypal_client import (PayPalClient, PayPalError, amount_to_cents)
from .errors import ErrorClass, classify_paypal_error
from .quote import quote_expired
from .audit import AuditLog

# Phase 4: feature flag for dispatcher migration. When True, the 6
# consumer sites route state transitions through app/dispatcher.py.
# When False, the original if/elif logic runs. Old code is NEVER
# deleted — only gated. Cleanup is a separate step after Gemini
# reviews Phase 4.
# Grok fix order #7: ATTEMPTED — 138 tests fail with the flag on.
# The dispatcher paths have pre-existing integration gaps beyond
# Grok's 7 fixes (Grok himself said "flag-on = NO (8 reasons)").
# Flag stays False until Phase 4 is complete.
USE_DISPATCHER = True

logger = logging.getLogger(__name__)


# v7.2: FSM/legacy disagreement is a correctness signal, not noise. The
# shadow trackers (reconcile, force_settle) must never raise -- legacy has
# already moved money -- but they must not whisper either. Every
# disagreement is logged at ERROR and counted so a deploy gate, a test or a
# metrics scrape can see it: fsm_divergence_count() / reset_fsm_divergences().
_FSM_DIVERGENCES: list = []


def _report_fsm_divergence(where: str, job_id: str, detail: str) -> None:
    _FSM_DIVERGENCES.append((where, job_id, detail))
    logger.error("FSM divergence [%s] job %s: %s", where, job_id, detail)


def fsm_divergence_count() -> int:
    return len(_FSM_DIVERGENCES)


def reset_fsm_divergences() -> None:
    _FSM_DIVERGENCES.clear()


# Round 5 (Oct 27, 2026): the recorded PayPal response body is capped so
# a future PayPal response change cannot bloat the durable job store.
# The guard is size-driven, not shape-driven: a normal response (a few
# hundred bytes — ids, status, amount, captures list) is recorded fully
# and unchanged; only an oversized body is trimmed. The cap is generous
# against real PayPal shapes (authorize ~600 B, capture ~300 B) and
# tight enough that no single response can grow the store without
# bound. The guard never raises: a pathological body degrades to a
# trimmed record, never an exception — a bad audit record must never
# break a settlement.
ENVELOPE_BODY_MAX_BYTES = 4096
ENVELOPE_MAX_LINKS = 1          # links array: keep the first entry only
ENVELOPE_MAX_LIST_ITEMS = 8     # any other list: keep the first 8
ENVELOPE_MAX_STRING_CHARS = 512  # any string: truncate to 512 chars
ENVELOPE_MAX_DEPTH = 6          # deeper nesting is replaced by a marker
# Round 6 (Oct 27, 2026): the three audit keys are kept first when an
# oversized body still has to shed top-level keys — they identify the
# response (which authorization, what state, how much), so a
# pathological inflation must never silently drop them while a
# droppable key exists. If the body holds audit keys alone and is
# still over the cap, the largest audit key is dropped last as the
# final resort — a preference, not a guarantee that breaks the cap.
# Preference order (round 7): non-audit keys first, largest first, audit keys last — and the 4 KB cap always wins over the allow-list.
ENVELOPE_AUDIT_KEYS = frozenset({"id", "status", "amount"})


def _trim_value(value, depth: int):
    """Recursively trim one JSON-shaped value for the envelope guard.
    Non-JSON values (sets, bytes, objects) become a marker string so
    the result is always JSON-safe; the store must never fail to save
    because a response carried something unserializable."""
    if depth > ENVELOPE_MAX_DEPTH:
        return "<nested>"
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key == "links" and isinstance(item, list):
                out[key] = ([_trim_value(item[0], depth + 1)]
                            if item else [])
            else:
                out[key] = _trim_value(item, depth + 1)
        return out
    if isinstance(value, list):
        return [_trim_value(item, depth + 1)
                for item in value[:ENVELOPE_MAX_LIST_ITEMS]]
    if isinstance(value, str) and len(value) > ENVELOPE_MAX_STRING_CHARS:
        return value[:ENVELOPE_MAX_STRING_CHARS - 3] + "..."
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return "<unserializable>"


# -- recorded responses (round 10) --------------------------------------------
# A PayPal response body is recorded on the job so an operator can audit
# what PayPal said without re-querying it — but only the audit fields
# may reach the store. Round 9 stripped buyer data with a denylist
# (payer, payment_source); Claude's round-9 review showed why a
# denylist cannot hold: any buyer-identifying field the list does not
# name — a purchase_units[0].shipping block with the buyer's name and
# address — passes straight through to unauthenticated reads and
# receipts, and the mock never emitted one, so no test caught it.
# Round 10 records an ALLOWLIST instead: only the named fields
# survive, everything else is dropped, so a field PayPal adds tomorrow
# cannot leak either. The allowlist is the audit shape — ids, status,
# amounts, timestamps, and the linked captures/authorizations reduced
# to the same three fields — and it runs AFTER the size trim
# (_trim_paypal_body), which is what degrades a pathological body
# (circular, unserializable) to the {"_trimmed": True} marker. The
# same sanitizer runs again on the way out over HTTP (Job.to_dict()
# and the receipt builder), so a body that reached the store by some
# other path is still clean on the wire.
def _sanitize_amount(amount):
    """A PayPal amount object, reduced to its two audit fields.
    Anything else in the object is dropped; a non-dict amount
    becomes {} rather than raising."""
    if not isinstance(amount, dict):
        return {}
    out = {}
    for key in ("currency_code", "value"):
        if isinstance(amount.get(key), str):
            out[key] = amount[key]
    return out


def _sanitize_capture(cap):
    """One linked capture/authorization, reduced to id, status and
    amount — the three fields the audit needs."""
    if not isinstance(cap, dict):
        return {}
    out = {}
    for key in ("id", "status"):
        if isinstance(cap.get(key), str):
            out[key] = cap[key]
    if "amount" in cap:
        out["amount"] = _sanitize_amount(cap["amount"])
    return out


def _sanitize_purchase_unit(unit):
    """One purchase unit, reduced to its payments — authorizations
    and captures, each reduced by _sanitize_capture. The unit's
    shipping block, payee, description and amount are dropped: the
    shipping block is buyer data, and the amounts the audit needs
    live on the authorization/capture records themselves."""
    if not isinstance(unit, dict):
        return {}
    out = {}
    payments = unit.get("payments")
    if isinstance(payments, dict):
        kept = {}
        for key in ("authorizations", "captures"):
            items = payments.get(key)
            if isinstance(items, list):
                kept[key] = [_sanitize_capture(item) for item in items]
        if kept:
            out["payments"] = kept
    return out


def sanitize_paypal_body(value):
    """Reduce a PayPal response body to the recorded allowlist.
    Returns a new structure; the input is not mutated. Never raises:
    a pathological body degrades to {} rather than breaking a
    settlement (the size trim upstream already turned circular or
    unserializable bodies into the {"_trimmed": True} marker, so
    this normally sees JSON-safe input)."""
    try:
        if not isinstance(value, dict):
            return {}
        out = {}
        for key in ("id", "status", "create_time", "expiration_time",
                    "authorization_id"):
            if isinstance(value.get(key), str):
                out[key] = value[key]
        if "amount" in value:
            out["amount"] = _sanitize_amount(value["amount"])
        captures = value.get("captures")
        if isinstance(captures, list):
            out["captures"] = [_sanitize_capture(cap) for cap in captures]
        units = value.get("purchase_units")
        if isinstance(units, list):
            out["purchase_units"] = [_sanitize_purchase_unit(u) for u in units]
        # The size trim's marker is service-generated, not PayPal
        # data: keep it, so a trimmed record still says it was
        # trimmed instead of silently becoming an empty body.
        if value.get("_trimmed") is True:
            out["_trimmed"] = True
        return out
    except Exception:  # noqa: BLE001 — sanitizer must never raise
        return {}


def sanitize_paypal_record(record):
    """Sanitize a stored last_paypal_response ENVELOPE for the way
    out over HTTP: the envelope's operation/status/id pass through
    untouched, and only its body is allowlisted. The record-time
    sanitizer already did this once; this is the second layer for
    bodies that reached the store by some other path (e.g. a store
    file written before round 10). Returns a new dict; None stays
    None."""
    if record is None:
        return None
    out = dict(record) if isinstance(record, dict) else {}
    out["body"] = sanitize_paypal_body(record.get("body")
                                       if isinstance(record, dict) else None)
    return out


def _trim_paypal_body(body) -> dict:
    """Cap a PayPal response body before it is persisted in the
    last_paypal_response envelope. Bodies at or under the cap are
    returned untouched (normal responses record fully and unchanged).
    An oversized body is trimmed deterministically — links to one
    entry, long strings and lists cut, nesting capped — and if it is
    still over the cap, the largest top-level keys are dropped first,
    except the three audit keys (id, status, amount), which are kept
    while any other key can be dropped instead (round 6); at the
    limit the loop ends with a body that fits, even if that means
    dropping an audit key or every key. Never raises.
    """
    try:
        raw = json.dumps(body, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError):
        return {"_trimmed": True}
    if len(raw.encode("utf-8")) <= ENVELOPE_BODY_MAX_BYTES:
        return body if isinstance(body, dict) else {}
    trimmed = _trim_value(body, 0)
    try:
        raw = json.dumps(trimmed, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError):
        return {"_trimmed": True}
    if len(raw.encode("utf-8")) <= ENVELOPE_BODY_MAX_BYTES:
        return trimmed
    # Still over the cap: shed top-level keys until it fits — the
    # preference order lives in _drop_keys_to_fit (round 7).
    return _drop_keys_to_fit(trimmed)


def _drop_keys_to_fit(body: dict) -> dict:
    """Drop top-level keys from an oversized body until its serialized
    size fits the envelope cap. Pure (mutates and returns the dict it
    is given) so the size-guard logic can be unit-tested directly
    without going through _record_paypal_response (round 7: extracted
    from _trim_paypal_body, behaviour unchanged). Deterministic
    order: the largest key first (size desc, then key name).
    Preference order: the largest NON-audit key first, while any
    remains; only when the body holds audit keys alone does the
    largest audit key go, last. The cap always wins — the loop ends
    with a body that fits even if that means dropping an audit key
    (or, at the limit, every key, leaving {})."""
    def _size(value) -> int:
        return len(json.dumps(value, separators=(",", ":"),
                              sort_keys=True).encode("utf-8"))

    while _size(body) > ENVELOPE_BODY_MAX_BYTES:
        droppable = [k for k in body if k not in ENVELOPE_AUDIT_KEYS]
        pool = droppable if droppable else list(body)
        victim = sorted(pool, key=lambda k: (-_size(body[k]), k))[0]
        del body[victim]
    return body


# -- webhooks (round 7) -------------------------------------------------------
# PayPal webhook event types the service understands. Anything else is
# ignored safely — logged, 200, no state change.
WEBHOOK_EVENT_TYPES = frozenset({
    "PAYMENT.AUTHORIZATION.CREATED",
    "PAYMENT.AUTHORIZATION.VOIDED",
    "PAYMENT.CAPTURE.COMPLETED",
    "PAYMENT.CAPTURE.DENIED",
})


def _authorization_id_from_event(event: dict) -> str | None:
    """The authorization id a webhook event refers to, or None when the
    event carries none. Authorization events name it directly
    (resource.id); capture events name it in the resource's
    supplementary_data.related_ids (with two fallback spellings, since
    PayPal surfaces the link in more than one place)."""
    resource = event.get("resource") or {}
    if not isinstance(resource, dict):
        return None
    if event.get("event_type", "").startswith("PAYMENT.AUTHORIZATION."):
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


def _is_order_absent(error: PayPalError) -> bool:
    """True when PayPal definitively says the order does not exist:
    HTTP 404, or RESOURCE_NOT_FOUND in the error name/body. Only then
    is "the order lists no capture" a proven fact ("none"). Any other
    definitive 4xx (401/403/400) means the read failed — "unread",
    not "none". Round 23 (Grok round-22 finding 4): a 403 is
    "couldn't read," not "nothing listed"."""
    if error.status == 404:
        return True
    body = error.body
    if isinstance(body, str):
        return "RESOURCE_NOT_FOUND" in body
    if isinstance(body, dict):
        name = body.get("name")
        if isinstance(name, str) and "RESOURCE_NOT_FOUND" in name:
            return True
    return False


def _is_transient_paypal_error(error: PayPalError) -> bool:
    """True when a PayPal call failed in a way that says nothing about
    PayPal's state — a transport failure (status None), a 5xx, a 408
    (timeout) or a 429 (rate limited). A 4xx (e.g. 404, the
    authorization is gone) is definitive: PayPal answered, and
    re-reading it will answer the same. Only a transient failure
    leaves a webhook event unmarked, so a later re-delivery can still
    settle it. Round 16 (Claude round-15 finding 2): 408/429 are
    transient — the old code marked a rate-limited probe processed
    and PayPal never redelivered."""
    return (error.status is None or error.status >= 500
            or error.status in (408, 429))


def _is_hold_released(status: object) -> bool:
    """Round 31 (C4): one predicate for "the hold is released" — a
    VOIDED, EXPIRED, or DENIED authorization can never move money.
    Used by void_after_deadline, _void_with_reconcile, _void_residual,
    _resolve_post_void, and force_settle so the released set cannot
    drift between arms again."""
    return status in ("VOIDED", "EXPIRED", "DENIED")


def _authorized_cents_for_evidence(job) -> int:
    """Best-effort hold amount for dispatcher Evidence (Gemini v6
    review, Q3.2). A quote missing price_cents must not wedge an
    ordinary void reconciliation to S5 — fall back through
    amount_cents, else 0. The dispatcher's None-wedge remains as a
    safety net for Evidence built without this helper."""
    quote = job.quote or {}
    for key in ("price_cents", "amount_cents"):
        val = quote.get(key)
        if isinstance(val, bool):
            continue
        if isinstance(val, int) and val > 0:
            return val
    return 0


def _capture_status_rank(status: object) -> int:
    """Lifecycle precedence for capture statuses (round 37 refined
    round 38, Claude round-37 finding). A capture's status only moves
    forward — it cannot un-happen — so the most advanced state seen is
    the truth. Shared by the enumeration dedup and the "kept" branch of
    _record_listed_capture so the rule cannot drift between paths.
    (The summed-record label is a separate rule — see
    _summed_capture_status — because a mixed sum is PARTIALLY_REFUNDED,
    not the max rank.)
    Rank: REFUNDED 4 > PARTIALLY_REFUNDED 3 > COMPLETED 2 >
    DECLINED/FAILED 1 > PENDING/unknown/None/UNAUDITED 0. A dead
    capture outranks a pending one: dead is definitive, pending may
    still move either way."""
    if status == "REFUNDED":
        return 4
    if status == "PARTIALLY_REFUNDED":
        return 3
    if status == "COMPLETED":
        return 2
    if status in ("DECLINED", "FAILED"):
        return 1
    return 0


def _summed_capture_status(member_statuses: set) -> str:
    """The status label for a summed capture record (round 39, Claude
    round-38). Stated once, next to _capture_status_rank, so the two
    rules cannot drift: REFUNDED only when every member is REFUNDED; a
    mixed set with any refunded member is PARTIALLY_REFUNDED ($6
    COMPLETED + $4 REFUNDED is $10 partially refunded, not $10
    refunded); otherwise COMPLETED."""
    if member_statuses <= {"REFUNDED"}:
        return "REFUNDED"
    if member_statuses & {"REFUNDED", "PARTIALLY_REFUNDED"}:
        return "PARTIALLY_REFUNDED"
    return "COMPLETED"


class EscrowError(Exception):
    """A state transition was refused (wrong state, wrong caller, too early).

    Carries an http_status for the API layer (409 Conflict for domain
    refusals — matches the server mapping of EscrowError to 409)."""

    def __init__(self, message: str = "", http_status: int = 409):
        self.http_status = http_status
        super().__init__(message)


def _per_job_locked(method):
    """Decorator: run a settlement method under the job's per-job lock
    (round 9). capture, capture_partial and void_after_deadline all
    check-then-act on the job state, so two concurrent settlement
    calls on one job could both pass the status check and move money
    twice — the lock (the same one submit_delivery already uses) makes
    the whole body atomic per job. The lock is an RLock:
    capture_partial's degenerate branch calls capture(), which
    re-enters the same lock on the same thread."""
    @functools.wraps(method)
    def wrapper(self, job, *args, **kwargs):
        with self._delivery_lock(job.job_id):
            return method(self, job, *args, **kwargs)
    return wrapper


def _money_snapshot(job) -> dict:
    """The job's money fields as the audit log's "recorded" payload:
    what the books say after the operation."""
    return {
        "status": job.status,
        "capture_ids": list(job.capture_ids or []),
        "capture_amount_cents": job.capture_amount_cents,
        "voided_amount_cents": job.voided_amount_cents,
        "capture_status": job.capture_status,
        "settlement_method": job.settlement_method,
        "needs_review": bool(job.needs_review),
        "needs_review_reason": job.needs_review_reason,
    }


def _audited(operation):
    """Decorator: record one structured audit event per invocation of
    a financial operation (v8.2, production-readiness item 6).

    Additive: the wrapped method's logic is untouched. On success the
    event carries outcome "completed" (or "wedged" when the job left
    with the review flag set); on exception, "wedged" when the job
    carries the review flag, else "failed", with the reason. The
    idempotency key is the in-flight intent key when one exists at
    entry (the replay path — the money-dangerous case), the
    deterministic authorize key for authorize, else None; in-flight
    replays record their own "retried" events with the exact key.

    Recording never raises: a store failure is logged, never
    propagated — observability must not break money movement.
    """
    def deco(method):
        @functools.wraps(method)
        def wrapper(self, job, *args, **kwargs):
            entry_key = self._audit_entry_key(job, operation)
            try:
                result = method(self, job, *args, **kwargs)
            except Exception as exc:
                self._audit(operation, job,
                            idempotency_key=entry_key,
                            outcome=self._audit_settled_outcome(job),
                            reason=self._audit_failure_reason(job, exc))
                raise
            # Success — but a job that left with the review flag set
            # is a wedge, not a clean completion. Re-read the job:
            # some methods (force_settle) rebind their local job to a
            # fresh store object, so the wrapper's reference may be
            # stale.
            fresh = self.store.get(job.job_id)
            settled = fresh if fresh is not None else job
            # A dict result carrying a truthy "error" (e.g.
            # reconcile_job's soft refusals) is recorded with the
            # reason, so the log distinguishes it from a clean run.
            result_reason = None
            if isinstance(result, dict) and result.get("error"):
                result_reason = str(result["error"])
            if settled.needs_review:
                self._audit(operation, settled,
                            idempotency_key=entry_key,
                            outcome="wedged",
                            reason=(settled.needs_review_reason
                                    or "job left with review flag set"))
            else:
                self._audit(operation, settled,
                            idempotency_key=entry_key,
                            outcome="completed",
                            reason=result_reason)
            return result
        return wrapper
    return deco


class Job:
    def __init__(self, *, job_id: str, quote: dict, payer: str, agent: str,
                 deadline_seconds: int = 3600):
        self.job_id = job_id
        self.quote = quote
        self.payer = payer
        self.agent = agent
        self.created_at = int(time.time())
        self.deadline = self.created_at + deadline_seconds
        self.status = "QUOTED"          # QUOTED | AUTHORIZED | DELIVERED | CAPTURED | VOIDED
        self.order_id: str = ""
        self.authorization_id: str = ""
        self.capture_id: str = ""
        # All capture IDs recorded for this job (suggestion #5). PayPal
        # can split a hold across multiple captures (partial shipments,
        # multi-vendor). The singular capture_id keeps the first for
        # backward compat; this list keeps them all. Store files written
        # before this change load with [] via .get() and backfill from
        # capture_id on load.
        self.capture_ids: list[str] = []
        # Amount per capture, parallel to capture_ids (HIGH #3): PayPal
        # can split a hold across multiple captures, and each capture's
        # own amount must be attributable for audit. capture_amount_cents
        # stays the summed total (fund-conservation comparisons keep
        # working); this list keeps the per-capture breakdown. A None
        # entry means the id is known but the amount is not yet (MEDIUM
        # #3: never pad with 0 — the total is recomputed only when all
        # amounts are known). Store files written before this change
        # load with [] via .get() and backfill from capture_amount_cents
        # when there is exactly one capture id.
        self.capture_amounts: list[int | None] = []
        # Amount actually captured, in cents, when known (recorded from
        # the capture result, or from the authorization's linked captures
        # on reconcile). None when the job was voided, or when the capture
        # was reconciled from an authorization that lists no capture.
        self.capture_amount_cents: int | None = None
        # Amount voided back to the payer as the residual of a partial
        # capture (round 8), in cents. Only a partial release sets it:
        # capture_partial captures part of the hold and voids the rest
        # in one move, so the receipt can show the split (captured X +
        # voided Y = the hold). None for a full capture or a plain void.
        # Set from the actual captured amount (hold - captured), never
        # from the requested amount — if PayPal captured something
        # different from what was asked, the receipt records what
        # actually moved and what was actually released.
        self.voided_amount_cents: int | None = None
        self.delivery_hash: str = ""
        self.settled_at: int | None = None
        # True when the capture was recorded by reconciling PayPal's
        # authorization state after the capture response was lost: the
        # money moved, but the capture was not observed through the
        # normal capture call. The capture id is the real one when
        # PayPal's authorization resource lists a COMPLETED capture, and
        # "" only when it lists none — that case stays unauditable.
        self.capture_reconciled: bool = False
        # The PayPal status of the recorded capture (round 36, Claude
        # round-35 item 1): COMPLETED, REFUNDED, or PARTIALLY_REFUNDED.
        # A refunded capture settles the job (the money moved and cannot
        # move again on that capture) but the books must not look
        # identical to a paid one — without this field a fully-refunded
        # capture was indistinguishable from a completed payment in the
        # job record. None until a capture is recorded. Store files
        # written before round 36 load with None via .get().
        self.capture_status: str | None = None
        # How the settlement was established (round 31, B2): the
        # receipt previously could not distinguish these, but for a
        # money system the distinction matters for audit.
        #   "observed"      — the capture was recorded from a direct
        #                     POST response (capture/capture_partial).
        #   "reconciled"    — the capture was found by reading PayPal
        #                     state (reconcile, order lookup) after the
        #                     POST response was lost or never made.
        #   "force_settled" — the job was settled via the payer-only
        #                     force_settle path.
        # None until the job settles with a capture (voids leave it
        # None). Store files written before round 31 load with None
        # via .get().
        self.settlement_method: str | None = None
        # Round 50 (Claude round-49 Low 6): when a settlement books
        # money that cannot be fully accounted for (unattributable
        # captures), this note says so on the receipt. None when the
        # settlement is fully accounted.
        self.settlement_note: str | None = None

        # The last successful PayPal response, so an operator can audit
        # what PayPal actually said without re-querying it. Envelope
        # (not the raw body alone): the void call returns no body, so
        # the raw body alone would say nothing — the envelope always
        # carries at least operation + status + id, plus the raw body
        # for the full audit. Only successful responses are recorded;
        # a failed call never overwrites the last good record.
        # {"operation": "authorize"|"capture"|"void"|"reconcile",
        #  "status": "CREATED"|"COMPLETED"|"VOIDED"|..., "id": <PayPal id>,
        #  "body": <allowlisted PayPal response dict>}. None until the
        # first PayPal call succeeds (store files written before round
        # 4 load with None via .get()). The body is allowlisted
        # (round 10: sanitize_paypal_body) — only audit fields are
        # kept, so no buyer data can reach a read or a receipt.
        self.last_paypal_response: dict | None = None
        # The capture intent (round 11): the amount (cents) and the
        # idempotency key of a capture the service asked PayPal to
        # move but has not yet recorded. Set and SAVED before the
        # PayPal call — the response may be lost after PayPal acted,
        # and without a persisted intent the job would look untouched
        # while money had moved. While set, a different amount is
        # refused and a full capture() is refused (a partial intent):
        # the pending capture may already have executed, so a second
        # capture would over-capture. The intent is cleared when the
        # capture is recorded (observed or reconciled), or by
        # reconcile when the authorization proves nothing moved
        # (still CREATED). The amount is None for a full capture's
        # intent — a full capture asks PayPal for the whole remaining
        # hold with no amount in the call, so a replay of it must
        # send no amount either; a retry of the same full capture is
        # allowed because its intent amount is None, not a different
        # partial amount. None (the whole field pair) when no capture
        # is in flight (store files written before round 11 load with
        # None via .get()).
        self.pending_capture_amount_cents: int | None = None
        self.pending_capture_request_id: str | None = None
        # When the pending intent was recorded, as epoch seconds
        # (round 28, C3). Lets the payer cancel a stale intent after
        # a TTL — an intent saved but never POSTed (process died in
        # the window) must not be executable by background paths
        # forever. None when no capture is in flight (older store
        # files load with None via .get()).
        self.pending_capture_created_at: int | None = None
        # Phase 5: capture recovery point. Persisted progress markers
        # so a crashed capture request RESUMES instead of RESTARTS:
        #   "intent_saved" — intent persisted, PSP not yet called.
        #                    Safe to call PSP (or re-call with same key).
        #   "psp_called"   — PSP was called; outcome unknown (returned
        #                    ambiguously or raised). NEVER re-POST blindly —
        #                    verify-then-act with the original key.
        #   None           — no capture in flight (not started, recorded,
        #                    abandoned, or reconciled).
        # Set in the same save as the step it marks (same-transaction
        # rule). Older store files load with None via .get().
        self.capture_recovery_point: str | None = None
        # Grok fix order #2: a void intent is NOT a capture intent.
        # SaveVoidIntent / SaveResidualVoidIntent used to be stored in
        # pending_capture_request_id, so resolve_state() saw a "pending
        # capture" and forced S7 — after a partial capture the job
        # resolved to S7 instead of S8, capture_partial dispatched
        # E_START_CAPTURE_PARTIAL in S7 (not in the table ->
        # UnhandledTransition), and the residual was never voided.
        # Void keys live here now; resolve_state() never consults
        # this field, so a void intent cannot force a capture state.
        # None when no void is in flight (older store files load with
        # None via .get()).
        self.pending_void_request_id: str | None = None
        # True once authorize() has attempted its PayPal POST (round
        # 28, C2). A failed authorize may still have placed a hold
        # server-side — the sweep must never delete a job that
        # attempted authorize, or the only local record of a live
        # hold is lost. False by default (older store files load
        # with False via .get()).
        # A capture denial PayPal reported for this job (round 10):
        # {"event_id", "resource_id", "status": "DENIED"}. Kept in
        # its own field — the DENIED branch used to overwrite
        # last_paypal_response with the whole event body, destroying
        # the authorize record it replaced. None until a denial
        # arrives (store files written before round 10 load with
        # None via .get()).
        self.webhook_denial: dict | None = None
        # Capture attempt counter (round 13): disambiguates capture
        # idempotency keys across retries after a definitive decline.
        # A key PayPal declined is replayed forever under the same
        # key, so a new attempt must use a new key — the counter
        # increments only when an intent is cleared because its key
        # is known-bad (a definitive DECLINED/FAILED body, or a
        # CAPTURE.DENIED webhook for the pending intent), never when
        # the key was proven never-seen (CREATED reconcile clear) or
        # spent (capture recorded). Store files written before round
        # 13 load with 0 via .get().
        self.capture_attempt: int = 0
        # Operator-review flag (round 18): set when reconcile sees a
        # state it cannot resolve on its own — an unauditable partial
        # capture (PARTIALLY_CAPTURED with no recorded capture and no
        # pending key that can recover it). While set, the
        # VOIDED/EXPIRED/DENIED arm refuses to settle: the job wedges
        # with a review error instead of booking VOIDED over money
        # that may have moved. Cleared only when a capture is recorded
        # from PayPal's state, or by the held force-settle. False by
        # default (store files written before round 18 load with False
        # via .get()).
        self.needs_review: bool = False
        # The attempted amount to display in wedge errors while
        # needs_review is set (round 19). Separate from the live
        # intent: pending_capture_amount_cents is cleared when its key
        # is retired, but the operator still needs to see the amount.
        # Set when the flag is set, cleared when it clears. None when
        # no amount was in flight (store files written before round 19
        # load with None via .get()).
        self.needs_review_amount_cents: int | None = None
        # Why the flag was set (round 20): a short operator-readable
        # reason, so POST /admin/reconcile can list wedged jobs with
        # their reasons instead of making the operator parse error
        # strings. Set alongside the flag, cleared with it. None when
        # the flag is not set (store files written before round 20
        # load with None via .get()).
        self.needs_review_reason: str | None = None
        # v8.13: dispute review window. dispute_opened_at stamps when
        # the payer filed the dispute (epoch seconds);
        # dispute_deadline is when the review expires — the sweeper
        # auto-captures at/after it (silence pays the worker), so a
        # dispute can never wedge a job forever. None when no dispute
        # was filed (store files written before v8.13 load None via
        # .get() — the sweeper treats those as legacy and skips them).
        self.dispute_opened_at: int | None = None
        self.dispute_deadline: int | None = None
        # v8.14: worker's counter-note on a payment pause. Stored as
        # chargeback evidence only — it never changes any outcome.
        # The worker sets it via POST /jobs/<id>/pause/note; the payer
        # never sees it in the public dict. None when unset (older
        # store files load None via .get()).
        # Internal field names keep the "dispute_*" prefix for
        # continuity; the user-facing term is "payment pause" (see
        # dispute_job docstring) — a timer that delays capture, not
        # an adjudication. No one reviews the reason.
        self.pause_worker_note: str | None = None
        # Set once authorize() attempts its PayPal POST (round 28,
        # C2) — see the field comment above.
        self.authorize_attempted: bool = False
        # Delivery inspection deadline (suggestion #6): epoch seconds
        # when the buyer's inspection window closes after delivery.
        # Set when the job enters S6 (DELIVERED_OPEN) on submit_delivery.
        # When T_DEADLINE fires on S6 with no active dispute, the
        # service auto-starts capture so the seller's money is not
        # locked indefinitely. None until delivery is submitted
        # (older store files load with None via .get()). Configurable
        # via EscrowService.inspection_window_seconds (default 48h).
        self.inspection_deadline: int | None = None
        # When the PayPal authorization was placed (unix timestamp).
        # LOW #7 (Claude): the honor period (~72h) counts from
        # authorization, not delivery — the inspection deadline is
        # clamped to min(delivery + window, authorized_at + 72h -
        # margin) so auto-capture never fires on an expired hold.
        self.authorized_at: int | None = None
        # Void attempt counter (suggestion #3): tracks consecutive
        # pre-void read failures. After 3 consecutive unreadable
        # failures, the job wedges for operator review instead of
        # returning 409 forever. Reset on any successful read.
        # Older store files load with 0 via .get().
        self.void_attempts: int = 0
        # When the last void-read strike was counted, as epoch seconds
        # (MEDIUM #6): strikes only count when 60+ seconds (growing
        # with exponential backoff) have passed since the previous
        # one — a tight retry loop during an outage must not burn
        # through the strike budget. None when no strike was ever
        # counted (older store files load with None via .get()).
        self.last_void_attempt_time: int | None = None
        # Persistent void-read failure flag (MEDIUM #6): set when the
        # retry budget is exhausted. SEPARATE from needs_review — an
        # outage must not block the inspection auto-capture sweep
        # (which skips needs_review jobs). Cleared on any successful
        # pre-void read. Older store files load with False via .get().
        self.void_read_failed: bool = False

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "service": self.quote["service"],
            "price_cents": self.quote["price_cents"],
            "currency": self.quote.get("currency", "USD"),
            "payer": self.payer,
            "agent": self.agent,
            "quote_id": self.quote["quote_id"],
            "created_at": self.created_at,
            "deadline": self.deadline,
            "order_id": self.order_id,
            "authorization_id": self.authorization_id,
            "capture_id": self.capture_id,
            # All capture IDs (suggestion #5).
            "capture_ids": list(self.capture_ids),
            "capture_amounts": list(self.capture_amounts),
            "capture_amount_cents": self.capture_amount_cents,
            "voided_amount_cents": self.voided_amount_cents,
            "capture_reconciled": self.capture_reconciled,
            # The recorded capture's PayPal status (round 36).
            "capture_status": self.capture_status,
            # How the settlement was established (round 31, B2).
            "settlement_method": self.settlement_method,
            # Settlement note (round 50).
            "settlement_note": self.settlement_note,
            "delivery_hash": self.delivery_hash,
            "settled_at": self.settled_at,
            # The capture intent, if a capture is in flight (round 11).
            "pending_capture_amount_cents": self.pending_capture_amount_cents,
            "pending_capture_request_id": self.pending_capture_request_id,
            # Intent timestamp (round 28, C3).
            "pending_capture_created_at": self.pending_capture_created_at,
            # Capture recovery point (Phase 5): intent_saved |
            # psp_called | None. Internal operational detail — not
            # on the public dict.
            "capture_recovery_point": self.capture_recovery_point,
            # Void intent key (Grok fix order #2) — separate from the
            # capture intent so resolve_state() never mistakes a void
            # for a capture.
            "pending_void_request_id": self.pending_void_request_id,
            # Authorize-attempted flag (round 28, C2).
            "authorize_attempted": self.authorize_attempted,
            # Inspection deadline (suggestion #6).
            "inspection_deadline": self.inspection_deadline,
            "authorized_at": self.authorized_at,
            # Void attempt counter (suggestion #3).
            "void_attempts": self.void_attempts,
            "last_void_attempt_time": self.last_void_attempt_time,
            "void_read_failed": self.void_read_failed,
            # Capture attempt counter (round 13).
            "capture_attempt": self.capture_attempt,
            # Operator-review flag (round 18).
            "needs_review": self.needs_review,
            # Review display amount (round 19).
            "needs_review_amount_cents": self.needs_review_amount_cents,
            # Wedge reason (round 20).
            "needs_review_reason": self.needs_review_reason,
            # Dispute review window (v8.13).
            "dispute_opened_at": self.dispute_opened_at,
            "dispute_deadline": self.dispute_deadline,
            # Worker counter-note on a payment pause (v8.14).
            "pause_worker_note": self.pause_worker_note,
            # The capture denial, if PayPal reported one (round 10).
            "webhook_denial": self.webhook_denial,
            # Round 10, second layer: the stored record is already
            # allowlisted at record time, but anything returned over
            # HTTP is sanitized again here, so a body that reached
            # the store by some other path is still clean on the wire.
            "last_paypal_response": sanitize_paypal_record(
                self.last_paypal_response),
        }

    def to_public_dict(self) -> dict:
        """Public job representation for the unauthenticated endpoints
        (POST /jobs, GET /jobs, GET /jobs/<id>). to_dict() carries
        internal operational detail — idempotency keys
        (pending_capture_request_id, pending_void_request_id), review
        flags (needs_review*), attempt counters, and PayPal-side ids —
        none of which belong on a public listing. This is the minimal
        safe subset; authenticated callers keep using to_dict()."""
        return {
            "job_id": self.job_id,
            "status": self.status,
            "service": self.quote["service"],
            "price_cents": self.quote["price_cents"],
            "currency": self.quote.get("currency", "USD"),
            "created_at": self.created_at,
            "deadline": self.deadline,
            "settled_at": self.settled_at,
        }

    def to_store_dict(self) -> dict:
        """Full job state for the durable store (app/store.py). Unlike
        to_dict() this keeps the whole quote, so a job loaded after a
        restart still has its price, payee and expiry."""
        return {
            "job_id": self.job_id,
            "quote": self.quote,
            "payer": self.payer,
            "agent": self.agent,
            "created_at": self.created_at,
            "deadline": self.deadline,
            "status": self.status,
            "order_id": self.order_id,
            "authorization_id": self.authorization_id,
            "capture_id": self.capture_id,
            # All capture IDs (suggestion #5).
            "capture_ids": list(self.capture_ids),
            "capture_amounts": list(self.capture_amounts),
            "capture_amount_cents": self.capture_amount_cents,
            "voided_amount_cents": self.voided_amount_cents,
            "delivery_hash": self.delivery_hash,
            "settled_at": self.settled_at,
            "capture_reconciled": self.capture_reconciled,
            # Capture status (round 36).
            "capture_status": self.capture_status,
            # Settlement method (round 31, B2).
            "settlement_method": self.settlement_method,
            # Settlement note (round 50).
            "settlement_note": self.settlement_note,
            "pending_capture_amount_cents": self.pending_capture_amount_cents,
            "pending_capture_request_id": self.pending_capture_request_id,
            # Intent timestamp (round 28, C3).
            "pending_capture_created_at": self.pending_capture_created_at,
            # Capture recovery point (Phase 5): intent_saved |
            # psp_called | None. Must persist — a crashed process
            # resumes from this on the next attempt.
            "capture_recovery_point": self.capture_recovery_point,
            # Void intent key (Grok fix order #2).
            "pending_void_request_id": self.pending_void_request_id,
            # Authorize-attempted flag (round 28, C2).
            "authorize_attempted": self.authorize_attempted,
            # Inspection deadline (suggestion #6).
            "inspection_deadline": self.inspection_deadline,
            "authorized_at": self.authorized_at,
            # Void attempt counter (suggestion #3).
            "void_attempts": self.void_attempts,
            "last_void_attempt_time": self.last_void_attempt_time,
            "void_read_failed": self.void_read_failed,
            # Capture attempt counter (round 13).
            "capture_attempt": self.capture_attempt,
            # Operator-review flag (round 18).
            "needs_review": self.needs_review,
            # Review display amount (round 19).
            "needs_review_amount_cents": self.needs_review_amount_cents,
            # Wedge reason (round 20).
            "needs_review_reason": self.needs_review_reason,
            # Dispute review window (v8.13).
            "dispute_opened_at": self.dispute_opened_at,
            "dispute_deadline": self.dispute_deadline,
            # Worker counter-note on a payment pause (v8.14).
            "pause_worker_note": self.pause_worker_note,
            "last_paypal_response": self.last_paypal_response,
            "webhook_denial": self.webhook_denial,
        }

    @classmethod
    def from_store_dict(cls, data: dict) -> "Job":
        """Rebuild a Job saved by to_store_dict()."""
        job = cls(job_id=data["job_id"], quote=data["quote"],
                  payer=data["payer"], agent=data["agent"],
                  deadline_seconds=0)
        job.created_at = data["created_at"]
        job.deadline = data["deadline"]
        job.status = data["status"]
        job.order_id = data["order_id"]
        job.authorization_id = data["authorization_id"]
        job.capture_id = data["capture_id"]
        # .get(): store files written before suggestion #5 have no
        # capture_ids — backfill from the singular capture_id.
        job.capture_ids = data.get("capture_ids", [])
        if not job.capture_ids and job.capture_id:
            job.capture_ids = [job.capture_id]
        # .get(): store files written before round 3 have no
        # capture_amount_cents — they still load, with amount unknown.
        job.capture_amount_cents = data.get("capture_amount_cents")
        # .get(): store files written before HIGH #3 have no per-capture
        # amounts — they still load. Backfill from the total when there
        # is exactly one capture id; otherwise the breakdown is unknown
        # (the total stands alone, as before).
        job.capture_amounts = data.get("capture_amounts", [])
        if (not job.capture_amounts and job.capture_amount_cents is not None
                and len(job.capture_ids) == 1):
            job.capture_amounts = [job.capture_amount_cents]
        # .get(): store files written before round 8 have no
        # voided_amount_cents — they still load, with no residual void.
        job.voided_amount_cents = data.get("voided_amount_cents")
        job.delivery_hash = data["delivery_hash"]
        job.settled_at = data["settled_at"]
        job.capture_reconciled = data.get("capture_reconciled", False)
        # .get(): store files written before round 36 have no
        # capture status — they still load, with none recorded.
        job.capture_status = data.get("capture_status")
        # .get(): store files written before round 31 have no
        # settlement method — they still load, with none recorded.
        job.settlement_method = data.get("settlement_method")
        # .get(): store files written before round 50 have no
        # settlement note — they still load, with none.
        job.settlement_note = data.get("settlement_note")
        # .get(): store files written before round 11 have no capture
        # intent — they still load, with nothing in flight.
        job.pending_capture_amount_cents = data.get(
            "pending_capture_amount_cents")
        job.pending_capture_request_id = data.get(
            "pending_capture_request_id")
        # .get(): store files written before round 28 have no intent
        # timestamp — they still load, with no timestamp.
        job.pending_capture_created_at = data.get(
            "pending_capture_created_at")
        # .get(): store files written before Phase 5 have no recovery
        # point — they still load, with no point recorded. A job with
        # a pending intent but no point is treated as "intent_saved"
        # (the PSP was never called under the new scheme).
        job.capture_recovery_point = data.get("capture_recovery_point")
        # .get(): store files written before Grok fix order #2 have no
        # void intent — they still load, with no void in flight.
        job.pending_void_request_id = data.get("pending_void_request_id")
        # .get(): store files written before round 28 have no
        # authorize-attempted flag — they still load, as not attempted.
        job.authorize_attempted = data.get("authorize_attempted", False)
        # .get(): store files written before suggestion #6 have no
        # inspection deadline — they still load, with none set.
        job.inspection_deadline = data.get("inspection_deadline")
        job.authorized_at = data.get("authorized_at")
        # .get(): store files written before suggestion #3 have no
        # void attempt counter — they still load, at attempt 0.
        job.void_attempts = data.get("void_attempts", 0)
        # .get(): store files written before MEDIUM #6 have no
        # last-void-attempt timestamp — they still load, with none.
        job.last_void_attempt_time = data.get("last_void_attempt_time")
        # .get(): store files written before MEDIUM #6 have no
        # void-read-failed flag — they still load, as not failed.
        job.void_read_failed = data.get("void_read_failed", False)
        # .get(): store files written before round 13 have no
        # capture attempt counter — they still load, at attempt 0.
        job.capture_attempt = data.get("capture_attempt", 0)
        # .get(): store files written before round 18 have no review
        # flag — they still load, with review not needed.
        job.needs_review = data.get("needs_review", False)
        # .get(): store files written before round 19 have no review
        # amount — they still load, with no amount to display.
        job.needs_review_amount_cents = data.get(
            "needs_review_amount_cents")
        # .get(): store files written before round 20 have no wedge
        # reason — they still load, with no reason recorded.
        job.needs_review_reason = data.get("needs_review_reason")
        # .get(): store files written before v8.13 have no dispute
        # window — they still load; v8.14 treats a flagged job with
        # no dispute_deadline as already expired (captured, not
        # skipped forever).
        job.dispute_opened_at = data.get("dispute_opened_at")
        job.dispute_deadline = data.get("dispute_deadline")
        # v8.14: worker counter-note on a payment pause.
        job.pause_worker_note = data.get("pause_worker_note")
        # .get(): store files written before round 4 have no
        # last_paypal_response — they still load, with no audit record.
        job.last_paypal_response = data.get("last_paypal_response")
        # .get(): store files written before round 10 have no
        # webhook_denial — they still load, with no denial recorded.
        job.webhook_denial = data.get("webhook_denial")
        return job


class EscrowService:
    """Owns jobs and drives the PayPal hold/release/void calls.

    Jobs live in a pluggable store (app/store.py): MemoryStore by
    default (tests), JsonFileStore for the server, so jobs survive a
    process restart. Every state change is saved to the store before
    the method returns.

    Delivery verification: capture() requires the original delivery
    text and runs it through ``delivery_verifier`` (the hiring agent's
    verify_delivery when the server wires it, a plain hash comparison
    by default) BEFORE any PayPal call — funds can never be released
    on a delivery whose hash was not checked against the work.
    """

    # Suggestion #6 (T_DEADLINE): default buyer-inspection window —
    # 72 hours from delivery (S6 entry) to auto-capture when the
    # buyer never disputes. Overridable per service via the
    # inspection_window_seconds constructor kwarg.
    INSPECTION_WINDOW_SECONDS = 48 * 3600

    # MEDIUM #7: the inspection window is clamped to 48h — 72h plus
    # delivery time can outlast PayPal's ~3-day authorization honor
    # period, and auto-capture against an expired hold always fails.
    MAX_INSPECTION_WINDOW_SECONDS = 48 * 3600

    # v8.13: dispute review window — how long a payer-filed dispute
    # pauses auto-capture before the sweeper captures anyway (silence
    # pays the worker). 72h, clamped to the PayPal authorization
    # honor period (same ~72h-from-authorization convention as the
    # inspection deadline, with the same 1h margin).
    DISPUTE_WINDOW_SECONDS = 72 * 3600

    # v8.13: minimum authorization time remaining (after the 1h
    # honor margin) for a dispute to be accepted. A dispute filed
    # with less than this left cannot cover a meaningful review
    # window before the hold expires — those jobs must settle or
    # void now, not enter review (409).
    DISPUTE_MIN_HONOR_REMAINING_SECONDS = 6 * 3600

    # v8.13: PayPal authorization honor period and the safety margin
    # applied to it (shared by the inspection-deadline clamp and the
    # dispute-deadline clamp). Honor is three days from authorization,
    # not the 29-day auth lifetime.
    HONOR_PERIOD_SECONDS = 3 * 24 * 60 * 60
    HONOR_MARGIN_SECONDS = 60 * 60

    def _honor_limit(self, job: Job) -> int | None:
        """v8.14: the latest wall-clock time a pause deadline may use —
        authorized_at + honor period − margin. None when authorized_at
        is unknown (fail closed: the caller refuses the pause)."""
        authorized_at = job.authorized_at
        if authorized_at is None:
            return None
        return (authorized_at + self.HONOR_PERIOD_SECONDS
                - self.HONOR_MARGIN_SECONDS)

    # MEDIUM #6 / Gemini Q3: default cap on consecutive void-read
    # strikes before the job wedges for operator review. Transient
    # failures back off exponentially between strikes; permanent
    # failures wedge faster. Overridable per service via the
    # max_void_retries constructor kwarg.
    DEFAULT_MAX_VOID_RETRIES = 5

    # Phase 1 design (wedge-vs-awaiting): when a capture intent is
    # in-flight and PayPal returns authorization CAPTURED with no
    # capture listed (ENUM_NONE — replication lag), the service
    # returns awaiting=True (HTTP 503, PayPal redelivers) instead of
    # wedging, for this long from pending_capture_created_at. After
    # the grace period, the existing wedge applies.
    IN_FLIGHT_READ_LAG_GRACE_SECONDS = 15 * 60

    def _now(self) -> float:
        """Wall clock for time-window decisions; override in tests."""
        return time.time()

    # MEDIUM #6: minimum seconds between counted strikes. A strike
    # only counts when this long (growing with exponential backoff)
    # has passed since the previous one — a tight retry loop during
    # an outage must not burn through the strike budget.
    VOID_RETRY_BASE_INTERVAL_SECONDS = 60

    # MEDIUM #6: backoff cap — 2^attempt * 60, capped at 30 minutes.
    VOID_RETRY_MAX_BACKOFF_SECONDS = 1800

    # MEDIUM #6: jitter on the Retry-After hint (thundering-herd
    # protection). The enforced backoff stays deterministic so tests
    # can control time exactly.
    VOID_RETRY_JITTER_MAX_SECONDS = 30

    def __init__(self, paypal: PayPalClient, *, store=None,
                 delivery_verifier=None,
                 inspection_window_seconds: int = INSPECTION_WINDOW_SECONDS,
                 max_void_retries: int = DEFAULT_MAX_VOID_RETRIES):
        self.paypal = paypal
        if store is None:
            # Deferred import: app/store.py imports Job from this module.
            from .store import MemoryStore
            store = MemoryStore()
        self.store = store
        # v8.2 (production-readiness item 6): structured audit events
        # for every financial operation, recorded durably alongside
        # the jobs. Additive — recording never raises (see _audit),
        # so observability can never break money movement.
        self._audit_log = AuditLog(store)
        self.delivery_verifier = (
            delivery_verifier if delivery_verifier is not None
            else self._default_delivery_verifier)
        # MEDIUM #7: clamp to 48h — a longer window can outlast
        # PayPal's authorization honor period.
        if inspection_window_seconds > self.MAX_INSPECTION_WINDOW_SECONDS:
            logger.warning(
                "inspection_window_seconds=%s exceeds the 48h honor-period "
                "margin — clamped to %s",
                inspection_window_seconds,
                self.MAX_INSPECTION_WINDOW_SECONDS)
            inspection_window_seconds = self.MAX_INSPECTION_WINDOW_SECONDS
        self.inspection_window_seconds = inspection_window_seconds
        # MEDIUM #6 / Gemini Q3: configurable void-read strike budget.
        self.max_void_retries = max_void_retries
        # One lock per job so settlement is exactly-once even if two
        # requests arrive at the same instant: the status check and
        # the state change happen inside the lock, so the second call
        # always sees the settled state and is refused. submit_delivery
        # takes it directly; capture, capture_partial and
        # void_after_deadline take it through the @_per_job_locked
        # decorator (round 9). RLock: capture_partial's degenerate
        # branch calls capture(), which re-enters the same lock.
        self._delivery_locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    @staticmethod
    def _default_delivery_verifier(job: Job, delivery_text: str) -> bool:
        """The delivery text hashes to the hash recorded on the job.
        Same check as HiringAgent.verify_delivery, without needing a
        hiring agent — used when no verifier is injected."""
        return (hashlib.sha256(delivery_text.encode()).hexdigest()
                == job.delivery_hash)

    @property
    def jobs(self) -> dict[str, Job]:
        """Jobs by id, read from the store. Read-only view: mutate
        through the store (save/delete), never through this dict."""
        return {job.job_id: job for job in self.store.all()}

    def _save(self, job: Job) -> None:
        """Persist a job after a state change, so a restart never
        loses work already done (or money already moved)."""
        self.store.save(job)

    # v8.2 (production-readiness item 6): audit helpers. All recording
    # goes through _audit, which never raises — observability must not
    # break money movement.

    def _audit(self, operation: str, job: Job, *,
               idempotency_key: str | None = None,
               paypal_reported: dict | None = None,
               recorded: dict | None = None,
               outcome: str, reason: str | None = None) -> None:
        """Record one structured audit event. Never raises: a store
        failure is logged and swallowed."""
        audit = getattr(self, "_audit_log", None)
        if audit is None:
            return
        try:
            if recorded is None:
                recorded = _money_snapshot(job)
            if paypal_reported is None:
                paypal_reported = job.last_paypal_response
            audit.record(job_id=job.job_id, operation=operation,
                         idempotency_key=idempotency_key,
                         paypal_reported=paypal_reported,
                         recorded=recorded, outcome=outcome,
                         reason=reason)
        except Exception:
            logger.warning("audit record failed for %s on job %s",
                           operation, job.job_id, exc_info=True)

    def _audit_entry_key(self, job: Job, operation: str) -> str | None:
        """The idempotency key associated with an operation invocation,
        as best known at entry: the deterministic authorize key for
        authorize, the in-flight intent key for capture/void (the
        replay path — the money-dangerous case), else None. Keys
        minted mid-operation on a fresh attempt are carried by the
        in-flight "retried" events instead."""
        if operation == "authorize":
            return self._request_id(job, "authorize")
        if operation in ("capture", "capture_partial"):
            return job.pending_capture_request_id
        if operation == "void_after_deadline":
            return (job.pending_void_request_id
                    or self._request_id(job, "void"))
        return None

    def _audit_settled_outcome(self, job: Job) -> str:
        """Outcome for an operation that raised: "wedged" when the job
        carries the review flag (on the passed object or the stored
        one — wedge paths save before raising), else "failed"."""
        if job.needs_review:
            return "wedged"
        try:
            fresh = self.store.get(job.job_id)
        except Exception:
            fresh = None
        if fresh is not None and fresh.needs_review:
            return "wedged"
        return "failed"

    @staticmethod
    def _audit_failure_reason(job: Job, exc: BaseException) -> str:
        """The reason for a failed/wedged operation: the review reason
        when the job is wedged, else the exception message."""
        if job.needs_review and job.needs_review_reason:
            return job.needs_review_reason
        return str(exc) or type(exc).__name__

    @staticmethod
    def _record_capture_id(job: Job, cap_id: str) -> None:
        """Record a capture ID on the job (suggestion #5).

        Sets the singular ``capture_id`` (first capture, for backward
        compat) and appends to ``capture_ids`` (all captures). Empty
        IDs are ignored. Duplicate IDs are not appended twice.
        """
        if not cap_id:
            return
        if not job.capture_id:
            job.capture_id = cap_id
        if cap_id not in job.capture_ids:
            job.capture_ids.append(cap_id)

    @staticmethod
    def _book_capture(job: Job, cap_id: str,
                      amount_cents: int | None) -> None:
        """Book one capture's id AND amount (HIGH #3) — the single
        choke point for capture booking.

        Maintains the parallel ``capture_ids`` / ``capture_amounts``
        lists and recomputes ``capture_amount_cents`` as the summed
        total (fund-conservation comparisons keep working on the
        total). An id-only booking (amount None) records the id
        without touching amounts. A booking for an id already in the
        list updates that capture's amount in place (the round-29
        amount fill).

        MEDIUM #3 (Claude): unknown amounts are None, never 0. The
        total is recomputed ONLY when every listed id has a known
        amount — padding with 0 understated money and corrupted the
        sum (repro: book id A with no amount, then id B for 400 on a
        1000 hold, and the total read 400 instead of staying unknown).
        When any amount is unknown, the previously known total stands.
        """
        EscrowService._record_capture_id(job, cap_id)
        if amount_cents is None:
            # Id-only booking: ensure the slot exists as None so the
            # total is NOT recomputed until the amount arrives.
            if cap_id:
                idx = job.capture_ids.index(cap_id)
                while len(job.capture_amounts) <= idx:
                    job.capture_amounts.append(None)
            return
        if not cap_id:
            # Amount-only record (no attributable id): keep the old
            # behavior — the total is set, the breakdown stays empty.
            job.capture_amount_cents = amount_cents
            return
        # cap_id is now in job.capture_ids (appended above if new).
        idx = job.capture_ids.index(cap_id)
        while len(job.capture_amounts) <= idx:
            job.capture_amounts.append(None)
        job.capture_amounts[idx] = amount_cents
        # Recompute only when every amount is known (no Nones).
        if (len(job.capture_amounts) == len(job.capture_ids)
                and all(a is not None for a in job.capture_amounts)):
            job.capture_amount_cents = sum(
                a for a in job.capture_amounts if a is not None)

    def _set_pending_capture(self, job: Job, amount_cents: int | None,
                             request_id: str) -> None:
        """Record a capture intent (round 11) and save it BEFORE the
        PayPal call: the amount asked and the idempotency key that
        identifies it. ``amount_cents`` is None for a full capture —
        the call carries no amount, so a replay must not either. If
        the response is then lost after PayPal acted, the intent is
        what a retry or a reconcile replays — without it the job
        would look untouched while money moved.
        Round 28 (C3): the intent is timestamped. A background
        reconcile never POSTs — only the payer's explicit retry
        replays the key — so a stale intent needs an age for the
        payer-only cancel path."""
        job.pending_capture_amount_cents = amount_cents
        job.pending_capture_request_id = request_id
        job.pending_capture_created_at = int(time.time())
        # Phase 5: recovery point — the intent is saved, the PSP has
        # not been called yet. Persisted in the same save (same-
        # transaction rule).
        job.capture_recovery_point = "intent_saved"
        self._save(job)

    def _clear_pending_capture(self, job: Job) -> None:
        """Clear a capture intent without saving: the caller saves
        (it always saves right after recording the capture, or when
        reconcile proves nothing moved). Phase 5: also clears the
        recovery point — no intent in flight means no point."""
        job.pending_capture_amount_cents = None
        job.pending_capture_request_id = None
        job.pending_capture_created_at = None
        job.capture_recovery_point = None

    def _set_pending_void(self, job: Job, request_id: str) -> None:
        """Record a void intent (Grok fix order #2) and save it BEFORE
        the PayPal call: the idempotency key that identifies the void.
        Stored separately from the capture intent — resolve_state()
        never consults this field, so a void intent cannot force a
        capture-pending state (the S7-instead-of-S8 bug)."""
        job.pending_void_request_id = request_id
        self._save(job)

    def _clear_pending_void(self, job: Job) -> None:
        """Clear a void intent without saving: the caller saves."""
        job.pending_void_request_id = None

    def _abandon_pending_capture(self, job: Job,
                                 request_id: str | None = None,
                                 op: str | None = None) -> None:
        """Clear a capture intent whose key PayPal definitively
        declined (round 13) and retire the key: the attempt counter
        increments so the next capture derives a fresh key instead
        of replaying the declined one — PayPal replays a declined
        body for the same key forever. Saved: the caller raises
        right after, so the cleared intent and the retired key must
        persist, or the next attempt re-arms the poisoned key.

        ``request_id``/``op`` name the declined key and the owning
        operation for the audit event. They are passed explicitly
        because the dispatcher path applies ClearIntent (via
        R_CAP_DECLINED) before this runs, so the job's pending
        fields may already be cleared."""
        # v8.2 (item 6): the abandoned key is audit-relevant — a later
        # operator reading the log can see why this key never settled.
        declined_key = (request_id if request_id is not None
                        else job.pending_capture_request_id)
        operation = op or ("capture"
                           if job.pending_capture_amount_cents is None
                           else "capture_partial")
        self._audit(operation, job,
                    idempotency_key=declined_key,
                    outcome="abandoned",
                    reason="PayPal definitively declined the idempotency "
                           "key; the intent was cleared and the attempt "
                           "counter retired the key")
        self._clear_pending_capture(job)
        job.capture_attempt += 1
        self._save(job)

    # Intent-cancel TTL (round 28, C3): a pending intent may be
    # cancelled by the payer once it is this old and PayPal lists no
    # capture. 24 hours is conservative — well past PayPal's
    # idempotency replay window for a key that was actually sent, so
    # a silent key is almost certainly one that never left this
    # service.
    PENDING_CAPTURE_CANCEL_TTL_SECONDS = 24 * 3600

    @_per_job_locked
    def cancel_pending_capture(self, job: Job, *, payer: str,
                               now: int | None = None) -> dict:
        """Payer-only cancel of a stale capture intent (round 28, C3).
        A process that died after saving the intent but before the
        POST leaves a key PayPal never saw; background reconcile no
        longer replays it (read-only), and the payer may not want to
        retry — only to abandon it. Cancel requires: the payer, a
        pending intent older than the TTL, and an authorization +
        order read that lists no capture (a listed capture means money
        may have moved — use the normal paths). The intent is cleared
        WITHOUT retiring the key: the key was never sent, so there is
        nothing poisoned to retire; the next capture derives its key
        from the unchanged attempt counter. Raises EscrowError when
        any precondition fails. ``now`` is a test-only hook."""
        fresh = self.store.get(job.job_id)
        if fresh is None:
            raise EscrowError(f"job not found: {job.job_id}")
        job = fresh
        if payer != job.payer:
            raise EscrowError("only the payer can cancel a pending capture")
        if not self._has_pending_capture(job):
            raise EscrowError("no capture is pending for this job")
        if job.status not in ("AUTHORIZED", "DELIVERED"):
            raise EscrowError(
                f"cannot cancel a pending capture in state {job.status}")
        effective_now = now if now is not None else int(time.time())
        created_at = job.pending_capture_created_at
        if created_at is None:
            # Round 31 (C7): a missing timestamp (pre-round-28 store,
            # or a hand-edited file) used to read as 0 — decades old
            # — so the TTL passed immediately and the check failed
            # open. Treat it as "now" and stamp it: the TTL starts on
            # first sight.
            created_at = effective_now
            job.pending_capture_created_at = created_at
            self._save(job)
        if effective_now - created_at < self.PENDING_CAPTURE_CANCEL_TTL_SECONDS:
            raise EscrowError(
                "pending capture is not old enough to cancel — retry "
                "the capture to replay the key")
        try:
            auth = self.paypal.get_authorization(job.authorization_id)
        except PayPalError as e:
            raise EscrowError(
                f"cannot cancel: the authorization could not be re-read "
                f"({e}) — refusing to cancel blind")
        enum = self._first_completed_capture_including_order(
            job, auth)
        outcome, cap = enum.outcome, enum.capture
        # Round 50 (Claude round-49 Low 3): call the guard so the job
        # is flagged (the invariant says the guard sits behind every
        # read; the sweep and force_settle's own read are the
        # intentional exceptions, documented in the invariant text).
        if self._wedge_on_unattributable(
                job, outcome, "cancel_pending_capture"):
            raise EscrowError(
                job.needs_review_reason or
                "captures unattributable (operator review needed)")
        if enum.outcome in (enum.UNREAD, enum.AMBIGUOUS) or cap is not None:
            raise EscrowError(
                "cannot cancel: a capture may have moved — use the "
                "normal capture paths (operator review needed)")
        if auth.get("status") in ("PARTIALLY_CAPTURED", "CAPTURED"):
            # Round 29: PayPal often reports these statuses while
            # omitting captures from the linked array (outcome "none"
            # above). The status alone proves money may have moved —
            # cancelling would destroy the idempotency key of an
            # operation that executed. Use the normal paths.
            raise EscrowError(
                "cannot cancel: authorization status indicates money "
                "moved — use the normal capture or reconcile paths "
                "(operator review needed)")
        self._clear_pending_capture(job)
        self._save(job)
        return {"job_id": job.job_id, "cancelled": True, "error": ""}

    @staticmethod
    def _has_pending_capture(job: Job) -> bool:
        """Round 12: an intent exists iff its idempotency key is set.
        The amount is None for a full capture — that is a real
        intent, not "no intent" — so keying existence on the amount
        (as round 11's reconcile branches did) made a pending full
        capture invisible to reconcile: CREATED never cleared it,
        PARTIALLY_CAPTURED never replayed it, VOIDED never saw it."""
        return job.pending_capture_request_id is not None

    @staticmethod
    def _has_recorded_capture(job: Job) -> bool:
        """True when the job already has a recorded capture on its
        books: either a known capture id or a recorded capture amount
        (round 28, G2). An unauditable capture (id "" with an amount
        on the books) counts as recorded money. Round 43 (Claude
        round-42): one predicate everywhere — the fragmented
        `job.capture_id` / `capture_amount_cents is not None` checks
        treated amount-only records as empty in some branches,
        wedging jobs that already had money on the books."""
        return bool(job.capture_id) or job.capture_amount_cents is not None

    def _delivery_lock(self, job_id: str) -> threading.RLock:
        # Round 28 (C7): these locks are PER-PROCESS. They do not
        # coordinate across workers — two processes (or two machines)
        # serving the same store can still interleave on one job. The
        # cross-worker safety net is the store (single-writer per job
        # in practice) plus PayPal-side idempotency: every mutating
        # PayPal call carries a stable idempotency key, so a
        # double-execution degrades to a replayed response, not
        # double-moved money. Do not rely on these locks for
        # multi-worker exclusion.
        with self._locks_guard:
            lock = self._delivery_locks.get(job_id)
            if lock is None:
                lock = threading.RLock()
                self._delivery_locks[job_id] = lock
            return lock

    def _prune_settled_locks(self) -> int:
        """Drop per-job locks for jobs that are settled or gone (round
        28, C7): _delivery_locks otherwise grows without bound, one
        entry per job id ever seen. Only locks for terminal
        (CAPTURED/VOIDED) or missing jobs are pruned — every locked
        method re-reads the job and refuses a terminal job before
        doing anything, so a pruned lock can never let a new
        operation slip past a settle in flight (the settle saves
        before it releases). Returns how many were dropped."""
        pruned = 0
        with self._locks_guard:
            for job_id in list(self._delivery_locks):
                job = self.store.get(job_id)
                if job is None or job.status in ("CAPTURED", "VOIDED"):
                    del self._delivery_locks[job_id]
                    pruned += 1
        return pruned

    @staticmethod
    def _request_id(job: Job, operation: str) -> str:
        """Stable idempotency key for one job + one operation. PayPal
        replays the original result when the same key is retried, so a
        lost response can be recovered without executing twice.
        Round 43 (Claude round-42): the authorize key binds the order
        id — after a DENIED reset the payer creates a FRESH order, and
        reusing the same key on a different order would replay the
        DENIED (or fail as a duplicate request id). A distinct order
        gets a distinct key."""
        if operation == "authorize":
            return f"lamplight-{operation}-{job.job_id}-{job.order_id}"
        return f"lamplight-{operation}-{job.job_id}"

    @staticmethod
    def _capture_request_id(job: Job, operation: str) -> str:
        """Idempotency key for one capture attempt (round 13): the
        attempt counter disambiguates retries after a definitive
        decline, so a new attempt never replays a key PayPal already
        declined. The counter moves only when an intent is cleared
        because its key is known-bad (a DECLINED/FAILED body was
        observed) — never when the key was spent (capture recorded).
        A CREATED reconcile no longer clears at all (round 14): it
        probes the stored key, because CREATED proves nothing was
        captured, not that the key never reached PayPal — so the
        normal lost-response replay still reuses the stored key
        untouched."""
        return (f"lamplight-{operation}-{job.job_id}"
                f"-attempt-{job.capture_attempt}")

    def create_job(self, *, quote: dict, payer: str, agent: str,
                   deadline_seconds: int = 3600) -> Job:
        # Round 9: job ids are full UUID hex (32 characters). The old
        # 8-hex-character ids (32 bits) collided often enough to
        # matter, and a collision silently overwrote the existing job
        # in the store. Widen the ids AND check for a collision on
        # create, regenerating until the id is free — a collision can
        # no longer silently overwrite, however unlikely it is now.
        # Claude Q3: only USD is supported. amount_to_cents assumes
        # 2 decimal places; zero-decimal currencies (JPY) would
        # silently mis-parse. Reject non-USD at creation — fail fast,
        # not mid-escrow.
        ccy = str((quote or {}).get("currency") or "USD").upper()
        if ccy != "USD":
            raise EscrowError(
                f"unsupported currency: {ccy} (only USD is supported; "
                "zero-decimal currencies like JPY need an exponent "
                "table — future work)")
        while True:
            job_id = uuid.uuid4().hex
            if self.store.get(job_id) is None:
                break
        job = Job(job_id=job_id, quote=quote, payer=payer,
                  agent=agent, deadline_seconds=deadline_seconds)
        self.store.save(job)
        return job

    def get_job(self, job_id: str) -> Job:
        job = self.store.get(job_id)
        if job is None:
            raise EscrowError(f"job not found: {job_id}")
        return job

    # -- hold ------------------------------------------------------------------
    @_audited("create_order")
    def create_order(self, job: Job) -> dict:
        """Create the PayPal AUTHORIZE order for this job's quote. The
        worker's pay_to account is passed as the order payee, so the
        capture pays the worker — not the merchant that owns the API
        credentials. If order creation fails, the job is removed from
        the store instead of lingering as QUOTED with no order id.
        Round 31 (C6): idempotent — takes the per-job lock, re-reads,
        and refuses when an order already exists or authorize was
        already attempted. A second call must not create a second
        order and orphan the first (the adoption path would then read
        the wrong order)."""
        if USE_DISPATCHER:
            return self._create_order_via_dispatcher(job)
        with self._delivery_lock(job.job_id):
            fresh = self.store.get(job.job_id)
            if fresh is None:
                raise EscrowError(f"job not found: {job.job_id}")
            job = fresh
            if job.status != "QUOTED":
                raise EscrowError(f"cannot create order in state {job.status}")
            if job.order_id:
                raise EscrowError(
                    "order already created for this job "
                    f"({job.order_id}) — refusing to create a second one")
            if job.authorize_attempted:
                raise EscrowError(
                    "authorize already attempted for this job — refusing "
                    "to create a new order over it")
            try:
                order = self.paypal.create_order(
                    amount_cents=job.quote["price_cents"],
                    currency=job.quote.get("currency", "USD"),
                    description=job.quote["service"],
                    custom_id=job.quote["quote_id"],
                    payee_email=job.quote.get("pay_to"))
            except PayPalError:
                self.store.delete(job.job_id)
                raise
            job.order_id = order["id"]
            self._save(job)
            return order

    def _create_order_via_dispatcher(self, job: Job) -> dict:
        """Phase 4: create_order via dispatcher.

        Guards are checked first (preserving old EscrowError semantics),
        then the S1+E_CREATE_ORDER transition is dispatched. The
        dispatcher returns [NoOp] — the caller still performs the PayPal
        API call and DB write (two-phase pattern: dispatcher decides,
        caller executes).
        """
        from .dispatcher import resolve_state, transition, State, Event
        with self._delivery_lock(job.job_id):
            fresh = self.store.get(job.job_id)
            if fresh is None:
                raise EscrowError(f"job not found: {job.job_id}")
            job = fresh
            # Guards — same as old code, preserving EscrowError semantics.
            # (The dispatcher would raise UnhandledTransition for these,
            # which is HTTP 500; the old code raises EscrowError/HTTP 400.)
            if job.status != "QUOTED":
                raise EscrowError(f"cannot create order in state {job.status}")
            if job.order_id:
                raise EscrowError(
                    "order already created for this job "
                    f"({job.order_id}) — refusing to create a second one")
            if job.authorize_attempted:
                raise EscrowError(
                    "authorize already attempted for this job — refusing "
                    "to create a new order over it")
            # Dispatch: S1_QUOTED_FRESH + E_CREATE_ORDER → S2_QUOTED_ORDERED
            state = resolve_state(job)
            # Guards above guarantee S1
            new_state, actions = transition(state, Event.E_CREATE_ORDER)
            # actions == [NoOp]: caller creates the PayPal order
            try:
                order = self.paypal.create_order(
                    amount_cents=job.quote["price_cents"],
                    currency=job.quote.get("currency", "USD"),
                    description=job.quote["service"],
                    custom_id=job.quote["quote_id"],
                    payee_email=job.quote.get("pay_to"))
            except PayPalError:
                self.store.delete(job.job_id)
                raise
            job.order_id = order["id"]
            self._save(job)
            return order

    @staticmethod
    def _extract_first_authorization(result: object) -> dict:
        """Pull the first authorization from an authorize-order (or
        order) response, guarding the shape (round 28, C2c). Raises
        EscrowError — never IndexError/KeyError/TypeError — on a
        malformed body."""
        try:
            if not isinstance(result, dict):
                raise EscrowError(
                    "authorize returned a non-object response")
            pus = result.get("purchase_units") or []
            pu = pus[0] if pus else {}
            if not isinstance(pu, dict):
                raise EscrowError(
                    "authorize returned a malformed purchase_units entry")
            auths = (pu.get("payments") or {}).get("authorizations") or []
            auth = auths[0] if auths else {}
            if not isinstance(auth, dict) or not auth:
                raise EscrowError("authorize returned no authorization")
            return auth
        except (IndexError, KeyError, TypeError, AttributeError) as e:
            raise EscrowError(
                f"authorize returned a malformed response: {e}") from e

    def _adopt_authorization_from_order(self, job: Job,
                                        original_error: Exception) -> dict:
        """Fallback when both authorize POSTs failed (round 28, C2b):
        the POSTs may have authorized server-side before the response
        was lost. GET the order and adopt its first authorization when
        present. Raises EscrowError when the order cannot be read or
        lists no authorization."""
        try:
            order = self.paypal.get_order(job.order_id)
        except PayPalError as e2:
            raise EscrowError(
                f"PayPal authorize failed: {original_error} (order "
                f"re-read also failed: {e2})") from e2
        try:
            auth = self._extract_first_authorization(order)
        except EscrowError:
            raise EscrowError(
                f"PayPal authorize failed: {original_error} (order "
                "lists no authorization)") from None
        return auth

    def _read_order_authorization(self, job: Job) -> tuple[str, dict | None]:
        """Read-only order check for the authorize retry path (round 31,
        C1). Returns (outcome, auth):
          "adopted" — the order lists an authorization; auth is it.
          "empty"   — the order was read (200) and lists no
                      authorization, or the order is definitively absent
                      (404): nothing to adopt.
          "unread"  — the order could not be read (transport/5xx/4xx
                      other than 404): unknown, never "empty".
        Never POSTs, never raises."""
        try:
            order = self.paypal.get_order(job.order_id)
        except PayPalError as e:
            if _is_order_absent(e):
                return ("empty", None)
            return ("unread", None)
        except AttributeError:
            # No get_order on the client (incomplete test fakes).
            return ("unread", None)
        try:
            auth = self._extract_first_authorization(order)
        except EscrowError:
            return ("empty", None)
        return ("adopted", auth)

    @_audited("authorize")
    @_per_job_locked
    def authorize(self, job: Job) -> dict:
        """Place the hold, or recover one already placed (round 34 / v2).
        Live quote: POSTs authorize to PayPal (idempotent retry on transport
        failure, order fallback when both POSTs fail), adopts a CREATED or
        PENDING authorization as the live hold and returns it. A fresh POST
        that PayPal declines (DENIED/DECLINED) never placed a hold: the job
        stays QUOTED and the call raises (round 36, Claude round-35 item 3 —
        the long-standing "a DECLINED or DENIED result never settles the
        job" rule).
        Expired quote with a prior attempt: read-only adoption from the
        order — never POSTs; raises unless the order lists an authorization.
        An adopted authorization in a terminal state (VOIDED, EXPIRED,
        DENIED, CAPTURED, PARTIALLY_CAPTURED) is saved first
        (authorization_id, AUTHORIZED, persisted PayPal response) and then
        reconciled from the read in hand: a released hold with no captures
        listed anywhere books VOIDED; unread/ambiguous reads wedge for
        operator review (raising EscrowError, job left AUTHORIZED so
        force_settle can exit it); listed captures settle. Nothing is ever
        left in QUOTED with an adopted authorization.

        Phase 4: when USE_DISPATCHER, routes through _authorize_via_dispatcher."""
        if USE_DISPATCHER:
            return self._authorize_via_dispatcher(job)
        fresh = self.store.get(job.job_id)
        if fresh is None:
            raise EscrowError(f"job not found: {job.job_id}")
        job = fresh
        if job.status != "QUOTED" or not job.order_id:
            raise EscrowError(f"cannot authorize in state {job.status}")
        quote_is_expired = quote_expired(job.quote)
        if quote_is_expired and not job.authorize_attempted:
            raise EscrowError("quote expired")
        request_id = self._request_id(job, "authorize")
        job.authorize_attempted = True
        self._save(job)
        auth = None
        audit_result = None
        # Round 36 (Claude round-35 item 3): track whether the
        # authorization came from the fresh POST. A fresh denial
        # (DENIED/DECLINED) never placed a hold — the job stays
        # QUOTED. The adoption paths below recover an
        # already-placed hold, where a released status must book.
        fresh_post_auth = False
        if quote_is_expired:
            outcome, auth = self._read_order_authorization(job)
            if outcome == "adopted":
                audit_result = auth
            elif outcome == "empty":
                raise EscrowError(
                    "quote expired and the order lists no authorization "
                    "to adopt")
            else:
                raise EscrowError(
                    "quote expired and the order could not be re-read — "
                    "refusing to conclude blind")
        else:
            try:
                result = self.paypal.authorize_order(job.order_id,
                                                     request_id=request_id)
                auth = self._extract_first_authorization(result)
                audit_result = result
                fresh_post_auth = True
            except PayPalError as e:
                try:
                    result = self.paypal.authorize_order(job.order_id,
                                                         request_id=request_id)
                    auth = self._extract_first_authorization(result)
                    audit_result = result
                    fresh_post_auth = True
                except (PayPalError, EscrowError) as e2:
                    auth = self._adopt_authorization_from_order(job, e2)
                    audit_result = auth
            except EscrowError:
                auth = self._adopt_authorization_from_order(
                    job, "malformed authorize response")
                audit_result = auth

        status = auth.get("status")
        auth_id = auth.get("id")
        if not isinstance(auth_id, str) or not auth_id:
            raise EscrowError(
                "authorize returned an authorization with no id")
        if fresh_post_auth and status in ("DENIED", "DECLINED"):
            # The fresh authorize attempt was declined: no hold was
            # ever placed, so there is nothing to adopt. The job stays
            # QUOTED and the denial is recorded for the audit trail.
            # This is the long-standing rule ("a DECLINED or DENIED
            # result never settles the job"); it does not cover
            # recovering an already-placed hold the adoption paths
            # report as released, where the books must say the hold
            # died.
            # Round 40 (ChatGPT round-39): reset to a clean QUOTED —
            # clear the attempted flag and drop the order — so the
            # payer can fix the funding source and run create_order
            # -> authorize fresh. The old code left authorize_attempted
            # set on the same order: a retry replayed the same
            # idempotency key (PayPal replays the DENIED forever) and
            # create_order refused, so the job was permanently stuck
            # with no executable next action. A DENIED authorization
            # never placed a hold, so abandoning the order orphans
            # nothing.
            # Round 43 (Claude round-42): reset order_id to "" (not
            # None) — Job.order_id is typed str, and "" keeps the
            # type consistent across serialization.
            self._record_paypal_response(job, operation="authorize",
                                         status=status, id_=auth_id,
                                         body=audit_result)
            job.authorize_attempted = False
            job.order_id = ""
            self._save(job)
            raise EscrowError(
                f"PayPal declined the authorization (status {status}) — "
                "no hold was placed")
        # Adopt the authorization onto the job and advance to AUTHORIZED.
        job.authorization_id = auth_id
        job.status = "AUTHORIZED"
        # LOW #7: stamp when the hold was placed — the inspection
        # deadline clamps to the PayPal honor period from here.
        if job.authorized_at is None:
            job.authorized_at = int(time.time())
        self._record_paypal_response(job, operation="authorize",
                                     status=status,
                                     id_=auth_id, body=audit_result)
        self._save(job)

        if status in ("CREATED", "PENDING"):
            return auth

        # Terminal or non-live authorizations (VOIDED, EXPIRED, DENIED,
        # CAPTURED, PARTIALLY_CAPTURED): reconcile immediately using the
        # read already in hand. This fail-closes on unread/ambiguous order
        # reads, avoids premature VOIDED settlement, and settles unauditable
        # captures rather than wedging in QUOTED forever.
        rec = self._reconcile_with_auth(job, auth, "AUTHORIZED")
        if rec.get("error"):
            raise EscrowError(
                f"authorization not created (status {status}): {rec['error']}")
        return {**rec, "status": job.status, "authorization_id": auth_id}

    def _authorize_via_dispatcher(self, job: Job) -> dict:
        """Phase 4: authorize via dispatcher (two-phase).

        Phase 1: S2 + E_START_AUTHORIZE → S3, [SaveIntent].
        Phase 2: based on PayPal result, dispatch R_AUTH_CREATED /
                 R_AUTH_DENIED / R_AUTH_RETRYABLE → S4 / S1 / S3.
        The PayPal I/O and adoption/reconcile logic is preserved from
        the old code — the dispatcher decides the state transitions,
        the caller executes the side effects.
        """
        from .dispatcher import (resolve_state, transition, State, Event,
                                 Evidence, SaveIntent)
        fresh = self.store.get(job.job_id)
        if fresh is None:
            raise EscrowError(f"job not found: {job.job_id}")
        job = fresh
        if job.status != "QUOTED" or not job.order_id:
            raise EscrowError(f"cannot authorize in state {job.status}")
        quote_is_expired = quote_expired(job.quote)
        if quote_is_expired and not job.authorize_attempted:
            raise EscrowError("quote expired")

        # Phase 1: dispatch E_START_AUTHORIZE → SaveIntent
        # (only if in S2; if already S3 from a prior attempt, skip)
        state = resolve_state(job)
        if state == State.S2_QUOTED_ORDERED:
            ev = Evidence(job_id=job.job_id,
                          amount_cents=job.quote.get("price_cents"),
                          attempt=1)
            new_state, actions = transition(state, Event.E_START_AUTHORIZE, ev)
            # Execute SaveIntent: mark authorize_attempted (the intent marker)
            for action in actions:
                if action.__class__.__name__ == "SaveIntent":
                    job.authorize_attempted = True
                    self._save(job)

        request_id = self._request_id(job, "authorize")
        auth = None
        audit_result = None
        fresh_post_auth = False
        if quote_is_expired:
            outcome, auth = self._read_order_authorization(job)
            if outcome == "adopted":
                audit_result = auth
            elif outcome == "empty":
                raise EscrowError(
                    "quote expired and the order lists no authorization "
                    "to adopt")
            else:
                raise EscrowError(
                    "quote expired and the order could not be re-read — "
                    "refusing to conclude blind")
        else:
            try:
                result = self.paypal.authorize_order(job.order_id,
                                                     request_id=request_id)
                auth = self._extract_first_authorization(result)
                audit_result = result
                fresh_post_auth = True
            except PayPalError as e:
                try:
                    result = self.paypal.authorize_order(job.order_id,
                                                         request_id=request_id)
                    auth = self._extract_first_authorization(result)
                    audit_result = result
                    fresh_post_auth = True
                except (PayPalError, EscrowError) as e2:
                    auth = self._adopt_authorization_from_order(job, e2)
                    audit_result = auth
            except EscrowError:
                auth = self._adopt_authorization_from_order(
                    job, "malformed authorize response")
                audit_result = auth

        status = auth.get("status")
        auth_id = auth.get("id")
        if not isinstance(auth_id, str) or not auth_id:
            raise EscrowError(
                "authorize returned an authorization with no id")

        # Phase 2: dispatch based on result
        state = resolve_state(job)  # now S3
        if fresh_post_auth and status in ("DENIED", "DECLINED"):
            # R_AUTH_DENIED → S1 (clean reset)
            new_state, actions = transition(state, Event.R_AUTH_DENIED)
            # Execute: ClearIntent + NoOp(clear order_id, authorize_attempted=False)
            self._record_paypal_response(job, operation="authorize",
                                         status=status, id_=auth_id,
                                         body=audit_result)
            job.authorize_attempted = False
            job.order_id = ""
            self._save(job)
            raise EscrowError(
                f"PayPal declined the authorization (status {status}) — "
                "no hold was placed")

        # R_AUTH_CREATED → S4 (or stays S3 on retryable)
        if status in ("CREATED", "PENDING"):
            r_event = Event.R_AUTH_CREATED
        elif status in ("DENIED", "DECLINED"):
            r_event = Event.R_AUTH_DENIED
        else:
            r_event = Event.R_AUTH_500  # retryable → stays S3
        new_state, actions = transition(state, r_event)
        # Execute ClearIntent (no-op for the flag on success — it stays True)

        # Adopt the authorization onto the job and advance to AUTHORIZED.
        job.authorization_id = auth_id
        job.status = "AUTHORIZED"
        # LOW #7: stamp when the hold was placed — the inspection
        # deadline clamps to the PayPal honor period from here.
        if job.authorized_at is None:
            job.authorized_at = int(time.time())
        self._record_paypal_response(job, operation="authorize",
                                     status=status,
                                     id_=auth_id, body=audit_result)
        self._save(job)

        if status in ("CREATED", "PENDING"):
            return auth

        rec = self._reconcile_with_auth(job, auth, "AUTHORIZED")
        if rec.get("error"):
            raise EscrowError(
                f"authorization not created (status {status}): {rec['error']}")
        return {**rec, "status": job.status, "authorization_id": auth_id}

    # -- delivery --------------------------------------------------------------
    def submit_delivery(self, job: Job, *, agent: str, delivery_text: str,
                        now: int | None = None) -> str:
        """Worker agent marks delivery with a hash of the work. Exactly once,
        and only before the deadline — a delivery at or after the deadline
        is refused, so it can never block the payer's void.
        ``now`` is a test-only hook (lets tests simulate the deadline
        without sleeping); the server never passes it — production calls
        always use wall-clock time.

        Phase 4: when USE_DISPATCHER, routes through _submit_delivery_via_dispatcher."""
        if USE_DISPATCHER:
            return self._submit_delivery_via_dispatcher(
                job, agent=agent, delivery_text=delivery_text, now=now)
        with self._delivery_lock(job.job_id):
            # Round 13: re-read the job inside the lock, like every
            # other locked method — the passed Job may be a stale
            # copy, and delivering over a job another thread already
            # settled (voided, captured) would corrupt its state.
            fresh = self.store.get(job.job_id)
            if fresh is None:
                raise EscrowError(f"job not found: {job.job_id}")
            job = fresh
            if job.status != "AUTHORIZED":
                raise EscrowError(f"cannot deliver in state {job.status}")
            # Round 36 (Claude round-35 item 2): v2 leaves a wedged
            # authorization in AUTHORIZED with needs_review set — the
            # hold PayPal reported may already be gone. The worker must
            # not deliver against a dead hold; the operator clears the
            # wedge first (force_settle). capture and
            # void_after_deadline already carry this guard.
            if job.needs_review:
                raise EscrowError("cannot deliver: job needs operator review")
            if agent != job.agent:
                raise EscrowError("only the worker agent can deliver this job")
            # Round 9: a non-string delivery text (e.g. a JSON number)
            # used to reach .encode() and return a 500. Refuse it
            # here, before the empty check — the server also requires
            # the field, so HTTP answers 400.
            if not isinstance(delivery_text, str):
                raise EscrowError("delivery text must be a string")
            if not delivery_text:
                raise EscrowError("empty delivery")
            effective_now = now if now is not None else int(time.time())
            if effective_now >= job.deadline:
                raise EscrowError("delivery deadline passed")
            # Round 45 (Claude round-44 Low 3): read the authorization
            # before accepting the delivery. A hold voided or expired
            # at PayPal (webhook missed/delayed) must not accept work —
            # the worker would deliver against nothing. A definitively
            # dead hold goes through _reconcile_with_auth and the
            # delivery is refused. A transient read failure allows the
            # delivery: it moves no money, and capture() re-reads
            # before any POST.
            # Round 50 (Claude round-49 note): catch PayPalError only —
            # a bare "except Exception" would hide programming errors.
            try:
                auth = self.paypal.get_authorization(job.authorization_id)
            except PayPalError:
                auth = None
            if auth is not None:
                status = auth.get("status")
                # Round 46 (Claude round-45 Low 4): PARTIALLY_CAPTURED
                # also refuses — money moved before delivery, and the
                # worker must not deliver against a partially-consumed
                # hold.
                if (_is_hold_released(status) or status == "CAPTURED"
                        or status == "PARTIALLY_CAPTURED"):
                    self._reconcile_with_auth(job, auth,
                                              from_status=job.status)
                    self._save(job)
                    raise EscrowError(
                        f"cannot deliver: the PayPal hold is {status} — "
                        "delivery refused")
                # Round 52 (ChatGPT round-51 Low): a CREATED auth can
                # hide a completed capture the order lists. Enumerate
                # the order too — deliver against a partially-consumed
                # hold is the same shape the capture pre-flight refuses.
                enum = (
                    self._first_completed_capture_including_order(
                        job, auth))
                if enum.outcome in (enum.FOUND, enum.AMBIGUOUS,
                                    enum.UNATTRIBUTABLE, enum.ORDER_UNREAD):
                    self._reconcile_with_auth(job, auth,
                                              from_status=job.status)
                    self._save(job)
                    raise EscrowError(
                        "cannot deliver: the order lists a capture "
                        f"({enum.outcome}) against a CREATED hold — "
                        "delivery refused")
            job.delivery_hash = hashlib.sha256(delivery_text.encode()).hexdigest()
            job.status = "DELIVERED"
            # Suggestion #6 (T_DEADLINE): start the buyer-inspection
            # window on S6 entry — the sweep auto-captures when this
            # passes with no dispute (needs_review still False).
            # MEDIUM #7: only on FIRST S6 entry — a re-delivery must
            # not extend the window.
            # LOW #7 (Claude): the honor period (~72h) counts from
            # AUTHORIZATION, not delivery. Clamp the deadline to
            # min(delivery + window, authorized_at + 72h - 1h margin)
            # so auto-capture never fires on an expired hold. When
            # authorized_at is unknown (old jobs), fall back to the
            # delivery-based deadline.
            if job.inspection_deadline is None:
                candidate = effective_now + self.inspection_window_seconds
                if job.authorized_at is not None:
                    honor_limit = (job.authorized_at
                                   + 72 * 3600 - 3600)  # 1h margin
                    candidate = min(candidate, honor_limit)
                job.inspection_deadline = candidate
            self._save(job)
            return job.delivery_hash

    def _submit_delivery_via_dispatcher(self, job: Job, *, agent: str,
                                        delivery_text: str,
                                        now: int | None = None) -> str:
        """Phase 4: submit_delivery via dispatcher.

        Guards are checked first (preserving old EscrowError semantics),
        then S4+E_SUBMIT_DELIVERY is dispatched. The dispatcher returns
        [NoOp] — the caller saves delivery_hash and sets status=DELIVERED.
        The PayPal pre-flight read is preserved (caller-side I/O).
        """
        from .dispatcher import (resolve_state, transition, State, Event,
                                 Evidence, GuardRejection)
        with self._delivery_lock(job.job_id):
            fresh = self.store.get(job.job_id)
            if fresh is None:
                raise EscrowError(f"job not found: {job.job_id}")
            job = fresh
            # Guards — same as old code
            if job.status != "AUTHORIZED":
                raise EscrowError(f"cannot deliver in state {job.status}")
            if job.needs_review:
                raise EscrowError("cannot deliver: job needs operator review")
            if agent != job.agent:
                raise EscrowError("only the worker agent can deliver this job")
            if not isinstance(delivery_text, str):
                raise EscrowError("delivery text must be a string")
            if not delivery_text:
                raise EscrowError("empty delivery")
            effective_now = now if now is not None else int(time.time())
            deadline_passed = effective_now >= job.deadline
            # PayPal pre-flight read (caller-side I/O, preserved)
            try:
                auth = self.paypal.get_authorization(job.authorization_id)
            except PayPalError:
                auth = None
            if auth is not None:
                status = auth.get("status")
                if (_is_hold_released(status) or status == "CAPTURED"
                        or status == "PARTIALLY_CAPTURED"):
                    self._reconcile_with_auth(job, auth,
                                              from_status=job.status)
                    self._save(job)
                    raise EscrowError(
                        f"cannot deliver: the PayPal hold is {status} — "
                        "delivery refused")
                enum = (
                    self._first_completed_capture_including_order(
                        job, auth))
                if enum.outcome in (enum.FOUND, enum.AMBIGUOUS,
                                    enum.UNATTRIBUTABLE, enum.ORDER_UNREAD):
                    self._reconcile_with_auth(job, auth,
                                              from_status=job.status)
                    self._save(job)
                    raise EscrowError(
                        "cannot deliver: the order lists a capture "
                        f"({enum.outcome}) against a CREATED hold — "
                        "delivery refused")
            # Dispatch: S4 + E_SUBMIT_DELIVERY → S6
            # (raises GuardRejection/HTTP 410 if deadline_passed)
            state = resolve_state(job)
            ev = Evidence(deadline_passed=deadline_passed)
            try:
                new_state, actions = transition(state, Event.E_SUBMIT_DELIVERY,
                                                ev)
            except GuardRejection:
                raise EscrowError("delivery deadline passed")
            # Execute: NoOp → caller saves delivery_hash, status=DELIVERED
            job.delivery_hash = hashlib.sha256(
                delivery_text.encode()).hexdigest()
            job.status = "DELIVERED"
            # Suggestion #6 (T_DEADLINE): start the buyer-inspection
            # window on S6 entry — the sweep auto-captures when this
            # passes with no dispute (needs_review still False).
            # MEDIUM #7: only on FIRST S6 entry — a re-delivery must
            # not extend the window.
            # LOW #7 (Claude): clamp to the authorization honor period.
            if job.inspection_deadline is None:
                candidate = effective_now + self.inspection_window_seconds
                if job.authorized_at is not None:
                    honor_limit = (job.authorized_at
                                   + 72 * 3600 - 3600)  # 1h margin
                    candidate = min(candidate, honor_limit)
                job.inspection_deadline = candidate
            self._save(job)
            return job.delivery_hash

    # -- release ---------------------------------------------------------------

    def _retry_pre_read(self, job: Job, verb: str) -> dict | None:
        """Shared retry pre-read (round 50, Claude round-49 Medium 2):
        when a capture intent is already stored, read the authorization
        before replaying the key. PayPal replays the original PENDING
        body for a replayed key, so a blind retry returns "not
        completed" forever even after the capture completes.
        Returns an ALREADY_SETTLED dict if the reconcile settled, None
        if the caller should proceed to POST the stored key. Raises
        EscrowError for awaiting/wedge/refuse cases. On "unread"
        (transport failure), returns None — the caller replays the key
        (documented decision: a failed read must not block a retry
        that might be the only path to recovery).

        v8.2 (Finding 1 verdict): this rationale SURVIVES. When the auth
        read fails entirely (transport), we know NOTHING — the replay
        is the only recovery path, and the idempotency key protects us
        if PayPal accepted the first request. This is distinct from
        _capture_partial_with_reconcile Case A, where the auth read
        SUCCEEDS showing PARTIALLY_CAPTURED but the order read fails:
        there we KNOW money moved but not how much, so replaying risks
        stacking a new capture on moved money.
        """
        try:
            pre_auth = self.paypal.get_authorization(job.authorization_id)
        except PayPalError:
            # Documented: a failed read falls through to the replay.
            # The replay is idempotent (same key), so this is safe.
            # v8.2 (item 6): the replay decision is audit-relevant.
            self._audit("capture_partial" if "partial" in verb else "capture",
                        job, idempotency_key=job.pending_capture_request_id,
                        outcome="retried",
                        reason="retry pre-read could not read the "
                               "authorization (transport failure); "
                               "replaying the stored idempotency key")
            return None
        if pre_auth.get("status") != "CREATED":
            rec = self._reconcile_with_auth(job, pre_auth, job.status)
            if rec["to_status"] in ("CAPTURED", "VOIDED"):
                return {"status": "ALREADY_SETTLED",
                        "to_status": rec["to_status"],
                        "capture_id": rec["capture_id"],
                        "reconciled": True, "error": ""}
            if rec.get("awaiting"):
                raise EscrowError(rec["error"])
            if job.needs_review:
                raise EscrowError(
                    job.needs_review_reason or
                    f"{verb} retry pre-read: the reconcile wedged — "
                    "operator review needed")
            # Round 51 (Grok round-50 High): the reconcile may have
            # recorded a capture and cleared the intent (out-of-band
            # capture landed). The caller snapshotted the key BEFORE
            # this read — replaying it now would POST over the
            # recorded capture. Only return None (replay) when the
            # intent is still stored AND no capture is on the books.
            if (not self._has_pending_capture(job)
                    or self._has_recorded_capture(job)):
                recorded = (job.capture_amount_cents or 0)
                raise EscrowError(
                    f"{verb} retry: the pre-read recorded a capture "
                    f"({recorded} cents) — finish it with "
                    "capture_partial, do not replay the old key")
            # The reconcile did not settle, did not wedge, recorded
            # nothing, and the intent is intact — the capture is
            # still in flight. Return None so the caller replays the
            # stored key (the retry is the recovery path).
            # v8.2 (item 6): the replay decision is audit-relevant.
            self._audit("capture_partial" if "partial" in verb else "capture",
                        job, idempotency_key=job.pending_capture_request_id,
                        outcome="retried",
                        reason="retry pre-read found the capture still in "
                               "flight with the intent intact; replaying "
                               "the stored idempotency key")
            return None
        # Auth still CREATED: enumerate for a listed terminal capture
        # the replay would miss.
        pre = self._first_completed_capture_including_order(
            job, pre_auth)
        if self._wedge_on_unattributable(
                job, pre.outcome, f"{verb} retry pre-read"):
            raise EscrowError(
                job.needs_review_reason or
                "captures unattributable (operator review needed)")
        # Round 51 (Grok round-50 Medium 2): an ambiguous (non-terminal)
        # listing must NOT fall through to a replay — the fresh
        # pre-flight in capture()/capture_partial() refuses this exact
        # state. Raise "awaiting COMPLETED" instead.
        # Round 53 (Gemini round-52 High 3): order_unread also blocks
        # the replay — an auth-only capture with an unread order cannot
        # be replayed over.
        if pre.outcome == pre.AMBIGUOUS:
            raise EscrowError(
                f"{verb} retry pre-read: a non-terminal capture is "
                "listed — awaiting COMPLETED (do not replay the key)")
        if pre.has_terminal_capture() and pre.capture is not None:
            rec = self._reconcile_with_auth(job, pre_auth, job.status)
            if rec["to_status"] in ("CAPTURED", "VOIDED"):
                return {"status": "ALREADY_SETTLED",
                        "to_status": rec["to_status"],
                        "capture_id": rec["capture_id"],
                        "reconciled": True, "error": ""}
            if job.needs_review:
                raise EscrowError(
                    job.needs_review_reason or
                    f"{verb} retry pre-read found a listed capture; "
                    "the reconcile wedged — operator review needed")
            # Round 51 (Grok round-50 Low 4): the intent is already
            # cleared and a partial is recorded — point at
            # capture_partial, not a "new attempt".
            recorded = (job.capture_amount_cents or 0)
            raise EscrowError(
                f"{verb} retry: partial capture recorded for "
                f"{recorded} cents — finish it with capture_partial, "
                "not with the stale key")
        return None

    @_audited("capture")
    @_per_job_locked
    def capture(self, job: Job, *, payer: str, delivery_text: str) -> dict:
        """Release the held money to the worker agent. Payer only, delivered only.
        The original delivery text is required, and it is verified against
        the hash recorded on the job (via delivery_verifier — the hiring
        agent's verify_delivery when the server wires it) BEFORE any PayPal
        call: a delivery whose hash was never checked can never release
        funds, whatever the caller claims. Only settles on a COMPLETED
        capture — a DECLINED, PENDING or FAILED result is refused, so the
        receipt never claims money that did not move.

        Phase 4: when USE_DISPATCHER, routes through _capture_via_dispatcher."""
        if USE_DISPATCHER:
            return self._capture_via_dispatcher(job, payer=payer,
                                                delivery_text=delivery_text)
        # Round 12: re-read the job inside the lock (see authorize):
        # the passed Job may be a stale copy, and a future store
        # must not hand a locked method a Job another thread already
        # settled. No-op against the current stores (they return the
        # same object).
        fresh = self.store.get(job.job_id)
        if fresh is None:
            raise EscrowError(f"job not found: {job.job_id}")
        job = fresh
        if job.status != "DELIVERED":
            raise EscrowError(f"cannot capture in state {job.status} (needs DELIVERED)")
        if payer != job.payer:
            raise EscrowError("only the payer can release this job")
        if not delivery_text:
            raise EscrowError("delivery text required for verification")
        if not self.delivery_verifier(job, delivery_text):
            raise EscrowError("delivery verification failed")
        return self._capture_core(job)

    def _capture_core(self, job: Job, _sweep_dispute: bool = False) -> dict:
        """The money-moving core of a full capture, shared by the
        payer-initiated capture() and the inspection-deadline
        auto-capture sweep (suggestion #6).

        Runs the full safety path — needs_review guard, partial
        guards, PayPal pre-flight read + reconcile, intent recording,
        the capture POST, the COMPLETED gate, amount checks, and the
        receipt — WITHOUT the delivery-text verification, which only
        capture() performs. The sweep calls this directly: an expired
        inspection window with no dispute is deemed acceptance (the
        buyer had the whole window to dispute and did not), so there
        is no delivery text for the system to verify.

        Must run under the job's per-job lock: capture() holds it via
        @_per_job_locked; check_inspection_deadlines() takes
        self._delivery_lock(job.job_id) per job. The job must already
        be re-read inside the lock (fresh from the store).

        _sweep_dispute (v8.14): the sweep's expired-pause capture.
        The needs_review flag stays SET until PayPal accepts — it is
        the 409 that blocks a concurrent pause from landing in the
        gap — so the normal guard is bypassed here. The flag is
        cleared in the success save, same write as the terminal
        status. On PayPal failure the flag stays set and the deadline
        stays in the past, so the next sweep retries.
        """
        # Round 26 (Gemini round-25 suggestion): the needs_review flag
        # guards capture too, matching void_after_deadline — a wedged
        # job must not accept new capture attempts. The payer-only
        # force-settle is the exit. v8.14: the sweep's expired-pause
        # capture bypasses this — the flag is the ladder-blocker and
        # clears on success below.
        if job.needs_review and not _sweep_dispute:
            amount_note = (
                f" (attempted ${job.needs_review_amount_cents/100:.2f})"
                if job.needs_review_amount_cents is not None else "")
            raise EscrowError(
                "cannot capture: job needs operator review"
                + amount_note)
        if self._has_recorded_capture(job):
            # A partial capture was already recorded for this job (the
            # capture moved money, the residual void did not finish).
            # Round 29: the guard keys on the G2 "already stored"
            # definition — a truthy capture_id OR a recorded amount —
            # so an unauditable capture (id "" with an amount on the
            # books) is also caught. A full capture now would capture
            # the REMAINING hold and overwrite the first capture's id
            # and amount on the job, corrupting the audit trail —
            # there is no "capture the rest" operation. Finish the
            # recorded partial with capture_partial and the same
            # amount instead.
            raise EscrowError(
                "partial capture already recorded for this job — "
                "finish it with capture_partial")
        if (self._has_pending_capture(job)
                and job.pending_capture_amount_cents is not None):
            # Round 11: a partial capture is in flight and may
            # already have executed server-side — a full capture now
            # would capture the residual on top of it and
            # over-capture. Finish the pending partial with
            # capture_partial instead. (A pending FULL capture has
            # amount None — this same operation's retry — and passes
            # through. Round 12: the guard keys on the intent's
            # existence plus a partial amount, so a full intent is
            # never mistaken for a partial one.)
            raise EscrowError(
                "partial capture already pending for "
                f"{job.pending_capture_amount_cents} cents — finish it "
                "with capture_partial")
        if job.pending_capture_request_id is None:
            # Round 13: pre-flight read — never POST blind. A fresh
            # capture with no stored intent on a hold PayPal already
            # reports as PARTIALLY_CAPTURED / CAPTURED / VOIDED /
            # EXPIRED must not invent a key and capture on top of
            # moved money (the disclosed residual hole). Reconcile
            # from the read instead, then refuse with the reconcile
            # outcome — the payer's path forward is the recorded
            # state, not another POST.
            try:
                pre = self.paypal.get_authorization(job.authorization_id)
            except PayPalError as e:
                raise EscrowError(
                    f"PayPal capture pre-flight read failed: {e}") from e
            pre_status = pre.get("status")
            if pre_status != "CREATED":
                # Round 15 (Grok round-14 finding 4): reconcile from
                # the pre-flight read under the lock already held —
                # reconcile_job would fetch the authorization a
                # second time, and a transport failure on that read
                # discarded the pre-flight's own answer. The job was
                # re-read at the top of this method and is open
                # (AUTHORIZED), which is all reconcile_job's
                # open-check would establish.
                rec = self._reconcile_with_auth(job, pre, job.status)
                if rec["to_status"] in ("CAPTURED", "VOIDED"):
                    # Round 14 (ChatGPT round-13 improvement 1): the
                    # pre-flight read found the money already moved
                    # (or the hold released) and reconcile settled it
                    # — return the settlement instead of a 409 whose
                    # only clue is "reconciled to CAPTURED". The
                    # endpoint returns the settled job and its receipt
                    # from this same job object.
                    return {"status": "ALREADY_SETTLED",
                            "to_status": rec["to_status"],
                            "capture_id": rec["capture_id"],
                            "reconciled": True, "error": ""}
                # Round 46 (Claude round-45 Low 5): use the structured
                # "awaiting" flag, not substring-matching (brittle).
                if rec.get("awaiting"):
                    raise EscrowError(rec["error"])
                raise EscrowError(
                    f"cannot capture: authorization is {pre_status} "
                    f"(reconciled to {rec['to_status']})")
            # Round 24 (Grok round-23 structural): the authorization
            # says CREATED, but the order may list a COMPLETED
            # capture the authorization omits — minting a key now
            # would capture on top of moved money. Same tri-state
            # check as the void pre-read: fail closed on unread,
            # record-and-handle on found.
            pre = self._first_completed_capture_including_order(
                job, pre)
            if pre.outcome == pre.UNREAD:
                raise EscrowError(
                    "PayPal capture pre-flight: authorization is "
                    "CREATED but the order could not be read — "
                    "refusing to capture blind")
            if pre.outcome == pre.AMBIGUOUS:
                # Round 28 (C5): a non-terminal capture is listed —
                # capturing on top of it could double-move. Refuse.
                raise EscrowError(
                    "PayPal capture pre-flight: authorization is "
                    "CREATED but a non-terminal capture is listed — "
                    "refusing to capture blind (operator review "
                    "needed)")
            if pre.outcome == pre.UNATTRIBUTABLE:
                # Round 49 (Claude round-48 MEDIUM 5): route through the
                # shared guard — sets needs_review so the operator sees
                # it and force_settle has an exit. A bare raise would
                # leave the job unflagged and every retry refusing
                # forever.
                self._wedge_on_unattributable(
                    job, pre.outcome,
                    "PayPal capture pre-flight")
                raise EscrowError(
                    job.needs_review_reason or
                    "captures unattributable (operator review needed)")
            if pre.capture is not None:
                return self._handle_preflight_order_capture(
                    job, pre.capture, "capture")
            # Fix 1 (unified idempotency): when the dispatcher is on,
            # mint the HMAC intent key here too, so the inspection
            # auto-capture path and the manual capture path share the
            # identical key format and never conflict on races.
            if USE_DISPATCHER:
                from .dispatcher import make_intent_key
                attempt_num = max(1, job.capture_attempt + 1)
                request_id = make_intent_key(job.job_id, "capture", attempt_num)
            else:
                request_id = self._capture_request_id(job, "capture")
            # Round 11: record the intent (whole hold, no amount in the
            # call) BEFORE the PayPal call, so a lost response still
            # leaves a replayable record of what was asked.
            self._set_pending_capture(job, None, request_id)
        else:
            # A full capture is already in flight — this is its retry:
            # reuse the stored key, never derive a fresh one.
            # Round 50 (Claude round-49 Medium 2): the pre-read is now
            # the shared _retry_pre_read helper.
            request_id = job.pending_capture_request_id
            # Phase 5: if the recovery point says the PSP was already
            # called, this is a resume after a crash in the ambiguous
            # window. The _retry_pre_read below already reads before
            # replaying (verify-then-act semantics) — the explicit
            # point makes the resume path auditable and prevents
            # future refactors from weakening it into a blind re-POST.
            if job.capture_recovery_point == "psp_called":
                self._audit("capture", job, idempotency_key=request_id,
                            outcome="resumed",
                            reason="resuming after psp_called recovery "
                                   "point; verify-then-act with the "
                                   "original key, never a blind re-POST")
            settled = self._retry_pre_read(job, "capture")
            if settled is not None:
                return settled
        try:
            result = self.paypal.capture_authorization(
                job.authorization_id, request_id=request_id)
            # Phase 5: the PSP was called and returned. Mark psp_called
            # BEFORE processing the result — a crash between here and
            # recording must resume via verify-then-act, not re-POST.
            # Same-transaction rule: the point persists in the same
            # save as the step it marks (the callers save after).
            job.capture_recovery_point = "psp_called"
            self._save(job)
        except PayPalError as e:
            # Phase 5: the PSP call raised. We are past the RPC attempt,
            # so request_was_sent=True — a transport failure here is
            # AMBIGUOUS (the request may have executed), not RETRYABLE.
            # Mark psp_called so a crash in the handler resumes via
            # verify-then-act, then route through the canonical helper.
            job.capture_recovery_point = "psp_called"
            self._save(job)
            error_class = classify_paypal_error(e, request_was_sent=True)
            result = self._verify_then_act(job, e, error_class)
        # Round 40 (ChatGPT round-39): the COMPLETED gate is for
        # observed (direct POST) results only. A reconciled result
        # carries the capture's REAL status from the read — the money
        # moved (the auth read CAPTURED), so REFUNDED,
        # PARTIALLY_REFUNDED, PENDING or UNAUDITED settle; they are
        # audit facts, not failures.
        if not result.get("reconciled"):
            if result.get("status") != "COMPLETED":
                if result.get("status") in ("DECLINED", "FAILED"):
                    # Definitive: PayPal will replay the declined body
                    # for this key forever. Clear the intent BEFORE
                    # raising and retire the key (round 13) — the next
                    # attempt must start clean with a new key, not replay
                    # the decline. PENDING is left alone: it may still
                    # complete, and the webhook/reconcile will resolve it.
                    self._abandon_pending_capture(job, request_id,
                                                  op="capture")
                raise EscrowError(
                    f"capture not completed (status {result.get('status')})")
        self._record_capture_id(job, result.get("id", ""))
        self._record_capture_amount(job, result)
        # Round 40 (ChatGPT round-39): record the result's actual
        # status. Observed results passed the COMPLETED gate above;
        # reconciled results carry the real status
        # _record_captured_authorization recorded — never manufacture
        # COMPLETED over it.
        job.capture_status = result.get("status")
        # The capture is recorded: the intent is spent.
        self._clear_pending_capture(job)
        # Round 11: the returned amount is checked against the hold.
        # A full capture can never legitimately move more than the
        # hold — but if it did, the money has still moved, so the
        # amount is recorded (never refused with nothing recorded:
        # that was Claude's round-10 finding 1 in reverse) and the
        # anomaly is logged for the operator instead of hidden.
        if (job.capture_amount_cents is not None
                and job.capture_amount_cents > job.quote["price_cents"]):
            logger.warning(
                "capture for job %s returned %s cents, more than the "
                "%s-cent hold — recorded as returned",
                job.job_id, job.capture_amount_cents,
                job.quote["price_cents"])
            # Claude v5 review: unify with the dispatcher path — an
            # impossible amount wedges with needs_review on BOTH paths,
            # not just a log line on legacy.
            self._set_review_flag(
                job,
                f"capture returned {job.capture_amount_cents} cents, more "
                f"than the {job.quote['price_cents']}-cent hold "
                f"(operator review needed)")
        # Round 45 (Claude round-44 Low 4): set the reconciled flag and
        # settlement method BEFORE the partial check — the partial
        # branch routes through _finish_partial_capture, which does
        # not set them (capture_partial sets them before calling it,
        # so only capture() left the gap).
        job.capture_reconciled = bool(result.get("reconciled"))
        job.settlement_method = (
            "reconciled" if result.get("reconciled") else "observed")
        # Round 28 (C4): a partial may have landed out of band between
        # the pre-flight read and the POST — PayPal then captures only
        # the remainder. Booking a full CAPTURED with a short amount
        # would leave the rest of the hold open at PayPal with no
        # residual void. Route a genuine partial through the partial
        # finish (residual void + split receipt) instead.
        if (job.capture_amount_cents is not None
                and 0 < job.capture_amount_cents < job.quote["price_cents"]):
            self._record_paypal_response(job, operation="capture",
                                         status=result.get("status"),
                                         id_=result.get("id"), body=result)
            self._save(job)
            return self._finish_partial_capture(job)
        # Round 10: set the reconciled flag explicitly from THIS
        # result — False for a capture observed through the normal
        # call, True only for the synthetic reconciled result from
        # _capture_with_reconcile. Before this, capture() never
        # touched the flag, so a concurrent reconcile that had set
        # it (its read saw CAPTURED while this capture was still
        # in flight) left the receipt claiming "reconciled" for a
        # capture that was observed normally. (The reconcile path
        # is now under the same per-job lock, so that interleave
        # cannot happen — this is the belt to its braces, and it
        # keeps the flag's meaning exact: observed vs recovered.)
        # (Round 45: the flag and settlement_method are now set above,
        # before the partial branch.)
        # The capture result is the last PayPal response that settled
        # this job — including the reconciled case, where the result is
        # the synthetic dict from _capture_with_reconcile (its body
        # carries "reconciled": True and the capture's real status,
        # round 40).
        self._record_paypal_response(job, operation="capture",
                                     status=result.get("status"),
                                     id_=result.get("id"), body=result)
        job.status = "CAPTURED"
        job.settled_at = int(time.time())
        if _sweep_dispute:
            # v8.14: clear the pause flag in the SAME write as the
            # terminal status. The flag stayed set until PayPal
            # accepted, blocking the ladder; now the job is settled.
            self._clear_review_flag(job)
        self._save(job)
        return result

    def _resolve_expired_dispute(self, job: Job,
                                 now: int | None = None) -> None:
        """Deadline reached. Record that capture is owed. Do not clear
        needs_review here — dispute_job 409s while the flag is set, and a
        failed capture must retry instead of falling back to a fresh DELIVERED."""
        effective_now = now if now is not None else int(time.time())
        self._audit("dispute_deadline_reached", job, outcome="completed",
                    reason=(
                        f"dispute deadline {job.dispute_deadline} reached "
                        f"at {effective_now} — capture owed"))
        # no _clear_review_flag, no status change. The flag stays set
        # until the capture succeeds (cleared in _capture_core's success
        # save, same write as the terminal status).

    def _dispute_sweep_action(self, job: Job, effective_now: int) -> str:
        if not job.needs_review:
            return "skip"
        # No deadline is the v8.12 wedge (and any other writer that sets
        # the flag and forgets the timestamp). Capture it. Do not skip forever.
        if job.dispute_deadline is None or job.dispute_deadline <= effective_now:
            return "capture"
        return "skip"

    def check_inspection_deadlines(self, now: int | None = None) -> list[str]:
        """Auto-capture jobs whose inspection window expired (suggestion
        #6, T_DEADLINE in S6).

        For each job in S6 (DELIVERED) whose inspection_deadline has
        passed and which has no active pause (needs_review is False),
        run the full capture through _capture_core — the same
        pre-flight read, reconcile, intent, and COMPLETED gate the
        payer-initiated capture uses. The delivery-text verification is
        waived: an expired window with no pause is deemed acceptance
        (the buyer had the whole window to pause and did not), so
        there is no delivery text for the system to verify.

        v8.14: a payer-filed payment pause (see dispute_job) holds
        auto-capture only until dispute_deadline. At/after the deadline
        the sweep records the timeout (audit) and captures through
        _capture_core with _sweep_dispute=True. The needs_review flag
        stays SET until PayPal accepts — it is the 409 that blocks a
        concurrent pause from landing in the clear-then-capture gap —
        and clears in the same write as the terminal CAPTURED status.
        On PayPal failure the flag stays set and the deadline stays in
        the past, so the next sweep retries. A pause can never wedge a
        job forever, and the job never passes through DELIVERED with
        the flag cleared.

        On the dispatcher (step 4 of the design): app/dispatcher.py
        already has an S6 + T_DEADLINE cell (_s6_deadline), but it is a
        NoOp — "awaiting capture" — because the dispatcher never
        performs I/O by design; the caller must POST. E_START_CAPTURE
        in S6 guards on delivery_verified evidence the sweep cannot
        honestly supply, so the sweep captures through the live
        _capture_core instead of fabricating dispatcher evidence.

        Sweep-frequency requirement (v8.14): the default sweep runs
        every 300s; the honor margin is 3600s. The interval plus one
        retry must fit inside the margin, or a deadline that passes
        just after a tick waits out the margin and the capture goes
        out past the honor period. Do not configure
        LAMPLIGHT_SWEEP_INTERVAL_SECONDS above ~1800.

        A job that fails to capture (PayPal error, wedge raised
        mid-flight) is logged and skipped — the sweep never dies on
        one bad job; the next run retries it. Jobs under an unexpired
        pause are skipped. Returns the job ids that reached CAPTURED
        on this run.

        ``now`` is a test-only hook; production calls always use
        wall-clock time.
        """
        effective_now = now if now is not None else int(time.time())
        captured: list[str] = []
        for job in self.store.all():
            if job.status != "DELIVERED":
                continue
            if job.needs_review:
                # v8.14: pause clock, not the inspection clock. Skip
                # while under review; fall through to the locked
                # capture path once the pause deadline has passed.
                # The flag stays set through the capture — it is the
                # ladder-blocker.
                if (self._dispute_sweep_action(job, effective_now)
                        != "capture"):
                    continue
            else:
                if job.inspection_deadline is None:
                    continue
                if job.inspection_deadline >= effective_now:
                    continue
            with self._delivery_lock(job.job_id):
                # Re-read inside the lock: the sweep's snapshot may be
                # stale, and a concurrent capture/void/pause may
                # have moved the job since.
                fresh = self.store.get(job.job_id)
                if fresh is None:
                    continue
                job = fresh
                if job.status != "DELIVERED":
                    continue
                sweep_dispute = False
                if job.needs_review:
                    if (self._dispute_sweep_action(job, effective_now)
                            != "capture"):
                        continue
                    # v8.14: pause expired unresolved — record the
                    # timeout (audit only; the flag stays SET). A
                    # concurrent pause 409s on the flag. Capture
                    # below with _sweep_dispute=True; the flag clears
                    # in the same write as CAPTURED on success, and
                    # stays set for retry on failure.
                    try:
                        self._resolve_expired_dispute(job,
                                                      now=effective_now)
                    except Exception as e:  # noqa: BLE001 — e.g. a
                        # SQLite optimistic-concurrency conflict: another
                        # process won the race (filed or resolved first).
                        # Skip this run; the next sweep sees fresh state.
                        # The sweep never dies on one bad job.
                        logger.warning(
                            "inspection auto-capture for job %s: pause "
                            "resolution failed: %s", job.job_id, e)
                        continue
                    job = self.store.get(job.job_id)
                    if (job is None or job.status != "DELIVERED"
                            or not job.needs_review):
                        continue
                    sweep_dispute = True
                elif (job.inspection_deadline is None
                        or job.inspection_deadline >= effective_now):
                    continue
                # MEDIUM #7: verify the authorization is still valid
                # before auto-capturing. A 72h window plus delivery time
                # can outlast PayPal's ~3-day honor period — capturing
                # against an expired hold always fails. Wedge (don't
                # silently skip) so the operator sees it.
                try:
                    auth = self.paypal.get_authorization(
                        job.authorization_id)
                except Exception as e:  # noqa: BLE001 — transient read
                    # failure: skip this run, retry next time.
                    logger.warning(
                        "inspection auto-capture for job %s: auth read "
                        "failed: %s", job.job_id, e)
                    continue
                if (auth.get("status") in
                        ("EXPIRED", "VOIDED", "DENIED")):
                    self._set_review_flag(
                        job,
                        f"inspection window expired but the authorization "
                        f"is {auth.get('status')} — auto-capture refused "
                        f"(operator review needed)")
                    self._save(job)
                    logger.warning(
                        "inspection auto-capture for job %s wedged: "
                        "authorization %s", job.job_id, auth.get("status"))
                    continue
                try:
                    self._capture_core(job, _sweep_dispute=sweep_dispute)
                except Exception as e:  # noqa: BLE001 — MEDIUM #5: one
                    # bad job (PayPalError, GuardRejection, wedge
                    # raised mid-flight) must not kill the whole sweep;
                    # the next run retries it. For a sweep dispute the
                    # flag stays set and the deadline stays in the past,
                    # so the next sweep returns "capture" again.
                    logger.warning(
                        "inspection auto-capture for job %s refused: %s",
                        job.job_id, e)
                    continue
                if job.status == "CAPTURED":
                    captured.append(job.job_id)
        return captured

    def _apply_dispatcher_actions(self, job: Job, actions: list, new_state,
                                  read_only: bool = False) -> list:
        """Central executor for value-object actions emitted by transition().

        v8.2 (production-readiness item 2c): ONE executor. The sweeper
        calls this same function — one place where actions become state
        changes, with identical settle/flag semantics in both callers.

        Returns the list of unexecuted I/O actions (PostVoid, ReadOldOrder,
        DeleteJob, PlaceHold) for the caller to perform — the dispatcher
        is pure and never does I/O itself, so these must not be silently
        dropped (Grok fix order #3).

        read_only=True (the sweeper): I/O actions and intent-arming
        actions (SaveIntent/SaveResidualVoidIntent/SaveVoidIntent) are
        refused with GuardRejection instead of executed — the sweep never
        POSTs (round 28, C3). The caller treats the rejection as "leave
        the intent in place".
        """
        from .dispatcher import (
            State, RecordCapture, RecordVoid, SaveIntent, ClearIntent,
            SaveResidualVoidIntent, SaveVoidIntent, SetReviewFlag,
            ClearReviewFlag, NoOp, EmitReceipt, PostVoid, ReadOldOrder,
            DeleteJob, PlaceHold, GuardRejection,
        )
        import logging
        logger = logging.getLogger(__name__)
        # I/O actions the dispatcher cannot execute — returned to caller.
        pending_io: list = []
        for action in actions:
            if isinstance(action, (PostVoid, ReadOldOrder, DeleteJob,
                                   PlaceHold)):
                if read_only:
                    # The sweep never performs I/O — fail closed, keep
                    # the intent for the next sweep (or the operator).
                    raise GuardRejection(
                        f"sweeper: refusing I/O action "
                        f"{type(action).__name__} (read-only sweep)",
                        category="read_only")
                # Grok fix order #3: never drop I/O — hand it back.
                pending_io.append(action)
                continue
            if isinstance(action, NoOp) or isinstance(action, EmitReceipt):
                continue
            elif isinstance(action, RecordCapture):
                # Claude MEDIUM #5: use _book_capture (not direct writes)
                # to keep the per-capture breakdown and summed total
                # consistent for multi-capture jobs.
                # Grok fix order #3: book the total ONCE, not per ID —
                # booking amount_cents per id multiplied the total by
                # the id count (two ids at 1000 became 2000).
                if action.capture_ids:
                    for cid in action.capture_ids:
                        # Gemini v5 review (Q3.1): when there is exactly
                        # one id, the full amount belongs to it — book
                        # it directly so capture_amounts holds a real
                        # amount (not None), keeping future recomputes
                        # honest. With multiple ids the per-id split is
                        # unknown: record the ids, set the total below.
                        amt = (action.amount_cents
                               if len(action.capture_ids) == 1 else None)
                        self._book_capture(job, cid, amt)
                    if action.amount_cents is not None:
                        job.capture_amount_cents = action.amount_cents
                elif action.amount_cents is not None:
                    # No IDs (unaudited) — book amount without id
                    self._book_capture(job, "", action.amount_cents)
            elif isinstance(action, RecordVoid):
                # Grok fix order #3: if the dispatcher's void amount
                # disagrees with hold-minus-captured, wedge — do NOT
                # silently overwrite (that would erase money).
                hold = (job.quote or {}).get("price_cents", 0)
                captured = job.capture_amount_cents or 0
                expected = hold - captured
                if action.amount_cents != expected:
                    self._set_review_flag(
                        job,
                        f"RecordVoid amount {action.amount_cents} disagrees "
                        f"with hold-minus-captured {expected} (operator "
                        f"review needed)")
                else:
                    job.voided_amount_cents = action.amount_cents
            elif isinstance(action, SaveIntent):
                if read_only:
                    logger.info("sweeper: job %s: capture intent not "
                                "armed (read-only sweep)", job.job_id)
                    continue
                self._set_pending_capture(job, action.amount_cents, action.key)
            elif isinstance(action, SaveResidualVoidIntent):
                if read_only:
                    # Read-only sweep (round 28, C3): the sweep never
                    # POSTs, so it never arms a residual void either.
                    # The partial capture stays recorded on the books;
                    # the residual is left for force_settle or the payer.
                    logger.info("sweeper: job %s: residual void not "
                                "armed (read-only sweep)", job.job_id)
                    continue
                # Grok fix order #2: a residual void intent is NOT a
                # capture intent — storing it in pending_capture_request_id
                # made resolve_state() force S7 after a partial capture
                # (S8), breaking capture_partial with UnhandledTransition.
                self._set_pending_void(job, action.key)
            elif isinstance(action, SaveVoidIntent):
                if read_only:
                    logger.info("sweeper: job %s: void intent not armed "
                                "(read-only sweep)", job.job_id)
                    continue
                # Same: a void intent must never look like a capture
                # intent to resolve_state().
                self._set_pending_void(job, action.key)
            elif isinstance(action, ClearIntent):
                # Clear both intents — a stale key of either kind must
                # not survive settlement.
                self._clear_pending_capture(job)
                self._clear_pending_void(job)
            elif isinstance(action, SetReviewFlag):
                self._set_review_flag(job, action.reason,
                                      action.amount_cents)
            elif isinstance(action, ClearReviewFlag):
                self._clear_review_flag(job)

        # Synchronize status enum with Job string status
        if new_state in (State.S1_QUOTED_FRESH, State.S2_QUOTED_ORDERED, State.S3_QUOTED_ATTEMPTED):
            job.status = "QUOTED"
        elif new_state in (State.S4_AUTHORIZED_LIVE, State.S5_AUTHORIZED_WEDGED):
            job.status = "AUTHORIZED"
        elif new_state in (State.S6_DELIVERED_OPEN, State.S7_DELIVERED_PENDING,
                          State.S8_DELIVERED_PARTIAL_RECORDED, State.S9_DELIVERED_WEDGED):
            job.status = "DELIVERED"
        elif new_state in (State.S10a_CAPTURED_FULL, State.S10b_CAPTURED_SPLIT, State.S10c_CAPTURED_UNAUDITED):
            job.status = "CAPTURED"
            job.settled_at = int(time.time())
            if new_state == State.S10b_CAPTURED_SPLIT:
                job.voided_amount_cents = self._compute_voided_residual(job)
            elif new_state == State.S10c_CAPTURED_UNAUDITED:
                self._mark_unaudited(job)
        elif new_state == State.S11_VOIDED_SETTLED:
            job.status = "VOIDED"
            job.settled_at = int(time.time())

        # Grok fix order #3: hand unexecuted I/O back to the caller.
        return pending_io

    def _capture_via_dispatcher(self, job: Job, *, payer: str,
                                delivery_text: str) -> dict:
        from .dispatcher import (resolve_state, transition, State, Event,
                                 Evidence, SaveIntent)
        fresh = self.store.get(job.job_id)
        if fresh is None:
            raise EscrowError(f"job not found: {job.job_id}")
        job = fresh

        if job.status != "DELIVERED":
            raise EscrowError(f"cannot capture in state {job.status} (needs DELIVERED)")
        if payer != job.payer:
            raise EscrowError("only the payer can release this job")
        if not delivery_text:
            raise EscrowError("delivery text required for verification")
        if not self.delivery_verifier(job, delivery_text):
            raise EscrowError("delivery verification failed")
        if job.needs_review:
            # Round 26: match legacy _capture_core verbatim — the
            # amount_note is part of the contract (tests assert on it).
            amount_note = (
                f" (attempted ${job.needs_review_amount_cents/100:.2f})"
                if job.needs_review_amount_cents is not None else "")
            raise EscrowError(
                "cannot capture: job needs operator review"
                + amount_note)
        # Grok fix order #4: same guards as legacy _capture_core.
        if self._has_recorded_capture(job):
            # A partial capture was already recorded — a full capture
            # now would capture the residual on top of it and corrupt
            # the audit trail. Finish with capture_partial instead.
            raise EscrowError(
                "partial capture already recorded for this job — "
                "finish it with capture_partial")
        if (self._has_pending_capture(job)
                and job.pending_capture_amount_cents is not None):
            # A partial capture is in flight and may already have
            # executed server-side — a full capture now would
            # over-capture. Finish the pending partial instead.
            raise EscrowError(
                "partial capture already pending for "
                f"{job.pending_capture_amount_cents} cents — finish it "
                "with capture_partial")

        state = resolve_state(job)
        if job.pending_capture_request_id is None:
            try:
                pre = self.paypal.get_authorization(job.authorization_id)
            except PayPalError as e:
                raise EscrowError(
                    f"PayPal capture pre-flight read failed: {e}") from e
            if pre.get("status") != "CREATED":
                # v8 money-safety fix: one reconcile implementation —
                # route through the dispatcher bridge, not legacy
                # directly. The pre-fetched read is passed through so
                # the bridge reconciles exactly what the preflight saw.
                rec = self._reconcile_job_via_dispatcher(job, auth=pre)
                if rec["to_status"] in ("CAPTURED", "VOIDED"):
                    return {"status": "ALREADY_SETTLED", "to_status": rec["to_status"],
                            "capture_id": rec["capture_id"], "reconciled": True, "error": ""}
                # Surface the bridge's own error (e.g. "awaiting
                # COMPLETED", "order could not be read") — it knows why
                # the job isn't capturable. The generic message is only
                # the fallback when the bridge reported nothing.
                raise EscrowError(
                    rec.get("error")
                    or f"cannot capture: authorization is {pre.get('status')} "
                       f"(reconciled to {rec['to_status']})")
            # Grok fix order #4: same order-level tri-state check as
            # legacy — the authorization may say CREATED while the
            # order lists a COMPLETED capture the auth omits.
            pre = self._first_completed_capture_including_order(job, pre)
            if pre.outcome == pre.UNREAD:
                raise EscrowError(
                    "PayPal capture pre-flight: authorization is "
                    "CREATED but the order could not be read — "
                    "refusing to capture blind")
            if pre.outcome == pre.AMBIGUOUS:
                raise EscrowError(
                    "PayPal capture pre-flight: authorization is "
                    "CREATED but a non-terminal capture is listed — "
                    "refusing to capture blind (operator review needed)")
            if pre.outcome == pre.UNATTRIBUTABLE:
                self._wedge_on_unattributable(
                    job, pre.outcome, "PayPal capture pre-flight")
                raise EscrowError(
                    job.needs_review_reason or
                    "captures unattributable (operator review needed)")
            if pre.capture is not None:
                return self._handle_preflight_order_capture(
                    job, pre.capture, "capture")

            # Dynamic attempt counter prevents idempotency decline loops
            attempt_num = max(1, job.capture_attempt + 1)
            # Refined Fix 1: a FULL capture intent carries
            # amount_cents=None (the legacy convention: None = full
            # capture, int = partial). Storing the full price made the
            # retry guard misread it as a partial in flight and refuse
            # with "finish it with capture_partial", which then 500'd.
            ev = Evidence(job_id=job.job_id, attempt=attempt_num,
                          is_payer=True, delivery_verified=True,
                          amount_cents=None,
                          authorized_cents=job.quote.get("price_cents"))
            new_state, actions = transition(state, Event.E_START_CAPTURE, ev)
            self._apply_dispatcher_actions(job, actions, new_state)
            # Grok v7.1 finding #2: use the HMAC key set by
            # _apply_dispatcher_actions via SaveIntent — do NOT
            # overwrite with the legacy string. The HMAC key is what
            # DISPATCHER_INTENT_SECRET actually protects.
            request_id = job.pending_capture_request_id
        else:
            request_id = job.pending_capture_request_id
            settled = self._retry_pre_read(job, "capture")
            if settled is not None:
                return settled

        try:
            result = self.paypal.capture_authorization(job.authorization_id, request_id=request_id)
        except PayPalError:
            result = self._capture_with_reconcile(job)

        state = resolve_state(job)
        cap_id = (result.get("id") or "").strip()
        if result.get("reconciled"):
            # Claude review: a reconciled result carries no amount of
            # its own — _capture_with_reconcile already booked it on
            # the job via _record_captured_authorization (id + amount
            # when PayPal listed a capture, unaudited marker when it
            # listed none). Use the already-booked amount; never wedge
            # a reconciled capture for a missing amount in the result
            # dict. (Mirrors _record_capture_amount's round-25 rule: an
            # absent amount leaves the books alone.)
            listed_amount = job.capture_amount_cents
        else:
            listed_amount = self._listed_capture_amount_cents(result)
        if listed_amount is not None and listed_amount <= 0:
            # A zero/negative amount is not a real capture — wedge.
            # (None = unknown/malformed: legacy settles with the ID
            # recorded and amount unknown, per Claude finding 2.)
            self._set_review_flag(
                job,
                f"capture amount invalid (id {cap_id or 'unknown'})")
            self._save(job)
            raise EscrowError(
                f"capture amount invalid "
                f"(id {cap_id or 'unknown'}) — operator review needed")

        if not result.get("reconciled") and result.get("status") != "COMPLETED":
            if result.get("status") in ("DECLINED", "FAILED"):
                if state == State.S7_DELIVERED_PENDING:
                    new_state, actions = transition(state, Event.R_CAP_DECLINED)
                    self._apply_dispatcher_actions(job, actions, new_state)
                self._abandon_pending_capture(job, request_id,
                                              op="capture")
            raise EscrowError(f"capture not completed (status {result.get('status')})")

        if state in (State.S7_DELIVERED_PENDING, State.S6_DELIVERED_OPEN):
            if not cap_id:
                ev = Evidence(job_id=job.job_id, amount_cents=listed_amount,
                              authorized_cents=job.quote.get("price_cents"))
                new_state, actions = transition(state, Event.E_UNAUDITED_CAPTURE, ev)
            else:
                # Unknown amount (None) cannot be partial — settle as
                # full with amount unknown (legacy: ID recorded, amount
                # None). Only a known positive amount below the hold
                # is partial.
                is_partial = (listed_amount is not None
                              and 0 < listed_amount < job.quote["price_cents"])
                ev = Evidence(job_id=job.job_id, capture_ids=(cap_id,),
                              amount_cents=listed_amount,
                              authorized_cents=job.quote.get("price_cents"))
                r_event = Event.R_PARTIAL_CAP_COMPLETED if is_partial else Event.R_CAP_COMPLETED
                new_state, actions = transition(state, r_event, ev)
            self._apply_dispatcher_actions(job, actions, new_state)

        # If the FSM dispatch was skipped (e.g. _capture_with_reconcile
        # already recorded a full capture, leaving resolve_state at S10a),
        # sync job.status from the FSM state like _apply_dispatcher_actions
        # does. Legacy sets status=CAPTURED unconditionally on success.
        _cur = resolve_state(job)
        if _cur in (State.S10a_CAPTURED_FULL, State.S10b_CAPTURED_SPLIT,
                    State.S10c_CAPTURED_UNAUDITED) and job.status == "DELIVERED":
            job.status = "CAPTURED"
            job.settled_at = int(time.time())
            if _cur == State.S10c_CAPTURED_UNAUDITED:
                self._mark_unaudited(job)

        job.capture_status = result.get("status")
        job.capture_reconciled = bool(result.get("reconciled"))
        job.settlement_method = "reconciled" if result.get("reconciled") else "observed"
        # Grok fix order #4: same over-capture warning as legacy — a
        # full capture can never legitimately move more than the hold,
        # but if it did the money moved, so it is recorded and the
        # anomaly is logged, never hidden.
        if (job.capture_amount_cents is not None
                and job.capture_amount_cents > job.quote["price_cents"]):
            logger.warning(
                "capture for job %s returned %s cents, more than the "
                "%s-cent hold — recorded as returned",
                job.job_id, job.capture_amount_cents,
                job.quote["price_cents"])
            # Claude v5 review: unify — an impossible amount wedges
            # with needs_review on BOTH paths.
            self._set_review_flag(
                job,
                f"capture returned {job.capture_amount_cents} cents, more "
                f"than the {job.quote['price_cents']}-cent hold "
                f"(operator review needed)")
        self._record_paypal_response(job, operation="capture", status=result.get("status"),
                                     id_=result.get("id"), body=result)
        self._save(job)

        if job.status == "DELIVERED" and resolve_state(job) == State.S8_DELIVERED_PARTIAL_RECORDED:
            return self._finish_partial_capture(job)

        return result

    @staticmethod
    def _record_paypal_response(job: Job, *, operation: str, status,
                                id_, body) -> None:
        """Persist the last successful PayPal response on the job (see
        Job.last_paypal_response). Call this where a PayPal call's
        successful result is accepted as the basis for a state change:
        authorize / capture / void, and the reconcile path's
        get_authorization results. Only successful responses are
        recorded — a failed call never overwrites the last good record,
        so a job whose reconcile 404s keeps the authorize record it
        already had. The body is capped by _trim_paypal_response_body
        (round 5): normal responses record fully and unchanged; only
        an oversized body is trimmed, and the guard never raises."""
        job.last_paypal_response = {
            "operation": operation,
            "status": status if isinstance(status, str) else "",
            "id": id_ if isinstance(id_, str) else "",
            # Round 10: the body is allowlisted before it is
            # recorded — only the audit fields survive, so buyer data
            # (payer, payment_source, shipping) can never reach the
            # store, whatever PayPal returns. The allowlist runs
            # AFTER the size trim: the trim is what degrades a
            # pathological body (circular, unserializable) to the
            # {"_trimmed": True} marker, and the allowlist keeps
            # that marker, so the cap still holds.
            "body": sanitize_paypal_body(
                _trim_paypal_body(body if isinstance(body, dict) else {})),
        }

    @staticmethod
    def _record_capture_amount(job: Job, capture_like: dict,
                               clear_review: bool = True) -> None:
        """Record the amount a capture moved, in cents, on the job.
        ``capture_like`` is a PayPal capture resource (or a dict with
        the same ``amount`` shape). An absent amount leaves the field
        alone; an unparseable amount keeps the known amount (round 25,
        Grok round-24 finding 1 — a failed parse must never blank a
        recorded amount). The id still stands either way.
        HIGH #3: routes through _book_capture so the per-capture
        breakdown (capture_amounts) and the summed total stay
        consistent for multi-capture jobs.
        Round 20: a recorded capture also clears the review flag —
        once a capture is on the books, the unauditable-partial
        concern the flag was set for is resolved. (The void pre-read
        wedge records a capture and then sets the flag — the set
        comes after, so the wedge still stands.) Round 25: wedge
        paths pass clear_review=False so the flag is never cleared
        just to be re-set by the caller — no intermediate state where
        a read could see it cleared."""
        amount = capture_like.get("amount")
        parsed: int | None = None
        if isinstance(amount, dict):
            try:
                parsed = amount_to_cents(amount.get("value"))
            except ValueError:
                pass
        if parsed is not None:
            EscrowService._book_capture(
                job, capture_like.get("id") or "", parsed)
        if clear_review:
            job.needs_review = False
            job.needs_review_reason = None
            job.needs_review_amount_cents = None

    @staticmethod
    def _listed_capture_amount_cents(cap: dict) -> int | None:
        """Parse a listed capture's amount to cents without touching
        the job (round 28, G2). Returns None when the amount is
        absent or unparseable — the caller keeps the stored amount."""
        amount = cap.get("amount")
        if isinstance(amount, dict):
            try:
                return amount_to_cents(amount.get("value"))
            except ValueError:
                return None
        return None

    @staticmethod
    def _listed_capture_currency(cap: dict) -> str | None:
        """The listed capture's currency code, uppercased (round 42).
        Returns None when absent or not a string — the caller treats
        that as unattributable."""
        amount = cap.get("amount")
        if isinstance(amount, dict):
            ccy = amount.get("currency_code")
            if isinstance(ccy, str) and ccy:
                return ccy.upper()
        return None

    def _new_capture_members(self, job: Job,
                               cap: dict) -> list[dict] | str:
        """New capture members in a listed capture (HIGH #3).

        Returns the list of {"id", "amount_cents"} members whose ids
        are not yet on the books — legitimate additional captures to
        book. Returns [] when every member is already recorded, or
        when the capture is not a synthetic sum (a bare differing id
        stays a conflict per the existing logic). Returns the string
        "conflict" when a new member cannot be attributed safely —
        the caller wedges instead of booking unknown money.

        Members come ONLY from the synthetic record's "_members" (see
        _sum_completed_captures), which proves PayPal listed both
        captures. Currency uniformity was verified at sum time.
        """
        raw_members = cap.get("_members")
        if not (isinstance(raw_members, list) and raw_members):
            # Not a synthetic sum: a bare listed id that differs from
            # the stored id is a conflict (existing logic below). Only
            # the summed record — which proves PayPal listed BOTH
            # captures — can book an additional capture. A single new
            # id with no _members cannot be attributed as a second
            # capture; fail closed.
            return []
        members = raw_members
        new_members: list[dict] = []
        # An id is "already on the books" when it matches the singular
        # capture_id OR is in the capture_ids list (older code and some
        # tests set the singular field directly).
        recorded_ids = set(job.capture_ids)
        if job.capture_id:
            recorded_ids.add(job.capture_id)
        if not recorded_ids:
            # No ids on the books (e.g. an amount-only record): the
            # existing logic below decides whether the listed id fills
            # a blank — this is not a multi-capture booking.
            return []
        for m in members:
            if not isinstance(m, dict):
                continue
            mid = m.get("id") or ""
            if not mid or mid in recorded_ids:
                continue
            mcents = m.get("amount_cents")
            if not isinstance(mcents, int) or mcents < 0:
                return "conflict"
            # Currency uniformity was verified when the sum was built
            # (_sum_completed_captures fails closed on mismatch).
            new_members.append({"id": mid, "amount_cents": mcents})
        return new_members

    def _record_listed_capture(self, job: Job, cap: dict) -> str:
        """Record a capture PayPal listed (found via read, never
        POSTed by us). Never clobbers a stored capture (round 25,
        Grok round-24 finding 1; round 28, G1/G2). Returns:
          "recorded" — this call wrote the books (first record; the
              capture was found via read, so reconciled is truthful);
          "kept"     — the books already hold this capture: the
              listed id is empty or matches the stored id (or fills
              a blank id on an amount-only record with an agreeing
              amount AND currency), so the stored amount and
              reconciled flag stand untouched;
          "conflict" — the listed id is a different non-empty id, or
              a non-empty listed id carries a different amount than
              a stored amount-only record: the books are untouched;
              the caller wedges without overwriting.
        "Already stored" (round 28, G2) means a truthy capture_id OR
        a non-None capture_amount_cents — the unauditable
        money-on-the-books state (id "" with a recorded amount) is
        stored, not empty. A present-but-empty listed id ("") never
        wipes a real stored id — cap.get("id", "") returns "" for a
        present empty key."""
        listed_id = cap.get("id") or ""
        stored_id = bool(job.capture_id)
        stored_amount = job.capture_amount_cents is not None
        job_ccy = ((job.quote or {}).get("currency") or "USD").upper()
        if stored_id or stored_amount:
            # HIGH #3 (Gemini Q4): multi-capture — a listed capture
            # that names NEW capture ids (not in job.capture_ids) is
            # a legitimate additional capture, not a conflict. The
            # summed synthetic record carries _members with each
            # member's id and amount; a bare new id is booked only
            # when its amount parses and the summed total stays within
            # the authorized hold (fund conservation — Gemini's exact
            # check). Anything unattributable stays "conflict".
            new_members = self._new_capture_members(job, cap)
            if new_members == "conflict":
                return "conflict"
            if new_members:
                authorized = (job.quote or {}).get("price_cents") or 0
                add_total = sum(m["amount_cents"] for m in new_members)
                new_total = (job.capture_amount_cents or 0) + add_total
                if authorized and new_total > authorized:
                    # Fund conservation breach — not this job's money.
                    return "conflict"
                for m in new_members:
                    self._book_capture(job, m["id"], m["amount_cents"])
                job.capture_reconciled = True
                return "recorded"
            # HIGH #3: a synthetic sum whose members are all already on
            # the books is "kept" — the joined id differs from the
            # singular capture_id, but every member is recorded.
            raw_members = cap.get("_members")
            if isinstance(raw_members, list) and raw_members:
                return "kept"
            if listed_id and stored_id and listed_id != job.capture_id:
                return "conflict"
            if (stored_id and not stored_amount
                    and listed_id == job.capture_id):
                # Round 29: an id was recorded without an amount (an
                # ambiguous capture). The listed capture carries the
                # same id with a parseable amount — fill it in so
                # residual math and receipts work. The reconciled flag
                # stands untouched.
                # Round 43 (Claude round-42): the fill must be in the
                # job's quote currency — a mismatched or missing
                # currency cannot be this job's money.
                listed_ccy = self._listed_capture_currency(cap)
                if listed_ccy != job_ccy:
                    return "conflict"
                listed_cents = self._listed_capture_amount_cents(cap)
                if listed_cents is not None:
                    # HIGH #3: fill via _book_capture so the per-capture
                    # breakdown stays consistent.
                    self._book_capture(job, listed_id, listed_cents)
            if listed_id and not stored_id:
                # Amount-only record (id ""): the listed id may fill
                # the blank — but ONLY on a positively matching amount.
                # Round 41 (ChatGPT round-40): a missing, unparseable,
                # or different listed amount cannot attribute the
                # capture — conflict, books untouched, operator review
                # decides. The old code filled the id when the listed
                # amount was unparseable, associating the stored money
                # with a capture it was never verified against.
                # Round 42 (ChatGPT round-41): the SAME money, not just
                # the same number — the listed currency must match the
                # job's quote currency. A missing, malformed, or
                # different currency conflicts: 10 EUR is not 10 USD.
                listed_cents = self._listed_capture_amount_cents(cap)
                listed_ccy = self._listed_capture_currency(cap)
                if (listed_cents is None
                        or listed_cents != job.capture_amount_cents
                        or listed_ccy != job_ccy):
                    return "conflict"
                self._record_capture_id(job, listed_id)
            # Empty listed id, matching id, or filled blank id: the
            # stored amount and reconciled flag stand.
            # Round 38 (Claude round-37 finding, Medium): the status
            # follows the same lifecycle precedence as the dedup, via
            # the shared _capture_status_rank — a stored PENDING
            # advances to a listed COMPLETED, and a stored REFUNDED
            # never drops back to PARTIALLY_REFUNDED. None, UNAUDITED
            # and unknown values rank 0, so any real listed status
            # fills a blank (this also backfills pre-round-36
            # records).
            listed_status = cap.get("status") or ""
            # Round 39 (Claude round-38): only a listed capture WITH an
            # id (matching the stored id, or filling a blank one) can
            # advance the stored status — an id-less capture cannot be
            # attributed to the stored capture, so its status must not
            # be written onto it (the same attribution rule as the
            # r36 dedup/sum logic).
            if listed_id and _capture_status_rank(listed_status) > \
                    _capture_status_rank(job.capture_status):
                job.capture_status = listed_status or "COMPLETED"
            return "kept"
        # Round 43 (Claude round-42): the first record must be in the
        # job's quote currency — a listed capture in a different
        # currency is not this job's money. A present-but-mismatched
        # currency conflicts; a missing currency is allowed through
        # (PayPal always sends one in practice; the amount is still
        # verified by the caller).
        # Round 44 (Claude round-43 Low 2, known limitation): a
        # currency conflict on empty books wedges with no force_settle
        # exit (the recorder re-conflicts). This is effectively
        # unreachable — PayPal never mixes currencies within one
        # authorization — and the wedge is the safe state. If the
        # invariant ever breaks, the operator investigates the wedge.
        listed_ccy = self._listed_capture_currency(cap)
        if listed_ccy is not None and listed_ccy != job_ccy:
            return "conflict"
        self._record_capture_id(job, listed_id)
        self._record_capture_amount(job, cap, clear_review=False)
        job.capture_reconciled = True
        # Round 31 (B2): found via read, not POSTed by us.
        job.settlement_method = "reconciled"
        # Round 36 (Claude round-35 item 1): keep the capture's real
        # PayPal status for the audit trail — a REFUNDED capture
        # settles the job but must not look identical to a paid one
        # in the books.
        job.capture_status = cap.get("status") or "COMPLETED"
        return "recorded"

    @staticmethod
    def _set_review_flag(job: Job, reason: str,
                         amount_cents: int | None = None) -> None:
        """Wedge the job for operator review (round 20): set the flag,
        the reason, and the display amount together, so they can never
        drift apart. The reason is a short operator-readable string —
        POST /admin/reconcile lists wedged jobs with their reasons.
        Caller saves."""
        job.needs_review = True
        job.needs_review_reason = reason
        job.needs_review_amount_cents = amount_cents

    @staticmethod
    def _clear_review_flag(job: Job) -> None:
        """A capture is now on the books, or the job settled with the
        money accounted for — the unauditable-partial concern the flag
        was set for is resolved (round 20). Clears the flag, the
        reason, and the display amount together."""
        job.needs_review = False
        job.needs_review_reason = None
        job.needs_review_amount_cents = None

    @staticmethod
    def _mark_unaudited(job: Job) -> None:
        """Mark an unauditable settlement (round 37, Gemini round-36
        improvement 3): the job settles with no attributable capture.
        The status marker is explicit ("UNAUDITED") rather than null,
        so API consumers can distinguish "no capture to point to"
        from "we forgot to record it". Only fills a blank status — a
        recorded real status is never overwritten, and a later-found
        real capture replaces the marker via the "recorded" branch."""
        job.capture_reconciled = True
        if job.capture_status is None:
            job.capture_status = "UNAUDITED"

    def _sum_completed_captures(self, completed: list[dict],
                                job: Job | None = None
                                ) -> tuple[str, dict | None]:
        """Sum several terminal captures into one synthetic record
        (round 28, C5). The id joins the listed ids with "+" — the
        record is traceable to exactly the captures PayPal listed —
        and the amount is the exact cent sum. Round 36 (Claude
        round-35 item 1): the synthetic carries the members' real
        status — a refunded member must not be laundered into
        COMPLETED. The label rule lives in _summed_capture_status
        (round 39). Round 43 (Claude round-42): all members must share
        one currency, and it must match the job's quote currency —
        never sum cross-currency amounts. Returns ("found", synthetic)
        or ("unattributable", None) when an amount does not parse, a
        currency is missing/mismatched (fail closed: never sum a
        wrong total). Round 46 (Claude round-45 Medium):
        "unattributable" is distinct from "ambiguous" (a PENDING
        listing) — callers wedge with an accurate reason instead of
        reporting "awaiting COMPLETED"."""
        total = 0
        seen: set[str] = set()
        members: list[dict] = []
        for c in completed:
            cid = c.get("id") or ""
            if cid:
                seen.add(cid)
            amount = self._listed_capture_amount_cents(c)
            if amount is None:
                # Unparseable amount — fail closed, never sum wrong.
                return ("unattributable", None)
            total += amount
            if cid:
                members.append({"id": cid, "amount_cents": amount})
        if not seen:
            # No ids at all — cannot attribute; unattributable, not a sum.
            return ("unattributable", None)
        # Round 36 (Claude round-35 item 5): an id-less member cannot
        # be deduped — the same capture listed on both resources would
        # be counted twice. Never sum what cannot be attributed; fail
        # closed. (A single id-less capture still returns directly via
        # _finalize, unsummed.)
        if any(not (c.get("id") or "") for c in completed):
            return ("unattributable", None)
        # Round 43 (Claude round-42): currency uniformity — every
        # member must carry the same currency, and it must match the
        # job's quote currency. A missing or mismatched currency
        # fails closed; summing 10 USD + 10 EUR as 20 of anything
        # would be wrong.
        currency = self._listed_capture_currency(completed[0])
        if currency is None:
            return ("unattributable", None)
        if any(self._listed_capture_currency(c) != currency
               for c in completed[1:]):
            return ("unattributable", None)
        if job is not None:
            job_ccy = ((job.quote or {}).get("currency") or "USD").upper()
            if currency != job_ccy:
                return ("unattributable", None)
        member_statuses = {c.get("status") for c in completed}
        synth_status = _summed_capture_status(member_statuses)
        return ("found", {
            "id": "+".join(sorted(seen)),
            "status": synth_status,
            "amount": {"value": f"{total / 100:.2f}",
                       "currency_code": currency},
            "_summed": True,
            # HIGH #3: the per-member breakdown, so callers can book
            # each capture individually instead of treating a new
            # member as a conflict against the stored id.
            "_members": members,
        })

    class CaptureEnumeration:
        """The result of enumerating captures across the authorization
        and order resources (round 54).

        Replaces the stringly-typed (outcome, capture) tuple. The six
        outcomes are class constants; consumers use the predicate
        methods instead of string matching, so a new outcome cannot
        silently slip past a consumer (Gemini round-52 suggestion 1).

        Outcomes:
          FOUND — a terminal capture was listed; the read is complete
            and auditable. Residual math is safe.
          AMBIGUOUS — a non-terminal capture is listed; money may still
            move. Never settle, never replay.
          UNATTRIBUTABLE — terminal captures were listed but cannot be
            summed. Money moved but the books cannot close it. Wedge.
          NONE — both resources were read and list no capture.
          UNREAD — the auth listed nothing usable and the order could
            not be read. Fail closed.
          ORDER_UNREAD — the auth listed captures but the order could
            not be read. Has a capture, but residual math is unsafe.
        """
        FOUND = "found"
        AMBIGUOUS = "ambiguous"
        UNATTRIBUTABLE = "unattributable"
        NONE = "none"
        UNREAD = "unread"
        ORDER_UNREAD = "order_unread"

        def __init__(self, outcome: str, capture: dict | None):
            self.outcome = outcome
            self.capture = capture

        def has_terminal_capture(self) -> bool:
            """A terminal capture was listed (found or order_unread)."""
            return self.outcome in (self.FOUND, self.ORDER_UNREAD)

        def is_fully_auditable(self) -> bool:
            """The read is complete — both resources were read (or the
            order was proven absent). Only FOUND supports residual math."""
            return self.outcome == self.FOUND

        def requires_wedge(self) -> bool:
            """This outcome must fail closed with the review flag."""
            return self.outcome in (
                self.UNATTRIBUTABLE, self.UNREAD, self.ORDER_UNREAD,
                self.AMBIGUOUS)

        def blocks_replay(self) -> bool:
            """A stored key must not be replayed on this outcome."""
            return self.outcome != self.NONE

        def is_empty(self) -> bool:
            """No capture was listed on either resource."""
            return self.outcome == self.NONE

        def __repr__(self):
            return f"CaptureEnumeration({self.outcome!r}, ...)"

        def __iter__(self):
            """Backward compatibility: allow tuple unpacking as
            (outcome, capture) during the r54 migration. New code
            should use the predicate methods instead."""
            return iter((self.outcome, self.capture))

    def _first_completed_capture_including_order(
            self, job: Job, auth: dict) -> "EscrowService.CaptureEnumeration":
        """COMPLETED captures for this authorization: the authorization
        resource first, then the order (round 20). Real PayPal often
        omits captures on the authorization resource even when money
        moved — the order's purchase_units[].payments.captures is the
        second place captures can be listed. Read-only: GETs at most,
        never a POST, never invents a capture.
        Round 21: tri-state return (outcome, capture). The order GET
        can fail while the authorization lists nothing — "no capture
        listed" and "the order was never read" are different facts,
        and settling VOIDED (or POSTing a void) on the second would
        book a clean void over money that may have moved. Outcomes:
          "found"     — at least one terminal capture was listed
                        (COMPLETED, REFUNDED, or PARTIALLY_REFUNDED —
                        round 35: refunded captures are terminal, money
                        moved and will not move again on that capture);
                        capture is the capture dict, or a synthetic
                        summed dict when more than one was listed
                        (round 28, C5).
          "ambiguous" — no terminal capture, but a capture in a
                        non-terminal state (PENDING or an unrecognized
                        status — anything not COMPLETED/REFUNDED/
                        PARTIALLY_REFUNDED/DECLINED/FAILED) was listed:
                        money may still move. Capture is that capture
                        dict. Callers must fail closed (wedge), never
                        treat this as "none" (round 28, C5). Round 46:
                        a non-terminal listing takes precedence over a
                        terminal one — if any capture is in flight, the
                        read is ambiguous even when a sibling completed.
          "unattributable" — terminal captures were listed but cannot
                        be summed into one record (unparseable amount,
                        missing/mismatched currency, id-less member).
                        Money moved but the books cannot close it.
                        Capture is None. Callers wedge with needs_review
                        and an accurate reason — never "awaiting
                        COMPLETED" (round 46, Claude round-45 Medium).
          "none"      — both resources were read and list no capture
                        outside DECLINED/FAILED; capture is None.
          "unread"    — the authorization listed nothing usable and
                        the order could not be read (transport/5xx);
                        capture is None. The caller must fail closed:
                        wedge with the review flag (or return an
                        error) — never settle VOIDED, never POST a
                        void.
          "order_unread" — the authorization listed captures but the
                        order could not be read. Capture is the
                        auth-only capture. Callers doing residual math
                        must wedge (an order-only capture may be
                        hiding); callers that only need "is there some
                        terminal capture" may treat it as found
                        (round 52, ChatGPT round-51 Medium 3)."""
        # Statuses that prove the capture will never move money.
        _DEAD = ("DECLINED", "FAILED")
        # Round 35 (v2, Claude round-34 item 1): REFUNDED and
        # PARTIALLY_REFUNDED are terminal — money moved and will not
        # move again on that capture. They count as completed for
        # accounting, never as ambiguous: a refunded capture left in
        # pending_like wedged the job permanently, because force_settle
        # refuses ambiguous reads and nothing else clears it. The
        # recorded capture keeps its REFUNDED status for the audit
        # trail; amounts are gross (PayPal reports the capture amount,
        # not net of refunds).
        _RETURNED = ("REFUNDED", "PARTIALLY_REFUNDED")

        def _is_terminal(c: dict) -> bool:
            return (c.get("status") == "COMPLETED"
                    or c.get("status") in _RETURNED)

        # Round 36 (Claude round-35 item 5), refined round 37 (Gemini
        # round-36 improvement 1) and round 38 (Claude round-37): dedupe
        # by capture id with lifecycle precedence via the shared
        # _capture_status_rank — the most advanced state seen for an id
        # is the truth. First-seen-wins wedged on auth=PENDING +
        # order=COMPLETED for the same id although the later read
        # proved completion; and a stale COMPLETED no longer masks a
        # fresher REFUNDED.
        _by_id: dict[str, dict] = {}
        _idless: list[dict] = []

        def _collect(caps) -> None:
            for c in caps or []:
                if not isinstance(c, dict):
                    continue
                cid = c.get("id") or ""
                if not cid:
                    _idless.append(c)
                    continue
                old = _by_id.get(cid)
                if old is None:
                    _by_id[cid] = c
                elif _capture_status_rank(c.get("status")) > \
                        _capture_status_rank(old.get("status")):
                    # The most advanced lifecycle state wins.
                    _by_id[cid] = c
                # Else: keep the stored entry — the state never moves
                # backward.

        def _split() -> tuple[list[dict], list[dict]]:
            seen = list(_by_id.values()) + _idless
            completed = [c for c in seen if _is_terminal(c)]
            pending_like = [c for c in seen
                            if not _is_terminal(c)
                            and c.get("status") not in _DEAD]
            # DECLINED/FAILED: proven dead — ignored.
            return completed, pending_like

        _collect(auth.get("captures"))

        def _finalize() -> tuple[str, dict | None]:
            completed, pending_like = _split()
            # Round 46 (Claude round-45 Low 2): a non-terminal listing
            # wins over a terminal one. If any capture is still in
            # flight, the read is "ambiguous" (awaiting COMPLETED) —
            # booking the completed member while a sibling is PENDING
            # would settle money that may still move.
            if pending_like:
                return self.CaptureEnumeration(self.CaptureEnumeration.AMBIGUOUS, pending_like[0])
            if completed:
                if len(completed) == 1:
                    return self.CaptureEnumeration(self.CaptureEnumeration.FOUND, completed[0])
                return self._sum_completed_captures(completed, job=job)
            return self.CaptureEnumeration(self.CaptureEnumeration.NONE, None)

        if not job.order_id:
            outcome, cap = _finalize()
            return self.CaptureEnumeration(outcome, cap)
        try:
            order = self.paypal.get_order(job.order_id)
        except AttributeError:
            # The client has no get_order (incomplete test fakes;
            # the real client always has it). Use the authorization's
            # captures when it listed any. If the auth explicitly lists
            # an empty captures array, that's definitive (NONE) — not
            # unreadable. Only fail closed to UNREAD when the auth has
            # no captures key at all.
            completed, pending_like = _split()
            if completed or pending_like:
                outcome, cap = _finalize()
                return self.CaptureEnumeration(outcome, cap)
            if "captures" in auth:
                return self.CaptureEnumeration(self.CaptureEnumeration.NONE, None)
            return self.CaptureEnumeration(self.CaptureEnumeration.UNREAD, None)
        except PayPalError as e:
            # Round 22 (Grok round-21 finding 6): only a transient
            # failure leaves the order unread.
            # Round 23 (Grok round-22 finding 4): "none" requires
            # proof of absence — a 404 (or RESOURCE_NOT_FOUND).
            # A 401/403/400 is "couldn't read," not "nothing
            # listed" — it returns "unread" so callers fail closed.
            # When the authorization already listed captures, a
            # failed order read degrades to those captures instead
            # of "unread" (round 28).
            if _is_transient_paypal_error(e) or not _is_order_absent(e):
                completed, pending_like = _split()
                if completed or pending_like:
                    # Round 52 (ChatGPT round-51 Medium 3): the auth
                    # listed captures but the order could not be read.
                    # Return a DISTINCT outcome — "found" would let
                    # the released-hold branch book a residual from an
                    # auth-only list, hiding an order-only capture B.
                    # Callers that only need "is there some terminal
                    # capture" treat this as found; residual math must
                    # wedge.
                    outcome, cap = _finalize()
                    if outcome == "found":
                        return self.CaptureEnumeration(self.CaptureEnumeration.ORDER_UNREAD, cap)
                    return self.CaptureEnumeration(outcome, cap)
                return self.CaptureEnumeration(self.CaptureEnumeration.UNREAD, None)
            # Absent (404): the order has no captures.
            outcome, cap = _finalize()
            return self.CaptureEnumeration(outcome, cap)
        for pu in order.get("purchase_units") or []:
            if not isinstance(pu, dict):
                continue
            payments = pu.get("payments") or {}
            _collect(payments.get("captures"))
        outcome, cap = _finalize()
        return self.CaptureEnumeration(outcome, cap)

    def _wedge_on_unattributable(self, job: Job, outcome: str,
                                 context: str) -> bool:
        """Shared guard (round 47, Claude round-46 HIGH): call
        immediately after every _first_completed_capture_including_order
        read. On "unattributable" (terminal captures listed but
        unsummable — money moved but the books cannot close it), sets
        needs_review with an accurate reason, saves, and returns True.
        The caller must then wedge/refuse — never treat it as "none"
        (booking VOIDED or POSTing a void over moved money). Returns
        False for all other outcomes."""
        if outcome != "unattributable":
            return False
        reason = (f"{context}: the listed captures cannot be attributed "
                  "(amount, currency, or id problem — operator review "
                  "needed)")
        self._set_review_flag(job, reason)
        self._save(job)
        return True

    def _resolve_post_void(self, job: Job, post_auth: dict | None, *,
                           partial_origin: bool, verb: str,
                           final_capture_released: bool = False):
        """Shared post-void confirmation decision (round 24, Grok
        round-23 structural): the one place "record and wedge" vs
        "settle with residual" is decided, so void_after_deadline's
        post-read, _confirm_residual_void, and _finish_partial_capture
        cannot drift again. post_auth is the confirmation read, or
        None when that read failed (wedges — a failed confirmation
        never settles).
        final_capture_released (Claude v8.6): when final_capture=True was
        sent on the partial capture, PayPal releases the residual and the
        authorization reads CAPTURED — not VOIDED. Treat CAPTURED as
        released in this path only; the void path still wedges on it.
        Returns:
          ("captured", cap) — a COMPLETED capture is listed and the
              hold is released: caller settles via
              _settle_captured_with_residual.
          ("settle_books",) — hold released, no capture listed, but
              the job has a recorded capture: caller settles CAPTURED
              from the books.
          ("voided",) — hold released, nothing moved, not
              partial_origin: caller books VOIDED.
        Raises EscrowError (after setting the review flag and saving)
        for every other case: failed confirmation read, unread order,
        listed capture on an open hold (the capture is recorded
        first — round 24, Grok round-23 finding 1), released hold
        with unaccounted money (partial_origin), open hold with
        nothing listed. verb prefixes the raised message
        ("cannot confirm void", "cannot force-settle", "cannot
        finish partial capture")."""
        if post_auth is None:
            reason = ("post-void confirmation read failed — state "
                      "unknown (operator review needed)")
            self._set_review_flag(job, reason)
            self._save(job)
            raise EscrowError(f"{verb}: " + reason)
        enum = self._first_completed_capture_including_order(
            job, post_auth)
        # Round 53 (Gemini round-52 High 2): order_unread wedges here
        # too — an auth-only capture cannot support post-void settlement.
        if enum.outcome in (enum.UNREAD, enum.ORDER_UNREAD):
            reason = ("post-void read could not confirm no capture "
                      "moved — the order could not be read (operator "
                      "review needed)")
            self._set_review_flag(job, reason)
            self._save(job)
            raise EscrowError(f"{verb}: " + reason)
        if enum.outcome == enum.AMBIGUOUS:
            # Round 28 (C5): a non-terminal capture is listed — money
            # may still move. Wedge, never settle on it.
            reason = ("post-void read lists a non-terminal capture — "
                      "money may still move (operator review needed)")
            self._set_review_flag(job, reason)
            self._save(job)
            raise EscrowError(f"{verb}: " + reason)
        # Round 48 (Claude round-47 HIGH): the shared guard —
        # "unattributable" wedges here, never returns ("voided",)
        # over moved money.
        if self._wedge_on_unattributable(
                job, enum.outcome, f"{verb} post-void read"):
            raise EscrowError(
                f"{verb}: " + (job.needs_review_reason or
                               "captures unattributable (operator review needed)"))
        post_status = post_auth.get("status")
        released = _is_hold_released(post_status)
        # Claude v8.6: final_capture=True releases the residual and the
        # auth reads CAPTURED — treat as released in this path only.
        if final_capture_released and post_status == "CAPTURED":
            released = True
        if enum.capture is not None:
            if released:
                return ("captured", enum.capture)
            # The capture is real but the hold is still open — record
            # it BEFORE wedging (round 24, Grok round-23 finding 1):
            # a later force_settle must see the capture on the job,
            # never book a clean VOIDED over observed money. Round
            # 25 (Grok round-24 finding 1): never clobber a stored
            # capture — an empty or matching listed id keeps the
            # books; a different listed id wedges without touching
            # them.
            record_outcome = self._record_listed_capture(job, enum.capture)
            if record_outcome == "conflict":
                reason = (f"post-void read lists capture "
                          f"{enum.capture.get('id')} but the recorded capture "
                          f"is {job.capture_id} — money moved outside "
                          "the recorded flow (operator review needed)")
            else:
                reason = (f"post-void read lists a capture but the "
                          f"authorization is {post_status} — the hold "
                          "may still be open (operator review needed)")
            self._set_review_flag(job, reason)
            self._save(job)
            raise EscrowError(f"{verb}: " + reason)
        if released:
            if self._has_recorded_capture(job):
                return ("settle_books",)
            if partial_origin:
                # PARTIALLY_CAPTURED means money moved by definition,
                # but no capture is listed or recorded — booking
                # VOIDED would lie about moved money, and CAPTURED
                # has nothing to record. Wedge.
                reason = ("hold released but no capture is listed or "
                          "recorded — money moved that cannot be "
                          "accounted for (operator review needed)")
                self._set_review_flag(job, reason)
                self._save(job)
                raise EscrowError(f"{verb}: " + reason)
            return ("voided",)
        reason = (f"post-void read shows {post_status} with no capture "
                  "listed — the hold may still be open (operator "
                  "review needed)")
        self._set_review_flag(job, reason)
        self._save(job)
        raise EscrowError(f"{verb}: " + reason)

    def _handle_preflight_order_capture(self, job: Job, cap: dict,
                                        operation: str) -> dict:
        """Round 24 (Grok round-23 structural): the capture pre-flight
        found a COMPLETED capture on the order that the CREATED
        authorization omits. Record it and settle or wedge — never
        mint a fresh capture key for money that already moved.
        Returns an ALREADY_SETTLED-style dict when the listed capture
        covers the hold; raises EscrowError (wedging) when the listed
        capture is partial — the authorization says CREATED but money
        moved, so the state is inconsistent and needs a human.
        operation names the caller ("capture" / "capture_partial")
        for messages.
        Round 25 (Grok round-24 finding 3): a full-cover preflight
        capture settles the way capture() does — voided_amount_cents
        stays None (no residual was voided; the receipt contract
        reserves a set voided_amount_cents for partial release, and
        the residual helper would book 0 or a negative), an
        over-hold amount is logged like capture() does, and the
        listed capture itself is the audit record — not the stale
        CREATED authorize envelope.
        Round 28 (G1): the listed capture goes through
        _record_listed_capture — a stored capture is never
        overwritten by the raw write. On "conflict" the job wedges;
        on "kept" the stored id, amount, and reconciled flag stand
        and the settlement below reads them from the books."""
        record_outcome = self._record_listed_capture(job, cap)
        if record_outcome == "conflict":
            reason = (f"order lists capture {cap.get('id')} conflicting "
                      f"with the recorded capture {job.capture_id} "
                      f"(operator review needed)")
            self._set_review_flag(job, reason)
            self._save(job)
            raise EscrowError(f"cannot {operation}: " + reason)
        hold_cents = job.quote["price_cents"]
        if (job.capture_amount_cents is not None
                and job.capture_amount_cents >= hold_cents):
            if job.capture_amount_cents > hold_cents:
                logger.warning(
                    "preflight for job %s found order capture %s for "
                    "%s cents, more than the %s-cent hold — recorded "
                    "as returned", job.job_id, job.capture_id,
                    job.capture_amount_cents, hold_cents)
                # Claude v5 review: unify — an impossible amount wedges
                # with needs_review on BOTH paths.
                self._set_review_flag(
                    job,
                    f"preflight found order capture {job.capture_id} for "
                    f"{job.capture_amount_cents} cents, more than the "
                    f"{hold_cents}-cent hold (operator review needed)")
            self._record_paypal_response(
                job, operation=operation, status=cap.get("status") or "COMPLETED",
                id_=job.capture_id, body=cap)
            job.status = "CAPTURED"
            job.settled_at = int(time.time())
            self._save(job)
            return {"status": "ALREADY_SETTLED", "to_status": "CAPTURED",
                    "capture_id": job.capture_id,
                    "reconciled": job.capture_reconciled,
                    "error": ""}
        reason = (f"order lists a completed capture but the "
                  f"authorization is CREATED — state inconsistent "
                  f"(operator review needed)")
        self._set_review_flag(job, reason)
        self._save(job)
        raise EscrowError(f"cannot {operation}: " + reason)

    @staticmethod
    def _compute_voided_residual(job) -> int | None:
        """The uncaptured residual of a partial capture (round 27,
        Gemini round-26 finding 3). voided_amount_cents is reserved
        strictly for the uncaptured residual of a partial capture:
        hold minus captured — but ONLY when the capture is genuinely
        partial. A full-cover or over-hold capture books None, never
        0 or a negative, so the receipt never claims a partial
        release (captured + voided == hold) that did not happen.
        Every CAPTURED settlement path uses this, never the raw
        subtraction."""
        captured = job.capture_amount_cents
        if captured is None:
            return None
        hold = job.quote["price_cents"]
        if captured >= hold:
            return None
        return hold - captured

    def _flag_out_of_band_captures(self, job: Job, auth: dict) -> None:
        """Claude v8.7: when final_capture released the residual, check the
        authorization for captures outside the recorded flow. If PayPal
        lists captures beyond the job's recorded capture (different id,
        or total exceeding the hold), set the review flag — the money is
        accounted for, but it moved outside the expected path and a
        reviewer should see it. Does not wedge; the settlement proceeds.
        Called AFTER settlement (Claude v8.8) so the flag survives."""
        from decimal import Decimal
        import logging
        log = logging.getLogger(__name__)
        try:
            listed = auth.get("captures", []) or []
            # Sum all listed captures using Decimal (never float).
            total = Decimal("0")
            ids = []
            for cap in listed:
                amt = cap.get("amount", {}) if isinstance(cap, dict) else {}
                val = amt.get("value") if isinstance(amt, dict) else None
                if val is not None:
                    total += Decimal(str(val))
                cid = cap.get("id") if isinstance(cap, dict) else None
                if cid:
                    ids.append(cid)
            hold = Decimal(str(job.quote.get("price_cents", 0))) / 100
            # Flag if: total exceeds the hold, or any listed id doesn't
            # match the recorded capture.
            recorded_id = job.capture_id
            unexpected = [cid for cid in ids if cid != recorded_id]
            if total > hold or unexpected:
                reason = (f"out-of-band capture(s) on final_capture release: "
                          f"listed {ids} totaling ${total} vs hold ${hold} "
                          f"(recorded capture {recorded_id}) — money moved "
                          f"outside the recorded flow (operator review needed)")
                self._set_review_flag(job, reason)
        except Exception as e:
            # Log, don't swallow — a bug here must be visible.
            log.warning("out-of-band capture check failed: %s", e)

    def _settle_captured_with_residual(self, job: Job, cap: dict) -> str:
        """Record a COMPLETED capture found via read and settle the job
        CAPTURED with the voided residual (round 21). The single
        implementation behind the VOIDED-arm settlement, the post-void
        settlement, and the force-settle settlement — one helper so the
        copies stop drifting (Grok round-20 recommendation).
        Round 28 (G1): the non-clobber rule lives here. The listed
        capture goes through _record_listed_capture first; on
        "conflict" the books are untouched and "conflict" is returned
        — the caller wedges instead of settling. On "ok" the books
        hold the capture (recorded just now, or kept from before):
        the review flag clears — the money is accounted for — and the
        job settles CAPTURED from the books with the voided residual.
        A kept capture's id, amount, and reconciled flag are never
        overwritten. Caller saves (and records the PayPal response
        where the path needs it)."""
        record_outcome = self._record_listed_capture(job, cap)
        if record_outcome == "conflict":
            return "conflict"
        self._clear_review_flag(job)
        job.voided_amount_cents = self._compute_voided_residual(job)
        job.status = "CAPTURED"
        job.settled_at = int(time.time())
        return "ok"

    def _record_captured_authorization(
            self, job: Job, auth: dict,
            _precomputed: tuple[str, dict | None] | None = None
    ) -> tuple[str, str]:
        """Record an authorization PayPal reports as CAPTURED on the job.
        Returns (result, read_outcome):
          result: "ok", "conflict", "ambiguous", or "unattributable".
          read_outcome: the outcome from
            _first_completed_capture_including_order — "found",
            "ambiguous", "unattributable", "unread", or "none".
        _precomputed (round 45): an optional (outcome, cap) tuple from
        a caller that already enumerated — avoids a second get_order
        that could disagree with the first read.
        Round 28 (G1): the listed capture goes through
        _record_listed_capture — a stored capture is never overwritten
        by a raw write. On "conflict" the books are untouched and
        "conflict" is returned; the caller wedges instead of settling.
        When the authorization lists no COMPLETED capture, the capture
        stays unauditable (id "" and reconciled flag) ONLY when the
        books are empty — a stored capture is left alone. This is
        capture bookkeeping only: the reconcile read itself is recorded
        by the reconcile tail (_record_reconcile_read), and in the
        capture path capture() records the capture result afterwards.
        Caller sets status/settled_at (and the voided residual) on
        "ok". Round 31 (C2): the caller needs the read outcome to
        decide what the CAPTURED status proves — only a positive
        "found" listing lets it book the voided residual and clear a
        wedge."""
        if _precomputed is not None:
            outcome, cap = _precomputed
        else:
            outcome, cap = self._first_completed_capture_including_order(
                job, auth)
        if outcome == "ambiguous":
            # Round 45 (Claude round-44 Medium): a non-terminal
            # (PENDING) capture is listed. PENDING never settles — a
            # PENDING capture can later be DENIED, and booking terminal
            # CAPTURED on it would leave the books saying "paid" with
            # no correction path (the round-32 rule: an observed PENDING
            # never settles; reconcile must not contradict it). Write
            # NOTHING to the books and keep the intent. The caller
            # raises/wedges with "awaiting COMPLETED".
            return ("ambiguous", outcome)
        if outcome == "unattributable":
            # Round 46 (Claude round-45 Medium): terminal captures
            # were listed but cannot be summed (bad amount, currency,
            # or id-less member). Money moved but the books cannot
            # close it. Wedge with an accurate reason — NOT "awaiting
            # COMPLETED" (nothing is in flight; the read is complete
            # but unusable).
            reason = ("authorization is CAPTURED but the listed "
                      "captures cannot be attributed (amount, currency, "
                      "or id problem — operator review needed)")
            self._set_review_flag(job, reason)
            return ("unattributable", outcome)
        if outcome == "unread":
            # The CAPTURED status itself proves money moved — the
            # capture stays unauditable rather than wedging on a
            # definitive status (round 26). But a stored capture is
            # never clobbered.
            if not self._has_recorded_capture(job):
                self._mark_unaudited(job)
            self._clear_pending_capture(job)
            return ("ok", outcome)
        if cap is not None:
            record_outcome = self._record_listed_capture(job, cap)
            if record_outcome == "conflict":
                reason = (f"authorization is CAPTURED and lists capture "
                          f"{cap.get('id')} conflicting with the recorded "
                          f"capture {job.capture_id} (operator review "
                          "needed)")
                self._set_review_flag(job, reason)
                return ("conflict", outcome)
        elif not self._has_recorded_capture(job):
            # Nothing listed and nothing stored: unauditable.
            self._mark_unaudited(job)
        # Else: the books already hold a capture ("kept") — leave
        # id, amount, and the reconciled flag exactly as stored.
        # Round 11: the capture is now recorded, so a pending
        # intent for it is spent.
        self._clear_pending_capture(job)
        return ("ok", outcome)

    def _record_reconcile_read(self, job: Job, auth: dict) -> None:
        """Record a successful reconcile read and persist it — the one
        place the "always persist a successful read" rule lives (round
        5). Every reconcile branch (CREATED, CAPTURED, VOIDED) ends
        here, so a successful read is never left in memory only and
        survives a restart."""
        self._record_paypal_response(job, operation="reconcile",
                                     status=auth.get("status"),
                                     id_=auth.get("id"), body=auth)
        self._save(job)

    def _verify_then_act(self, job: Job, error: PayPalError,
                         error_class: ErrorClass) -> dict:
        """Phase 5: canonical ambiguous-outcome handler for the capture path.

        Contract: the PSP call raised `error` (classified as
        `error_class`) after the request was attempted. The caller must
        NOT re-POST blindly. This helper implements verify-then-act:

        1. Replay the stored idempotency key (PayPal replays the
           original response if it executed; executes now if it did not).
        2. If the replay also fails, read the authorization's actual
           state and reconcile from it.
        3. Never invent a result — a capture is recorded only from an
           observed POST response or a PayPal read that proves it.

        For DEFINITIVE errors the replay is still safe (PayPal replays
        the declined/error body for the same key); the reconcile then
        records the finality. For RETRYABLE errors the replay is the
        retry. For AMBIGUOUS errors the replay-then-read is the verify.

        Currently delegates to the battle-tested `_capture_with_reconcile`
        (rounds 11-53 of hardening). The taxonomy classification is
        audit-relevant and future branches (e.g. giving up after N
        ambiguous retries) will key off `error_class` here — the single
        choke point, not scattered call sites.
        """
        self._audit("capture", job,
                    idempotency_key=job.pending_capture_request_id,
                    outcome="verify_then_act",
                    reason=f"ambiguous PSP outcome ({type(error).__name__}, "
                           f"status {error.status}) classified "
                           f"{error_class.value}; verifying with original "
                           f"key before acting")
        return self._capture_with_reconcile(job)

    def _capture_with_reconcile(self, job: Job) -> dict:
        """The capture call failed — but it may have executed server-side
        before the response was lost. First retry with the same
        idempotency key (PayPal replays the original capture). If the
        retry also fails, ask PayPal for the authorization's actual
        state: if it is CAPTURED the money moved, so record the job as
        captured — with the real capture id and amount from the
        authorization's linked captures when PayPal lists them (only
        when it lists none does the id stay "" with the reconciled flag)
        — and return the capture's REAL status (round 40: never a
        manufactured COMPLETED) so a receipt can be produced.
        Round 12: the retried key is always the job's STORED pending
        key, never a fresh one. A PARTIALLY_CAPTURED read only proves
        some capture exists while listing none — replaying a key
        PayPal has never seen would execute another slice on top of
        the hidden one. With no stored key there is nothing safe to
        replay, so the POST retries are skipped and the read alone
        decides."""
        key = job.pending_capture_request_id
        if key is not None:
            # v8.2 (item 6): the replay is audit-relevant — it is the
            # exact idempotency key PayPal already saw.
            self._audit("capture", job, idempotency_key=key,
                        outcome="retried",
                        reason="capture POST lost its response; replaying "
                               "the stored idempotency key so PayPal "
                               "replays the original instead of capturing "
                               "twice")
            try:
                return self.paypal.capture_authorization(
                    job.authorization_id, request_id=key)
            except PayPalError:
                pass
        try:
            auth = self.paypal.get_authorization(job.authorization_id)
        except PayPalError as e:
            raise EscrowError(
                f"PayPal capture failed and state could not be reconciled: {e}")
        if auth.get("status") == "CAPTURED":
            # Round 29: _record_captured_authorization returns
            # "conflict" when the listed capture conflicts with the
            # stored books (it sets needs_review). That must not be
            # swallowed — a conflicted capture is not a clean
            # COMPLETED. Save the wedge and raise; capture() must not
            # clear the review flag and settle over it.
            # Round 31 (C2): the helper now returns
            # (result, read_outcome) — unpack the tuple.
            outcome, _read_outcome = self._record_captured_authorization(
                job, auth)
            if outcome == "conflict":
                self._save(job)
                raise EscrowError(
                    "PayPal capture failed: "
                    f"{job.needs_review_reason or 'capture conflict'}")
            if outcome == "ambiguous":
                # Round 45 (Claude round-44 Medium): PENDING never
                # settles. The intent is kept (the recorder wrote
                # nothing); the job stays DELIVERED for a retry once
                # the capture reads COMPLETED.
                self._save(job)
                raise EscrowError(
                    "PayPal capture failed: authorization is CAPTURED "
                    "but the listed capture is PENDING — awaiting "
                    "COMPLETED; retry or reconcile")
            if outcome == "unattributable":
                # Round 46: the recorder wedged with an accurate
                # reason. Save and raise it.
                self._save(job)
                raise EscrowError(
                    "PayPal capture failed: "
                    f"{job.needs_review_reason or 'captures unattributable'}")
            self._save(job)
            # Round 40 (ChatGPT round-39): return the REAL status
            # _record_captured_authorization just recorded — never
            # manufacture COMPLETED over it. The caller (capture())
            # preserves it instead of stamping COMPLETED.
            return {"id": job.capture_id, "status": job.capture_status,
                    "reconciled": True}
        if auth.get("status") == "PARTIALLY_CAPTURED" and key is not None:
            # Round 11: part of the hold was captured (by a partial
            # that executed out of band, or by this capture
            # degenerating server-side) and PayPal lists no captures
            # for it. The full capture may never have executed at
            # all — replay the stored key: PayPal replays the original
            # result if it executed, or executes it now if it did
            # not. A replayed result is observed, not reconciled.
            # Round 52 (ChatGPT round-51 High): enumerate BEFORE the
            # replay. The comment above says "PayPal lists no captures"
            # but the code never checked — if a capture IS listed,
            # replaying a never-seen key would execute another slice
            # on top of it. The partial path already does this check.
            enum = (
                self._first_completed_capture_including_order(job, auth))
            if self._wedge_on_unattributable(
                    job, enum.outcome,
                    "_capture_with_reconcile PARTIALLY_CAPTURED"):
                self._save(job)
                raise EscrowError(
                    job.needs_review_reason or
                    "captures unattributable (operator review needed)")
            if enum.has_terminal_capture() and enum.capture is not None:
                # A capture is listed — record it, do NOT replay.
                # Round 53 (Gemini round-52 High 3): order_unread also
                # blocks the replay — an auth-only capture with an
                # unread order cannot be replayed over.
                rec_outcome, _ = self._record_captured_authorization(
                    job, auth)
                self._save(job)
                if rec_outcome == "conflict":
                    raise EscrowError(
                        "PayPal capture failed: "
                        f"{job.needs_review_reason or 'capture conflict'}")
                return {"id": job.capture_id,
                        "status": job.capture_status,
                        "reconciled": True}
            if enum.requires_wedge():
                # A non-terminal capture is listed, or the read failed
                # — do not POST blind.
                self._save(job)
                raise EscrowError(
                    "PayPal capture failed: authorization is "
                    "PARTIALLY_CAPTURED but the capture state is "
                    f"{enum.outcome} — refusing to replay blind "
                    "(operator review needed)")
            # enum.outcome == "none": safe to replay the stored key.
            try:
                return self.paypal.capture_authorization(
                    job.authorization_id, request_id=key)
            except PayPalError:
                pass
        raise EscrowError(
            f"PayPal capture failed; authorization is {auth.get('status')}")

    # -- partial release -----------------------------------------------------
    @_audited("capture_partial")
    @_per_job_locked
    def capture_partial(self, job: Job, *, payer: str, delivery_text: str,
                        amount_cents: int) -> dict:
        """Release only part of the held money and void the rest in one
        move (round 8). The payer verified only part of the work — the
        agent is paid for that part, the unverified remainder goes back
        to the payer instead of sitting in a hold. Same guards as the
        full capture (DELIVERED, payer only, delivery verified before
        any PayPal call), plus the amount rules: an integer number of
        cents, greater than zero, no more than the hold. An amount
        equal to the full hold degenerates to a normal capture — the
        capture path runs and no residual void is attempted, so there
        is nothing left to release.

        One settlement receipt covers both legs: the job settles
        CAPTURED with capture_amount_cents = what PayPal actually
        captured and voided_amount_cents = hold - captured, so the
        split is auditable from the receipt alone (outcome "released",
        voided_amount_cents set). The last PayPal response recorded
        is the void — the final settling action; the capture id and
        amount persist on the job, so nothing about the first leg is
        lost. There is no second capture-recording path: the capture
        leg goes through the same client call and the same
        _record_capture_amount as the full capture, and the residual
        void goes through the same void call as void_after_deadline.

        Failure modes, and what the job looks like after each:
          * capture fails — same recovery as the full capture
            (idempotent retry, then reconcile from the authorization:
            a linked COMPLETED capture with the requested amount means
            the partial capture applied). The job stays DELIVERED with
            nothing recorded, exactly as a failed full capture does.
          * capture succeeds, residual void fails — the job must NOT
            report settled. The partial capture IS recorded on the job
            (capture id + amount + the capture response) and the job
            stays DELIVERED. Two ways to finish it: the payer retries
            capture_partial with the SAME amount — the recorded capture
            is recognised, the capture call is skipped, and only the
            void runs (a different amount is refused, because that
            would be a second capture, not a retry); or the admin
            reconcile finishes it, when the void did apply but the
            service never learned it — reconcile_job records a
            partially-captured job whose authorization PayPal reports
            VOIDED as CAPTURED with the voided residual, not as
            VOIDED. Until one of those runs, the admin summary keeps
            reporting the job still pending (authorization CREATED) —
            it is never lost, and it can never double-capture: the
            recorded capture makes a second capture_partial with a
            different amount, and a full capture(), both refuse.
        Returns the void result ({} on success — PayPal's void returns
        204), the same shape void_after_deadline returns."""
        if USE_DISPATCHER:
            return self._capture_partial_via_dispatcher(
                job, payer=payer, delivery_text=delivery_text,
                amount_cents=amount_cents)
        # Round 12: re-read the job inside the lock (see authorize):
        # the passed Job may be a stale copy, and a future store
        # must not hand a locked method a Job another thread already
        # settled. No-op against the current stores (they return the
        # same object).
        fresh = self.store.get(job.job_id)
        if fresh is None:
            raise EscrowError(f"job not found: {job.job_id}")
        job = fresh
        if job.status != "DELIVERED":
            raise EscrowError(f"cannot capture in state {job.status} (needs DELIVERED)")
        if payer != job.payer:
            raise EscrowError("only the payer can release this job")
        if not delivery_text:
            raise EscrowError("delivery text required for verification")
        if not self.delivery_verifier(job, delivery_text):
            raise EscrowError("delivery verification failed")
        if isinstance(amount_cents, bool) or not isinstance(amount_cents, int):
            raise EscrowError("partial capture amount must be an integer number of cents")
        if amount_cents <= 0:
            raise EscrowError("partial capture amount must be greater than zero")
        hold_cents = job.quote["price_cents"]
        if amount_cents > hold_cents:
            raise EscrowError("partial capture amount exceeds the authorized hold")
        # Round 26 (Gemini round-25 suggestion): the needs_review flag
        # guards capture_partial too, matching void_after_deadline —
        # a wedged job must not accept new capture attempts (not even
        # the retry path below). The payer-only force-settle is the
        # exit.
        if job.needs_review:
            amount_note = (
                f" (attempted ${job.needs_review_amount_cents/100:.2f})"
                if job.needs_review_amount_cents is not None else "")
            raise EscrowError(
                "cannot capture partial: job needs operator review"
                + amount_note)
        if self._has_recorded_capture(job):
            # A previous attempt recorded the partial capture but the
            # residual void did not finish: this is the retry. Round
            # 29: the guard keys on the G2 "already stored" definition
            # — a truthy capture_id OR a recorded amount — so an
            # unauditable capture (id "" with an amount on the books)
            # also routes here instead of minting a second capture.
            # The amount must match the recorded capture — a different
            # amount would be a second capture, not a retry — and the
            # capture call is skipped, only the void runs. (Checked
            # before the degenerate branch below, so a retry with the
            # full hold still gets this exact-amount error instead of
            # falling through to the full capture's refusal.)
            if job.capture_amount_cents != amount_cents:
                raise EscrowError(
                    "partial capture already recorded for "
                    f"{job.capture_amount_cents} cents — retry with that "
                    "amount to finish the residual void")
            return self._finish_partial_capture(job)
        if job.pending_capture_request_id is not None:
            # Round 11: a capture is already in flight. A pending
            # FULL capture (amount None) blocks any partial — the
            # whole hold may already have moved. A pending PARTIAL
            # capture blocks a retry with a DIFFERENT amount (the
            # pending capture may already have executed, and a
            # different amount would be a second capture on top of
            # it); a retry with the SAME amount replays it under its
            # own key — that is the recovery path, not an error.
            if job.pending_capture_amount_cents is None:
                raise EscrowError(
                    "full capture already pending — finish it with "
                    "capture, not a partial capture")
            if job.pending_capture_amount_cents != amount_cents:
                raise EscrowError(
                    "partial capture already pending for "
                    f"{job.pending_capture_amount_cents} cents — a "
                    "different amount would over-capture; retry the "
                    "same amount or reconcile")
            # The retry reuses the stored key, never a fresh one.
            # Round 50 (Claude round-49 Medium 2): the shared pre-read
            # — a blind replay would return PENDING forever.
            request_id = job.pending_capture_request_id
            settled = self._retry_pre_read(job, "capture partial")
            if settled is not None:
                return settled
        if amount_cents == hold_cents:
            # Degenerates to a normal capture — no residual to void.
            # (Checked after the intent guard above, so a pending
            # partial is refused with the partial error, not routed
            # here; a pending FULL capture has amount None, so this
            # branch is only reached when nothing is pending.)
            return self.capture(job, payer=payer, delivery_text=delivery_text)
        # Round 11: record the intent BEFORE the PayPal call, so a
        # lost response still leaves a replayable record of what was
        # asked. (Skipped when an intent is already pending — the
        # retry reuses it, request id included.)
        # Dynamic attempt counter (Grok fix order): each attempt mints
        # a fresh idempotency key so a declined key is never replayed.
        # Defined once here — the completion Evidence below needs it on
        # both the fresh-intent and the retry path.
        attempt_num = max(1, job.capture_attempt + 1)
        if job.pending_capture_request_id is None:
            # Round 13: pre-flight read — never POST blind. A fresh
            # partial with no stored intent on a hold PayPal already
            # reports as PARTIALLY_CAPTURED / CAPTURED / VOIDED /
            # EXPIRED must not invent a key and capture on top of
            # moved money (the disclosed residual hole: a blind
            # partial on a partially-captured hold over-captures).
            # Reconcile from the read instead, then refuse — the
            # payer's path forward is the recorded state.
            try:
                pre = self.paypal.get_authorization(job.authorization_id)
            except PayPalError as e:
                raise EscrowError(
                    f"PayPal capture pre-flight read failed: {e}") from e
            pre_status = pre.get("status")
            if pre_status != "CREATED":
                # Round 15 (Grok round-14 finding 4): reconcile from
                # the pre-flight read under the lock already held —
                # reconcile_job would fetch the authorization a
                # second time, and a transport failure on that read
                # discarded the pre-flight's own answer. The job was
                # re-read at the top of this method and is open
                # (AUTHORIZED or DELIVERED), which is all
                # reconcile_job's open-check would establish.
                rec = self._reconcile_with_auth(job, pre, job.status)
                if rec["to_status"] in ("CAPTURED", "VOIDED"):
                    # Round 14 (ChatGPT round-13 improvement 1): the
                    # pre-flight read found the money already moved
                    # (or the hold released) and reconcile settled it
                    # — return the settlement instead of a 409 whose
                    # only clue is "reconciled to CAPTURED". The
                    # endpoint returns the settled job and its receipt
                    # from this same job object.
                    return {"status": "ALREADY_SETTLED",
                            "to_status": rec["to_status"],
                            "capture_id": rec["capture_id"],
                            "reconciled": True, "error": ""}
                # Round 45 (Claude round-44 Medium): propagate the
                # Round 46 (Claude round-45 Low 5): use the structured
                # "awaiting" flag, not substring-matching (brittle).
                if rec.get("awaiting"):
                    raise EscrowError(rec["error"])
                raise EscrowError(
                    f"cannot capture partial: authorization is {pre_status} "
                    f"(reconciled to {rec['to_status']})")
            # Round 24 (Grok round-23 structural): the authorization
            # says CREATED, but the order may list a COMPLETED
            # capture the authorization omits — minting a key now
            # would capture on top of moved money. Same tri-state
            # check as the void pre-read: fail closed on unread,
            # record-and-handle on found.
            pre = self._first_completed_capture_including_order(
                job, pre)
            if pre.outcome == pre.UNREAD:
                raise EscrowError(
                    "PayPal capture pre-flight: authorization is "
                    "CREATED but the order could not be read — "
                    "refusing to capture blind")
            if pre.outcome == pre.AMBIGUOUS:
                # Round 28 (C5): a non-terminal capture is listed —
                # capturing on top of it could double-move. Refuse.
                raise EscrowError(
                    "PayPal capture pre-flight: authorization is "
                    "CREATED but a non-terminal capture is listed — "
                    "refusing to capture blind (operator review "
                    "needed)")
            if pre.outcome == pre.UNATTRIBUTABLE:
                # Round 49 (Claude round-48 MEDIUM 5): route through the
                # shared guard — sets needs_review so the operator sees
                # it and force_settle has an exit.
                self._wedge_on_unattributable(
                    job, pre.outcome,
                    "PayPal capture pre-flight")
                raise EscrowError(
                    job.needs_review_reason or
                    "captures unattributable (operator review needed)")
            if pre.capture is not None:
                return self._handle_preflight_order_capture(
                    job, pre.capture, "capture_partial")
            request_id = self._capture_request_id(
                job, f"capture-partial-{amount_cents}")
            self._set_pending_capture(job, amount_cents, request_id)
        try:
            result = self.paypal.capture_authorization(
                job.authorization_id, amount_cents=amount_cents,
                request_id=request_id,
                # Claude v8.4: tell PayPal this is the final capture so any
                # remaining held amount is released automatically. Without
                # it the residual stays held on real PayPal (the mock
                # simulates the void, the sandbox does not).
                final_capture=True)
        except PayPalError:
            result = self._capture_partial_with_reconcile(
                job, amount_cents)
        # Round 40 (ChatGPT round-39): the COMPLETED gate is for
        # observed (direct POST) results only — same rule as capture().
        # A reconciled result carries the listed capture's real status;
        # the money moved, so it settles.
        if not result.get("reconciled"):
            if result.get("status") != "COMPLETED":
                if result.get("status") in ("DECLINED", "FAILED"):
                    # Definitive: retire the declined key (round 13) —
                    # see capture(). PENDING is left alone.
                    self._abandon_pending_capture(job, request_id,
                                                  op="capture_partial")
                raise EscrowError(
                    f"partial capture not completed (status {result.get('status')})")
        # Round 11: the returned amount is recorded as-is — PayPal's
        # capture amount is authoritative, and the job settles on
        # the actual split (the residual below is computed from it).
        # The round-10 refusal of a COMPLETED capture for a
        # different amount is gone: refusing it left money moved
        # with nothing recorded (Claude's round-10 finding 1 in
        # reverse). The reconciled path returns the linked capture's
        # amount the same way.
        self._record_capture_id(job, result.get("id", ""))
        self._record_capture_amount(job, result)
        # Round 40 (ChatGPT round-39): record the result's actual
        # status. Observed results passed the COMPLETED gate above;
        # reconciled results carry the listed capture's real status —
        # never manufacture COMPLETED over it.
        job.capture_status = result.get("status")
        # The capture is recorded: the intent is spent.
        self._clear_pending_capture(job)
        # The reconciled flag comes from the capture result itself:
        # a normal capture leaves it False (absent), a reconciled one
        # sets it — the same flag the full capture path records, so
        # the receipt says whether the capture was observed or
        # recovered from PayPal's state.
        job.capture_reconciled = bool(result.get("reconciled"))
        # Round 31 (B2): how the settlement was established.
        job.settlement_method = (
            "reconciled" if result.get("reconciled") else "observed")
        self._record_paypal_response(job, operation="capture-partial",
                                     status=result.get("status"),
                                     id_=result.get("id"), body=result)
        # The actual captured amount decides the residual — if PayPal
        # captured the whole hold despite a partial request, there is
        # nothing left to void and this settles as a full capture.
        # Round 44 (Claude round-43): an unaudited recovery (auth
        # proved CAPTURED, no capture listed) settles CAPTURED
        # directly — the amount is unknowable, so there is no residual
        # to void. Do NOT loosen the `captured is not None` test below:
        # that would book CAPTURED on an observed result with no amount
        # and skip a legitimate void.
        if result.get("authorization_captured"):
            # Round 45 (Claude round-44 Low 5a): the recorder may have
            # found a partial capture on its second enumeration — book
            # the voided residual so captured + voided == hold.
            job.voided_amount_cents = self._compute_voided_residual(job)
            job.status = "CAPTURED"
            job.settled_at = int(time.time())
            self._save(job)
            return result
        captured = job.capture_amount_cents
        if captured is not None and captured >= hold_cents:
            job.status = "CAPTURED"
            job.settled_at = int(time.time())
            self._save(job)
            return result
        self._save(job)
        return self._finish_partial_capture(job)

    def _capture_partial_via_dispatcher(self, job: Job, *, payer: str,
                                        delivery_text: str, amount_cents: int) -> dict:
        """Dispatcher bridge for capture_partial — mirrors capture_partial()
        verbatim, with transition() calls only at state-change points.

        Phase 2 Method 2: every guard, error message, and return
        contract matches legacy byte-for-byte. The dispatcher tracks
        state (S6 -> S7 on intent, S7 -> S8 on capture recorded); it
        does not reimplement business logic.
        """
        from .dispatcher import (resolve_state, transition, State, Event, Evidence)
        # Round 12: re-read the job inside the lock (see authorize):
        # the passed Job may be a stale copy, and a future store
        # must not hand a locked method a Job another thread already
        # settled. No-op against the current stores (they return the
        # same object).
        fresh = self.store.get(job.job_id)
        if fresh is None:
            raise EscrowError(f"job not found: {job.job_id}")
        job = fresh
        if job.status != "DELIVERED":
            raise EscrowError(f"cannot capture in state {job.status} (needs DELIVERED)")
        if payer != job.payer:
            raise EscrowError("only the payer can release this job")
        if not delivery_text:
            raise EscrowError("delivery text required for verification")
        if not self.delivery_verifier(job, delivery_text):
            raise EscrowError("delivery verification failed")
        if isinstance(amount_cents, bool) or not isinstance(amount_cents, int):
            raise EscrowError("partial capture amount must be an integer number of cents")
        if amount_cents <= 0:
            raise EscrowError("partial capture amount must be greater than zero")
        hold_cents = job.quote["price_cents"]
        if amount_cents > hold_cents:
            raise EscrowError("partial capture amount exceeds the authorized hold")
        # Round 26 (Gemini round-25 suggestion): the needs_review flag
        # guards capture_partial too, matching void_after_deadline —
        # a wedged job must not accept new capture attempts (not even
        # the retry path below). The payer-only force-settle is the
        # exit.
        if job.needs_review:
            amount_note = (
                f" (attempted ${job.needs_review_amount_cents/100:.2f})"
                if job.needs_review_amount_cents is not None else "")
            raise EscrowError(
                "cannot capture partial: job needs operator review"
                + amount_note)
        if self._has_recorded_capture(job):
            # A previous attempt recorded the partial capture but the
            # residual void did not finish: this is the retry. Round
            # 29: the guard keys on the G2 "already stored" definition
            # — a truthy capture_id OR a recorded amount — so an
            # unauditable capture (id "" with an amount on the books)
            # also routes here instead of minting a second capture.
            # The amount must match the recorded capture — a different
            # amount would be a second capture, not a retry — and the
            # capture call is skipped, only the void runs. (Checked
            # before the degenerate branch below, so a retry with the
            # full hold still gets this exact-amount error instead of
            # falling through to the full capture's refusal.)
            if job.capture_amount_cents != amount_cents:
                raise EscrowError(
                    "partial capture already recorded for "
                    f"{job.capture_amount_cents} cents — retry with that "
                    "amount to finish the residual void")
            return self._finish_partial_capture(job)
        if job.pending_capture_request_id is not None:
            # Round 11: a capture is already in flight. A pending
            # FULL capture (amount None) blocks any partial — the
            # whole hold may already have moved. A pending PARTIAL
            # capture blocks a retry with a DIFFERENT amount (the
            # pending capture may already have executed, and a
            # different amount would be a second capture on top of
            # it); a retry with the SAME amount replays it under its
            # own key — that is the recovery path, not an error.
            if job.pending_capture_amount_cents is None:
                raise EscrowError(
                    "full capture already pending — finish it with "
                    "capture, not a partial capture")
            if job.pending_capture_amount_cents != amount_cents:
                raise EscrowError(
                    "partial capture already pending for "
                    f"{job.pending_capture_amount_cents} cents — a "
                    "different amount would over-capture; retry the "
                    "same amount or reconcile")
            # The retry reuses the stored key, never a fresh one.
            # Round 50 (Claude round-49 Medium 2): the shared pre-read
            # — a blind replay would return PENDING forever.
            request_id = job.pending_capture_request_id
            settled = self._retry_pre_read(job, "capture partial")
            if settled is not None:
                return settled
        if amount_cents == hold_cents:
            # Degenerates to a normal capture — no residual to void.
            # (Checked after the intent guard above, so a pending
            # partial is refused with the partial error, not routed
            # here; a pending FULL capture has amount None, so this
            # branch is only reached when nothing is pending.)
            return self.capture(job, payer=payer, delivery_text=delivery_text)
        # Round 11: record the intent BEFORE the PayPal call, so a
        # lost response still leaves a replayable record of what was
        # asked. (Skipped when an intent is already pending — the
        # retry reuses it, request id included.)
        # Dynamic attempt counter (Grok fix order): each attempt mints
        # a fresh idempotency key so a declined key is never replayed.
        # Defined once here — the completion Evidence below needs it on
        # both the fresh-intent and the retry path.
        attempt_num = max(1, job.capture_attempt + 1)
        if job.pending_capture_request_id is None:
            # Round 13: pre-flight read — never POST blind. A fresh
            # partial with no stored intent on a hold PayPal already
            # reports as PARTIALLY_CAPTURED / CAPTURED / VOIDED /
            # EXPIRED must not invent a key and capture on top of
            # moved money (the disclosed residual hole: a blind
            # partial on a partially-captured hold over-captures).
            # Reconcile from the read instead, then refuse — the
            # payer's path forward is the recorded state.
            try:
                pre = self.paypal.get_authorization(job.authorization_id)
            except PayPalError as e:
                raise EscrowError(
                    f"PayPal capture pre-flight read failed: {e}") from e
            pre_status = pre.get("status")
            if pre_status != "CREATED":
                # Round 15 (Grok round-14 finding 4): reconcile from
                # the pre-flight read under the lock already held —
                # reconcile_job would fetch the authorization a
                # second time, and a transport failure on that read
                # discarded the pre-flight's own answer. The job was
                # re-read at the top of this method and is open
                # (AUTHORIZED or DELIVERED), which is all
                # reconcile_job's open-check would establish.
                rec = self._reconcile_with_auth(job, pre, job.status)
                if rec["to_status"] in ("CAPTURED", "VOIDED"):
                    # Round 14 (ChatGPT round-13 improvement 1): the
                    # pre-flight read found the money already moved
                    # (or the hold released) and reconcile settled it
                    # — return the settlement instead of a 409 whose
                    # only clue is "reconciled to CAPTURED". The
                    # endpoint returns the settled job and its receipt
                    # from this same job object.
                    return {"status": "ALREADY_SETTLED",
                            "to_status": rec["to_status"],
                            "capture_id": rec["capture_id"],
                            "reconciled": True, "error": ""}
                # Round 45 (Claude round-44 Medium): propagate the
                # Round 46 (Claude round-45 Low 5): use the structured
                # "awaiting" flag, not substring-matching (brittle).
                if rec.get("awaiting"):
                    raise EscrowError(rec["error"])
                raise EscrowError(
                    f"cannot capture partial: authorization is {pre_status} "
                    f"(reconciled to {rec['to_status']})")
            # Round 24 (Grok round-23 structural): the authorization
            # says CREATED, but the order may list a COMPLETED
            # capture the authorization omits — minting a key now
            # would capture on top of moved money. Same tri-state
            # check as the void pre-read: fail closed on unread,
            # record-and-handle on found.
            pre = self._first_completed_capture_including_order(
                job, pre)
            if pre.outcome == pre.UNREAD:
                raise EscrowError(
                    "PayPal capture pre-flight: authorization is "
                    "CREATED but the order could not be read — "
                    "refusing to capture blind")
            if pre.outcome == pre.AMBIGUOUS:
                # Round 28 (C5): a non-terminal capture is listed —
                # capturing on top of it could double-move. Refuse.
                raise EscrowError(
                    "PayPal capture pre-flight: authorization is "
                    "CREATED but a non-terminal capture is listed — "
                    "refusing to capture blind (operator review "
                    "needed)")
            if pre.outcome == pre.UNATTRIBUTABLE:
                # Round 49 (Claude round-48 MEDIUM 5): route through the
                # shared guard — sets needs_review so the operator sees
                # it and force_settle has an exit.
                self._wedge_on_unattributable(
                    job, pre.outcome,
                    "PayPal capture pre-flight")
                raise EscrowError(
                    job.needs_review_reason or
                    "captures unattributable (operator review needed)")
            if pre.capture is not None:
                return self._handle_preflight_order_capture(
                    job, pre.capture, "capture_partial")
            # --- Dispatcher state-change point: mint the intent. ---
            state = resolve_state(job)
            ev = Evidence(job_id=job.job_id, attempt=attempt_num,
                          is_payer=True, delivery_verified=True,
                          amount_cents=amount_cents,
                          authorized_cents=job.quote.get("price_cents"))
            new_state, actions = transition(
                state, Event.E_START_CAPTURE_PARTIAL, ev)
            self._apply_dispatcher_actions(job, actions, new_state)
            # Grok v7.1 finding #2: use the HMAC key set by
            # _apply_dispatcher_actions via SaveIntent — do NOT
            # overwrite with the legacy string.
            request_id = job.pending_capture_request_id
        try:
            result = self.paypal.capture_authorization(
                job.authorization_id, amount_cents=amount_cents,
                request_id=request_id,
                # Claude v8.4: final_capture releases the residual on real PayPal.
                final_capture=True)
        except PayPalError:
            # Grok fix order #4: partial captures reconcile with the
            # partial-aware helper (not the full-capture one).
            result = self._capture_partial_with_reconcile(
                job, amount_cents)
        # Round 40 (ChatGPT round-39): the COMPLETED gate is for
        # observed (direct POST) results only — same rule as capture().
        # A reconciled result carries the linked capture's real status;
        # the money moved, so it settles.
        if not result.get("reconciled"):
            if result.get("status") != "COMPLETED":
                if result.get("status") in ("DECLINED", "FAILED"):
                    # Definitive: retire the declined key (round 13) —
                    # see capture(). PENDING is left alone.
                    self._abandon_pending_capture(job, request_id,
                                                  op="capture_partial")
                raise EscrowError(
                    f"partial capture not completed (status {result.get('status')})")
        # --- Dispatcher state-change point: record the capture. ---
        # Round 11: the returned amount is recorded as-is — PayPal's
        # capture amount is authoritative. A malformed amount arrives
        # as None (legacy _record_capture_amount leaves the field
        # alone instead of wedging): the ID is recorded, the amount
        # stays unknown, and the job settles with the split unknown
        # (test_malformed_partial_capture_amount_never_raises).
        state = resolve_state(job)
        cap_id = result.get("id") or ""
        paypal_amount = self._listed_capture_amount_cents(result)
        if not cap_id:
            # No capture ID: explicit unaudited event (not
            # R_PARTIAL_CAP_COMPLETED, which requires IDs).
            ev_cap = Evidence(job_id=job.job_id,
                              amount_cents=paypal_amount,
                              authorized_cents=job.quote.get("price_cents"),
                              attempt=attempt_num)
            new_state, actions = transition(state, Event.E_UNAUDITED_CAPTURE,
                                            ev_cap)
        else:
            ev_cap = Evidence(job_id=job.job_id,
                              capture_ids=(cap_id,),
                              amount_cents=paypal_amount,
                              authorized_cents=job.quote.get("price_cents"),
                              attempt=attempt_num)
            new_state, actions = transition(state,
                                            Event.R_PARTIAL_CAP_COMPLETED,
                                            ev_cap)
        self._apply_dispatcher_actions(job, actions, new_state)
        # Round 40 (ChatGPT round-39): record the result's actual
        # status. Observed results passed the COMPLETED gate above;
        # reconciled results carry the listed capture's real status —
        # never manufacture COMPLETED over it.
        job.capture_status = result.get("status")
        # The reconciled flag comes from the capture result itself:
        # a normal capture leaves it False (absent), a reconciled one
        # sets it — the same flag the full capture path records, so
        # the receipt says whether the capture was observed or
        # recovered from PayPal's state.
        job.capture_reconciled = bool(result.get("reconciled"))
        # Round 31 (B2): how the settlement was established.
        job.settlement_method = (
            "reconciled" if result.get("reconciled") else "observed")
        self._record_paypal_response(job, operation="capture-partial",
                                     status=result.get("status"),
                                     id_=result.get("id"), body=result)
        # The actual captured amount decides the residual — if PayPal
        # captured the whole hold despite a partial request, there is
        # nothing left to void and this settles as a full capture.
        # Round 44 (Claude round-43): an unaudited recovery (auth
        # proved CAPTURED, no capture listed) settles CAPTURED
        # directly — the amount is unknowable, so there is no residual
        # to void. Do NOT loosen the `captured is not None` test below:
        # that would book CAPTURED on an observed result with no amount
        # and skip a legitimate void.
        if result.get("authorization_captured"):
            # Round 45 (Claude round-44 Low 5a): the recorder may have
            # found a partial capture on its second enumeration — book
            # the voided residual so captured + voided == hold.
            job.voided_amount_cents = self._compute_voided_residual(job)
            job.status = "CAPTURED"
            job.settled_at = int(time.time())
            self._save(job)
            return result
        captured = job.capture_amount_cents
        if captured is not None and captured >= hold_cents:
            job.status = "CAPTURED"
            job.settled_at = int(time.time())
            self._save(job)
            return result
        self._save(job)
        return self._finish_partial_capture(job)

    def _finish_partial_capture(self, job: Job) -> dict:
        """Void the residual authorization after a partial capture was
        recorded, and settle the job CAPTURED with the split on it.
        Called by capture_partial after the capture leg, by capture()
        when a partial landed out of band between its pre-flight read
        and its POST (round 28, C4), and again on a retry when the
        capture leg is already recorded. Returns the
        void result ({} — PayPal's void returns 204). Raises
        EscrowError and leaves the job DELIVERED (partial capture
        still recorded) when the void genuinely never happened, so the
        payer can retry and the admin reconcile keeps the job in view;
        raises when the authorization state could not be read at all.
        Round 23 (Grok round-22 recommendation 5): the residual is
        never booked on a bare void POST without confirmation — after
        _void_residual reports "voided", the authorization is re-read
        and the hold must prove released before the residual is
        booked."""
        hold_cents = job.quote["price_cents"]
        captured = job.capture_amount_cents
        # Claude v8.6: when final_capture=True was sent on the partial
        # capture, PayPal may have released the residual already — POSTing
        # a void then fails ("already captured") and wedges the job.
        # Re-read first: on a CAPTURED read, skip ONLY the void POST and
        # feed the read into _resolve_post_void (never shortcut around it —
        # it wedges on out-of-band captures that a bare status check would
        # misbook as released).
        try:
            pre_auth = self.paypal.get_authorization(job.authorization_id)
        except PayPalError:
            pre_auth = None
        if (pre_auth is not None
                and str(pre_auth.get("status", "")).upper() == "CAPTURED"):
            auth = pre_auth
        else:
            # Grok fix order #2: POST the residual void with the persisted
            # HMAC key from the dispatcher's SaveResidualVoidIntent — not a
            # freshly minted legacy key. Two keys for one void risks a
            # double-void or a replay mismatch on retry.
            request_id = (job.pending_void_request_id
                          or self._request_id(job, "void-residual"))
            outcome = self._void_residual(job, request_id)
            if outcome == "not_applied":
                raise EscrowError(
                    "residual void failed; partial capture recorded — retry "
                    "the capture to finish the void")
            # Round 24 (Grok round-23 structural): the post-void
            # decision lives in _resolve_post_void — shared with
            # void_after_deadline's post-read and _confirm_residual_void.
            # partial_origin=True: the capture leg already moved money,
            # so a released hold with nothing listed or recorded wedges
            # instead of booking VOIDED over unaccounted money.
            try:
                auth = self.paypal.get_authorization(job.authorization_id)
            except PayPalError:
                # Round 28 (C7): the confirmation read can lag a
                # successful void — retry once before wedging on it.
                try:
                    auth = self.paypal.get_authorization(job.authorization_id)
                except PayPalError:
                    auth = None
        # Track whether we skipped the void via the final_capture pre-read,
        # so _resolve_post_void treats CAPTURED as released.
        skipped_void = (pre_auth is not None
                        and str(pre_auth.get("status", "")).upper() == "CAPTURED")
        decision = self._resolve_post_void(
            job, auth, partial_origin=True,
            verb="cannot finish partial capture",
            final_capture_released=skipped_void)
        if decision[0] == "captured":
            # A capture is listed. Round 28 (G1): the settle helper
            # routes it through _record_listed_capture — the leg's
            # record is never overwritten. On "conflict" (a genuinely
            # different capture id, or a different amount on an
            # amount-only record) the books are untouched and the job
            # wedges. On "ok" the books hold the capture — recorded
            # just now ("recorded", reconciled truthful) or kept from
            # the leg ("kept", the leg's observed record stands,
            # reconciled untouched) — and the tail below settles
            # from the books.
            settle_outcome = self._settle_captured_with_residual(
                job, decision[1])
            if settle_outcome == "conflict":
                reason = (f"post-void read lists capture "
                          f"{decision[1].get('id')} but the recorded "
                          f"capture is {job.capture_id} — money moved "
                          "outside the recorded flow (operator review "
                          "needed)")
                self._set_review_flag(job, reason)
                self._save(job)
                raise EscrowError(
                    "cannot finish partial capture: " + reason)
            captured = job.capture_amount_cents
        # ("settle_books",): the normal path — the capture leg
        # recorded the capture, the hold is released, the residual
        # books from the books. ("voided",) is unreachable with
        # partial_origin=True.
        # Round 24 (Grok round-23 finding 2): the confirmation read
        # is the audit record — not a hardcoded VOIDED with an empty
        # body — and the review flag clears on this success path. The
        # capture id and amount persist on the job, so the receipt
        # still shows the first leg.
        self._record_paypal_response(job, operation="void",
                                     status=auth.get("status"),
                                     id_=job.authorization_id, body=auth)
        self._clear_review_flag(job)
        self._clear_pending_void(job)
        job.voided_amount_cents = self._compute_voided_residual(job)
        job.status = "CAPTURED"
        job.settled_at = int(time.time())
        # Claude v8.8: flag out-of-band captures AFTER settlement — the
        # tail above clears the review flag, so flagging earlier gets
        # wiped. A flagged terminal job is a supported state (F2 path).
        if skipped_void:
            self._flag_out_of_band_captures(job, auth)
        self._save(job)
        return {}

    def _capture_partial_with_reconcile(self, job: Job,
                                        amount_cents: int) -> dict:
        """The partial capture call failed — but it may have executed
        server-side before the response was lost. First retry with the
        same idempotency key (PayPal replays the original capture). If
        the retry also fails, ask PayPal for the authorization's actual
        state: a linked COMPLETED capture with the requested amount
        means the partial capture applied — return it, flagged
        reconciled, with its REAL status (round 40), so the receipt can
        be produced. If the authorization is CAPTURED with a linked capture of at least the hold, the
        whole hold was captured (the partial request degenerated
        server-side) — return that capture, flagged reconciled, and
        the caller settles it as a full capture with no residual void.
        Anything else raises: the capture did not happen.
        Round 12: the retried key is always the job's STORED pending
        key, never a fresh one (see _capture_with_reconcile). With no
        stored key there is nothing safe to replay, so the POST
        retries are skipped and the read alone decides."""
        key = job.pending_capture_request_id
        if key is not None:
            # v8.2 (item 6): the replay is audit-relevant — it is the
            # exact idempotency key PayPal already saw.
            self._audit("capture_partial", job, idempotency_key=key,
                        outcome="retried",
                        reason="partial capture POST lost its response; "
                               "replaying the stored idempotency key so "
                               "PayPal replays the original instead of "
                               "capturing twice")
            try:
                return self.paypal.capture_authorization(
                    job.authorization_id, amount_cents=amount_cents,
                    request_id=key)
            except PayPalError:
                pass
        try:
            auth = self.paypal.get_authorization(job.authorization_id)
        except PayPalError as e:
            raise EscrowError(
                f"PayPal partial capture failed and state could not be "
                f"reconciled: {e}")
        # Round 26 (Gemini round-25 finding 4): check the order too —
        # the authorization resource may omit a COMPLETED capture the
        # order lists.
        # v8.2 (Finding 1): use the CaptureEnumeration predicates, not
        # ad-hoc outcome checks. An incomplete enumeration must never
        # trigger a replay or a booking.
        enum_res = self._first_completed_capture_including_order(
            job, auth)
        _outcome, cap = enum_res.outcome, enum_res.capture
        if enum_res.requires_wedge():
            # UNREAD / ORDER_UNREAD / AMBIGUOUS / UNATTRIBUTABLE: the
            # evidence is incomplete. Preserve the pending intent and
            # wedge — never replay the key (Case A: a replay could
            # stack a new capture on moved money) and never book
            # (Case B: ORDER_UNREAD with a listed capture is not safe
            # for residual arithmetic).
            if _outcome == "unattributable":
                # Round 48 (Claude round-47 Low 5): use the shared guard —
                # sets the review flag and saves, so the operator sees it.
                self._wedge_on_unattributable(
                    job, _outcome,
                    "PayPal partial capture failed")
                raise EscrowError(
                    job.needs_review_reason or
                    "captures unattributable (operator review needed)")
            # UNREAD, ORDER_UNREAD, AMBIGUOUS: wedge with the intent kept.
            reason = {
                "unread": "PayPal partial capture failed and the order "
                          "could not be read — money may have moved "
                          "(operator review needed)",
                "order_unread": "PayPal partial capture failed and the "
                                "order could not be read — a listed "
                                "capture cannot be reconciled "
                                "(operator review needed)",
                "ambiguous": "PayPal partial capture failed and a "
                             "non-terminal capture is listed — money may "
                             "still move (operator review needed)",
            }[_outcome]
            self._set_review_flag(job, reason,
                                  job.pending_capture_amount_cents)
            self._save(job)
            raise EscrowError(reason)
        if cap is None:
            # Round 11: PayPal reports PARTIALLY_CAPTURED but lists
            # no captures — the partial may still have executed with
            # every response lost (the retry above lost its response
            # too). Replay the stored key once more before giving up:
            # PayPal replays the original result if it executed, or
            # executes it now if it did not.
            # v8.2 (Finding 1): only replay when the enumeration does
            # NOT block it. blocks_replay() is True for every outcome
            # except NONE — and we've already wedged on UNREAD/
            # ORDER_UNREAD/AMBIGUOUS/UNATTRIBUTABLE above, so this
            # branch only runs on NONE (definitive absence).
            if (auth.get("status") == "PARTIALLY_CAPTURED"
                    and key is not None
                    and not enum_res.blocks_replay()):
                # v8.2 (item 6): the replay is audit-relevant — it is
                # the exact idempotency key PayPal already saw.
                self._audit("capture_partial", job, idempotency_key=key,
                            outcome="retried",
                            reason="PARTIALLY_CAPTURED with no listed "
                                   "capture and a fully-auditable empty "
                                   "enumeration; replaying the stored "
                                   "idempotency key")
                try:
                    return self.paypal.capture_authorization(
                        job.authorization_id, amount_cents=amount_cents,
                        request_id=key)
                except PayPalError:
                    pass
            if auth.get("status") == "CAPTURED":
                # Round 43 (Claude round-42): the authorization is
                # CAPTURED but no capture is listed anywhere — the
                # money moved but cannot be attributed. Route through
                # the recorder (marks unaudited) instead of raising
                # with the partial intent stranded. The caller settles
                # CAPTURED; the unaudited status says the amount is
                # unknowable.
                # Round 44 (Claude round-43): carry an explicit marker
                # so capture_partial settles directly instead of
                # falling into _finish_partial_capture (which would
                # POST voids at a dead authorization).
                # Round 45 (Claude round-44 Low 5b): pass the first
                # enumeration — the recorder must not re-read and
                # disagree with it.
                outcome, _read_outcome = self._record_captured_authorization(
                    job, auth, _precomputed=(_outcome, cap))
                if outcome == "conflict":
                    self._save(job)
                    raise EscrowError(
                        "PayPal partial capture failed: "
                        f"{job.needs_review_reason or 'capture conflict'}")
                if outcome == "ambiguous":
                    # Round 45 (Claude round-44 Medium): PENDING never
                    # settles. The intent is kept; the job stays
                    # DELIVERED for a retry once the capture reads
                    # COMPLETED.
                    self._save(job)
                    raise EscrowError(
                        "PayPal partial capture failed: authorization "
                        "is CAPTURED but the listed capture is PENDING "
                        "— awaiting COMPLETED; retry or reconcile")
                if outcome == "unattributable":
                    # Round 46: the recorder wedged with an accurate
                    # reason. Save and raise it.
                    self._save(job)
                    raise EscrowError(
                        "PayPal partial capture failed: "
                        f"{job.needs_review_reason or 'captures unattributable'}")
                self._save(job)
                # Round 45 (Claude round-44 Low 5c): record the
                # authorization read that proved CAPTURED — the
                # synthetic result alone doesn't show what PayPal said.
                self._record_paypal_response(job, operation="reconcile",
                                             status=auth.get("status"),
                                             id_=auth.get("id"), body=auth)
                return {
                    "id": job.capture_id,
                    "status": job.capture_status,
                    "reconciled": True,
                    "authorization_captured": True,
                    "amount": ({"value":
                                f"{job.capture_amount_cents / 100:.2f}",
                                "currency_code":
                                job.quote.get("currency", "USD")}
                               if job.capture_amount_cents is not None
                               else None),
                }
            raise EscrowError(
                f"PayPal partial capture failed; authorization is "
                f"{auth.get('status')}")
        # Round 11: the linked capture is authoritative over the
        # request — return it for whatever amount PayPal actually
        # captured, so the job settles on the actual split. The old
        # code refused a linked capture for a different amount, which
        # left money moved with nothing recorded (Claude's round-10
        # finding 1 in reverse). A linked capture of at least the
        # hold still settles as a full capture with no residual void,
        # exactly as before.
        # Round 40 (ChatGPT round-39): return the listed capture's
        # REAL status — never manufacture COMPLETED over it.
        # v8.2 (Finding 1, Case B): only book when fully auditable.
        # requires_wedge() already rejected ORDER_UNREAD above, so this
        # is defense-in-depth — a non-FOUND outcome with a cap must
        # never reach the booking.
        if not enum_res.is_fully_auditable():
            reason = (f"PayPal partial capture failed: capture listed "
                      f"but enumeration is {_outcome}, not fully "
                      f"auditable (operator review needed)")
            self._set_review_flag(job, reason,
                                  job.pending_capture_amount_cents)
            self._save(job)
            raise EscrowError(reason)
        return {"id": cap.get("id", ""), "status": cap.get("status"),
                "reconciled": True, "amount": cap.get("amount")}

    def _void_residual(self, job: Job, request_id: str) -> str:
        """Void the residual authorization after a partial capture.
        Returns "voided" when the hold is released — directly, by
        idempotent retry, or confirmed by a reconcile read — and
        "not_applied" when PayPal says the authorization is still
        CREATED, i.e. the void never happened (the caller records the
        partial capture and leaves the job open for a retry). Raises
        EscrowError when the authorization state could not be read at
        all, and when it is already CAPTURED (too late to void)."""
        try:
            self.paypal.void_authorization(
                job.authorization_id, request_id=request_id)
            return "voided"
        except PayPalError:
            pass
        # v8.2 (item 6): the second POST replays the same idempotency
        # key after a lost response — audit-relevant. (Only called
        # from the partial-capture flow, so the operation is
        # capture_partial.)
        self._audit("capture_partial", job, idempotency_key=request_id,
                    outcome="retried",
                    reason="residual void POST lost its response; "
                           "retrying the same idempotency key")
        try:
            self.paypal.void_authorization(
                job.authorization_id, request_id=request_id)
            return "voided"
        except PayPalError:
            pass
        try:
            auth = self.paypal.get_authorization(job.authorization_id)
        except PayPalError as e:
            raise EscrowError(
                f"PayPal residual void failed and state could not be "
                f"reconciled: {e}")
        status = auth.get("status")
        if _is_hold_released(status):
            # VOIDED: the hold really was released. EXPIRED (round 15,
            # Grok round-14 finding 3): an expired hold releases the
            # residual exactly like a void — the money is back with
            # the payer. DENIED (round 31, C4): a denied hold can
            # never move money — same terminal state.
            return "voided"
        if status == "CAPTURED":
            raise EscrowError(
                "authorization already captured — cannot void residual")
        return "not_applied"

    # -- reconcile -----------------------------------------------------------
    @_audited("reconcile_job")
    @_per_job_locked
    def reconcile_job(self, job: Job) -> dict:
        """Route reconcile through dispatcher when USE_DISPATCHER is active."""
        if USE_DISPATCHER:
            return self._reconcile_job_via_dispatcher(job)
        # Legacy path: force one open job's state from PayPal's actual
        fresh = self.store.get(job.job_id)
        if fresh is None:
            raise EscrowError(f"job not found: {job.job_id}")
        job = fresh
        if job.status not in ("AUTHORIZED", "DELIVERED"):
            return {"job_id": job.job_id, "from_status": job.status,
                    "to_status": job.status, "updated": False,
                    "capture_id": job.capture_id,
                    "error": "job is not open"}
        from_status = job.status
        # Round 13: fetch inside the lock, unconditionally — see above.
        try:
            auth = self.paypal.get_authorization(job.authorization_id)
        except PayPalError as e:
            return {"job_id": job.job_id, "from_status": from_status,
                    "to_status": job.status, "updated": False,
                    "capture_id": job.capture_id, "error": str(e),
                    "transport_error": True}
        # Round 14: one read, one decision — the branch logic lives in
        # _reconcile_with_auth so the webhook path can pass its own
        # in-lock read instead of fetching a second time (ChatGPT +
        # Claude round-13 Medium: a transient failure on the second
        # read used to be marked processed).
        return self._reconcile_with_auth(job, auth, from_status)

    def _reconcile_job_via_dispatcher(self, job: Job,
                                      auth: dict | None = None) -> dict:
        """Reconcile through the dispatcher bridge.

        The dispatcher FSM decides the reconcile paths via transition(),
        including the pending capture intent lifecycle (v8.2: modeled in
        the FSM — background reconcile is read-only and never replays
        the key). Legacy (_reconcile_with_auth) remains the fallback for
        genuinely unhandled cases:
        - UnhandledTransition (unknown state/event combos)
        - Books-ahead-of-status (resolve_state infers S10* while the
          job status is still open)

        The return contract is derived from the FSM outcome.

        ``auth`` is an optional pre-fetched authorization read (the
        capture preflight already read it) — avoids a second PayPal GET
        and the TOCTOU between two reads. When None, the bridge reads
        it itself.
        """
        from .dispatcher import (resolve_state, transition, State, Event,
                                 GuardRejection, UnhandledTransition,
                                 ClearIntent)
        import logging
        logger = logging.getLogger(__name__)

        # --- Entry: same as legacy reconcile_job ---
        fresh = self.store.get(job.job_id)
        if fresh is None:
            raise EscrowError(f"job not found: {job.job_id}")
        job = fresh
        if job.status not in ("AUTHORIZED", "DELIVERED"):
            return {"job_id": job.job_id, "from_status": job.status,
                    "to_status": job.status, "updated": False,
                    "capture_id": job.capture_id,
                    "error": "job is not open"}
        from_status = job.status
        if auth is None:
            try:
                auth = self.paypal.get_authorization(job.authorization_id)
            except PayPalError as e:
                # Legacy: a failed GET is a transport error — the webhook
                # gateway needs this flag to answer 503 (retry) instead of
                # 200 (ack).
                return {"job_id": job.job_id, "from_status": from_status,
                        "to_status": job.status, "updated": False,
                        "capture_id": job.capture_id, "error": str(e),
                        "transport_error": True}

        try:
            enum_res = self._first_completed_capture_including_order(
                job, auth)
        except PayPalError:
            enum_res = None

        # Phase 1 design (grace window): in-flight intent + auth
        # CAPTURED + no capture listed (ENUM_NONE) = PayPal replication
        # lag, not a definitive outcome. Return awaiting=True (webhook
        # answers 503, PayPal redelivers) for the grace period.
        if (job.pending_capture_request_id is not None
                and auth.get("status") == "CAPTURED"
                and enum_res is not None
                and enum_res.outcome == "none"):
            created_at = job.pending_capture_created_at or 0
            age = self._now() - created_at
            if age < self.IN_FLIGHT_READ_LAG_GRACE_SECONDS:
                return {"job_id": job.job_id, "from_status": from_status,
                        "to_status": job.status, "updated": False,
                        "capture_id": job.capture_id,
                        "error": ("authorization reads CAPTURED but no "
                                  "capture is listed yet — awaiting "
                                  "PayPal's read to catch up"),
                        "awaiting": True}

        before_state = resolve_state(job)

        # FALLBACK 0: Books-ahead-of-status.
        # If resolve_state infers a captured state (S10a/b/c) from the
        # books but job.status is not CAPTURED, the books were manually
        # set or the bridge hasn't updated status yet. The dispatcher
        # can't safely dispatch E_RECONCILE from a captured state when
        # the job is still open — fall back to legacy which handles
        # this via _record_captured_authorization.
        from .dispatcher import State as _State
        if (before_state in (_State.S10a_CAPTURED_FULL,
                             _State.S10b_CAPTURED_SPLIT,
                             _State.S10c_CAPTURED_UNAUDITED)
                and job.status != "CAPTURED"):
            result = self._reconcile_with_auth(job, auth, from_status)
            after_state = resolve_state(job)
            if after_state != before_state:
                self._fsm_track_reconcile(job, before_state, auth, enum_res)
            if enum_res is not None and enum_res.outcome in (
                    "unread", "order_unread"):
                result["probe_transport_failed"] = True
            return result

        # FALLBACK 1 removed in v8.2 (production-readiness item 2): the
        # FSM now models the pending capture intent lifecycle
        # (_pending_intent_reconcile in app/dispatcher.py), so in-flight
        # keys no longer divert to legacy. The intent evidence rides on
        # the Evidence built below.

        # A wedged job NEVER auto-settles in reconcile (legacy rule),
        # EXCEPT when the read is a definitive CAPTURED — PayPal confirms
        # money moved, so the flag clears and it settles. For all other
        # statuses, the operator must use force-settle.
        # v8.2: a wedged job WITH a pending capture intent still
        # dispatches — the FSM re-evaluates the key's fate (it may have
        # landed since the wedge). The pending-intent branch never
        # settles on a non-CAPTURED read, so the never-auto-settle rule
        # holds; a listed capture on a wedged job stays wedged
        # (force-settle is the exit).
        if (job.needs_review and auth.get("status") != "CAPTURED"
                and job.pending_capture_request_id is None):
            self._save(job)
            amount_note = (
                f" (attempted ${job.needs_review_amount_cents/100:.2f})"
                if job.needs_review_amount_cents is not None else "")
            return {"job_id": job.job_id, "from_status": from_status,
                    "to_status": job.status, "updated": False,
                    "capture_id": job.capture_id,
                    "error": f"authorization is {auth.get('status')} but "
                             f"the job needs operator review — use "
                             f"force-settle" + amount_note}

        # Dispatcher bridge: the FSM decides the reconcile paths via
        # transition(), including the pending capture intent lifecycle.
        # Legacy remains the fallback for genuinely unhandled cases
        # (UnhandledTransition, books-ahead-of-status). See the bridge
        # docstring.
        status_before = job.status
        try:
            ev = self._build_reconcile_evidence(job, auth, enum_res)
            if ev.read is None:
                # Unknown auth status — cannot dispatch, fall back.
                raise UnhandledTransition(
                    before_state, Event.E_RECONCILE)
            new_state, actions = transition(
                before_state, Event.E_RECONCILE, ev)
        except UnhandledTransition:
            # FALLBACK 2: Genuinely unknown state/event — legacy decides.
            result = self._reconcile_with_auth(job, auth, from_status)
            after_state = resolve_state(job)
            if after_state != before_state:
                self._fsm_track_reconcile(job, before_state, auth, enum_res)
            if enum_res is not None and enum_res.outcome in (
                    "unread", "order_unread"):
                result["probe_transport_failed"] = True
            return result
        except GuardRejection as e:
            # Dispatcher refuses — definitive outcome, not a fallback.
            if e.category == "retryable":
                return {"job_id": job.job_id, "from_status": from_status,
                        "to_status": job.status, "updated": False,
                        "capture_id": job.capture_id, "error": str(e),
                        "transport_error": True}
            reason = f"reconcile refused: {e}"
            self._set_review_flag(job, reason)
            self._save(job)
            return {"job_id": job.job_id, "from_status": from_status,
                    "to_status": job.status, "updated": False,
                    "capture_id": job.capture_id, "error": reason}

        # Apply dispatcher actions (state + money mutations).
        pending_io = self._apply_dispatcher_actions(job, actions, new_state)
        if pending_io:
            # Reconcile should never produce I/O actions — log if it does.
            logger.warning(
                "reconcile produced unexpected I/O actions: %s", pending_io)

        # Reconcile-specific bookkeeping (legacy does this in
        # _record_listed_capture): a capture recorded from a PayPal read
        # is reconciled by definition. Also propagate the listed
        # capture's status so receipts and status checks see it.
        # S8 (partial recorded) also sets reconciled — the partial was
        # listed by PayPal, not posted by us.
        if new_state in (State.S10a_CAPTURED_FULL, State.S10b_CAPTURED_SPLIT,
                         State.S10c_CAPTURED_UNAUDITED,
                         State.S8_DELIVERED_PARTIAL_RECORDED):
            job.capture_reconciled = True
            if new_state != State.S8_DELIVERED_PARTIAL_RECORDED:
                job.settlement_method = "reconciled"
            if job.capture_status is None and enum_res is not None:
                cap = enum_res.capture
                if cap is not None and isinstance(cap.get("status"), str):
                    job.capture_status = cap["status"] or "COMPLETED"
                elif job.capture_status is None:
                    # Unaudited path — no listed status available.
                    job.capture_status = "COMPLETED"

        # Record the PayPal read (legacy does this on all success paths).
        self._record_reconcile_read(job, auth)

        # Derive return contract from FSM outcome.
        # updated = FSM state changed, EXCEPT for wedges (S5/S9) where
        # legacy reports updated=False (the job.status doesn't change,
        # only needs_review is set).
        if new_state.is_wedged:
            updated = False
            error = (job.needs_review_reason
                     or "reconcile wedged (operator review needed)")
        else:
            updated = (new_state != before_state)
            error = ""

        result = {"job_id": job.job_id, "from_status": from_status,
                  "to_status": job.status, "updated": updated,
                  "capture_id": job.capture_id, "error": error}
        # v8.2 (production-readiness item 2): pending-intent return
        # contract. When the FSM kept the in-flight key (no ClearIntent,
        # no state change, not wedged), surface the legacy-compatible
        # flags. The NoOp note carries the exact return-contract text.
        if (ev.pending_intent_id is not None and not job.needs_review
                and new_state == before_state
                and not any(isinstance(a, ClearIntent)
                            for a in actions)):
            from .dispatcher import NoOp as _NoOp
            note = next((a.note for a in actions
                         if isinstance(a, _NoOp) and a.note), "")
            if ev.enum == Event.ENUM_AMBIGUOUS:
                result["awaiting"] = True
            else:
                result["intent_kept_unconfirmed"] = True
            if note:
                result["error"] = note
        if enum_res is not None and enum_res.outcome in (
                "unread", "order_unread"):
            result["probe_transport_failed"] = True
        # Legacy surfaces a retry error when the CAPTURED arm can't
        # enumerate captures but the job stays open (not wedged).
        # Match the legacy message for the r31_c2 case.
        if (not error and not updated and not job.needs_review
                and ev.read is not None
                and getattr(ev.read, 'name', '') == 'READ_CAPTURED'
                and enum_res is not None):
            if enum_res.outcome in ("unread", "order_unread"):
                result["error"] = (
                    "authorization is CAPTURED with a recorded capture but "
                    "the captures could not be enumerated — will retry")
            elif enum_res.outcome == "ambiguous":
                # Round 45: PENDING listed — awaiting COMPLETED.
                result["error"] = (
                    "authorization is CAPTURED with a PENDING capture "
                    "listed — awaiting COMPLETED (will retry)")
        return result

    def _build_reconcile_evidence(self, job: Job, auth: dict,
                                    enum_res,
                                    deadline_passed: bool = False,
                                    intent_stale: bool = False) -> "Evidence":
        """Build E_RECONCILE Evidence from a PayPal auth read.

        Extracted from _fsm_track_reconcile so the bridge can build
        Evidence BEFORE dispatching (not after legacy runs).
        Supplies complete evidence — capture IDs, amounts, partial/void
        flags — so the FSM sees what the read actually found.

        v8.2 (item 2b): the sweeper calls this same builder (ONE
        evidence builder). deadline_passed is a caller-supplied guard
        field — the sweeper computes it from the job's deadline; the
        bridge leaves the default. intent_stale is True when the caller
        knows the intent is past the staleness threshold (the sweeper);
        the bridge leaves it False.
        """
        from .dispatcher import Event, Evidence, GuardRejection
        status_str = auth.get("status")
        read_event = {
            "CREATED": Event.READ_CREATED,
            "PENDING": Event.READ_PENDING,
            "PARTIALLY_CAPTURED": Event.READ_PARTIAL,
            "CAPTURED": Event.READ_CAPTURED,
            "VOIDED": Event.READ_RELEASED,
            "EXPIRED": Event.READ_RELEASED,
            "DENIED": Event.READ_RELEASED,
        }.get(status_str)
        enum_event = None
        if enum_res is not None:
            enum_event = {
                "found": Event.ENUM_FOUND,
                "ambiguous": Event.ENUM_AMBIGUOUS,
                "unattributable": Event.ENUM_UNATTRIBUTABLE,
                "none": Event.ENUM_NONE,
                "unread": Event.ENUM_UNREAD,
                "order_unread": Event.ENUM_ORDER_UNREAD,
            }.get(enum_res.outcome)
        stored_ids = tuple(job.capture_ids) if job.capture_ids else (
            (job.capture_id,) if job.capture_id else ())
        # v8 money-safety fix: unpack the listed capture's _members
        # (mirroring the sweeper). The synthetic "A+B" join id must not
        # re-enter as one opaque id — each member is a real capture —
        # and a second real capture with a new id must reach
        # transition() instead of being invisible behind the stored id.
        # Legacy's conflict rule (_record_listed_capture) is enforced:
        # a bare listed id that differs from the stored id wedges for
        # operator review instead of silently appending.
        listed_cap = (enum_res.capture if enum_res is not None else None)
        listed_ids: tuple = ()
        if isinstance(listed_cap, dict):
            raw_members = listed_cap.get("_members")
            if isinstance(raw_members, list) and raw_members:
                listed_ids = tuple(sorted({
                    m.get("id") for m in raw_members
                    if isinstance(m, dict) and m.get("id")}))
            elif listed_cap.get("id"):
                listed_ids = (listed_cap.get("id"),)
        # Member ids already covered by the books — decomposing any
        # synthetic "A+B" join that was stored as one id, so members
        # are never double-counted.
        covered: set = set()
        for _sid in stored_ids:
            covered.update(_sid.split("+"))
        has_members = isinstance(listed_cap, dict) and isinstance(
            listed_cap.get("_members"), list)
        # Conflict rule (legacy _record_listed_capture): a bare listed
        # id that differs from the stored id wedges for operator review
        # instead of silently appending. Only enforced when the outcome
        # would actually book the listing (FOUND) — an AMBIGUOUS
        # (PENDING) listing is stayed-open by Round 45, never booked,
        # so it is not a conflict.
        if (stored_ids and listed_ids and not has_members
                and enum_res is not None
                and enum_res.outcome == "found"):
            # Bare singular id: legacy conflicts when it differs from
            # the stored id.
            stored_singular = job.capture_id or stored_ids[0]
            if listed_ids[0] != stored_singular:
                raise GuardRejection(
                    f"listed capture {listed_ids[0]} conflicts with "
                    f"recorded capture {stored_singular} (operator "
                    f"review needed)",
                    category="conflict")
        # Every listed member not already covered reaches transition().
        new_ids = tuple(lid for lid in listed_ids
                        if lid not in covered and lid not in stored_ids)
        c_ids = stored_ids + new_ids
        c_amt = job.capture_amount_cents
        listed_amt = (self._listed_capture_amount_cents(listed_cap)
                      if isinstance(listed_cap, dict) else None)
        if c_amt is None:
            c_amt = listed_amt
        elif new_ids and listed_amt is not None:
            # New real capture(s) listed: the listed total (a synthetic
            # sum covers all members) is the honest amount.
            c_amt = listed_amt
        has_cap = bool(c_ids or c_amt is not None)
        hold = (job.quote or {}).get("price_cents", 0)
        is_part = bool(has_cap and c_amt is not None and 0 < c_amt < hold)
        void_conf = bool(
            job.voided_amount_cents is not None
            or status_str in ("VOIDED", "EXPIRED", "DENIED"))
        # Compute the voided residual for a released hold with a partial
        # capture, mirroring legacy _compute_voided_residual (hold - captured).
        # The job won't have it set if the void was applied outside the
        # service (e.g. sweeper crash-recovery path).
        voided_amt = job.voided_amount_cents
        if (voided_amt is None and is_part
                and status_str in ("VOIDED", "EXPIRED", "DENIED")):
            voided_amt = hold - c_amt
        return Evidence(
            job_id=job.job_id,
            authorization_id=job.authorization_id,
            read=read_event,
            enum=enum_event,
            has_capture=has_cap,
            capture_ids=c_ids,
            amount_cents=c_amt,
            is_partial=is_part,
            void_confirmed=void_conf,
            voided_amount_cents=voided_amt,
            authorized_cents=_authorized_cents_for_evidence(job),
            # v8.2 (production-readiness item 2): the FSM models the
            # pending capture intent lifecycle — no legacy fallback.
            pending_intent_id=job.pending_capture_request_id,
            pending_intent_amount_cents=job.pending_capture_amount_cents,
            pending_intent_stale=intent_stale,
            deadline_passed=deadline_passed,
        )

    def _fsm_track_reconcile(self, job: Job, before_state,
                             auth: dict, enum_res) -> None:
        """Dispatch E_RECONCILE for FSM tracking at a reconcile state change.

        Legacy (_reconcile_with_auth) has already decided the outcome and
        performed all mutations via legacy helpers. This validates that the
        FSM agrees the transition is legal. Actions are deliberately NOT
        applied — legacy already mutated. Any divergence (including
        UnhandledTransition) is logged, never raised: legacy is the source
        of truth for the return contract.
        """
        from .dispatcher import transition, Event
        import logging
        logger = logging.getLogger(__name__)
        try:
            ev = self._build_reconcile_evidence(job, auth, enum_res)
            if ev.read is None:
                return
            new_state, _actions = transition(
                before_state, Event.E_RECONCILE, ev)
            # Validation only — actions not applied.
            from .dispatcher import resolve_state as _rs
            if new_state != _rs(job):
                _report_fsm_divergence(
                    "reconcile", job.job_id,
                    f"legacy settled {_rs(job)}, FSM gives {new_state}")
        except Exception as e:  # noqa: BLE001 — tracking must never break reconcile
            _report_fsm_divergence(
                "reconcile", job.job_id, f"FSM tracking failed: {e}")


    def _reconcile_with_auth(self, job: Job, auth: dict,
                             from_status: str) -> dict:
        """Turn one authorization read into reconcile state changes
        (round 14). The caller supplies the read — reconcile_job's own
        locked fetch, or the webhook handler's in-lock fetch — so
        there is exactly one GET per reconcile decision, and a
        transient failure is reported by the reader, never swallowed
        here. Call with the job's lock held (reconcile_job's
        decorator, or the webhook handler's lock)."""
        status = auth.get("status")
        updated = False
        to_status = job.status
        if status == "CREATED" or status == "PENDING":
            # The hold is still there — no state change to the job
            # itself. PENDING (round 14): PayPal has not firmly placed
            # the hold; it is CREATED in every way that matters here.
            # Round 14 (ChatGPT + Claude round-13 reviews): a CREATED
            # read proves nothing was captured — NOT that a pending
            # capture's key never reached PayPal.
            # Round 28 (C3): background reconcile is READ-ONLY — it
            # never POSTs. The old code probed the pending key, but a
            # probe with an unseen key EXECUTES as a new capture: a
            # process that died after saving the intent but before the
            # POST would have its capture fired by a later webhook or
            # reconcile with no payer action. Only the payer's
            # explicit retry (capture/capture_partial) replays the key.
            # Here we only check what PayPal lists: a listed COMPLETED
            # capture is recorded (a full-cover one settles the job,
            # a partial leaves the residual open); anything else
            # leaves the intent for the payer's retry or the webhook
            # to resolve. (The intent fields may change, so the read
            # is still recorded and saved by the tail below.)
            if self._has_pending_capture(job):
                cap_outcome, cap = (
                    self._first_completed_capture_including_order(
                        job, auth))
                # Round 48 (Claude round-47 Low 4): the shared guard —
                # "unattributable" wedges with an accurate reason, not
                # "no capture listed".
                if self._wedge_on_unattributable(
                        job, cap_outcome,
                        "reconcile pending-intent arm"):
                    return {"job_id": job.job_id,
                            "from_status": from_status,
                            "to_status": job.status, "updated": False,
                            "capture_id": job.capture_id,
                            "error": job.needs_review_reason or
                                     "captures unattributable (operator review needed)"}
                if cap_outcome == "ambiguous":
                    # A non-terminal capture is listed — the intent may
                    # yet land. Leave it for the webhook or retry.
                    # Round 52 (ChatGPT round-51 Medium 2): set the
                    # structured awaiting flag.
                    return {"job_id": job.job_id,
                            "from_status": from_status,
                            "to_status": job.status, "updated": False,
                            "capture_id": job.capture_id,
                            "awaiting": True,
                            "error": "capture pending and a non-terminal "
                                     "capture is listed — awaiting the "
                                     "webhook or a payer retry"}
                if cap is not None:
                    # A COMPLETED capture is listed — the key executed
                    # (the read lagged) or something captured out of
                    # band. Record it through the helper (G1: never a
                    # raw write); a full-cover capture settles the
                    # job, a partial leaves the residual open.
                    record_outcome = self._record_listed_capture(job, cap)
                    if record_outcome == "conflict":
                        reason = (f"capture pending but the authorization "
                                  f"is {status} and lists capture "
                                  f"{cap.get('id')} conflicting with the "
                                  f"recorded capture {job.capture_id} "
                                  "(operator review needed)")
                        self._set_review_flag(job, reason)
                        self._save(job)
                        return {"job_id": job.job_id,
                                "from_status": from_status,
                                "to_status": job.status, "updated": False,
                                "capture_id": job.capture_id,
                                "error": reason}
                    self._clear_pending_capture(job)
                    updated = True
                    if (job.capture_amount_cents is not None
                            and job.capture_amount_cents
                            >= job.quote["price_cents"]):
                        job.settlement_method = "reconciled"
                        job.status = "CAPTURED"
                        job.settled_at = int(time.time())
                        to_status = "CAPTURED"
                else:
                    # No COMPLETED capture listed. On "unread" the order
                    # could not be read, so "nothing listed" is
                    # unproven — say so. Either way the intent stays
                    # for the payer's retry or the webhook: no POST, no
                    # state change. A later retry replays the key, and
                    # if PayPal never saw it, it executes then, on the
                    # payer's explicit action.
                    if cap_outcome == "unread":
                        err = ("capture pending but the authorization is "
                               f"{status} and the order could not be read "
                               "— retry the capture to replay the key")
                    else:
                        err = ("capture pending but the "
                               f"authorization is {status} and no "
                               "capture is listed — retry the capture "
                               "to replay the key")
                    # Round 28 (C3): the intent is kept because the
                    # read could not confirm — a later re-delivery's
                    # read may catch up, so the webhook leaves the
                    # event unmarked (like the old probe-transport
                    # case).
                    return {"job_id": job.job_id,
                            "from_status": from_status,
                            "to_status": job.status, "updated": False,
                            "capture_id": job.capture_id,
                            "error": err,
                            "intent_kept_unconfirmed": True}
        elif status == "PARTIALLY_CAPTURED":
            # Round 9: real PayPal reports a partially captured
            # authorization this way (and lists no captures on it).
            # The hold is still open — money moved, the residual is
            # still held — so with no capture in flight this is
            # CREATED in every way that matters to reconcile: no
            # state change, no error. The webhook path inherits this
            # through _webhook_reconcile, so a CAPTURE.COMPLETED
            # event on such an authorization is "ignored", not
            # "pending" with an error.
            # Round 11: with a capture in flight, the authorization
            # proves the capture moved — replay the same key to
            # recover it. PayPal replays the original result if it
            # executed, or executes it now if it did not. A
            # recovered capture is recorded (id + actual amount,
            # reconciled False — it was observed via the replay, not
            # from PayPal's state) and the job stays open: the
            # residual void is still pending, and the payer retry
            # finishes it. A replay that fails leaves the job open
            # with the intent pending and reports an error, so the
            # admin summary surfaces it instead of calling the job
            # still pending.
            # Round 12: the guard keys on the intent's key — a
            # pending FULL capture replays with no amount, exactly
            # like the full capture's own retry.
            # Round 28 (C3): background reconcile is READ-ONLY — it
            # never POSTs, so the key is never replayed here. Only the
            # payer's explicit retry (capture_partial) replays it. We
            # check what PayPal lists: a COMPLETED capture is recorded
            # through the helper (G1); anything else keeps the intent
            # for the payer's retry.
            if self._has_pending_capture(job):
                cap_outcome, cap = (
                    self._first_completed_capture_including_order(
                        job, auth))
                # Round 49 (Claude round-48 MEDIUM 4): the shared guard —
                # "unattributable" wedges with an accurate reason, not
                # "no capture listed".
                if self._wedge_on_unattributable(
                        job, cap_outcome,
                        "reconcile PARTIALLY_CAPTURED pending-intent arm"):
                    return {"job_id": job.job_id,
                            "from_status": from_status,
                            "to_status": job.status, "updated": False,
                            "capture_id": job.capture_id,
                            "error": job.needs_review_reason or
                                     "captures unattributable (operator review needed)"}
                if cap_outcome == "ambiguous":
                    # Round 52 (ChatGPT round-51 Medium 2): set the
                    # structured awaiting flag so _retry_pre_read
                    # raises instead of replaying.
                    return {"job_id": job.job_id,
                            "from_status": from_status,
                            "to_status": job.status, "updated": False,
                            "capture_id": job.capture_id,
                            "awaiting": True,
                            "error": "partial capture pending and a "
                                     "non-terminal capture is listed — "
                                     "awaiting the webhook or a payer retry"}
                if cap is None:
                    # No COMPLETED capture listed (or the order could
                    # not be read — "unread" also lands here): the
                    # intent stays for the payer's retry. Round 18's
                    # bare-4xx concern is moot — no replay was
                    # attempted, so nothing was refused.
                    # Round 28 (C3): the intent is kept unconfirmed —
                    # a later re-delivery's read may catch up.
                    return {"job_id": job.job_id,
                            "from_status": from_status,
                            "to_status": job.status, "updated": False,
                            "capture_id": job.capture_id,
                            "error": "partial capture pending but no "
                                     "capture is listed"
                                     + (f" (attempted "
                                        f"${job.pending_capture_amount_cents/100:.2f})"
                                        if job.pending_capture_amount_cents
                                        is not None else "")
                                     + " — retry capture_partial with the "
                                     "same amount to replay the key",
                            "intent_kept_unconfirmed": True}
                # A COMPLETED capture is listed: record it through the
                # helper (G1 — never a raw write) and clear the spent
                # intent. A full-cover capture settles the job; a
                # partial leaves the residual open.
                record_outcome = self._record_listed_capture(job, cap)
                if record_outcome == "conflict":
                    reason = (f"partial capture pending but the "
                              f"authorization lists capture "
                              f"{cap.get('id')} conflicting with the "
                              f"recorded capture {job.capture_id} "
                              "(operator review needed)")
                    self._set_review_flag(job, reason)
                    self._save(job)
                    return {"job_id": job.job_id,
                            "from_status": from_status,
                            "to_status": job.status, "updated": False,
                            "capture_id": job.capture_id,
                            "error": reason}
                self._clear_pending_capture(job)
                updated = True
                if (job.capture_amount_cents is not None
                        and job.capture_amount_cents
                        >= job.quote["price_cents"]):
                    # Round 13: the listed capture already covers the
                    # whole hold — there is no residual to void.
                    # Settle CAPTURED here instead of leaving DELIVERED
                    # with a capture_id set and a residual void that
                    # can only fail (Grok round-12 improvement 3).
                    job.settlement_method = "reconciled"
                    job.status = "CAPTURED"
                    job.settled_at = int(time.time())
                    to_status = "CAPTURED"
            elif not self._has_recorded_capture(job):
                # Round 14 (Claude round-13 Low 2): PARTIALLY_CAPTURED
                # with no pending intent and no recorded capture. If
                # PayPal's read lists a COMPLETED capture it is
                # authoritative — the partial moved outside the
                # service — so record it (reconciled) and stay open on
                # the residual. If it lists nothing, the partial is
                # unauditable and no path settles the job (the capture
                # endpoints pre-flight-refuse the hold and
                # void_after_deadline needs AUTHORIZED): surface the
                # wedge so the admin summary lists it instead of a
                # silent no-op wedging it open against MAX_OPEN_JOBS.
                # The exit is the payer-only force-settle (not built).
                # Round 26 (Gemini round-25 finding 4): check the order
                # too — the authorization resource may omit a
                # COMPLETED capture the order lists. On "unread" the
                # order could not be read, so the partial stays
                # unauditable and wedges like the "none" case.
                # Round 28 (C5): on "ambiguous" a non-terminal capture
                # is listed — it does not resolve the unauditable
                # partial, so wedge with a specific reason.
                # Round 28 (G1): the listed capture goes through
                # _record_listed_capture — never a raw write.
                _outcome, cap = (
                    self._first_completed_capture_including_order(
                        job, auth))
                # Round 48 (Claude round-47 Low 4): the shared guard.
                if self._wedge_on_unattributable(
                        job, _outcome,
                        "reconcile PARTIALLY_CAPTURED arm"):
                    return {"job_id": job.job_id,
                            "from_status": from_status,
                            "to_status": job.status, "updated": False,
                            "capture_id": job.capture_id,
                            "error": job.needs_review_reason or
                                     "captures unattributable (operator review needed)"}
                if _outcome == "ambiguous":
                    self._set_review_flag(
                        job,
                        "authorization is PARTIALLY_CAPTURED and a "
                        "non-terminal capture is listed — money may "
                        "still move (operator review needed)")
                    self._save(job)
                    return {"job_id": job.job_id,
                            "from_status": from_status,
                            "to_status": job.status, "updated": False,
                            "capture_id": job.capture_id,
                            "error": "authorization is PARTIALLY_CAPTURED "
                                     "and a non-terminal capture is listed "
                                     "— operator review needed"}
                if cap is None:
                    # Round 18 (Grok round-17 finding 1): the partial is
                    # unauditable — no key can recover it — so persist
                    # the review flag. The VOIDED/EXPIRED/DENIED arm
                    # honors it: a later released-hold read cannot
                    # silently settle over money that may have moved.
                    # The return is unchanged (round 9): the webhook
                    # still ignores this as "pending", not an error.
                    self._set_review_flag(
                        job,
                        "authorization is PARTIALLY_CAPTURED but lists no "
                        "capture and none is in flight")
                    self._save(job)
                    return {"job_id": job.job_id,
                            "from_status": from_status,
                            "to_status": job.status, "updated": False,
                            "capture_id": job.capture_id,
                            "error": "authorization is PARTIALLY_CAPTURED "
                                     "but no capture is recorded and none "
                                     "is in flight — operator review needed"}
                record_outcome = self._record_listed_capture(job, cap)
                if record_outcome == "conflict":
                    reason = (f"authorization is PARTIALLY_CAPTURED and "
                              f"lists capture {cap.get('id')} conflicting "
                              f"with the recorded capture {job.capture_id} "
                              "(operator review needed)")
                    self._set_review_flag(job, reason)
                    self._save(job)
                    return {"job_id": job.job_id,
                            "from_status": from_status,
                            "to_status": job.status, "updated": False,
                            "capture_id": job.capture_id,
                            "error": reason}
                updated = True
        elif status == "CAPTURED":
            # Round 28 (G1): _record_captured_authorization routes the
            # listed capture through _record_listed_capture. On
            # "conflict" the books are untouched and the wedge stands
            # — do NOT auto-clear it, do NOT settle; force-settle is
            # the exit.
            # Round 31 (C2): what the CAPTURED status proves depends
            # on the read outcome. Only a positive "found" listing
            # that agrees with the books lets this arm book the
            # voided residual and clear a wedge — a second out-of-band
            # capture the read could not see would otherwise be
            # missed, and the receipt would claim a split (captured +
            # voided) nothing verified.
            # Round 45 (Claude round-44 Medium): the recorder must not
            # write on "ambiguous" (PENDING never settles).
            rec_result, read_outcome = self._record_captured_authorization(
                job, auth)
            if rec_result == "conflict":
                self._save(job)
                return {"job_id": job.job_id,
                        "from_status": from_status,
                        "to_status": job.status, "updated": False,
                        "capture_id": job.capture_id,
                        "error": job.needs_review_reason or
                                 "capture conflict (operator review needed)"}
            if read_outcome == "ambiguous":
                # Round 45: the authorization is CAPTURED but the only
                # listed capture is PENDING. PENDING never settles —
                # the recorder wrote nothing and the intent (if any)
                # is kept. Not a wedge: the CAPTURE.COMPLETED webhook
                # (or a later reconcile) flows through the "found" path
                # and settles. No review flag.
                # Round 46 (Claude round-45 Low 5): use a structured
                # "awaiting" flag instead of substring-matching the
                # error text (brittle).
                self._save(job)
                return {"job_id": job.job_id,
                        "from_status": from_status,
                        "to_status": job.status, "updated": False,
                        "capture_id": job.capture_id,
                        "awaiting": True,
                        "error": "authorization is CAPTURED but the "
                                 "listed capture is PENDING — awaiting "
                                 "COMPLETED; will retry"}
            if read_outcome == "unattributable":
                # Round 46 (Claude round-45 Medium): the recorder
                # wedged with an accurate reason (money moved but the
                # captures cannot be attributed). The wedge is the safe
                # state; force-settle is the exit.
                self._save(job)
                return {"job_id": job.job_id,
                        "from_status": from_status,
                        "to_status": job.status, "updated": False,
                        "capture_id": job.capture_id,
                        "error": job.needs_review_reason or
                                 "captures unattributable (operator review needed)"}
            # has_books is computed post-recorder, which is correct
            # because the recorder only writes on "found"/"unread".
            has_books = self._has_recorded_capture(job)
            if read_outcome == "found":
                # Positively listed and agreeing with the books
                # (recorded just now, or kept). The money is
                # accounted for: clear the flag and settle.
                self._clear_review_flag(job)
                job.voided_amount_cents = self._compute_voided_residual(job)
                job.settlement_method = "reconciled"
                job.status = "CAPTURED"
                job.settled_at = int(time.time())
                updated = True
                to_status = "CAPTURED"
            elif read_outcome == "unread":
                if has_books:
                    # Partial books + transient read failure: the
                    # remainder is unconfirmed. Leave the job open —
                    # a later reconcile re-reads (transient, not a
                    # wedge).
                    # Round 45 (Claude round-44 Low 2): save before
                    # the early return — the recorder may have
                    # cleared the intent above.
                    self._save(job)
                    return {"job_id": job.job_id,
                            "from_status": from_status,
                            "to_status": job.status, "updated": False,
                            "capture_id": job.capture_id,
                            "error": "authorization is CAPTURED with a "
                                     "recorded capture but the captures "
                                     "could not be enumerated — will retry"}
                # Empty books: the status proves money moved; settle
                # unauditable (existing behavior).
                self._clear_review_flag(job)
                job.voided_amount_cents = self._compute_voided_residual(job)
                job.settlement_method = "reconciled"
                job.status = "CAPTURED"
                job.settled_at = int(time.time())
                updated = True
                to_status = "CAPTURED"
            else:  # "none"
                if has_books:
                    # The read cannot enumerate what moved — wedge
                    # rather than booking a split nothing verified.
                    # The flag is NOT cleared here; a conflict wedge
                    # only leaves via force-settle.
                    reason = ("authorization is CAPTURED with a "
                              "recorded capture but the captures could "
                              "not be enumerated (operator review needed)")
                    self._set_review_flag(job, reason)
                    self._save(job)
                    return {"job_id": job.job_id,
                            "from_status": from_status,
                            "to_status": job.status, "updated": False,
                            "capture_id": job.capture_id,
                            "error": reason}
                # Empty books: unauditable settle.
                self._clear_review_flag(job)
                job.voided_amount_cents = self._compute_voided_residual(job)
                job.settlement_method = "reconciled"
                job.status = "CAPTURED"
                job.settled_at = int(time.time())
                updated = True
                to_status = "CAPTURED"
        elif _is_hold_released(status):
            # Round 13: EXPIRED settles exactly like VOIDED — an
            # expired hold never moves money, so there is nothing to
            # wait for (Claude round-12 finding 2). Round 14: DENIED
            # settles the same way — a denied authorization can never
            # move money, so there is nothing to wait for either
            # (ChatGPT round-13 improvement 2).
            if self._has_pending_capture(job):
                # Round 28 (C3): background reconcile is READ-ONLY — it
                # never POSTs. The old code replayed the pending key,
                # but a replay with an unseen key EXECUTES as a new
                # capture: a process that died after saving the intent
                # but before the POST would have its capture fired by
                # a later webhook or reconcile with no payer action,
                # while the payer was refused a different amount. Only
                # the payer's explicit retry (capture/capture_partial)
                # replays the key. Here we only check what PayPal
                # lists; a listed COMPLETED capture is recorded (via
                # the settle helper — round 28, G1, never a raw write)
                # and the spent intent cleared, then the code falls
                # through to the needs_review check and settlement
                # below. Nothing listed keeps the intent and wedges —
                # the payer's retry or force-settle is the exit.
                cap_outcome, cap = (
                    self._first_completed_capture_including_order(
                        job, auth))
                # Round 47 (Claude round-46 HIGH): the shared guard —
                # "unattributable" wedges here, never falls through to
                # the "no capture listed" branch.
                if self._wedge_on_unattributable(
                        job, cap_outcome,
                        f"reconcile released-hold arm (status {status})"):
                    return {"job_id": job.job_id,
                            "from_status": from_status,
                            "to_status": job.status, "updated": False,
                            "capture_id": job.capture_id,
                            "error": job.needs_review_reason or
                                     "captures unattributable (operator review needed)"}
                if cap_outcome == "unread":
                    self._set_review_flag(
                        job,
                        f"authorization is {status} with a pending "
                        "capture key and the order could not be read "
                        "(operator review needed)")
                    self._save(job)
                    return {"job_id": job.job_id,
                            "from_status": from_status,
                            "to_status": job.status, "updated": False,
                            "capture_id": job.capture_id,
                            "error": f"authorization is {status} with a "
                                     "pending capture key and the order "
                                     "could not be read (operator review "
                                     "needed)"}
                if cap_outcome == "ambiguous":
                    # Round 28 (C5): a non-terminal capture is listed —
                    # money may still move. Wedge, never settle.
                    reason = (f"authorization is {status} with a pending "
                              "capture key and a non-terminal capture is "
                              "listed (operator review needed)")
                    self._set_review_flag(job, reason)
                    self._save(job)
                    return {"job_id": job.job_id,
                            "from_status": from_status,
                            "to_status": job.status, "updated": False,
                            "capture_id": job.capture_id,
                            "error": reason}
                if cap is None:
                    # No capture listed. The key may have executed with
                    # the capture unlisted, or never executed — on a
                    # released hold that is ambiguous, so the intent is
                    # kept and the job wedges. Round 28 (C1): the
                    # review flag IS set here. The old code returned a
                    # bare error, leaving a dead end: force_settle
                    # requires needs_review, so the wedge could never
                    # be reached. The payer's force-settle (which
                    # abandons a dead intent on a released hold with
                    # no capture listed) or retry is the exit.
                    attempted = job.pending_capture_amount_cents
                    reason = (f"authorization is {status} with a pending "
                              "capture key and no capture listed — the "
                              "key is kept (operator review needed)")
                    if attempted is not None:
                        reason += f" (attempted ${attempted/100:.2f})"
                    self._set_review_flag(job, reason, attempted)
                    self._save(job)
                    return {"job_id": job.job_id,
                            "from_status": from_status,
                            "to_status": job.status, "updated": False,
                            "capture_id": job.capture_id,
                            "error": reason}
                # A COMPLETED capture is listed: record it through the
                # helper (G1) and clear the spent intent, then fall
                # through to the needs_review check and the
                # recorded-capture settlement below.
                record_outcome = self._record_listed_capture(job, cap)
                if record_outcome == "conflict":
                    reason = (f"authorization is {status} with a pending "
                              f"capture key and lists capture "
                              f"{cap.get('id')} conflicting with the "
                              f"recorded capture {job.capture_id} "
                              "(operator review needed)")
                    self._set_review_flag(job, reason)
                    self._save(job)
                    return {"job_id": job.job_id,
                            "from_status": from_status,
                            "to_status": job.status, "updated": False,
                            "capture_id": job.capture_id,
                            "error": reason}
                self._clear_pending_capture(job)
            if job.needs_review:
                # Round 45 (Claude round-44 Low 2): save before the
                # early return — the recorder and the intent-clear
                # above mutated the job, and a copy-on-read store
                # would lose them.
                self._save(job)
                # Round 26 (Gemini round-25 finding 3): a wedged job
                # NEVER auto-settles in reconcile, regardless of
                # capture_id. The old guard (`needs_review and not
                # capture_id`) let a wedged conflict — review flag set
                # AND a capture recorded — fall through to the
                # CAPTURED auto-settle below, clearing the wedge
                # without a human decision. The wedge is the safe
                # state; the payer-only force-settle is the exit.
                # Round 19 (Grok round-18 finding 3): the display
                # amount lives in needs_review_amount_cents, not the
                # live intent.
                amount_note = (
                    f" (attempted "
                    f"${job.needs_review_amount_cents/100:.2f})"
                    if job.needs_review_amount_cents is not None
                    else "")
                return {"job_id": job.job_id,
                        "from_status": from_status,
                        "to_status": job.status, "updated": False,
                        "capture_id": job.capture_id,
                        "error": f"authorization is {status} but the "
                                 "job needs operator review — use "
                                 "force-settle" + amount_note}
            if not self._has_recorded_capture(job):
                # Round 20 (Grok round-19 finding 2): no pending key,
                # Round 27 (Gemini round-26 finding 4): also require
                # no recorded amount — an unauditable capture
                # (capture_id "" with capture_amount_cents set) is
                # money on the books. A transient order failure must
                # not wedge it; it settles CAPTURED from the books
                # below.
                # no flag — but the read may still list a COMPLETED
                # capture (an out-of-band capture; the authorization
                # resource or the order). Falling through would book a
                # clean VOIDED over moved money. Record it and fall
                # through to the CAPTURED settlement below instead.
                # Round 21: tri-state — an unread order wedges instead
                # of settling VOIDED on an unknown.
                outcome, cap = self._first_completed_capture_including_order(
                    job, auth)
                # Round 47 (Claude round-46 HIGH): the shared guard —
                # "unattributable" wedges here, never falls through to
                # book VOIDED over moved money.
                if self._wedge_on_unattributable(
                        job, outcome,
                        f"reconcile released-hold arm (status {status}, no intent)"):
                    return {"job_id": job.job_id,
                            "from_status": from_status,
                            "to_status": job.status, "updated": False,
                            "capture_id": job.capture_id,
                            "error": job.needs_review_reason or
                                     "captures unattributable (operator review needed)"}
                if outcome in ("unread", "order_unread"):
                    # Round 53 (Gemini round-52 High 1): order_unread
                    # wedges here too — an auth-only capture with an
                    # unread order cannot support residual math.
                    self._set_review_flag(
                        job,
                        f"authorization is {status} with no capture "
                        "listed and the order could not be read "
                        "(operator review needed)")
                    self._save(job)
                    return {"job_id": job.job_id,
                            "from_status": from_status,
                            "to_status": job.status, "updated": False,
                            "capture_id": job.capture_id,
                            "error": f"authorization is {status} with no "
                                     "capture listed and the order could "
                                     "not be read (operator review needed)"}
                if outcome == "ambiguous":
                    # Round 32 (Gemini round-31 finding 1), updated
                    # round 36: a non-terminal capture (PENDING or an
                    # unrecognized status — REFUNDED and
                    # PARTIALLY_REFUNDED are terminal since round 35
                    # and never reach this branch) is listed — money
                    # may still move. A PENDING capture must NEVER
                    # settle funds. Wedge for operator review instead
                    # of treating the pending capture dict as a
                    # completed one.
                    reason = (f"authorization is {status} but a "
                              "non-terminal capture is listed "
                              "(operator review needed)")
                    self._set_review_flag(job, reason)
                    self._save(job)
                    return {"job_id": job.job_id,
                            "from_status": from_status,
                            "to_status": job.status, "updated": False,
                            "capture_id": job.capture_id,
                            "error": reason}
                if cap is not None:
                    # Round 22 (Grok round-21 finding 5): settle
                    # through the single settle helper and return via
                    # the tail — no fall-through to the
                    # `if job.capture_id` check below, which would
                    # overwrite the helper's CAPTURED with VOIDED when
                    # the found capture has a falsy id ("").
                    # Round 28 (G1): the helper routes through
                    # _record_listed_capture. On "conflict" the books
                    # are untouched — wedge here instead of settling;
                    # the wedge is the safe state, force-settle is
                    # the exit. Reconcile returns dicts, never raises.
                    settle_outcome = self._settle_captured_with_residual(
                        job, cap)
                    if settle_outcome == "conflict":
                        reason = (f"authorization is {status} and lists "
                                  f"capture {cap.get('id')} conflicting "
                                  f"with the recorded capture "
                                  f"{job.capture_id} (operator review "
                                  "needed)")
                        self._set_review_flag(job, reason)
                        self._save(job)
                        return {"job_id": job.job_id,
                                "from_status": from_status,
                                "to_status": job.status, "updated": False,
                                "capture_id": job.capture_id,
                                "error": reason}
                    updated = True
                    to_status = "CAPTURED"
                    job.settled_at = int(time.time())
                    # Round 32 (Gemini round-31 finding 5): the capture
                    # was found via read, not POSTed.
                    job.settlement_method = "reconciled"
                    self._record_reconcile_read(job, auth)
                    return {"job_id": job.job_id,
                            "from_status": from_status,
                            "to_status": to_status, "updated": updated,
                            "capture_id": job.capture_id, "error": ""}
            if self._has_recorded_capture(job):
                # The capture moved — recorded by an earlier
                # partial-capture call whose residual void applied out
                # of band, or recovered above. The settlement is a
                # release, not a void — money moved — so the job is
                # recorded CAPTURED with the voided residual, and the
                # receipt shows the split instead of claiming "voided"
                # for a job that paid its agent.
                # Round 50 (Claude round-49 Low 5): re-enumerate before
                # booking the residual — an out-of-band capture B that
                # landed after the recorded capture A would otherwise
                # be missed, and the residual (hold - A) would be
                # wrong. Wedge on any mismatch.
                re_outcome, re_cap = (
                    self._first_completed_capture_including_order(
                        job, auth))
                if self._wedge_on_unattributable(
                        job, re_outcome,
                        "reconcile released-hold residual check"):
                    return {"job_id": job.job_id,
                            "from_status": from_status,
                            "to_status": job.status, "updated": False,
                            "capture_id": job.capture_id,
                            "error": job.needs_review_reason or
                                     "captures unattributable (operator review needed)"}
                # Round 51 (Grok round-50 Medium 3): an unread order
                # read must NOT be treated as "no other capture" —
                # capture B exists only on the order when PayPal omits
                # it from the authorization. Wedge instead of booking
                # the residual.
                if re_outcome in ("unread", "ambiguous", "order_unread"):
                    reason = (
                        f"authorization is {status} with recorded "
                        f"capture {job.capture_id} but the re-read "
                        f"returned {re_outcome} — cannot confirm no "
                        "other capture moved (operator review needed)")
                    self._set_review_flag(job, reason)
                    self._save(job)
                    return {"job_id": job.job_id,
                            "from_status": from_status,
                            "to_status": job.status, "updated": False,
                            "capture_id": job.capture_id,
                            "error": reason}
                if re_cap is not None:
                    # A capture is listed — it must match the books,
                    # or wedge. (The no-intent branch above already
                    # handled the "found" case; this is the
                    # recorded-capture branch, so a listed capture
                    # here is either the same one or an out-of-band B.)
                    listed_id = re_cap.get("id") or ""
                    listed_amt = self._listed_capture_amount_cents(re_cap)
                    if (listed_id != job.capture_id
                            or listed_amt != job.capture_amount_cents):
                        reason = (
                            f"authorization is {status} with recorded "
                            f"capture {job.capture_id} but lists a "
                            f"different capture {listed_id} (operator "
                            "review needed)")
                        self._set_review_flag(job, reason)
                        self._save(job)
                        return {"job_id": job.job_id,
                                "from_status": from_status,
                                "to_status": job.status, "updated": False,
                                "capture_id": job.capture_id,
                                "error": reason}
                # Round 20 (Grok round-19 finding 3): the flag clears
                # on every settle — a capture on the books resolves
                # the unauditable-partial concern.
                # Round 22 (Grok round-21 finding 5): the amount check
                # is load-bearing — a capture recorded with a falsy id
                # ("") still moved money, and keying only on the id
                # would book VOIDED over it. Found captures always
                # settle through _settle_captured_with_residual above
                # and return via the tail; this branch is only for
                # captures recorded by earlier calls.
                self._clear_review_flag(job)
                job.voided_amount_cents = self._compute_voided_residual(job)
                job.settlement_method = "reconciled"
                job.status = "CAPTURED"
                to_status = "CAPTURED"
            else:
                # Round 31 (C5): if the wedge reason records a
                # PARTIALLY_CAPTURED observation, part of the hold
                # moved — booking VOIDED would erase the only record
                # of it. Settle as unauditable CAPTURED instead: the
                # status proves money moved but the amount is
                # unknowable.
                _reason = job.needs_review_reason or ""
                if "PARTIALLY_CAPTURED" in _reason:
                    self._mark_unaudited(job)
                    job.voided_amount_cents = None
                    job.settlement_method = "reconciled"
                    job.status = "CAPTURED"
                    to_status = "CAPTURED"
                else:
                    job.status = "VOIDED"
                    to_status = "VOIDED"
            job.settled_at = int(time.time())
            updated = True
        else:
            return {"job_id": job.job_id, "from_status": from_status,
                    "to_status": job.status, "updated": False,
                    "capture_id": job.capture_id,
                    "error": f"unexpected authorization status {status}"}
        # One tail for every branch: the successful read is recorded
        # and persisted here, so no branch can forget to save it.
        self._record_reconcile_read(job, auth)
        return {"job_id": job.job_id, "from_status": from_status,
                "to_status": to_status, "updated": updated,
                "capture_id": job.capture_id, "error": ""}

    def sweep_expired_quotes(self) -> int:
        removed = 0
        for job in self.store.all():
            if job.status == "QUOTED" and quote_expired(job.quote):
                try:
                    if self._sweep_one_expired_quote(job):
                        removed += 1
                except EscrowError as e:
                    # An expected wedge during the adopt-and-reconcile
                    # (e.g. unread/ambiguous) — the job was already
                    # saved in its wedged state before the raise; move
                    # on to the next job.
                    logger.warning(
                        "sweep_expired_quotes wedged job %s: %s",
                        job.job_id, e)
                except Exception:
                    # Round 36 (Claude round-35 suggestion): one job's
                    # unexpected failure must not end the sweep or skip
                    # the lock pruning below. Logged; the job is left
                    # for the next sweep.
                    logger.exception(
                        "sweep_expired_quotes failed on job %s",
                        job.job_id)
        self._prune_settled_locks()
        return removed

    def _sweep_one_expired_quote(self, job: Job) -> bool:
        """Sweep a single expired QUOTED job. Returns True when the job
        was deleted. Round 36: extracted from sweep_expired_quotes so
        one job's failure is isolated (Claude round-35 suggestion)."""
        with self._delivery_lock(job.job_id):
            fresh = self.store.get(job.job_id)
            if (fresh is None or fresh.status != "QUOTED"
                    or not quote_expired(fresh.quote)):
                return False
            if not fresh.authorize_attempted:
                self.store.delete(fresh.job_id)
                return True
            outcome, auth = self._read_order_authorization(fresh)
            if outcome == "adopted":
                auth_id = auth.get("id")
                if not (isinstance(auth_id, str) and auth_id):
                    return False
                status = auth.get("status")
                if _is_hold_released(status):
                    cap_outcome, cap = (
                        self._first_completed_capture_including_order(
                            fresh, auth))
                    if cap_outcome == "none":
                        # Released hold, no capture listed anywhere:
                        # nothing moved, the quote is expired, the
                        # hold is dead — safe to delete.
                        self.store.delete(fresh.job_id)
                        return True
                    if cap_outcome == "unread":
                        # Transient order read failure — keep the job
                        # for a later sweep to retry reading.
                        return False
                    # Round 47 (Claude round-46 Low): "ambiguous" or
                    # "unattributable" — adopt as AUTHORIZED but set
                    # the review flag so the operator is told money
                    # may have moved.
                    if cap_outcome in ("ambiguous", "unattributable"):
                        self._set_review_flag(
                            fresh,
                            f"sweep adopted authorization {auth_id} with "
                            f"{cap_outcome} captures listed (operator "
                            "review needed)")
                # Hold is active, or money moved/uncertain: adopt as
                # AUTHORIZED so reconcile and force_settle can manage
                # it — never leave a job with an adopted authorization
                # in QUOTED state.
                fresh.authorization_id = auth_id
                fresh.status = "AUTHORIZED"
                self._record_paypal_response(
                    fresh, operation="authorize",
                    status=status, id_=auth_id, body=auth)
                self._save(fresh)
                if status not in ("CREATED", "PENDING"):
                    self._reconcile_with_auth(fresh, auth, "AUTHORIZED")
            elif outcome == "empty":
                self.store.delete(fresh.job_id)
                return True
            # "unread": keep the job; the next sweep retries.
            return False

    def reconcile_open_jobs(self) -> dict:
        """Walk every AUTHORIZED/DELIVERED job and reconcile it from
        PayPal — the admin endpoint's workhorse, for use after a
        prolonged outage. Returns a small summary: {"checked",
        "updated" (list of {job_id, from, to, capture_id}),
        "still_pending" (job ids PayPal still reports CREATED, or
        whose state could not be read), "errors" (list of
        {job_id, error}), "needs_review" (round 20: wedged jobs as
        {job_id, reason} — the operator's work list, separate from
        errors so a wedge never hides inside an error string)}. One
        job's failure never stops the rest."""
        checked = 0
        updated: list[dict] = []
        still_pending: list[str] = []
        errors: list[dict] = []
        needs_review: list[dict] = []
        for job in self.store.all():
            if job.status not in ("AUTHORIZED", "DELIVERED"):
                continue
            checked += 1
            result = self.reconcile_job(job)
            # Round 20: a wedge is a state, not an error — list it on
            # its own, with the persisted reason, whether this pass
            # just wedged it or it was already wedged. reconcile_job
            # re-reads the job inside the lock, so re-fetch here —
            # the loop's copy is stale.
            fresh = self.store.get(job.job_id)
            if fresh is not None and fresh.needs_review:
                needs_review.append({"job_id": fresh.job_id,
                                     "reason": fresh.needs_review_reason})
            if result["updated"]:
                updated.append({"job_id": job.job_id,
                                "from": result["from_status"],
                                "to": result["to_status"],
                                "capture_id": result["capture_id"]})
            else:
                still_pending.append(job.job_id)
                if result["error"]:
                    errors.append({"job_id": job.job_id,
                                   "error": result["error"]})
        return {"checked": checked, "updated": updated,
                "still_pending": still_pending, "errors": errors,
                "needs_review": needs_review}

    @staticmethod
    def _report_capture_amount_cents(cap: dict) -> int | None:
        """Amount of one PayPal-listed capture in cents, or None when
        the amount is missing/unparseable (never 0 — an unknown amount
        must not compare equal to a zero one)."""
        try:
            amount = (cap.get("amount") or {})
            value = amount.get("value")
            if value is None:
                return None
            return amount_to_cents(value)
        except (ValueError, TypeError, AttributeError):
            return None

    def _report_listed_captures(self, job: Job, auth: dict) -> tuple[dict, list]:
        """Union the captures PayPal reports for a job: the
        authorization resource plus the order resource (which may list
        a capture the authorization omits). Returns (captures, issues)
        where captures maps id -> (status, amount_cents|None). A failed
        order read is an issue, not a fatal error — the auth listing
        still stands."""
        captures: dict[str, tuple[str, int | None]] = {}
        issues: list[str] = []

        def _add(caps):
            for cap in caps or []:
                cid = cap.get("id")
                if not cid or cid in captures:
                    continue
                captures[cid] = (cap.get("status"),
                                 self._report_capture_amount_cents(cap))

        _add(auth.get("captures"))
        if job.order_id:
            try:
                order = self.paypal.get_order(job.order_id)
            except PayPalError as e:
                issues.append(f"order_read_failed: {e}")
                order = None
            except AttributeError:
                # A PayPal client without get_order (older stub): the
                # auth listing is all we have.
                order = None
            if order:
                for pu in order.get("purchase_units") or []:
                    payments = pu.get("payments") or {}
                    _add(payments.get("captures"))
        return captures, issues

    def ledger_reconciliation_report(
            self, job_id: str | None = None) -> dict:
        """Compare the books against fresh PayPal reads (v8.2,
        production-readiness item 6).

        For one job (job_id) or every job with an authorization id:
        GET the authorization fresh from PayPal (plus the order, which
        may list a capture the authorization resource omits), union
        the COMPLETED captures PayPal reports, and compare against
        the booked capture ids/amounts and the job status.

        Read-only: never writes jobs or audit events. Returns
        {"generated_at", "jobs_checked", "divergent_count", "entries"},
        where each entry is {"job_id", "book_status", "paypal_status",
        "book_capture_ids", "paypal_capture_ids", "book_amount_cents",
        "paypal_amount_cents", "divergent", "issues"}. A failed PayPal
        read is an issue, not a divergence — absence of evidence is
        not evidence of divergence. A PARTIALLY_CAPTURED authorization
        lists no captures by PayPal's own reporting, so the
        book-not-listed check is skipped when the enumeration cannot
        be completed from the order either.
        """
        if job_id is not None:
            jobs = [self.store.get(job_id)]
            jobs = [j for j in jobs if j is not None]
        else:
            jobs = self.store.all()
        entries = []
        for job in jobs:
            if not job.authorization_id:
                continue
            entry = {
                "job_id": job.job_id,
                "book_status": job.status,
                "book_capture_ids": sorted(job.capture_ids or []),
                "book_amount_cents": job.capture_amount_cents,
                "paypal_status": None,
                "paypal_capture_ids": [],
                "paypal_amount_cents": None,
                "divergent": False,
                "issues": [],
            }
            try:
                auth = self.paypal.get_authorization(job.authorization_id)
            except PayPalError as e:
                entry["issues"].append(f"paypal_read_failed: {e}")
                entries.append(entry)
                continue
            paypal_status = auth.get("status")
            entry["paypal_status"] = paypal_status
            listed, order_issues = self._report_listed_captures(job, auth)
            entry["issues"].extend(order_issues)
            completed = {cid: amt for cid, (st, amt) in listed.items()
                         if st == "COMPLETED"}
            entry["paypal_capture_ids"] = sorted(completed)
            if completed and all(a is not None for a in completed.values()):
                entry["paypal_amount_cents"] = sum(completed.values())

            # The enumeration is incomplete when PayPal reports
            # PARTIALLY_CAPTURED (the auth resource lists no captures
            # by PayPal's own reporting) and the order did not supply
            # them either — id/amount comparisons would be spurious.
            enumeration_complete = not (
                paypal_status == "PARTIALLY_CAPTURED" and not completed)

            book_ids = set(entry["book_capture_ids"])
            pp_ids = set(entry["paypal_capture_ids"])
            if enumeration_complete:
                for cid in sorted(book_ids - pp_ids):
                    entry["issues"].append(
                        f"book_capture_not_listed_by_paypal: {cid}")
                if (entry["paypal_amount_cents"] is not None
                        and entry["book_amount_cents"] is not None
                        and entry["paypal_amount_cents"]
                        != entry["book_amount_cents"]):
                    entry["issues"].append(
                        "amount_mismatch: books="
                        f"{entry['book_amount_cents']} paypal="
                        f"{entry['paypal_amount_cents']}")
            for cid in sorted(pp_ids - book_ids):
                entry["issues"].append(
                    f"paypal_capture_not_on_books: {cid}")

            if paypal_status == "CAPTURED" and job.status != "CAPTURED":
                entry["issues"].append(
                    f"status_mismatch: paypal=CAPTURED books={job.status}")
            elif (paypal_status in ("VOIDED", "EXPIRED", "DENIED")
                    and job.status != "VOIDED"):
                entry["issues"].append(
                    f"status_mismatch: paypal={paypal_status} "
                    f"books={job.status}")
            elif (paypal_status == "PARTIALLY_CAPTURED"
                    and not book_ids
                    and job.capture_amount_cents is None):
                entry["issues"].append(
                    "paypal_partially_captured_but_books_show_no_capture")
            elif (paypal_status == "CREATED"
                    and job.status in ("CAPTURED", "VOIDED")):
                entry["issues"].append(
                    f"status_mismatch: paypal=CREATED books={job.status}")

            entry["divergent"] = bool(entry["issues"])
            entries.append(entry)
        return {"generated_at": int(time.time()),
                "jobs_checked": len(entries),
                "divergent_count": sum(1 for e in entries if e["divergent"]),
                "entries": entries}

    # Phase 4 (property-testing game plan): scheduled reconciliation.
    # ledger_reconciliation_report() compares the books against fresh
    # PayPal reads but is read-only and unscheduled. This method is the
    # nightly runner's core: it persists the run + findings, and alerts
    # only on NEW divergences — deduped against open findings with the
    # same (job_id, issue_type), so a repeat offender does not page
    # twice. A failed PayPal read is recorded as a finding but is NOT
    # a divergence: absence of evidence is not evidence of divergence.
    #
    # Returns a summary dict; the run record itself lives in the store.

    RECON_ALERT_SEVERITY = {
        "book_capture_not_listed_by_paypal": "high",
        "amount_mismatch": "high",
        "paypal_capture_not_on_books": "high",
        "status_mismatch": "high",
        "paypal_partially_captured_but_books_show_no_capture": "medium",
        "paypal_read_failed": "medium",
    }

    def run_scheduled_reconciliation(self, *, alert_log_path=None,
                                     alert_url=None) -> dict:
        import os
        import uuid
        now = int(self._now())
        run_id = uuid.uuid4().hex
        if alert_log_path is None:
            alert_log_path = os.environ.get(
                "RECONCILE_ALERT_LOG", "reconciliation_alerts.jsonl")
        if alert_url is None:
            alert_url = os.environ.get("RECONCILE_ALERT_URL")

        report = self.ledger_reconciliation_report()
        entries = report.get("entries", [])
        new_findings = []
        alerts = []
        read_failures = 0
        divergent_entries = 0
        for entry in entries:
            job_id = entry["job_id"]
            issues = entry.get("issues", [])
            entry_divergent = False
            for issue in issues:
                issue_type = issue.split(":", 1)[0].strip()
                is_read_failure = (issue_type == "paypal_read_failed")
                if is_read_failure:
                    read_failures += 1
                else:
                    entry_divergent = True
                # Alert dedupe: an open finding for the same
                # (job_id, issue_type) means this is already known.
                if self.store.find_open_finding(job_id, issue_type):
                    continue
                finding = {
                    "finding_id": uuid.uuid4().hex,
                    "run_id": run_id,
                    "job_id": job_id,
                    "issue_type": issue_type,
                    "detail": issue,
                    "first_seen": now,
                    "status": "open",
                    "resolved_at": None,
                    "note": None,
                }
                self.store.record_reconciliation_finding(finding)
                new_findings.append(finding)
                alerts.append({
                    "ts": now,
                    "run_id": run_id,
                    "job_id": job_id,
                    "issue_type": issue_type,
                    "detail": issue,
                    "severity": self.RECON_ALERT_SEVERITY.get(
                        issue_type, "low"),
                })
            if entry_divergent:
                divergent_entries += 1

        if divergent_entries > 0:
            status = "divergent"
        elif read_failures > 0:
            status = "error"
        else:
            status = "ok"
        finished_at = int(self._now())
        self.store.record_reconciliation_run({
            "run_id": run_id,
            "started_at": now,
            "finished_at": finished_at,
            "jobs_checked": report.get("jobs_checked", 0),
            "divergent_count": divergent_entries,
            "status": status,
        })

        alerts_sent = 0
        if alerts:
            alerts_sent = self._emit_recon_alerts(
                alerts, alert_log_path=alert_log_path, alert_url=alert_url)
        return {"run_id": run_id, "status": status,
                "jobs_checked": report.get("jobs_checked", 0),
                "divergent_count": divergent_entries,
                "read_failures": read_failures,
                "new_findings": len(new_findings),
                "alerts_sent": alerts_sent,
                "alert_log": str(alert_log_path)}

    def _emit_recon_alerts(self, alerts, *, alert_log_path,
                           alert_url) -> int:
        """Append alerts to the JSONL alert log and, best-effort, POST
        them to the configured webhook URL. Returns the number of
        alerts written to the log. Never raises — alerting must not
        break the reconciliation run."""
        import json
        import urllib.request
        sent = 0
        try:
            with open(alert_log_path, "a") as f:
                for alert in alerts:
                    f.write(json.dumps(alert) + "\n")
                    sent += 1
        except OSError:
            return 0
        if alert_url:
            try:
                body = json.dumps(
                    {"alerts": alerts}).encode("utf-8")
                req = urllib.request.Request(
                    alert_url, data=body,
                    headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=10).read()
            except Exception:
                pass
        return sent

    def _job_for_authorization(self, authorization_id: str) -> Job | None:
        """The job whose hold is this authorization id, or None. Jobs
        are keyed by job id, so this scans — the same scan
        reconcile_open_jobs already does, and the job count is small."""
        for job in self.store.all():
            if job.authorization_id == authorization_id:
                return job
        return None

    # -- webhooks ------------------------------------------------------------
    def find_job_by_resource_id(self, resource_id: str) -> Job | None:
        """Find the job for a PayPal resource id (authorization or capture).

        Used by app.webhooks.handle_webhook for the cheap job lookup
        before signature verification (Fix 1, new reviewer).
        """
        # Try authorization id first
        job = self._job_for_authorization(resource_id)
        if job is not None:
            return job
        # Try capture id
        for j in self.store.all():
            if j.capture_id == resource_id or resource_id in (j.capture_ids or []):
                return j
        return None

    def job_is_settled(self, job: Job) -> bool:
        """Whether the job is in a terminal state (no further action needed).

        Used by app.webhooks.handle_webhook for the settled check.
        """
        return job.status in ("CAPTURED", "VOIDED", "SETTLED")

    def handle_webhook_event(self, event: dict) -> dict:
        """Handle one verified PayPal webhook event (round 7). The
        endpoint verifies the signature first — this method trusts
        nothing in the event body alone: every event that names an
        authorization is decided by a fresh get_authorization read
        taken inside the per-job lock, and any state change goes
        through _reconcile_with_auth with that read — the same branch
        logic the admin endpoint uses (via reconcile_job). There is
        no second capture-recording path. ``event`` must be a dict
        (the endpoint enforces it with a 400 otherwise).

        Outcomes ({"event_id", "event_type", "job_id", "action",
        "error"}):
          confirmed       — AUTHORIZATION.CREATED on an open job whose
                            hold PayPal still reports CREATED. No state
                            change and no audit-record overwrite: the
                            authorize record already says the hold was
                            placed, and the processed-event record
                            proves the event arrived.
          reconciled      — the read said the money moved (or the hold
                            was released) and reconcile_job recorded it.
          denial_recorded — CAPTURE.DENIED on a job whose hold PayPal
                            still reports CREATED: the denial is
                            recorded on the job in its own field
                            (job.webhook_denial) and the job stays open;
                            the authorize record is left untouched.
          ignored         — unknown event type, a job that is already
                            settled, or an event whose claim PayPal's
                            read contradicts without settling anything.
          unknown         — the event names no authorization id, or no
                            job carries it. Logged, left pending; the
                            admin reconcile endpoint remains the
                            fallback for those.
          duplicate       — this event id was already processed
                            (dedupe record in the store, durable across
                            restarts). A replayed delivery is a no-op.
          pending         — the event could not be decided (PayPal's
                            state could not be read, or the job is not
                            yet authorized). Left for a later
                            re-delivery or the admin endpoint.
          rejected        — the event has no usable id; nothing was done.
        Every outcome except a genuinely unreadable PayPal state is
        definitive and the event id is marked processed — a replay
        then returns "duplicate" instead of re-deciding. Only a
        transport failure or 5xx leaves the event unmarked, so a
        later re-delivery can still settle it.
        """
        event_id = event.get("id")
        event_type = event.get("event_type")
        summary = {"event_id": event_id if isinstance(event_id, str) else "",
                   "event_type": event_type if isinstance(event_type, str) else "",
                   "job_id": "", "action": "", "error": ""}
        if not isinstance(event_id, str) or not event_id:
            summary.update(action="rejected",
                           error="webhook event id is required")
            return summary
        # v8.2 (production-readiness item 3): atomic webhook dedupe.
        # The claim is a single conditional INSERT — two concurrent
        # deliveries of the same event id cannot both win. A completed
        # claim is never reprocessed; a stale (crashed) or failed claim
        # can be retried.
        claim = self.store.claim_webhook_event(event_id)
        if claim == "duplicate":
            summary.update(action="duplicate")
            return summary
        if claim == "in_progress":
            # Another worker holds a fresh claim — it will complete or
            # go stale. Don't process; PayPal will re-deliver if needed.
            summary.update(action="duplicate",
                           error="event is being processed by another worker")
            return summary
        # claim is "won" or "retry" — we must process and settle.
        # Marked completed/failed at the tail, unless PayPal's state could
        # genuinely not be read (see _handle_webhook_for_job).
        definitive = True
        if event_type not in WEBHOOK_EVENT_TYPES:
            logger.info("webhook event %s: unknown type %s, ignored",
                        event_id, event_type)
            summary.update(action="ignored")
        else:
            auth_id = _authorization_id_from_event(event)
            if not auth_id:
                logger.warning("webhook event %s (%s): no authorization "
                               "id in event resource, left pending",
                               event_id, event_type)
                summary.update(action="unknown",
                               error="no authorization id in event resource")
            else:
                job = self._job_for_authorization(auth_id)
                if job is None:
                    logger.warning("webhook event %s (%s): no job for "
                                   "authorization %s, left pending",
                                   event_id, event_type, auth_id)
                    summary.update(action="unknown",
                                   error=f"no job for authorization {auth_id}")
                elif job.status in ("CAPTURED", "VOIDED"):
                    summary.update(job_id=job.job_id, action="ignored",
                                   error="job already settled")
                else:
                    summary.update(job_id=job.job_id)
                    updates, definitive = self._handle_webhook_for_job(
                        event, job, auth_id)
                    summary.update(updates)
        if definitive:
            # v8.2 (item 3): settle the atomic claim. Completed events
            # are never reprocessed; only a transport failure or 5xx
            # leaves the claim unsettled (failed), so a later
            # re-delivery can still settle it.
            self.store.settle_webhook_event(event_id, completed=True)
            # Keep the legacy mark for backward compatibility with
            # readers of webhook_events.
            self.store.mark_webhook_event(event_id)
        else:
            self.store.settle_webhook_event(event_id, completed=False)
        return summary

    def _handle_webhook_for_job(self, event: dict, job: Job,
                                auth_id: str) -> tuple[dict, bool]:
        """Decide one known-type webhook event for one open job.
        Returns ({action, error, ...updates}, definitive): definitive
        is False only when PayPal's authorization state could
        genuinely not be read (transport failure / 5xx), so the event
        must NOT be marked processed — a later re-delivery can still
        settle it. Every other outcome is definitive.

        Round 13: the authorization is NEVER read once outside the
        lock and passed in. The job is re-read and the authorization
        is fetched INSIDE the per-job lock, so a stale read can
        never clear a live capture intent (Claude round-12 finding
        1), and the DENIED mutation cannot save a stale DELIVERED
        copy over a capture that just settled (Grok round-12
        improvement 2)."""
        event_type = event["event_type"]
        with self._delivery_lock(job.job_id):
            # Round 13: re-read inside the lock — the passed Job may
            # be the pre-lock object handle_webhook_event scanned.
            fresh = self.store.get(job.job_id)
            if fresh is None:
                return ({"action": "unknown",
                         "error": f"job {job.job_id} not found"}, True)
            job = fresh
            try:
                auth = self.paypal.get_authorization(auth_id)
            except PayPalError as e:
                logger.warning("webhook event %s (%s): could not read "
                               "authorization %s: %s", event.get("id"),
                               event_type, auth_id, e)
                return ({"action": "pending", "error": str(e)},
                        not _is_transient_paypal_error(e))
            status = auth.get("status")
            if event_type == "PAYMENT.AUTHORIZATION.CREATED":
                if status == "CREATED":
                    if job.status in ("AUTHORIZED", "DELIVERED"):
                        # Confirmed, nothing to change: the authorize
                        # record already says the hold was placed, and
                        # overwriting it with a restatement would lose
                        # the better audit record.
                        return ({"action": "confirmed", "error": ""}, True)
                    return ({"action": "pending",
                             "error": "job not yet authorized"}, True)
                # The read contradicts the event — trust the read.
                return self._webhook_reconcile(job, auth)
            if event_type == "PAYMENT.CAPTURE.DENIED":
                if status == "CREATED":
                    # New information, worth recording: a capture was
                    # attempted and denied, and the hold is still there.
                    # The job stays open — the denial settles nothing.
                    # Round 10: the denial lives in its own field
                    # (job.webhook_denial). Before this, the branch
                    # overwrote last_paypal_response with the whole event
                    # body — the same mistake the CREATED branch avoids —
                    # destroying the authorize record it replaced.
                    resource = event.get("resource") or {}
                    resource_id = resource.get("id")
                    job.webhook_denial = {
                        "event_id": event.get("id"),
                        "resource_id": (resource_id
                                       if isinstance(resource_id, str) else ""),
                        "status": "DENIED",
                    }
                    # Round 16 (Claude round-15 finding 3): persist the
                    # denial NOW. The delegation below has early
                    # returns that skip the reconcile tail's save, and
                    # in the definitive ones the event is already
                    # marked processed — a restart would lose the
                    # denial forever. (The abandon paths save anyway;
                    # this covers the ones that don't.)
                    self._save(job)
                    if not self._has_pending_capture(job):
                        return ({"action": "denial_recorded", "error": ""},
                                True)
                    # Round 15 (Grok round-14 finding 1): a CREATED
                    # read does not prove WHICH key PayPal declined —
                    # this event may be a late arrival for an older
                    # attempt while a newer key is live (its POST
                    # response lost). Retiring the stored key from the
                    # event dropped live keys, and a retry then minted
                    # a second key for a capture already executing —
                    # two live keys, two captures. So the stored key
                    # is never retired from the event: the read goes
                    # through _webhook_reconcile, whose CREATED probe
                    # decides the stored key's fate — abandon on
                    # DECLINED/FAILED, record on COMPLETED, keep on
                    # PENDING, unmarked on transport failure. The
                    # denial record above is saved by the reconcile
                    # tail along with the read.
                    action, definitive = self._webhook_reconcile(job, auth)
                    if (action["action"] == "ignored"
                            and not action["error"]):
                        # The event's news was the denial itself
                        # (recorded above); the read found nothing
                        # else to do.
                        action = {"action": "denial_recorded", "error": ""}
                    return (action, definitive)
                # The read contradicts the event — trust the read.
                return self._webhook_reconcile(job, auth)
            # PAYMENT.CAPTURE.COMPLETED / PAYMENT.AUTHORIZATION.VOIDED:
            # the read decides; the event is only the trigger.
            return self._webhook_reconcile(job, auth)

    def _webhook_reconcile(self, job: Job, auth: dict) -> tuple[dict, bool]:
        """Reconcile from the handler's in-lock authorization read and
        translate the result into a webhook outcome. Returns
        ({action, error}, definitive) — definitive except when the
        pending-key probe itself failed in transport: the read
        is the caller's fresh in-lock fetch, and _reconcile_with_auth
        never re-reads, so a read transport failure cannot be swallowed
        here (round 14 — the round-13 shape fetched a second time
        inside reconcile_job and marked a transport failure on that
        read as processed). The caller reports its own read failure
        as non-definitive before this is ever reached; a probe
        transport failure is reported non-definitive below, so a
        later re-delivery can re-probe.
        Round 10: when the reconcile result reports "job is not open"
        because the job settled while the read was in flight (a
        capture finishing under its lock ahead of this webhook),
        the event is moot, not pending — the job is already settled
        and the event is marked processed, so "ignored" is the
        honest outcome. A job that is not open because it was never
        authorized yet (QUOTED) stays "pending". Round 13: the
        settled check reads the reconcile result's from_status, not
        the passed Job — the passed object may be stale."""
        # Round 10: the job may have settled (or never been
        # authorized) while the event was in flight — reconcile_job's
        # open-check is bypassed by the in-lock read, so mirror it
        # here.
        if job.status not in ("AUTHORIZED", "DELIVERED"):
            result = {"job_id": job.job_id, "from_status": job.status,
                      "to_status": job.status, "updated": False,
                      "capture_id": job.capture_id,
                      "error": "job is not open"}
        else:
            # Fix 2 (webhooks connected to dispatcher): snapshot the FSM
            # state before legacy runs, so webhook reconciles are tracked
            # like the direct reconcile path — no longer an unmonitored
            # back-channel.
            before_state = None
            if USE_DISPATCHER:
                from .dispatcher import resolve_state
                try:
                    before_state = resolve_state(job)
                except Exception:
                    before_state = None
            result = self._reconcile_with_auth(job, auth, job.status)
            if USE_DISPATCHER and before_state is not None:
                from .dispatcher import resolve_state as _rs
                try:
                    after_state = _rs(job)
                except Exception:
                    after_state = None
                if after_state is not None and after_state != before_state:
                    # enum_res=None: _fsm_track_reconcile handles it
                    # gracefully. The redundant _first_completed_capture
                    # call was doubling the PayPal GETs on the hot path.
                    self._fsm_track_reconcile(
                        job, before_state, auth, None)
        if result["updated"]:
            return ({"action": "reconciled", "error": ""}, True)
        if result.get("probe_transport_failed"):
            # Round 14: the read succeeded but the pending-key probe
            # failed in transport — a later re-delivery re-reads and
            # re-probes, so the event stays unmarked (round-7 rule: a
            # transport failure must stay unmarked). The intent is
            # still live, so nothing is lost while we wait.
            return ({"action": "pending", "error": result["error"]}, False)
        if result.get("intent_kept_unconfirmed"):
            # Round 28 (C3): the intent was kept because the read
            # could not confirm it — a later re-delivery's read may
            # catch up, so the event stays unmarked for re-delivery.
            return ({"action": "pending", "error": result["error"]}, False)
        if result["error"]:
            if (result["error"] == "job is not open"
                    and result["from_status"] in ("CAPTURED", "VOIDED")):
                return ({"action": "ignored", "error": ""}, True)
            return ({"action": "pending", "error": result["error"]}, True)
        return ({"action": "ignored", "error": ""}, True)

    # -- timeout ---------------------------------------------------------------
    @_audited("void_after_deadline")
    @_per_job_locked
    def void_after_deadline(self, job: Job, *, payer: str, now: int | None = None) -> dict:
        """Release the hold back to the payer when the worker never delivered
        and the deadline has passed.
        ``now`` is a test-only hook (lets tests simulate the deadline
        without sleeping); the server never passes it — production calls
        always use wall-clock time, so no client can forge a future
        timestamp and void before the real deadline.

        Phase 4: when USE_DISPATCHER, routes through
        _void_after_deadline_via_dispatcher (two-phase void)."""
        if USE_DISPATCHER:
            return self._void_after_deadline_via_dispatcher(
                job, payer=payer, now=now)
        # Round 12: re-read the job inside the lock (see authorize).
        fresh = self.store.get(job.job_id)
        if fresh is None:
            raise EscrowError(f"job not found: {job.job_id}")
        job = fresh
        if job.status != "AUTHORIZED":
            raise EscrowError(f"cannot void in state {job.status} (needs AUTHORIZED)")
        if payer != job.payer:
            raise EscrowError("only the payer can void this job")
        effective_now = now if now is not None else int(time.time())
        if effective_now < job.deadline:
            raise EscrowError("deadline not reached")
        # Round 19 (Grok round-18 finding 1): the needs_review flag
        # guards the reconcile arm — it must guard the void too. An
        # unauditable partial was seen; voiding would book VOIDED over
        # money that may have moved. Refuse; the payer-only
        # force-settle is the exit. Round 20 (Grok round-19 finding
        # 4): the refusal carries the attempted amount, like the
        # reconcile wedge.
        if job.needs_review:
            amount_note = (
                f" (attempted ${job.needs_review_amount_cents/100:.2f})"
                if job.needs_review_amount_cents is not None else "")
            raise EscrowError(
                "cannot void: job needs operator review — a partial "
                "capture was reported that could not be accounted for"
                + amount_note)
        # Round 19: re-read the authorization before voiding. If PayPal
        # lists a COMPLETED capture, money moved — record it and wedge
        # for review instead of voiding over it.
        # Round 20 (Grok round-19 finding 1): fail closed. A
        # PARTIALLY_CAPTURED read lists no captures — the partial is
        # invisible on it — and void is allowed in that status, so
        # voiding on it would book a clean VOIDED over money that
        # moved. A CAPTURED read means it is too late to void at all.
        # And if the read itself fails, voiding blind is refused.
        try:
            auth = self.paypal.get_authorization(job.authorization_id)
        except PayPalError as e:
            raise EscrowError(
                "cannot void: the authorization could not be re-read "
                f"({e}) — refusing to void blind")
        # Round 20: the order is checked too — the authorization
        # resource may omit a capture the order lists.
        # Round 21: tri-state — an unread order wedges instead of
        # POSTing a void on an unknown.
        outcome, cap = self._first_completed_capture_including_order(
            job, auth)
        # Round 47 (Claude round-46 HIGH): the shared guard —
        # "unattributable" wedges here, never POSTs a void over moved
        # money.
        if self._wedge_on_unattributable(
                job, outcome, "void_after_deadline pre-void check"):
            raise EscrowError(
                job.needs_review_reason or
                "captures unattributable (operator review needed)")
        if outcome == "unread":
            # Round 28 (C7): a pure order-read failure before any POST
            # is a plain error — no review flag. Nothing was POSTed,
            # nothing moved on our watch; the payer retries.
            raise EscrowError(
                "cannot void: authorization lists no capture and the "
                "order could not be read — refusing to void blind")
        if outcome == "ambiguous":
            # Round 28 (C5): a non-terminal capture is listed — money
            # may still move. Void refused; wedge for a human.
            reason = ("a non-terminal capture is listed on the order "
                      "— void refused (operator review needed)")
            self._set_review_flag(job, reason)
            self._save(job)
            raise EscrowError("cannot void: " + reason)
        if auth.get("status") in ("PARTIALLY_CAPTURED", "CAPTURED") \
                or cap is not None:
            if cap is not None:
                # Round 28 (G1): the listed capture goes through
                # _record_listed_capture — a stored capture is never
                # overwritten by the raw write. The wedge stands
                # either way; only the books differ.
                record_outcome = self._record_listed_capture(job, cap)
                if record_outcome == "conflict":
                    reason = (f"authorization lists capture "
                              f"{cap.get('id')} conflicting with the "
                              f"recorded capture {job.capture_id} — "
                              "void refused (operator review needed)")
                else:
                    reason = ("authorization lists a completed capture — "
                              "void refused (operator review needed)")
            else:
                reason = (
                    f"authorization is {auth.get('status')} with no "
                    "capture listed — void refused (money may have "
                    "moved; operator review needed)")
            self._set_review_flag(job, reason)
            self._save(job)
            raise EscrowError("cannot void: " + reason)
        # Grok fix order #2: prefer the persisted dispatcher void key;
        # fall back to the legacy key when no void intent exists.
        request_id = (job.pending_void_request_id
                      or self._request_id(job, "void"))
        # Round 31 (C4): if the pre-read already shows a released
        # hold (VOIDED/EXPIRED/DENIED), there is no void to POST —
        # skip straight to the post-void confirmation with the
        # pre-read as the confirmation. The old code POSTed anyway
        # and relied on the error path.
        if _is_hold_released(auth.get("status")):
            result = {}
            post_auth = auth
        else:
            try:
                result = self.paypal.void_authorization(
                    job.authorization_id, request_id=request_id)
            except PayPalError:
                result = self._void_with_reconcile(job, request_id)
            # Round 20 (Grok round-19 finding 1): re-read after the void.
            # A capture that landed in the void window must not be booked
            # over as a clean VOIDED — record it and settle CAPTURED with
            # the residual instead.
            # Round 21 (Grok round-20 finding 4): the confirmation fails
            # closed. A failed confirmation read, or a CAPTURED /
            # PARTIALLY_CAPTURED post-read that lists no capture, wedges
            # instead of booking VOIDED — the void POSTed, but the state
            # is unconfirmed and money may have moved.
            # Round 26: the post-void confirmation is ALWAYS a fresh read
            # — it must not reuse _void_with_reconcile's internal read.
            # That read happens during void recovery, before the void
            # outcome is known; a capture landing in the window between
            # would be missed (test_void_post_read_catches_late_capture).
            # Round 28 (G1): the settle helper routes the listed capture
            # through _record_listed_capture. On "conflict" the books are
            # untouched — wedge instead of settling.
            # Round 28 (C7): the confirmation read is retried once — it
            # can lag a successful void, and wedging on the first lag
            # strands a good void.
            try:
                post_auth = self.paypal.get_authorization(job.authorization_id)
            except PayPalError:
                try:
                    post_auth = self.paypal.get_authorization(
                        job.authorization_id)
                except PayPalError:
                    post_auth = None
        # Round 24 (Grok round-23 structural): the post-void
        # decision lives in _resolve_post_void — "record and wedge"
        # vs "settle with residual" cannot drift between this path,
        # _confirm_residual_void, and _finish_partial_capture.
        decision = self._resolve_post_void(
            job, post_auth, partial_origin=False,
            verb="cannot confirm void")
        if decision[0] == "captured":
            settle_outcome = self._settle_captured_with_residual(
                job, decision[1])
            if settle_outcome == "conflict":
                reason = (f"post-void read lists capture "
                          f"{decision[1].get('id')} conflicting with "
                          f"the recorded capture {job.capture_id} "
                          "(operator review needed)")
                self._set_review_flag(job, reason)
                self._save(job)
                raise EscrowError("cannot confirm void: " + reason)
            self._record_paypal_response(
                job, operation="void", status="CAPTURED",
                id_=job.authorization_id, body=post_auth)
            # Round 32 (Gemini round-31 finding 5): the capture was
            # found via the post-void read, not POSTed.
            job.settlement_method = "reconciled"
            self._save(job)
            return result
        if decision[0] == "settle_books":
            # Defensive: the pre-read wedges when it records a
            # capture, so the job should have none here — but if it
            # does, settling CAPTURED from the books is the truthful
            # outcome, never VOIDED over recorded money.
            job.voided_amount_cents = self._compute_voided_residual(job)
            job.status = "CAPTURED"
            job.settled_at = int(time.time())
            # Round 32 (Gemini round-31 finding 5): settled from
            # books via read, not POSTed.
            job.settlement_method = "reconciled"
            self._record_paypal_response(
                job, operation="void", status="CAPTURED",
                id_=job.authorization_id, body=post_auth)
            self._save(job)
            return result
        # ("voided",): the hold is released and nothing moved.
        # Round 25 (Grok round-24 suggestion): the audit record is the
        # post-void confirmation authorization — not the empty void
        # body with a hardcoded VOIDED status — so an EXPIRED or
        # DENIED confirmation is not mislabeled. The confirmation
        # read is what proved the hold released; it is the last good
        # PayPal response on this job.
        self._record_paypal_response(job, operation="void",
                                     status=post_auth.get("status"),
                                     id_=job.authorization_id,
                                     body=post_auth)
        job.status = "VOIDED"
        job.settled_at = int(time.time())
        self._save(job)
        return result

    def _pre_void_unread_retry(self, job: Job, *,
                               now: int | None = None,
                               permanent: bool = False,
                               paypal_error: "PayPalError | None" = None,
                               ) -> None:
        """Retry with exponential backoff for unread PRE-VOID reads
        (MEDIUM #6, Gemini Q3, Claude Q2).

        A strike counts only when the backoff interval has elapsed
        since the previous strike: backoff = min(2^attempts * 60s,
        1800s), plus up to 30s of jitter on the Retry-After hint. A
        tight retry loop during an outage therefore cannot burn
        through the strike budget — three quick retries in a
        10-minute outage do NOT wedge the job.

        Claude Q2: when PayPal answers 429 with a Retry-After header,
        that exact duration (plus jitter) overrides the computed
        backoff — never guess when PayPal told us how long to wait.
        A 429 NEVER consumes the permanent strike budget.

        Transient failures (5xx, timeouts, unreadable order) back off
        up to ~30 minutes before the budget (default 5, configurable
        via max_void_retries) is exhausted. Permanent failures (4xx
        that will not heal, e.g. 401/404) skip the backoff and wedge
        after 3 strikes.

        The wedge sets void_read_failed=True — a SEPARATE flag from
        needs_review, so an outage never blocks the inspection
        auto-capture sweep (which skips needs_review jobs).

        Refusals answer 503 with Retry-After (not 409): the caller
        should wait and retry. Never returns (always raises)."""
        import random
        from .dispatcher import GuardRejection
        effective_now = now if now is not None else int(time.time())
        # Claude Q2: honor PayPal's Retry-After on 429 exactly.
        server_hint = (
            paypal_error.retry_after_seconds
            if paypal_error is not None else None)
        if not permanent and job.last_void_attempt_time is not None:
            backoff = min(
                (2 ** job.void_attempts)
                * self.VOID_RETRY_BASE_INTERVAL_SECONDS,
                self.VOID_RETRY_MAX_BACKOFF_SECONDS)
            if server_hint is not None:
                # PayPal told us how long — use it (plus jitter).
                backoff = min(server_hint, 1800.0)
            elapsed = effective_now - job.last_void_attempt_time
            if elapsed < backoff:
                # Too soon — refuse WITHOUT counting a strike.
                wait = backoff - elapsed
                raise GuardRejection(
                    "refusing to void blind — order unreadable "
                    f"(retry in {int(wait)}s)",
                    category="retryable",
                    retry_after_seconds=int(wait) + random.randint(
                        0, self.VOID_RETRY_JITTER_MAX_SECONDS))
        job.last_void_attempt_time = effective_now
        job.void_attempts += 1
        # Permanent failures wedge faster: no backoff was waited, and
        # the budget is 3 regardless of configuration.
        budget = 3 if permanent else self.max_void_retries
        if job.void_attempts >= budget:
            job.void_read_failed = True
            self._save(job)
            raise EscrowError(
                "persistent read failure — operator review required",
                http_status=503)
        self._save(job)
        next_backoff = min(
            (2 ** job.void_attempts) * self.VOID_RETRY_BASE_INTERVAL_SECONDS,
            self.VOID_RETRY_MAX_BACKOFF_SECONDS)
        raise GuardRejection(
            "refusing to void blind — order unreadable (will retry)",
            category="retryable",
            retry_after_seconds=next_backoff + random.randint(
                0, self.VOID_RETRY_JITTER_MAX_SECONDS))

    def _void_after_deadline_via_dispatcher(self, job: Job, *, payer: str,
                                            now: int | None = None) -> dict:
        """Phase 4: void_after_deadline via dispatcher (two-phase).

        Phase 1: S4 + E_VOID_DEADLINE → S4, [SaveVoidIntent, PostVoid].
        Phase 2: on POST success → R_VOID_OK → S11; on failure → R_VOID_FAIL → S4.
        Guards + pre-read are preserved (caller-side I/O). The dispatcher
        decides the transitions; the caller executes the PayPal POST.
        """
        from .dispatcher import (resolve_state, transition, State, Event,
                                 Evidence, GuardRejection)
        with self._delivery_lock(job.job_id):
            fresh = self.store.get(job.job_id)
            if fresh is None:
                raise EscrowError(f"job not found: {job.job_id}")
            job = fresh
            # Guards — same as old code
            if job.status != "AUTHORIZED":
                raise EscrowError(
                    f"cannot void in state {job.status} (needs AUTHORIZED)")
            if payer != job.payer:
                raise EscrowError("only the payer can void this job")
            effective_now = now if now is not None else int(time.time())
            deadline_passed = effective_now >= job.deadline
            if not deadline_passed:
                raise EscrowError("deadline not reached")
            if job.needs_review:
                amount_note = (
                    f" (attempted ${job.needs_review_amount_cents/100:.2f})"
                    if job.needs_review_amount_cents is not None else "")
                raise EscrowError(
                    "cannot void: job needs operator review — a partial "
                    "capture was reported that could not be accounted for"
                    + amount_note)
            if job.void_read_failed:
                # MEDIUM #6: wedged on persistent read failure — the
                # operator clears this (admin reconcile), not a retry.
                raise EscrowError(
                    "cannot void: persistent read failure — operator "
                    "review required",
                    http_status=503)
            # Pre-read (caller-side I/O)
            try:
                auth = self.paypal.get_authorization(job.authorization_id)
            except PayPalError as e:
                # Unreadable pre-void read (READ_500): refuse for a retry
                # rather than erroring at once. Transient failures back
                # off exponentially; permanent ones (4xx) wedge faster.
                # Claude Q2: the error carries PayPal's Retry-After
                # header on 429 — honored exactly, never guessed.
                # _pre_void_unread_retry never returns.
                self._pre_void_unread_retry(
                    job, now=effective_now,
                    permanent=not _is_transient_paypal_error(e),
                    paypal_error=e)
            outcome, cap = self._first_completed_capture_including_order(
                job, auth)
            if self._wedge_on_unattributable(
                    job, outcome, "void_after_deadline pre-void check"):
                raise EscrowError(
                    job.needs_review_reason or
                    "captures unattributable (operator review needed)")
            # Map pre-read outcome to Evidence(read, enum)
            if outcome == "unread":
                # The order could not be read — transient: back off.
                self._pre_void_unread_retry(job, now=effective_now)
            # The pre-void read was readable.
            # Grok fix order #6: do NOT reset void_attempts to 0 here.
            # The reset used to run immediately before the E_VOID_DEADLINE
            # key was minted, so every void was attempt=1 and PayPal
            # replayed the first attempt's response forever (decline
            # loop). The counter now only grows; a settled job makes it
            # irrelevant. The backoff timing state below still resets —
            # that is about retry pacing, not key uniqueness.
            job.last_void_attempt_time = None
            job.void_read_failed = False
            # Not saved yet — the flow below saves before every
            # return/raise.
            if outcome == "ambiguous":
                reason = ("a non-terminal capture is listed on the order "
                          "— void refused (operator review needed)")
                self._set_review_flag(job, reason)
                self._save(job)
                raise EscrowError("cannot void: " + reason)
            if cap is not None or auth.get("status") in (
                    "CAPTURED", "PARTIALLY_CAPTURED"):
                # Grok Q2/Q4: persist the capture from the raw payload
                # FIRST (before transition() and before any raise), then
                # dispatch for the wedge actions, apply, save, THEN raise.
                # The table cannot do commit ordering — this is caller work.
                is_partial = auth.get("status") == "PARTIALLY_CAPTURED"
                cap_id = cap.get("id") if cap is not None else None
                if cap is not None:
                    amt = cap.get("amount", {})
                    # Grok fix order #6: parse with amount_to_cents
                    # (Decimal-based), never int(float()*100) which is
                    # off by a cent (10.10 → 1009). NEVER book 0 on a
                    # failed parse — wedge for operator review instead.
                    try:
                        cap_cents = amount_to_cents(amt.get("value"))
                    except (ValueError, TypeError):
                        self._set_review_flag(
                            job,
                            f"capture amount unparseable "
                            f"(id {cap_id or 'unknown'}) — operator "
                            f"review needed")
                        self._save(job)
                        raise EscrowError(
                            f"capture amount could not be parsed "
                            f"(id {cap_id or 'unknown'})")
                    # Persist BEFORE transition() (Grok Q2).
                    self._book_capture(job, cap_id, cap_cents)
                else:
                    cap_cents = 0
                if auth.get("status") in ("CAPTURED", "PARTIALLY_CAPTURED") \
                        and cap is None:
                    # Status says captured but no capture listed — wedge
                    # with the "no capture listed" reason (matches old
                    # code; the money may have moved off-book).
                    reason = (
                        f"authorization is {auth.get('status')} with no "
                        f"capture listed — void refused (money may have "
                        f"moved; operator review needed)")
                    self._set_review_flag(job, reason)
                    self._save(job)
                    raise EscrowError("cannot void: " + reason)
                if is_partial:
                    # Grok Q4: E_VOID_DEADLINE + READ_PARTIAL is flag-only
                    # (drops the capture ID). Use E_RECONCILE to get
                    # RecordCapture in the action list.
                    ev_pre = Evidence(
                        job_id=job.job_id,
                        is_payer=True,
                        deadline_passed=deadline_passed,
                        authorization_id=job.authorization_id,
                        read=Event.READ_PARTIAL,
                        has_capture=cap is not None,
                        capture_ids=(cap_id,) if cap_id else (),
                        amount_cents=cap_cents if cap_cents > 0 else None,
                    )
                    try:
                        _, pre_actions = transition(
                            State.S4_AUTHORIZED_LIVE, Event.E_RECONCILE,
                            ev_pre)
                    except GuardRejection as e:
                        raise EscrowError(str(e))
                else:
                    # Grok Q2: E_VOID_DEADLINE + READ_CAPTURED → S5 +
                    # [RecordCapture, SetReviewFlag]. Does NOT raise.
                    if not cap_id or cap_cents <= 0:
                        # Cannot build valid evidence — manual wedge.
                        reason = ("authorization lists a completed "
                                  "capture — void refused (operator "
                                  "review needed)")
                        self._set_review_flag(job, reason)
                        self._save(job)
                        raise EscrowError("cannot void: " + reason)
                    ev_pre = Evidence(
                        job_id=job.job_id,
                        is_payer=True,
                        deadline_passed=deadline_passed,
                        authorization_id=job.authorization_id,
                        read=Event.READ_CAPTURED,
                        enum=Event.ENUM_FOUND,
                        has_capture=True,
                        capture_ids=(cap_id,),
                        amount_cents=cap_cents,
                    )
                    try:
                        _, pre_actions = transition(
                            State.S4_AUTHORIZED_LIVE,
                            Event.E_VOID_DEADLINE, ev_pre)
                    except GuardRejection as e:
                        raise EscrowError(str(e))
                # Apply actions in order (RecordCapture already persisted
                # above; apply SetReviewFlag), then save.
                for action in pre_actions:
                    if action.__class__.__name__ == "SetReviewFlag":
                        self._set_review_flag(job, action.reason)
                self._save(job)
                if is_partial:
                    raise EscrowError("cannot void: hold may still be open "
                                      "— void refused (operator review "
                                      "needed)")
                raise EscrowError("cannot void: authorization lists a "
                                  "completed capture — void refused "
                                  "(operator review needed)")
            # Clean path: S4 + E_VOID_DEADLINE with READ_CREATED/ENUM_NONE
            # → (S4, [SaveVoidIntent, PostVoid]) — two-phase, stays in S4
            # If the pre-read already shows a released hold, skip the POST
            # (matches old code: result = {}, use pre-read as confirmation)
            if _is_hold_released(auth.get("status")):
                self._record_paypal_response(
                    job, operation="void",
                    status=auth.get("status"),
                    id_=job.authorization_id, body=auth)
                job.status = "VOIDED"
                job.settled_at = int(time.time())
                self._save(job)
                return {}
            state = resolve_state(job)
            # Idempotency: attempt advances with job.void_attempts so a
            # retried void derives a fresh key (same decline-loop hazard
            # as captures — see _capture_via_dispatcher).
            # Grok fix order #6: increment BEFORE minting the key, and
            # persist it — never reset to 0 beforehand. Every void POST
            # gets a unique attempt number; PayPal will not replay a
            # prior attempt's response.
            job.void_attempts += 1
            self._save(job)
            ev = Evidence(
                job_id=job.job_id,
                is_payer=True,
                deadline_passed=deadline_passed,
                authorization_id=job.authorization_id,
                read=Event.READ_CREATED,
                enum=Event.ENUM_NONE,
                attempt=job.void_attempts,
            )
            try:
                new_state, actions = transition(
                    state, Event.E_VOID_DEADLINE, ev)
            except GuardRejection as e:
                raise EscrowError(str(e))
            # Phase 1: extract the intent key and PostVoid action
            void_key = None
            post_void_auth_id = None
            for action in actions:
                aname = action.__class__.__name__
                if aname == "SaveVoidIntent":
                    void_key = action.key
                elif aname == "PostVoid":
                    post_void_auth_id = action.authorization_id
                elif aname == "SetReviewFlag":
                    self._set_review_flag(job, action.reason)
                    self._save(job)
                    raise EscrowError("cannot void: " + action.reason)
            # Grok fix order #2: persist the void key BEFORE the POST.
            # The HMAC key was previously never persisted — a crash
            # between POST and settlement lost the key, and the key
            # lived (wrongly) in the capture-intent field.
            if void_key:
                self._set_pending_void(job, void_key)
            # Phase 1 I/O: POST the void (caller-side)
            if _is_hold_released(auth.get("status")):
                # Already released — treat as success
                post_ok = True
            else:
                try:
                    self.paypal.void_authorization(
                        post_void_auth_id or job.authorization_id,
                        request_id=void_key)
                    post_ok = True
                except PayPalError:
                    post_ok = False
            # Phase 2: dispatch R_VOID_OK or R_VOID_FAIL
            state = resolve_state(job)  # still S4
            if post_ok:
                # Confirm via fresh read (caller-side I/O)
                try:
                    post_auth = self.paypal.get_authorization(
                        job.authorization_id)
                except PayPalError:
                    # Retry once (old code Round 28 C7: confirmation can lag)
                    try:
                        post_auth = self.paypal.get_authorization(
                            job.authorization_id)
                    except PayPalError:
                        post_auth = None
                # Late-capture check: re-read once more to catch a capture
                # that landed in the void window (test_void_post_read_catches_late_capture).
                # The old code's _resolve_post_void does this via its internal
                # read; we do it explicitly here.
                if post_auth is not None:
                    try:
                        late_check = self.paypal.get_authorization(
                            job.authorization_id)
                        # If the late check lists captures, use it.
                        if late_check.get("captures"):
                            post_auth = late_check
                    except PayPalError:
                        pass
                # If the confirmation read failed, wedge — do not book
                # VOIDED without confirmation.
                if post_auth is None:
                    reason = ("void POST succeeded but the confirmation "
                              "read failed — the hold may still be open "
                              "(operator review needed)")
                    self._set_review_flag(job, reason)
                    self._save(job)
                    raise EscrowError("cannot void: " + reason)
                # Grok Fix 2 & 3: Check for late capture directly.
                # If post_auth lists a COMPLETED capture, persist it and
                # settle as CAPTURED (do not use _resolve_post_void which
                # may miss it when status is VOIDED).
                late_caps = (post_auth.get("captures") or []) if post_auth else []
                completed_cap = None
                for c in late_caps:
                    if c.get("status") == "COMPLETED":
                        completed_cap = c
                        break
                if completed_cap is not None:
                    # Grok Fix 3: persist capture BEFORE any raise/settle
                    cap_id = completed_cap.get("id")
                    amt = completed_cap.get("amount", {})
                    # Grok fix order #6: Decimal-based parse, never
                    # int(float()*100); wedge on unparseable, never book 0.
                    try:
                        cap_cents = amount_to_cents(amt.get("value"))
                    except (ValueError, TypeError):
                        self._set_review_flag(
                            job,
                            f"late capture amount unparseable "
                            f"(id {cap_id or 'unknown'}) — operator "
                            f"review needed")
                        self._save(job)
                        raise EscrowError(
                            f"late capture amount could not be parsed "
                            f"(id {cap_id or 'unknown'})")
                    self._book_capture(job, cap_id, cap_cents)
                    # Settle as CAPTURED with residual
                    settle_outcome = self._settle_captured_with_residual(
                        job, completed_cap)
                    if settle_outcome == "conflict":
                        reason = (f"post-void read lists capture "
                                  f"{completed_cap.get('id')} conflicting with "
                                  f"the recorded capture {job.capture_id} "
                                  "(operator review needed)")
                        self._set_review_flag(job, reason)
                        self._save(job)
                        raise EscrowError("cannot confirm void: " + reason)
                    return {"status": "CAPTURED",
                            "authorization_id": job.authorization_id}
                # No late capture: use _resolve_post_void for other cases
                decision = self._resolve_post_void(
                    job, post_auth, partial_origin=False,
                    verb="cannot confirm void")
                if decision[0] == "captured":
                    settle_outcome = self._settle_captured_with_residual(
                        job, decision[1])
                    if settle_outcome == "conflict":
                        reason = (f"post-void read lists capture "
                                  f"{decision[1].get('id')} conflicting with "
                                  f"the recorded capture {job.capture_id} "
                                  "(operator review needed)")
                        self._set_review_flag(job, reason)
                        self._save(job)
                        raise EscrowError("cannot confirm void: " + reason)
                    return {"status": "CAPTURED",
                            "authorization_id": job.authorization_id}
                elif decision[0] == "wedge":
                    reason = decision[1] or "post-void confirmation failed"
                    self._set_review_flag(job, reason)
                    self._save(job)
                    raise EscrowError("cannot confirm void: " + reason)
                # decision[0] == "voided": dispatch R_VOID_OK through the
                # dispatcher (Fix 4, new reviewer) — don't bypass it.
                # Meta-review nit: use the job's actual resolved state,
                # not a hardcoded S4_AUTHORIZED_LIVE.
                from .dispatcher import (transition as _transition,
                                         resolve_state as _resolve_state,
                                         Event as _Event,
                                         Evidence as _Evidence,
                                         RecordVoid as _RecordVoid,
                                         ClearIntent as _ClearIntent)
                # Claude review: the void releases the UNCATURED residual,
                # not the full hold. After a partial capture, booking
                # the full price as voided corrupts the books (the
                # executor's RecordVoid check would catch the mismatch
                # and wedge, but the Evidence should be right upfront).
                _void_hold = (job.quote or {}).get("price_cents", 0)
                _void_captured = job.capture_amount_cents or 0
                ev_ok = _Evidence(
                    job_id=job.job_id,
                    authorization_id=job.authorization_id,
                    voided_amount_cents=_void_hold - _void_captured,
                )
                new_state, actions = _transition(
                    _resolve_state(job), _Event.R_VOID_OK, ev_ok)
                # Apply dispatcher actions: RecordVoid books the amount,
                # ClearIntent clears the pending void intent (Grok fix
                # order #2: the void key lives in pending_void_request_id
                # now, so clear it — not just the capture intent).
                for action in actions:
                    if isinstance(action, _RecordVoid):
                        job.voided_amount_cents = action.amount_cents
                    elif isinstance(action, _ClearIntent):
                        self._clear_pending_capture(job)
                        self._clear_pending_void(job)
                # Record the void operation (matches old code behavior)
                if post_auth:
                    self._record_paypal_response(
                        job, operation="void",
                        status=post_auth.get("status"),
                        id_=job.authorization_id, body=post_auth)
                job.status = "VOIDED"
                job.settled_at = int(time.time())
                self._save(job)
                return {}
            else:
                # Void POST failed (response lost?) — reconcile via read.
                # Grok Q1/Q3/Q4: before settling, do a SECOND read to catch
                # late captures/partials, and check the order — a released
                # hold with an unreadable order wedges (money may have moved).
                try:
                    reconcile_auth = self.paypal.get_authorization(
                        job.authorization_id)
                except PayPalError:
                    reconcile_auth = None
                if reconcile_auth is not None:
                    # Grok Q2: check the reconcile read ITSELF for captures
                    # BEFORE the released check. A released hold that lists
                    # a capture is not a clean void — persist and wedge.
                    _, rcap0 = self._first_completed_capture_including_order(
                        job, reconcile_auth)
                    if (rcap0 is not None
                            and rcap0.get("status") == "COMPLETED"):
                        rcap_id0 = rcap0.get("id")
                        ramt0 = rcap0.get("amount", {})
                        try:
                            rcents0 = amount_to_cents(
                                ramt0.get("value"))
                        except (ValueError, TypeError):
                            self._set_review_flag(
                                job,
                                f"reconcile capture amount unparseable "
                                f"(id {rcap_id0 or 'unknown'}) — operator "
                                f"review needed")
                            self._save(job)
                            raise EscrowError(
                                f"reconcile capture amount could not be "
                                f"parsed (id {rcap_id0 or 'unknown'})")
                        # Persist BEFORE any raise (Grok Q2).
                        self._book_capture(job, rcap_id0, rcents0)
                        reason = ("reconcile lists a completed capture — "
                                  "void refused (operator review needed)")
                        self._set_review_flag(job, reason)
                        self._save(job)
                        raise EscrowError(
                            "cannot void: completed capture (operator "
                            "review needed)")
                if reconcile_auth and _is_hold_released(
                        reconcile_auth.get("status")):
                    # Second read: catch anything that landed in the void
                    # window (Grok Q1).
                    try:
                        second_auth = self.paypal.get_authorization(
                            job.authorization_id)
                    except PayPalError:
                        second_auth = None
                    if second_auth is not None:
                        outcome2, cap2 = (
                            self._first_completed_capture_including_order(
                                job, second_auth))
                        # PARTIALLY_CAPTURED + capture → persist + wedge
                        # (Grok Q4). Checked BEFORE the completed-capture
                        # arm: a partial means the hold is still open, so
                        # booking a residual would claim "residual returned"
                        # while money may still move. Do NOT settle.
                        if (second_auth.get("status")
                                == "PARTIALLY_CAPTURED"
                                and cap2 is not None):
                            cap_id2 = cap2.get("id")
                            amt2 = cap2.get("amount", {})
                            try:
                                cap_cents2 = amount_to_cents(
                                    amt2.get("value"))
                            except (ValueError, TypeError):
                                self._set_review_flag(
                                    job,
                                    f"post-void capture amount unparseable "
                                    f"(id {cap_id2 or 'unknown'}) — "
                                    f"operator review needed")
                                self._save(job)
                                raise EscrowError(
                                    f"post-void capture amount could not be "
                                    f"parsed (id {cap_id2 or 'unknown'})")
                            # Persist BEFORE any raise (Grok Q2).
                            self._book_capture(job, cap_id2, cap_cents2)
                            reason = ("hold may still be open — partial "
                                      "capture listed post-void (operator "
                                      "review needed)")
                            self._set_review_flag(job, reason)
                            self._save(job)
                            raise EscrowError(
                                "cannot void: hold may still be open "
                                "(operator review needed)")
                        # Late COMPLETED capture → settle CAPTURED, never
                        # VOIDED (Grok Q1).
                        if (cap2 is not None
                                and cap2.get("status") == "COMPLETED"):
                            cap_id2 = cap2.get("id")
                            amt2 = cap2.get("amount", {})
                            try:
                                cap_cents2 = amount_to_cents(
                                    amt2.get("value"))
                            except (ValueError, TypeError):
                                self._set_review_flag(
                                    job,
                                    f"late capture amount unparseable "
                                    f"(id {cap_id2 or 'unknown'}) — "
                                    f"operator review needed")
                                self._save(job)
                                raise EscrowError(
                                    f"late capture amount could not be "
                                    f"parsed (id {cap_id2 or 'unknown'})")
                            self._book_capture(job, cap_id2, cap_cents2)
                            settle_outcome = (
                                self._settle_captured_with_residual(
                                    job, cap2))
                            if settle_outcome == "conflict":
                                reason = (
                                    f"post-void read lists capture "
                                    f"{cap2.get('id')} conflicting with "
                                    f"the recorded capture "
                                    f"{job.capture_id} (operator review "
                                    f"needed)")
                                self._set_review_flag(job, reason)
                                self._save(job)
                                raise EscrowError(
                                    "cannot confirm void: " + reason)
                            return {"status": "CAPTURED",
                                    "authorization_id":
                                        job.authorization_id}
                        # Post-void unread order → wedge (Grok Q3 exception:
                        # the POST already left, state is unknown, money may
                        # have moved). Pre-void unread stays a 409 retry.
                        if outcome2 in ("unread", "order_unread"):
                            reason = (
                                f"authorization is "
                                f"{second_auth.get('status')} but the "
                                f"order could not be read (operator review "
                                f"needed)")
                            self._set_review_flag(job, reason)
                            self._save(job)
                            raise EscrowError(reason)
                    # Clean released hold → settle VOIDED.
                    self._record_paypal_response(
                        job, operation="void",
                        status=reconcile_auth.get("status"),
                        id_=job.authorization_id, body=reconcile_auth)
                    job.status = "VOIDED"
                    job.settled_at = int(time.time())
                    self._save(job)
                    return {}
                ev2 = Evidence(job_id=job.job_id,
                               attempt=max(1, job.void_attempts + 1))
                new_state, actions2 = transition(
                    state, Event.R_VOID_FAIL, ev2)
                # actions2 == [NoOp]: intent kept for retry
                raise EscrowError(
                    "PayPal void POST failed — job remains AUTHORIZED, "
                    "retry with the same idempotency key")

    def _confirm_residual_void(self, job: Job, request_id: str,
                               partial_origin: bool) -> tuple:
        """POST a residual void and confirm it, settling the job
        (round 22). Used by force_settle when the hold is still open
        (PARTIALLY_CAPTURED, CREATED, PENDING): the receipt must never
        claim "residual returned" without the void (Grok round-21
        finding 1), and a void booked without confirmation can erase
        a mid-window partial (finding 2). POSTs the void with the
        given idempotency key, then confirms like void_after_deadline's
        post-read: wedges on a failed confirmation read, an unread
        order, or a CAPTURED/PARTIALLY_CAPTURED post-read with no
        capture listed. Settles CAPTURED (via the single settle
        helper) when a capture is listed and the hold is released;
        settles CAPTURED from the job's books when the hold is
        released and a capture was recorded pre-void; settles VOIDED
        when the hold is released and nothing moved — except when the
        void originated from PARTIALLY_CAPTURED (money moved by
        definition): then a released hold with nothing recorded
        wedges instead of booking VOIDED over unaudited money.
        Returns (to_status, post_auth) — the post-void authorization
        read, so the caller can persist it as the audit record
        (round 23, Grok round-22 recommendation 6: a settled job's
        last response must not say CREATED/PARTIALLY_CAPTURED after
        the void). Every wedge sets the review flag, saves, and
        raises EscrowError."""
        try:
            self.paypal.void_authorization(job.authorization_id,
                                           request_id=request_id)
        except PayPalError as e:
            reason = ("residual void POST failed "
                      f"({e}) — the hold may still be open "
                      "(operator review needed)")
            self._set_review_flag(job, reason)
            self._save(job)
            raise EscrowError("cannot force-settle: " + reason)
        try:
            post_auth = self.paypal.get_authorization(job.authorization_id)
        except PayPalError:
            # Round 28 (C7): the confirmation read can lag a
            # successful void — retry once before wedging on it.
            try:
                post_auth = self.paypal.get_authorization(
                    job.authorization_id)
            except PayPalError:
                post_auth = None
        # Round 24 (Grok round-23 structural): the post-void
        # decision lives in _resolve_post_void — shared with
        # void_after_deadline's post-read and _finish_partial_capture.
        decision = self._resolve_post_void(
            job, post_auth, partial_origin=partial_origin,
            verb="cannot force-settle")
        if decision[0] == "captured":
            # Round 28 (G1): the settle helper routes the listed
            # capture through _record_listed_capture — "kept" settles
            # from the books without touching id/amount/reconciled,
            # "recorded" books it truthfully. On "conflict" the books
            # are untouched — wedge instead of settling.
            settle_outcome = self._settle_captured_with_residual(
                job, decision[1])
            if settle_outcome == "conflict":
                reason = (f"post-void read lists capture "
                          f"{decision[1].get('id')} conflicting with "
                          f"the recorded capture {job.capture_id} "
                          "(operator review needed)")
                self._set_review_flag(job, reason)
                self._save(job)
                raise EscrowError("cannot force-settle: " + reason)
            return ("CAPTURED", post_auth)
        if decision[0] == "settle_books":
            # A capture was recorded pre-void (or by an earlier
            # wedge) and the hold is now released — settle CAPTURED
            # with the residual from the books.
            job.voided_amount_cents = self._compute_voided_residual(job)
            job.status = "CAPTURED"
            return ("CAPTURED", post_auth)
        # ("voided",): the hold is released and nothing moved.
        job.status = "VOIDED"
        return ("VOIDED", post_auth)

    @_audited("dispute")
    @_per_job_locked
    def dispute_job(self, job: Job, *, payer: str, reason: str,
                    now: int | None = None) -> dict:
        """v8.14: payer-filed PAYMENT PAUSE — honest naming for what
        this is.

        This is NOT an adjudicated dispute. There is no arbiter, no
        evidence review, and no path to not-pay through this endpoint.
        It pauses the 48h auto-capture for up to 72h (clamped to the
        PayPal authorization honor period); at the deadline the
        sweeper captures anyway — silence pays the worker. The reason
        string is stored verbatim for the operator's records; no one
        will read it as part of a review, because there is no review.
        The worker may attach a counter-note (see set_pause_worker_note)
        as chargeback evidence; it does not change the outcome.

        Until v8.12, the inspection window auto-captured with no pause
        path. v8.12 added the pause with no timer ("dispute then
        disappear" was the dominant strategy). v8.13 gave it a
        deadline. v8.14 keeps the needs_review flag SET until PayPal
        accepts — the flag is the 409 that blocks a concurrent pause
        from landing in the clear-then-capture gap — and clears it in
        the same write as the terminal CAPTURED status.

        Deadline = min(now + DISPUTE_WINDOW_SECONDS (72h),
        authorized_at + HONOR_PERIOD_SECONDS - HONOR_MARGIN_SECONDS).
        The honor clamp keeps the timeout capture inside PayPal's
        ~3-day authorization honor period, so the money is still
        capturable when the clock runs out.

        A pause is refused (409) when authorized_at is unknown (v8.14:
        fail closed — a window without a known honor period is a trap,
        not protection) or when the remaining honor period cannot
        cover DISPUTE_MIN_HONOR_REMAINING_SECONDS (6h): those jobs
        must settle or void now, not enter a review window the hold
        cannot survive.

        Rules:
        - Only the job's payer can pause (403 otherwise).
        - Only a DELIVERED job can be paused (409 otherwise) — there
          is nothing to pause before delivery, and nothing to pause
          after settlement.
        - A job already carrying needs_review cannot be paused again
          (409).
        - The reason is required (400 on empty) and stored verbatim.

        ``now`` is a test-only hook; production uses wall-clock time.

        Returns {"job_id", "status", "needs_review", "reason",
        "dispute_opened_at", "dispute_deadline", "effect",
        "resolves_by"}. effect="payment_pause" and resolves_by="capture"
        say what this is: a timer that delays capture, not an
        adjudication.
        """
        effective_now = now if now is not None else int(time.time())
        # Re-read inside the lock: the caller's snapshot may be stale.
        fresh = self.store.get(job.job_id)
        if fresh is None:
            raise EscrowError(f"job not found: {job.job_id}")
        job = fresh
        if job.payer != payer:
            raise EscrowError("only the payer can pause this job")
        if job.status != "DELIVERED":
            raise EscrowError(
                f"only a DELIVERED job can be paused (status: {job.status})")
        if job.needs_review:
            raise EscrowError("job is already under review")
        if not reason or not reason.strip():
            raise EscrowError("pause reason is required")
        # v8.14: honor math via _honor_limit. A missing authorized_at
        # must not become a flat 72h — fail closed.
        honor_limit = self._honor_limit(job)
        if honor_limit is None:
            raise EscrowError(
                "cannot open a dispute: authorization honor period is "
                "unknown — settle or void now instead")
        if (honor_limit - effective_now
                < self.DISPUTE_MIN_HONOR_REMAINING_SECONDS):
            raise EscrowError(
                "cannot open a dispute: insufficient authorization "
                "time remains for a review window — settle or void "
                "now instead")
        deadline = min(effective_now + self.DISPUTE_WINDOW_SECONDS,
                       honor_limit)
        # Cap the reason length — it is stored on the job and surfaced
        # in admin listings; unbounded input has no business there.
        reason = reason.strip()[:500]
        # v8.14: authorized_at and both dispute timestamps commit in
        # the same save — a crash between "flag set" and "deadline set"
        # would leave the skip-forever row, which now captures instead
        # of wedging, but one write is still the correct atomicity.
        job.dispute_opened_at = effective_now
        job.dispute_deadline = deadline
        self._set_review_flag(job, f"payer pause: {reason}")
        self.store.save(job)
        return {"job_id": job.job_id, "status": job.status,
                "needs_review": True, "reason": reason,
                "dispute_opened_at": job.dispute_opened_at,
                "dispute_deadline": job.dispute_deadline,
                "effect": "payment_pause",
                "resolves_by": "capture"}

    @_audited("pause_worker_note")
    @_per_job_locked
    def set_pause_worker_note(self, job: Job, *, agent: str,
                              note: str) -> dict:
        """v8.14: the worker's counter-note on a payment pause.

        Stored as chargeback evidence only — it never changes any
        outcome. The pause still expires on its deadline and the
        sweeper still captures; this note is just the worker's side
        of the story, on the record, in case the payer later files a
        real dispute with PayPal.

        Rules: only the job's agent can set it; the job must be
        paused (needs_review set); the note is capped at
        2000 chars; setting it twice overwrites.
        """
        fresh = self.store.get(job.job_id)
        if fresh is None:
            raise EscrowError(f"job not found: {job.job_id}")
        job = fresh
        if agent != job.agent:
            raise EscrowError("only the job's worker can set this note")
        if not job.needs_review:
            raise EscrowError(
                "no active pause to respond to "
                f"(status: {job.status})")
        if not note or not note.strip():
            raise EscrowError("note is required")
        job.pause_worker_note = note.strip()[:2000]
        self._save(job)
        return {"job_id": job.job_id, "status": job.status,
                "note_set": True}

    @_audited("force_settle")
    @_per_job_locked
    def force_settle(self, job: Job, *, payer: str) -> dict:
        """Payer-only exit from a needs_review wedge (round 20). See
        _force_settle_legacy for the full contract."""
        if USE_DISPATCHER:
            return self._force_settle_legacy_with_fsm_tracking(job, payer=payer)
        return self._force_settle_legacy(job, payer=payer)

    @_audited("clear_terminal_review_flag")
    @_per_job_locked
    def clear_terminal_review_flag(self, job: Job, *, payer: str) -> dict:
        """Payer-only review-clear for TERMINAL flagged jobs (v8.2,
        Finding 2).

        A job that settled (CAPTURED/VOIDED) while carrying needs_review
        has no recovery route: _force_settle_legacy requires
        AUTHORIZED/DELIVERED, and ordinary reconciliation only processes
        open jobs. This is the exit: the payer re-reads the
        authorization (and the order), records what PayPal actually
        says on the job, and clears the flag with the decision on the
        record. Money is NEVER re-settled — the status, capture, and
        void records are untouched; only the review flag clears.

        Returns {"job_id", "status", "cleared", "paypal_status",
        "listed_captures"}.
        """
        # Re-read the job inside the lock (see authorize).
        fresh = self.store.get(job.job_id)
        if fresh is None:
            raise EscrowError(f"job not found: {job.job_id}")
        job = fresh
        if payer != job.payer:
            raise EscrowError("only the payer can clear the review flag")
        if not job.needs_review:
            raise EscrowError("job is not flagged for operator review")
        if job.status not in ("CAPTURED", "VOIDED"):
            raise EscrowError(
                f"clear_terminal_review_flag is for terminal jobs; "
                f"job is {job.status} — use force_settle")
        # Record what PayPal actually says — the decision must be on
        # the record. A transport failure refuses; the flag stays.
        try:
            auth = self.paypal.get_authorization(job.authorization_id)
        except PayPalError as e:
            raise EscrowError(
                "cannot clear review flag: the authorization could not "
                f"be re-read ({e})")
        enum_res = self._first_completed_capture_including_order(job, auth)
        listed = []
        cap = enum_res.capture
        if isinstance(cap, dict):
            members = cap.get("_members")
            if isinstance(members, list) and members:
                listed = [m.get("id") for m in members
                          if isinstance(m, dict) and m.get("id")]
            elif cap.get("id"):
                listed = [cap.get("id")]
        # Snapshot the money state — it must not change.
        money_before = (job.status, job.capture_id,
                        job.capture_amount_cents, job.voided_amount_cents)
        # Clear the flag with the decision on the record. The payer has
        # seen PayPal's state and accepted it; the money records stand
        # as they were. The decision is returned and recorded via
        # _record_paypal_response below.
        self._record_paypal_response(
            job, operation="clear_terminal_review_flag",
            status=auth.get("status"), id_=auth.get("id"), body=auth)
        self._clear_review_flag(job)
        self._save(job)
        # Verify no money state changed. v8.14: this was an assert —
        # python -O strips asserts, so a real EscrowError now guards
        # it. If money moved during a review-clear, the books are
        # corrupt and the operator must intervene, not silently
        # continue.
        money_after = (job.status, job.capture_id,
                       job.capture_amount_cents, job.voided_amount_cents)
        if money_before != money_after:
            raise EscrowError(
                f"money state changed during review-clear: "
                f"{money_before} -> {money_after} "
                f"(operator review needed)")
        decision = (
            f"payer cleared terminal review flag: PayPal reports "
            f"authorization {auth.get('status')}, "
            f"listed captures {listed or 'none'}; "
            f"money state unchanged ({job.status})")
        return {"job_id": job.job_id, "status": job.status,
                "cleared": True,
                "paypal_status": auth.get("status"),
                "listed_captures": listed,
                "decision": decision}

    @staticmethod
    def _is_active_pause(job: Job, now: int) -> bool:
        """v8.14: True when the job carries an ACTIVE payer pause —
        a payment pause whose deadline has not yet passed.

        A pause sets needs_review + dispute_opened_at +
        dispute_deadline. A reconcile wedge sets needs_review with no
        deadline (or an expired one). Only the active pause gets the
        capture-on-force_settle semantics; wedges keep the
        read-then-decide logic below.
        """
        return (job.needs_review
                and job.dispute_deadline is not None
                and job.dispute_deadline > now)

    def _force_settle_active_pause(self, job: Job, *,
                                   payer: str) -> dict:
        """v8.14: force_settle on an active payer pause.

        The payer paused auto-capture ("wait") and now explicitly
        settles ("pay the worker now"). This performs a real PayPal
        capture via _capture_core — the full safety path (pre-flight
        read, intent recording, COMPLETED gate) — not a void. The
        review flag clears in the same write as the terminal CAPTURED
        status.

        No delivery-text verification: the job is already DELIVERED
        (delivery was verified at delivery time), and force_settle is
        the payer's explicit override of their own pause.
        """
        # v8.14 fix (Grok review, Nov 2026): do NOT clear the pause
        # flag or timestamps before PayPal accepts. _capture_core is
        # called with _sweep_dispute=True — the flag bypasses the
        # needs_review guard, stays SET through the PayPal call, and
        # clears in the same write as the terminal CAPTURED status on
        # success. On PayPal failure the flag and the deadline stay
        # set, so the pause stands and the payer (or the sweep at the
        # deadline) can retry. Clearing first repeated the
        # clear-then-capture bug: _set_pending_capture saves inside
        # _capture_core, so a failed capture left the pause gone
        # while money was still in flight.
        result = self._capture_core(job, _sweep_dispute=True)
        # _capture_core settled CAPTURED with settlement_method="observed"
        # — the capture is clean (full amount, PayPal-confirmed), not
        # unaudited. Do NOT override to "force_settled": the dispatcher
        # maps that to S10c (unaudited), which is wrong here. The
        # force-settle decision is on the audit trail (below) and in
        # the settlement note.
        job.settlement_note = (
            "payer force-settled an active payment pause — "
            "captured now instead of waiting for the pause deadline")
        self._audit("force_settle_pause_captured", job,
                    outcome="completed",
                    reason="payer settled an active pause early")
        self._save(job)
        # Return the force-settle contract shape, not the raw capture
        # result: callers (and the FSM tracker) expect
        # {job_id, from_status, to_status, ...}.
        return {"job_id": job.job_id,
                "from_status": "DELIVERED",
                "to_status": "CAPTURED",
                "updated": True,
                "capture_id": job.capture_id,
                "error": ""}

    def _force_settle_legacy(self, job: Job, *, payer: str) -> dict:
        """Payer-only exit from a needs_review wedge (round 20). The
        wedge is the safe state — it never settles on its own — and
        this is the way out: the payer re-reads the authorization (and
        the order, which may list a capture the authorization omits),
        records any COMPLETED capture PayPal lists, and settles the
        job. A listed capture settles CAPTURED with the voided
        residual; genuinely nothing moved settles VOIDED. Either way
        the review flag clears — the payer has decided, and the
        decision is on the record. Only the job's payer can call it;
        only a wedged job qualifies; a capture still in flight must be
        resolved first (it may yet land)."""
        # Round 12: re-read the job inside the lock (see authorize).
        fresh = self.store.get(job.job_id)
        if fresh is None:
            raise EscrowError(f"job not found: {job.job_id}")
        job = fresh
        if payer != job.payer:
            raise EscrowError("only the payer can force-settle this job")
        if not job.needs_review:
            raise EscrowError("job is not wedged for operator review")
        if job.status not in ("AUTHORIZED", "DELIVERED"):
            raise EscrowError(
                f"cannot force-settle in state {job.status}")
        # v8.14 fix (Phase 1 property-test finding, Nov 2026): an
        # ACTIVE payer pause (dispute_deadline in the future) is not a
        # wedge — it is the payer saying "wait, don't auto-capture".
        # force_settle is the payer saying "settle it now", which
        # means CAPTURE (pay the worker), not void. The dispute
        # response text is explicit about the cases: an active pause
        # captures now; otherwise an open hold is voided (money back
        # to the payer); a landed capture stays with the worker.
        # The wedge logic below would void a CREATED hold ("nothing
        # moved"), treating a pause like a dispute — but a pause is
        # "wait", not "don't pay". Route to the capture path.
        if self._is_active_pause(job, int(time.time())):
            return self._force_settle_active_pause(job, payer=payer)
        # Round 28 (C1): the pending-intent check moved AFTER the
        # authorization read below. A capture still in flight must be
        # resolved first — EXCEPT on a released hold (VOIDED/EXPIRED/
        # DENIED) where the order and authorization read list no
        # capture: the key is dead (it can never execute on a released
        # hold), so the intent is abandoned and the settle proceeds.
        # This is the exit for the C1 dead-end wedge: a kept key on a
        # released hold now sets needs_review in reconcile, and this
        # path lets the payer out.
        try:
            auth = self.paypal.get_authorization(job.authorization_id)
        except PayPalError as e:
            raise EscrowError(
                "cannot force-settle: the authorization could not be "
                f"re-read ({e})")
        # Round 21: tri-state — an unread order refuses instead of
        # settling on an unknown.
        # Round 22 (Grok round-21 finding 6): a definitive 4xx on the
        # order is "none", not "unread" — only transient failures
        # wedge.
        # Round 28 (C5): "ambiguous" (a non-terminal capture listed)
        # also refuses — money may still move.
        outcome, cap = self._first_completed_capture_including_order(
            job, auth)
        # Round 53 (Gemini round-52 Medium): order_unread also refuses
        # — an auth-only capture with an unread order cannot be
        # force-settled.
        if outcome in ("unread", "order_unread"):
            raise EscrowError(
                "cannot force-settle: the order could not be read to "
                "confirm no capture moved (operator review needed)")
        if outcome == "ambiguous":
            raise EscrowError(
                "cannot force-settle: a non-terminal capture is listed "
                "— money may still move (operator review needed)")
        if outcome == "unattributable":
            # Round 49 (Claude round-48 HIGH + MEDIUM): "unattributable"
            # means terminal captures WERE listed — money moved. The
            # branch handles ONLY statuses where settling is safe:
            # CAPTURED, released (VOIDED/EXPIRED/DENIED), and
            # PARTIALLY_CAPTURED (voids the residual first). CREATED,
            # PENDING, or unknown statuses are rejected — settling
            # unaudited CAPTURED with the hold still open would book
            # terminal over capturable money.
            # Round 49 (Claude round-48 Low 6): do NOT clear the intent
            # before the void — a failed void must leave the key for
            # the retry. Keep capture_reconciled as-is when a real
            # capture is already recorded (don't clobber it to True
            # via _mark_unaudited). Write an explicit reason into the
            # audit body.
            status_now = auth.get("status")
            from_status = job.status
            # Round 50 (Claude round-49 Medium 1): CREATED and PENDING
            # with unattributable captures get the PARTIALLY_CAPTURED
            # recipe — void the (possibly open) hold, confirm it, then
            # settle unaudited. A void can only release held funds, so
            # it is safe here; without this the pre-flight wedge on
            # CREATED has no force_settle exit.
            if status_now not in ("CAPTURED", "VOIDED", "EXPIRED",
                                  "DENIED", "PARTIALLY_CAPTURED",
                                  "CREATED", "PENDING"):
                raise EscrowError(
                    "cannot force-settle: authorization is "
                    f"{status_now} with unattributable captures — "
                    "unknown hold status (operator review needed)")
            if status_now in ("PARTIALLY_CAPTURED", "CREATED", "PENDING"):
                # Void the residual/hold. _void_residual returns
                # "voided" or "not_applied" (raises only on
                # unread/CAPTURED). "not_applied" means the hold is
                # still open — the wedge stands.
                # Round 51 (Grok round-50 Medium 3): "voided" is only
                # the POST success — it is NOT proof the hold is
                # released. Re-read the authorization and require a
                # released status before settling. A lagging hold that
                # still reads open keeps the wedge AND the intent.
                void_result = self._void_residual(
                    job, self._request_id(job, "force-settle-void"))
                if void_result != "voided":
                    reason = (f"force-settle on {status_now} hold "
                              "with unattributable captures: residual "
                              "void did not apply — hold still open "
                              "(operator review needed)")
                    self._set_review_flag(job, reason)
                    self._save(job)
                    raise EscrowError("cannot force-settle: " + reason)
                try:
                    post_auth = self.paypal.get_authorization(
                        job.authorization_id)
                except PayPalError:
                    post_auth = None
                if (post_auth is None
                        or not _is_hold_released(
                            post_auth.get("status"))):
                    reason = (
                        f"force-settle on {status_now} hold with "
                        "unattributable captures: void posted but the "
                        "hold could not be confirmed released — "
                        "leaving the wedge and the intent in place "
                        "(operator review needed)")
                    self._set_review_flag(job, reason)
                    self._save(job)
                    raise EscrowError("cannot force-settle: " + reason)
            # The hold is released (or the residual is confirmed
            # voided): the intent is spent — clear it now.
            self._clear_pending_capture(job)
            # Settle unaudited. If a real capture is already recorded,
            # keep its reconciled flag; otherwise mark unaudited.
            if not self._has_recorded_capture(job):
                self._mark_unaudited(job)
            job.settlement_method = "force_settled"
            # Round 50 (Claude round-49 Low 6): the receipt must say
            # that additional money moved beyond the recorded capture.
            job.settlement_note = (
                "unaccounted captures: terminal captures were listed "
                "but could not be attributed — additional money may "
                "have moved beyond the recorded capture "
                "(operator force-settled)")
            self._record_paypal_response(
                job, operation="force-settle",
                status="UNAUDITED", id_=job.capture_id,
                body={"reason": "unattributable captures — terminal "
                                "captures listed but unsummable "
                                "(operator force-settled)",
                      "auth_status": status_now})
            job.status = "CAPTURED"
            job.settled_at = int(time.time())
            self._clear_review_flag(job)
            self._save(job)
            return {"job_id": job.job_id,
                    "from_status": from_status,
                    "to_status": "CAPTURED", "updated": True,
                    "capture_id": job.capture_id,
                    "error": ""}
        # Round 28 (C1): a pending intent on a released hold with no
        # capture listed anywhere is dead — the key can never execute
        # once the hold is gone. Abandon it so the arms below can
        # settle. On any other status (or when a capture might exist)
        # the intent must be resolved first.
        # Round 45 (Claude round-44 Low 2): a pending intent on a
        # released hold WITH a listed terminal capture is also dead —
        # the key cannot execute against a gone hold. Record the
        # listed capture, clear the spent intent, and continue (the
        # arms below settle from the books).
        if self._has_pending_capture(job):
            status_now = auth.get("status")
            if (_is_hold_released(status_now)
                    and outcome == "none" and cap is None):
                self._clear_pending_capture(job)
                self._save(job)
            elif (_is_hold_released(status_now) and outcome == "found"
                    and cap is not None
                    and cap.get("status") in ("COMPLETED",
                                              "PARTIALLY_REFUNDED",
                                              "REFUNDED")):
                self._record_listed_capture(job, cap)
                self._clear_pending_capture(job)
                self._save(job)
            elif status_now == "CAPTURED":
                # Round 46 (Claude round-45 Low 7): a conflict wedge on
                # a CAPTURED hold with a pending intent — the intent is
                # spent (the hold is gone, the key cannot execute).
                # Clear it; the wedge reason stands for the operator,
                # and the arms below settle from the books.
                self._clear_pending_capture(job)
                self._save(job)
            else:
                raise EscrowError(
                    "cannot force-settle: a capture is still in flight — "
                    "resolve it before force-settling")
        # Round 22 (Grok round-21 finding 1): the authorization status
        # is checked BEFORE any CAPTURED settlement. Settling
        # CAPTURED with voided_amount_cents = hold - captured on a
        # still-open hold books "residual returned" while the residual
        # is still held and capturable.
        # Round 23 (Grok round-22 recommendation 6): post_auth carries
        # the POST-void read from _confirm_residual_void — the audit
        # record below persists it, not the pre-void read.
        status = auth.get("status")
        post_auth = None
        if _is_hold_released(status):
            # Round 22 (Grok round-21 finding 4): DENIED joins
            # VOIDED/EXPIRED — the hold is dead, so VOIDED is
            # truthful and no void is POSTed. This is the exit for a
            # DENIED wedge (reconcile wedges DENIED on an unread
            # order; without this the wedge was permanent).
            if cap is not None:
                # Round 26 (Gemini round-25 finding 1): the listed
                # capture goes through _record_listed_capture first —
                # a different stored id is a conflict (wedge, never
                # overwrite), not a silent overwrite via the settle
                # helper.
                record_outcome = self._record_listed_capture(job, cap)
                if record_outcome == "conflict":
                    reason = (f"pre-read lists capture {cap.get('id')} "
                              f"but the recorded capture is "
                              f"{job.capture_id} — money moved outside "
                              "the recorded flow (operator review "
                              "needed)")
                    self._set_review_flag(job, reason)
                    self._save(job)
                    raise EscrowError("cannot force-settle: " + reason)
                # Round 27 (Gemini round-26 finding 1): the helper
                # already recorded (or kept) the capture — settle
                # CAPTURED from the books. Never pass the raw cap to
                # the settle helper: an empty listed id would wipe a
                # stored id via cap.get("id", ""), and the helper
                # would reset capture_reconciled on an observed
                # capture. The books are authoritative here.
                job.voided_amount_cents = self._compute_voided_residual(job)
                job.status = "CAPTURED"
                to_status = "CAPTURED"
            elif self._has_recorded_capture(job):
                # The capture was recorded by the wedge — settle
                # CAPTURED with the residual from the books. Round 23
                # (Grok round-22 finding 2): key on the id OR the
                # amount — a falsy capture_id ("") with a recorded
                # amount is still a capture, not "nothing moved".
                job.voided_amount_cents = self._compute_voided_residual(job)
                job.status = "CAPTURED"
                to_status = "CAPTURED"
            else:
                # Round 31 (C5): if the wedge reason records a
                # PARTIALLY_CAPTURED observation, part of the hold
                # moved — booking VOIDED would erase the only record
                # of it. Settle as unauditable CAPTURED instead.
                _reason = job.needs_review_reason or ""
                if "PARTIALLY_CAPTURED" in _reason:
                    self._mark_unaudited(job)
                    job.voided_amount_cents = None
                    job.status = "CAPTURED"
                    to_status = "CAPTURED"
                else:
                    job.status = "VOIDED"
                    to_status = "VOIDED"
        elif status == "CAPTURED":
            # Round 23 (Grok round-22 finding 3): the order may list a
            # COMPLETED capture the authorization omits — record it
            # instead of dropping it. No voided residual is booked:
            # the hold was captured, not voided, so
            # voided_amount_cents would be invented.
            # Round 25 (Grok round-24 finding 1): never clobber a
            # stored capture — a different listed id wedges.
            if cap is not None:
                record_outcome = self._record_listed_capture(job, cap)
                if record_outcome == "conflict":
                    reason = (f"pre-read lists capture {cap.get('id')} "
                              f"but the recorded capture is "
                              f"{job.capture_id} — money moved outside "
                              "the recorded flow (operator review "
                              "needed)")
                    self._set_review_flag(job, reason)
                    self._save(job)
                    raise EscrowError("cannot force-settle: " + reason)
            # Money moved — settle CAPTURED. When PayPal lists no
            # capture (neither the authorization nor the order) the
            # capture stays unaudited, like the reconcile CAPTURED
            # arm. Booking VOIDED here would lie about moved money.
            # Round 28 (G3): the reconciled flag is set by
            # _record_listed_capture on a first record, and left
            # alone on "kept" — the old unconditional assignment
            # marked an observed capture as recovered.
            if cap is None and not self._has_recorded_capture(job):
                self._mark_unaudited(job)
            job.status = "CAPTURED"
            to_status = "CAPTURED"
        elif status == "PARTIALLY_CAPTURED":
            # Round 22 (Grok round-21 finding 1): the residual is
            # still held and capturable — never book "residual
            # returned" without the void. Record any listed capture,
            # POST the residual void, confirm, then settle — or wedge
            # if the void cannot be confirmed.
            # Round 23 (Grok round-22 finding 2): the guard keys on
            # the id OR the amount, like the released-hold arm — a
            # falsy id with a recorded amount still counts as a
            # capture.
            if cap is None and not self._has_recorded_capture(job):
                # Round 31 (C5): the DELIVERED wedge — PARTIALLY_CAPTURED
                # with no capture listed, none recorded, none in
                # flight — had no exit while the hold was open. The
                # payer explicitly force-settling is the exit: POST
                # the residual void (the safe direction — it can only
                # release, never move more money), confirm the hold
                # released, then settle as unauditable CAPTURED. The
                # status proves part moved but the amount is
                # unknowable; booking VOIDED would erase it.
                # Grok fix order #2: prefer the persisted dispatcher void key;
                # fall back to the legacy key when no void intent exists.
                request_id = (job.pending_void_request_id
                              or self._request_id(job, "void"))
                try:
                    self.paypal.void_authorization(
                        job.authorization_id, request_id=request_id)
                except PayPalError:
                    try:
                        self._void_with_reconcile(job, request_id,
                                                  op="force_settle")
                    except EscrowError as e:
                        reason = (
                            "force-settle void of PARTIALLY_CAPTURED "
                            f"wedge could not be confirmed ({e}) "
                            "(operator review needed)")
                        self._set_review_flag(job, reason)
                        self._save(job)
                        raise EscrowError(
                            "cannot force-settle: " + reason) from e
                try:
                    post_auth = self.paypal.get_authorization(
                        job.authorization_id)
                except PayPalError:
                    post_auth = None
                if (post_auth is None
                        or not _is_hold_released(
                            post_auth.get("status"))):
                    reason = (
                        "force-settle void of PARTIALLY_CAPTURED wedge "
                        "could not be confirmed — hold not released "
                        "(operator review needed)")
                    self._set_review_flag(job, reason)
                    self._save(job)
                    raise EscrowError("cannot force-settle: " + reason)
                # Hold released. One last check for a capture that
                # listed late.
                _o2, cap2 = self._first_completed_capture_including_order(
                    job, post_auth)
                if _o2 == "unread":
                    # Round 32 (Gemini round-31 finding 2): the order
                    # could not be read — fail closed, never settle
                    # unauditable on an unknown.
                    reason = ("post-void read could not confirm order "
                              "captures (operator review needed)")
                    self._set_review_flag(job, reason)
                    self._save(job)
                    raise EscrowError("cannot force-settle: " + reason)
                if _o2 == "ambiguous":
                    # Round 32 (Gemini round-31 finding 2): a
                    # non-terminal capture is listed — money may still
                    # move. Wedge, never record a PENDING capture as
                    # the job's capture.
                    reason = ("post-void read lists a non-terminal "
                              "capture — money may still move "
                              "(operator review needed)")
                    self._set_review_flag(job, reason)
                    self._save(job)
                    raise EscrowError("cannot force-settle: " + reason)
                if _o2 == "found" and cap2 is not None:
                    record_outcome = self._record_listed_capture(job, cap2)
                    if record_outcome == "conflict":
                        reason = (
                            f"post-void read lists capture "
                            f"{cap2.get('id')} conflicting with the "
                            f"recorded capture {job.capture_id} "
                            "(operator review needed)")
                        self._set_review_flag(job, reason)
                        self._save(job)
                        raise EscrowError(
                            "cannot force-settle: " + reason)
                    job.voided_amount_cents = (
                        self._compute_voided_residual(job))
                elif _o2 == "unattributable":
                    # Round 49 (Claude round-48 Low 7): explicit branch.
                    # Terminal captures were listed but cannot be summed;
                    # the hold is released so money definitely moved.
                    # Unaudited is correct — and the guard's invariant
                    # ("the guard sits behind every read") is kept
                    # explicit rather than falling into the else.
                    self._mark_unaudited(job)
                    job.voided_amount_cents = None
                else:
                    # Unauditable: the status proves part moved.
                    self._mark_unaudited(job)
                    job.voided_amount_cents = None
                job.status = "CAPTURED"
                job.settled_at = int(time.time())
                to_status = "CAPTURED"
                # Settled — skip the rest of the PARTIALLY_CAPTURED
                # arm; the common tail below clears the flag, records
                # the audit, and saves.
            else:
                # Round 25 (Grok round-24 finding 1): never clobber a
                # stored capture — a different listed id wedges.
                if cap is not None:
                    record_outcome = self._record_listed_capture(job, cap)
                    if record_outcome == "conflict":
                        reason = (f"pre-read lists capture {cap.get('id')} "
                                  f"but the recorded capture is "
                                  f"{job.capture_id} — money moved outside "
                                  "the recorded flow (operator review "
                                  "needed)")
                        self._set_review_flag(job, reason)
                        self._save(job)
                        raise EscrowError("cannot force-settle: " + reason)
                # Grok fix order #2: prefer the persisted dispatcher void key;
                # fall back to the legacy key when no void intent exists.
                request_id = (job.pending_void_request_id
                              or self._request_id(job, "void"))
                to_status, post_auth = self._confirm_residual_void(
                    job, request_id, partial_origin=True)
        elif status in ("CREATED", "PENDING"):
            # Round 22 (Grok round-21 finding 2): the hold is still
            # open and capturable — POST the void AND confirm it. A
            # partial landing in the window must not be erased as a
            # clean VOIDED.
            # Round 25 (Grok round-24 finding 2): the pre-read may
            # already list a COMPLETED capture — record it BEFORE the
            # void (never replacing a different stored id), and run
            # the void as partial-origin so a post-read that lists
            # nothing settles from the books instead of booking
            # VOIDED over observed money.
            partial_origin = False
            if cap is not None:
                record_outcome = self._record_listed_capture(job, cap)
                if record_outcome == "conflict":
                    reason = (f"pre-read lists capture {cap.get('id')} "
                              f"but the recorded capture is "
                              f"{job.capture_id} — money moved outside "
                              "the recorded flow (operator review "
                              "needed)")
                    self._set_review_flag(job, reason)
                    self._save(job)
                    raise EscrowError("cannot force-settle: " + reason)
                partial_origin = True
            # Grok fix order #2: prefer the persisted dispatcher void key;
            # fall back to the legacy key when no void intent exists.
            request_id = (job.pending_void_request_id
                          or self._request_id(job, "void"))
            to_status, post_auth = self._confirm_residual_void(
                job, request_id, partial_origin=partial_origin)
        else:
            raise EscrowError(
                "cannot force-settle: unexpected authorization status "
                f"{status} (operator review needed)")
        self._clear_review_flag(job)
        job.settled_at = int(time.time())
        # Round 31 (B2): the payer decided — this settlement went
        # through force-settle, whatever the capture's origin.
        # Round 32 (Gemini round-31 finding 5): only set on CAPTURED
        # — a VOIDED settlement leaves it None per the spec ("None
        # until the job settles with a capture (voids leave it
        # None)").
        if to_status == "CAPTURED":
            job.settlement_method = "force_settled"
        # Round 23 (Grok round-22 recommendation 6): the audit record
        # is the POST-void authorization read when the path POSTed a
        # void — a settled job's last response must not say
        # CREATED/PARTIALLY_CAPTURED after the void.
        audit_auth = post_auth if post_auth is not None else auth
        self._record_paypal_response(job, operation="force-settle",
                                     status=audit_auth.get("status"),
                                     id_=job.authorization_id,
                                     body=audit_auth)
        self._save(job)
        return {"job_id": job.job_id, "to_status": to_status,
                "capture_id": job.capture_id}

    def _force_settle_legacy_with_fsm_tracking(self, job: Job, *, payer: str) -> dict:
        """Force-settle is decided by LEGACY — the FSM only tracks.

        This is the highest-stakes operator exit (payer-only escape
        from a needs_review wedge), and it is explicitly NOT
        dispatcher-decided. ``_force_settle_legacy`` performs every
        guard, PayPal re-read, void, state mutation, and return value
        verbatim; the dispatcher FSM is synced at the state-change
        point for tracking/validation only and never re-decides the
        outcome. Any divergence is logged, never raised: legacy is
        the source of truth for the return contract.

        History: the flag-on bridge reimplemented this path and
        produced 29 "dispatcher returns X, legacy returns Y"
        contract mismatches (reimplemented guards, error messages,
        and return shapes). Calling legacy directly eliminates the
        entire category. A future single-authority design would move
        this decision into the transition table; until then the name
        says what it is.
        """
        from .dispatcher import resolve_state
        import logging
        logger = logging.getLogger(__name__)

        # FSM state before legacy runs (for tracking below).
        # Note: resolve_state needs the fresh job; legacy re-reads
        # inside, so snapshot the state from the passed-in job only
        # for the before-state. The after-state is read from the
        # store after legacy saves.
        try:
            before_state = resolve_state(job)
        except Exception:
            before_state = None

        # --- Legacy decides and mutates (verbatim) ---
        # _force_settle_legacy performs all guards, PayPal I/O
        # (re-reads, voids, confirms), state mutations via legacy
        # helpers, and returns the byte-identical return contract.
        # Void-then-settle ordering, PostVoid execution, and the
        # "failed void wedges, never settles" rule are all legacy's.
        result = self._force_settle_legacy(job, payer=payer)

        # --- FSM tracking at the state-change point ---
        # Legacy has mutated the job and saved. If the FSM state
        # changed, dispatch E_FORCE_SETTLE for tracking/validation.
        # Actions are NOT applied — legacy helpers already performed
        # the mutations. Divergences are logged, not raised: legacy
        # is the source of truth for the return contract.
        if before_state is not None:
            self._fsm_track_force_settle(job, before_state, result)

        return result

    def _fsm_track_force_settle(self, job: Job, before_state,
                                result: dict) -> None:
        """Dispatch E_FORCE_SETTLE for FSM tracking at a force-settle
        state change.

        Legacy (_force_settle_legacy) has already decided the outcome
        and performed all mutations via legacy helpers. This validates
        that the FSM agrees the transition is legal. Actions are
        deliberately NOT applied — legacy already mutated. Any
        divergence (including UnhandledTransition) is logged, never
        raised: legacy is the source of truth for the return
        contract.
        """
        from .dispatcher import (transition, Event, Evidence,
                                 resolve_state as _rs)
        import logging
        logger = logging.getLogger(__name__)
        try:
            # Re-read the settled job for the after-state.
            settled = self.store.get(job.job_id)
            if settled is None:
                return
            after_state = _rs(settled)
            if after_state == before_state:
                return
            # Build best-effort evidence from the settled job.
            # The transition is validated for legality; the business
            # outcome was already decided by legacy.
            to_status = result.get("to_status", "")
            has_capture = bool(
                getattr(settled, "capture_id", "") or
                getattr(settled, "capture_amount_cents", None))
            # Unaudited settle -> UNATTRIBUTABLE; audited capture ->
            # FOUND; void -> NONE.
            if to_status == "VOIDED":
                enum_event = Event.ENUM_NONE
            elif getattr(settled, "capture_id", "") == "":
                # Unaudited (mark_unaudited) — money moved but the
                # capture could not be attributed.
                enum_event = Event.ENUM_UNATTRIBUTABLE
            else:
                enum_event = Event.ENUM_FOUND
            cap_ids = ((getattr(settled, "capture_id", "") or "").strip(),)
            cap_ids = tuple(c for c in cap_ids if c)
            # Fix 3: compute is_partial from the settled amounts instead
            # of hardcoding False — a partial capture + residual void
            # must be visible to the FSM.
            _c_amt = getattr(settled, "capture_amount_cents", None)
            _hold = (getattr(settled, "quote", {}) or {}).get(
                "price_cents", 0)
            _is_part = bool(
                has_capture and _c_amt is not None and 0 < _c_amt < _hold)
            ev = Evidence(
                job_id=settled.job_id,
                authorization_id=getattr(
                    settled, "authorization_id", ""),
                is_payer=True,
                readable=True,
                has_capture=has_capture,
                is_partial=_is_part,
                void_confirmed=(
                    to_status == "VOIDED"
                    or getattr(
                        settled, "voided_amount_cents", None) is not None),
                enum=enum_event,
                capture_ids=cap_ids,
                amount_cents=_c_amt,
                voided_amount_cents=getattr(
                    settled, "voided_amount_cents", None),
                authorized_cents=_authorized_cents_for_evidence(
                    settled),
            )
            new_state, _actions = transition(
                before_state, Event.E_FORCE_SETTLE, ev)
            # Validation only — actions not applied.
            if new_state != after_state:
                _report_fsm_divergence(
                    "force_settle", settled.job_id,
                    f"legacy settled {after_state}, FSM gives {new_state}")
        except Exception as e:  # noqa: BLE001 — tracking must never break force_settle
            _report_fsm_divergence(
                "force_settle", getattr(job, "job_id", "?"),
                f"FSM tracking failed: {e}")


    def _void_with_reconcile(self, job: Job, request_id: str,
                             op: str = "void_after_deadline") -> dict:
        """The void call failed — but it may have executed server-side.
        Retry with the same idempotency key first; if that also fails,
        ask PayPal for the authorization's actual state: VOIDED means
        the hold really was released, EXPIRED (round 14) means PayPal
        already released the hold itself — same terminal state;
        CAPTURED means it is too late to void, CREATED means the void
        never happened."""
        # v8.2 (item 6): this first POST is itself a retry — the
        # caller's POST lost its response — so the key replay is
        # audit-relevant. op names the owning public operation for
        # the event (void_after_deadline or force_settle).
        self._audit(op, job, idempotency_key=request_id,
                    outcome="retried",
                    reason="void POST lost its response; replaying the "
                           "stored idempotency key")
        try:
            result = self.paypal.void_authorization(
                job.authorization_id, request_id=request_id)
            return result
        except PayPalError:
            pass
        try:
            auth = self.paypal.get_authorization(job.authorization_id)
        except PayPalError as e:
            raise EscrowError(
                f"PayPal void failed and state could not be reconciled: {e}")
        status = auth.get("status")
        # Round 19 (Grok round-18 finding 1): a VOIDED/EXPIRED read that
        # lists a COMPLETED capture is not a clean void — money moved.
        # Do not return {} silently; the caller must not book VOIDED
        # over it.
        # Round 20 (Grok round-19 finding 2 + order-level check): persist
        # the capture and wedge BEFORE raising, so the next reconcile
        # or force-settle sees the capture on the job instead of a bare
        # raise that left nothing behind. The order is checked too —
        # the authorization resource may omit the capture.
        # Round 21: tri-state — an unread order wedges instead of
        # returning {} on an unknown.
        outcome, cap = self._first_completed_capture_including_order(
            job, auth)
        # Round 47 (Claude round-46 HIGH): the shared guard —
        # "unattributable" wedges here, never returns a clean void
        # over moved money.
        if self._wedge_on_unattributable(
                job, outcome, "_void_with_reconcile"):
            raise EscrowError(
                job.needs_review_reason or
                "captures unattributable (operator review needed)")
        if _is_hold_released(status):
            # Round 31 (C4): VOIDED, EXPIRED, and DENIED are one
            # terminal state — the hold is released. (EXPIRED: round
            # 14, ChatGPT round-13 Low — an expired hold is already
            # released, so the void is a no-op and the caller settles
            # VOIDED exactly as for a real void. DENIED: a denied
            # hold can never move money — same.)
            if outcome == "unread":
                self._set_review_flag(
                    job,
                    f"authorization is {status} but the order could not "
                    "be read (operator review needed)")
                self._save(job)
                raise EscrowError(
                    f"authorization is {status} but the order could not "
                    "be read (operator review needed)")
            if outcome == "ambiguous":
                # Round 28 (C5): a non-terminal capture is listed —
                # money may still move. Wedge, don't return a clean
                # void.
                self._set_review_flag(
                    job,
                    f"authorization is {status} but a non-terminal "
                    "capture is listed (operator review needed)")
                self._save(job)
                raise EscrowError(
                    f"authorization is {status} but a non-terminal "
                    "capture is listed (operator review needed)")
            if cap is not None:
                # Round 28 (G1): route through _record_listed_capture
                # — a stored capture is never overwritten by the raw
                # write. The wedge stands either way.
                record_outcome = self._record_listed_capture(job, cap)
                if record_outcome == "conflict":
                    reason = (f"authorization is {status} but lists "
                              f"capture {cap.get('id')} conflicting "
                              f"with the recorded capture "
                              f"{job.capture_id} (operator review "
                              "needed)")
                else:
                    reason = (f"authorization is {status} but lists a "
                              "completed capture (operator review "
                              "needed)")
                self._set_review_flag(job, reason)
                self._save(job)
                raise EscrowError(reason)
            return {}
        if status == "CAPTURED":
            raise EscrowError(
                "authorization already captured — cannot void")
        raise EscrowError(
            f"PayPal void failed; authorization is {status}")

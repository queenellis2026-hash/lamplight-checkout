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
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid

from .paypal_client import (PayPalClient, PayPalError, amount_to_cents,
                            first_completed_capture)
from .quote import quote_expired


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


def _trim_paypal_body(body) -> dict:
    """Cap a PayPal response body before it is persisted in the
    last_paypal_response envelope. Bodies at or under the cap are
    returned untouched (normal responses record fully and unchanged).
    An oversized body is trimmed deterministically — links to one
    entry, long strings and lists cut, nesting capped — and if it is
    still over the cap, the largest top-level keys are dropped first.
    As a last resort the body is replaced by a marker naming the keys
    it had, so the record always says what was dropped. Never raises.
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
    # Still over the cap: drop the largest top-level keys first
    # (deterministic order: size desc, then key name) until it fits.
    keys = sorted(trimmed,
                  key=lambda k: (-len(json.dumps(
                      trimmed[k], separators=(",", ":"),
                      sort_keys=True).encode("utf-8")), k))
    for key in keys:
        del trimmed[key]
        raw = json.dumps(trimmed, separators=(",", ":"), sort_keys=True)
        if len(raw.encode("utf-8")) <= ENVELOPE_BODY_MAX_BYTES:
            return trimmed
    return {"_trimmed": True, "keys": sorted(body)
            if isinstance(body, dict) else []}


class EscrowError(Exception):
    """A state transition was refused (wrong state, wrong caller, too early)."""


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
        # Amount actually captured, in cents, when known (recorded from
        # the capture result, or from the authorization's linked captures
        # on reconcile). None when the job was voided, or when the capture
        # was reconciled from an authorization that lists no capture.
        self.capture_amount_cents: int | None = None
        self.delivery_hash: str = ""
        self.settled_at: int | None = None
        # True when the capture was recorded by reconciling PayPal's
        # authorization state after the capture response was lost: the
        # money moved, but the capture was not observed through the
        # normal capture call. The capture id is the real one when
        # PayPal's authorization resource lists a COMPLETED capture, and
        # "" only when it lists none — that case stays unauditable.
        self.capture_reconciled: bool = False

        # The last successful PayPal response, so an operator can audit
        # what PayPal actually said without re-querying it. Envelope
        # (not the raw body alone): the void call returns no body, so
        # the raw body alone would say nothing — the envelope always
        # carries at least operation + status + id, plus the raw body
        # for the full audit. Only successful responses are recorded;
        # a failed call never overwrites the last good record.
        # {"operation": "authorize"|"capture"|"void"|"reconcile",
        #  "status": "CREATED"|"COMPLETED"|"VOIDED"|..., "id": <PayPal id>,
        #  "body": <raw PayPal response dict>}. None until the first
        # PayPal call succeeds (store files written before round 4
        # load with None via .get()).
        self.last_paypal_response: dict | None = None

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
            "capture_amount_cents": self.capture_amount_cents,
            "capture_reconciled": self.capture_reconciled,
            "delivery_hash": self.delivery_hash,
            "settled_at": self.settled_at,
            "last_paypal_response": self.last_paypal_response,
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
            "capture_amount_cents": self.capture_amount_cents,
            "delivery_hash": self.delivery_hash,
            "settled_at": self.settled_at,
            "capture_reconciled": self.capture_reconciled,
            "last_paypal_response": self.last_paypal_response,
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
        # .get(): store files written before round 3 have no
        # capture_amount_cents — they still load, with amount unknown.
        job.capture_amount_cents = data.get("capture_amount_cents")
        job.delivery_hash = data["delivery_hash"]
        job.settled_at = data["settled_at"]
        job.capture_reconciled = data["capture_reconciled"]
        # .get(): store files written before round 4 have no
        # last_paypal_response — they still load, with no audit record.
        job.last_paypal_response = data.get("last_paypal_response")
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

    def __init__(self, paypal: PayPalClient, *, store=None,
                 delivery_verifier=None):
        self.paypal = paypal
        if store is None:
            # Deferred import: app/store.py imports Job from this module.
            from .store import MemoryStore
            store = MemoryStore()
        self.store = store
        self.delivery_verifier = (
            delivery_verifier if delivery_verifier is not None
            else self._default_delivery_verifier)
        # One lock per job so delivery is exactly-once even if two
        # requests arrive at the same instant: the status check and the
        # state change happen inside the lock, so the second delivery
        # always sees DELIVERED and is refused.
        self._delivery_locks: dict[str, threading.Lock] = {}
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

    def _delivery_lock(self, job_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._delivery_locks.get(job_id)
            if lock is None:
                lock = threading.Lock()
                self._delivery_locks[job_id] = lock
            return lock

    @staticmethod
    def _request_id(job: Job, operation: str) -> str:
        """Stable idempotency key for one job + one operation. PayPal
        replays the original result when the same key is retried, so a
        lost response can be recovered without executing twice."""
        return f"lamplight-{operation}-{job.job_id}"

    def create_job(self, *, quote: dict, payer: str, agent: str,
                   deadline_seconds: int = 3600) -> Job:
        job = Job(job_id=str(uuid.uuid4())[:8], quote=quote, payer=payer,
                  agent=agent, deadline_seconds=deadline_seconds)
        self.store.save(job)
        return job

    def get_job(self, job_id: str) -> Job:
        job = self.store.get(job_id)
        if job is None:
            raise EscrowError(f"job not found: {job_id}")
        return job

    # -- hold ------------------------------------------------------------------
    def create_order(self, job: Job) -> dict:
        """Create the PayPal AUTHORIZE order for this job's quote. The
        worker's pay_to account is passed as the order payee, so the
        capture pays the worker — not the merchant that owns the API
        credentials. If order creation fails, the job is removed from
        the store instead of lingering as QUOTED with no order id."""
        if job.status != "QUOTED":
            raise EscrowError(f"cannot create order in state {job.status}")
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

    def authorize(self, job: Job) -> dict:
        """Place the hold. The buyer must have approved the order first
        (PayPal requires approval before authorize), and the quote must
        still be live — an expired quote is refused even if the order
        was created before it expired."""
        if job.status != "QUOTED" or not job.order_id:
            raise EscrowError(f"cannot authorize in state {job.status}")
        if quote_expired(job.quote):
            raise EscrowError("quote expired")
        request_id = self._request_id(job, "authorize")
        try:
            result = self.paypal.authorize_order(job.order_id,
                                                 request_id=request_id)
        except PayPalError:
            # The response may have been lost after PayPal authorized it.
            # Retry with the same idempotency key: PayPal replays the
            # original authorization instead of authorizing twice.
            try:
                result = self.paypal.authorize_order(job.order_id,
                                                     request_id=request_id)
            except PayPalError as e:
                raise EscrowError(f"PayPal authorize failed: {e}") from e
        auths = result.get("purchase_units", [{}])[0].get("payments", {}).get("authorizations", [])
        if not auths:
            raise EscrowError(f"authorize returned no authorization: {result}")
        auth = auths[0]
        if auth.get("status") != "CREATED":
            raise EscrowError(
                f"authorization not created (status {auth.get('status')})")
        job.authorization_id = auth["id"]
        job.status = "AUTHORIZED"
        self._record_paypal_response(job, operation="authorize",
                                     status=auth.get("status"),
                                     id_=auth.get("id"), body=result)
        self._save(job)
        return result

    # -- delivery --------------------------------------------------------------
    def submit_delivery(self, job: Job, *, agent: str, delivery_text: str,
                        now: int | None = None) -> str:
        """Worker agent marks delivery with a hash of the work. Exactly once,
        and only before the deadline — a delivery at or after the deadline
        is refused, so it can never block the payer's void.
        ``now`` is a test-only hook (lets tests simulate the deadline
        without sleeping); the server never passes it — production calls
        always use wall-clock time."""
        with self._delivery_lock(job.job_id):
            if job.status != "AUTHORIZED":
                raise EscrowError(f"cannot deliver in state {job.status}")
            if agent != job.agent:
                raise EscrowError("only the worker agent can deliver this job")
            if not delivery_text:
                raise EscrowError("empty delivery")
            effective_now = now if now is not None else int(time.time())
            if effective_now >= job.deadline:
                raise EscrowError("delivery deadline passed")
            job.delivery_hash = hashlib.sha256(delivery_text.encode()).hexdigest()
            job.status = "DELIVERED"
            self._save(job)
            return job.delivery_hash

    # -- release ---------------------------------------------------------------
    def capture(self, job: Job, *, payer: str, delivery_text: str) -> dict:
        """Release the held money to the worker agent. Payer only, delivered only.
        The original delivery text is required, and it is verified against
        the hash recorded on the job (via delivery_verifier — the hiring
        agent's verify_delivery when the server wires it) BEFORE any PayPal
        call: a delivery whose hash was never checked can never release
        funds, whatever the caller claims. Only settles on a COMPLETED
        capture — a DECLINED, PENDING or FAILED result is refused, so the
        receipt never claims money that did not move."""
        if job.status != "DELIVERED":
            raise EscrowError(f"cannot capture in state {job.status} (needs DELIVERED)")
        if payer != job.payer:
            raise EscrowError("only the payer can release this job")
        if not delivery_text:
            raise EscrowError("delivery text required for verification")
        if not self.delivery_verifier(job, delivery_text):
            raise EscrowError("delivery verification failed")
        request_id = self._request_id(job, "capture")
        try:
            result = self.paypal.capture_authorization(
                job.authorization_id, request_id=request_id)
        except PayPalError:
            result = self._capture_with_reconcile(job, request_id)
        if result.get("status") != "COMPLETED":
            raise EscrowError(
                f"capture not completed (status {result.get('status')})")
        job.capture_id = result.get("id", "")
        self._record_capture_amount(job, result)
        # The capture result is the last PayPal response that settled
        # this job — including the reconciled case, where the result is
        # the synthetic COMPLETED dict from _capture_with_reconcile
        # (its body carries "reconciled": True).
        self._record_paypal_response(job, operation="capture",
                                     status=result.get("status"),
                                     id_=result.get("id"), body=result)
        job.status = "CAPTURED"
        job.settled_at = int(time.time())
        self._save(job)
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
            "body": _trim_paypal_body(body if isinstance(body, dict) else {}),
        }

    @staticmethod
    def _record_capture_amount(job: Job, capture_like: dict) -> None:
        """Record the amount a capture moved, in cents, on the job.
        ``capture_like`` is a PayPal capture resource (or a dict with
        the same ``amount`` shape). An absent or unparseable amount
        leaves the field unknown (None) — the id still stands."""
        amount = capture_like.get("amount")
        if isinstance(amount, dict):
            try:
                job.capture_amount_cents = amount_to_cents(amount.get("value"))
            except ValueError:
                job.capture_amount_cents = None

    def _record_captured_authorization(self, job: Job, auth: dict) -> None:
        """Record an authorization PayPal reports as CAPTURED on the job.
        Sets the reconciled flag (the capture was not observed through
        the normal capture call) and, when the authorization resource
        lists a COMPLETED capture, its real id and amount. When it lists
        none, the id stays "" and the amount stays unknown — that case
        is flagged reconciled and documented as unauditable. This is
        capture bookkeeping only: the reconcile read itself is recorded
        by the reconcile tail (_record_reconcile_read), and in the
        capture path capture() records the capture result afterwards.
        Caller sets status/settled_at."""
        job.capture_reconciled = True
        cap = first_completed_capture(auth)
        if cap is not None:
            job.capture_id = cap.get("id", "")
            self._record_capture_amount(job, cap)

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

    def _capture_with_reconcile(self, job: Job, request_id: str) -> dict:
        """The capture call failed — but it may have executed server-side
        before the response was lost. First retry with the same
        idempotency key (PayPal replays the original capture). If the
        retry also fails, ask PayPal for the authorization's actual
        state: if it is CAPTURED the money moved, so record the job as
        captured — with the real capture id and amount from the
        authorization's linked captures when PayPal lists them (only
        when it lists none does the id stay "" with the reconciled flag)
        — and return a COMPLETED result so a receipt can be produced."""
        try:
            return self.paypal.capture_authorization(
                job.authorization_id, request_id=request_id)
        except PayPalError:
            pass
        try:
            auth = self.paypal.get_authorization(job.authorization_id)
        except PayPalError as e:
            raise EscrowError(
                f"PayPal capture failed and state could not be reconciled: {e}")
        if auth.get("status") == "CAPTURED":
            self._record_captured_authorization(job, auth)
            self._save(job)
            return {"id": job.capture_id, "status": "COMPLETED",
                    "reconciled": True}
        raise EscrowError(
            f"PayPal capture failed; authorization is {auth.get('status')}")

    # -- reconcile -----------------------------------------------------------
    def reconcile_job(self, job: Job) -> dict:
        """Force one open job's state from PayPal's actual authorization
        state. AUTHORIZED/DELIVERED only — settled jobs are left alone.
        CREATED means the hold is still there (no change); CAPTURED means
        the money moved outside the normal capture call, so the job is
        recorded CAPTURED (real id + amount from the linked captures
        when PayPal lists them, reconciled flag always); VOIDED means
        the hold was released, so the job is recorded VOIDED. A PayPal
        error leaves the job untouched and is reported, never raised —
        the admin endpoint must reconcile the rest.
        Returns {"job_id", "from_status", "to_status", "updated",
        "capture_id", "error"} (error "" when there is none)."""
        if job.status not in ("AUTHORIZED", "DELIVERED"):
            return {"job_id": job.job_id, "from_status": job.status,
                    "to_status": job.status, "updated": False,
                    "capture_id": job.capture_id,
                    "error": "job is not open"}
        from_status = job.status
        try:
            auth = self.paypal.get_authorization(job.authorization_id)
        except PayPalError as e:
            return {"job_id": job.job_id, "from_status": from_status,
                    "to_status": job.status, "updated": False,
                    "capture_id": job.capture_id, "error": str(e)}
        status = auth.get("status")
        updated = False
        to_status = job.status
        if status == "CREATED":
            # The hold is still there — no state change.
            pass
        elif status == "CAPTURED":
            self._record_captured_authorization(job, auth)
            job.status = "CAPTURED"
            job.settled_at = int(time.time())
            updated = True
            to_status = "CAPTURED"
        elif status == "VOIDED":
            job.status = "VOIDED"
            job.settled_at = int(time.time())
            updated = True
            to_status = "VOIDED"
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

    def reconcile_open_jobs(self) -> dict:
        """Walk every AUTHORIZED/DELIVERED job and reconcile it from
        PayPal — the admin endpoint's workhorse, for use after a
        prolonged outage. Returns a small summary: {"checked",
        "updated" (list of {job_id, from, to, capture_id}),
        "still_pending" (job ids PayPal still reports CREATED, or
        whose state could not be read), "errors" (list of
        {job_id, error})}. One job's failure never stops the rest."""
        checked = 0
        updated: list[dict] = []
        still_pending: list[str] = []
        errors: list[dict] = []
        for job in self.store.all():
            if job.status not in ("AUTHORIZED", "DELIVERED"):
                continue
            checked += 1
            result = self.reconcile_job(job)
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
                "still_pending": still_pending, "errors": errors}

    # -- timeout ---------------------------------------------------------------
    def void_after_deadline(self, job: Job, *, payer: str, now: int | None = None) -> dict:
        """Release the hold back to the payer when the worker never delivered
        and the deadline has passed.
        ``now`` is a test-only hook (lets tests simulate the deadline
        without sleeping); the server never passes it — production calls
        always use wall-clock time, so no client can forge a future
        timestamp and void before the real deadline."""
        if job.status != "AUTHORIZED":
            raise EscrowError(f"cannot void in state {job.status} (needs AUTHORIZED)")
        if payer != job.payer:
            raise EscrowError("only the payer can void this job")
        effective_now = now if now is not None else int(time.time())
        if effective_now < job.deadline:
            raise EscrowError("deadline not reached")
        request_id = self._request_id(job, "void")
        try:
            result = self.paypal.void_authorization(
                job.authorization_id, request_id=request_id)
        except PayPalError:
            result = self._void_with_reconcile(job, request_id)
        # The void result is the last PayPal response that settled this
        # job. Its body is normally {} (PayPal's void returns 204), so
        # the envelope's operation + status + id are the audit trail —
        # that is why the field is an envelope, not the raw body alone.
        # (In the reconcile path the get_authorization fetch is an
        # internal confirmation step, not the settling response, so it
        # is not recorded separately here — the void record stands.)
        self._record_paypal_response(job, operation="void",
                                     status="VOIDED",
                                     id_=job.authorization_id, body=result)
        job.status = "VOIDED"
        job.settled_at = int(time.time())
        self._save(job)
        return result

    def _void_with_reconcile(self, job: Job, request_id: str) -> dict:
        """The void call failed — but it may have executed server-side.
        Retry with the same idempotency key first; if that also fails,
        ask PayPal for the authorization's actual state: VOIDED means
        the hold really was released, CAPTURED means it is too late to
        void, CREATED means the void never happened."""
        try:
            return self.paypal.void_authorization(
                job.authorization_id, request_id=request_id)
        except PayPalError:
            pass
        try:
            auth = self.paypal.get_authorization(job.authorization_id)
        except PayPalError as e:
            raise EscrowError(
                f"PayPal void failed and state could not be reconciled: {e}")
        status = auth.get("status")
        if status == "VOIDED":
            return {}
        if status == "CAPTURED":
            raise EscrowError(
                "authorization already captured — cannot void")
        raise EscrowError(
            f"PayPal void failed; authorization is {status}")

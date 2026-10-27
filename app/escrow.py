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
    unrecorded
  * jobs live in a pluggable store (app/store.py) and every state
    change is saved, so a restart never loses a job or its PayPal ids
"""
from __future__ import annotations

import hashlib
import threading
import time
import uuid

from .paypal_client import PayPalClient, PayPalError
from .quote import quote_expired


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
        self.delivery_hash: str = ""
        self.settled_at: int | None = None
        # True when the capture was recorded by reconciling PayPal's
        # authorization state after the capture response was lost: the
        # money moved, but the capture id was never observed.
        self.capture_reconciled: bool = False

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
            "capture_reconciled": self.capture_reconciled,
            "delivery_hash": self.delivery_hash,
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
            "delivery_hash": self.delivery_hash,
            "settled_at": self.settled_at,
            "capture_reconciled": self.capture_reconciled,
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
        job.delivery_hash = data["delivery_hash"]
        job.settled_at = data["settled_at"]
        job.capture_reconciled = data["capture_reconciled"]
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
        job.status = "CAPTURED"
        job.settled_at = int(time.time())
        self._save(job)
        return result

    def _capture_with_reconcile(self, job: Job, request_id: str) -> dict:
        """The capture call failed — but it may have executed server-side
        before the response was lost. First retry with the same
        idempotency key (PayPal replays the original capture). If the
        retry also fails, ask PayPal for the authorization's actual
        state: if it is CAPTURED the money moved, so record the job as
        captured (capture id unobserved — see capture_reconciled) and
        return a COMPLETED result so a receipt can be produced."""
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
            job.capture_reconciled = True
            self._save(job)
            return {"id": "", "status": "COMPLETED", "reconciled": True}
        raise EscrowError(
            f"PayPal capture failed; authorization is {auth.get('status')}")

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

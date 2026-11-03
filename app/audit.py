"""Structured, correlated audit events for financial operations.

v8.2 (production-readiness item 6): every financial operation records
one structured event — which operation ran, which idempotency key it
used, what PayPal reported vs what was booked locally, and why it was
retried, abandoned, or wedged — plus a ledger-vs-PayPal reconciliation
report that flags divergences between the books and fresh PayPal reads.

Additive by design:

* Recording never raises. A store failure is swallowed after logging,
  so observability can never break money movement. (A bad outcome
  string is a programming error and raises at event-build time —
  that is a bug in the caller, not a runtime condition.)
* Events are correlated by ``job_id`` + ``idempotency_key``: the
  service records one event per public operation invocation, plus
  extra events for in-flight retries (replayed keys) and abandoned
  intents. Ordering within a job is by ``ts``.
* ``paypal_reported`` is PayPal's most recent report envelope as kept
  on the job (``job.last_paypal_response``) — already allowlisted by
  ``sanitize_paypal_body``, so no buyer data can reach the audit log.
  On a failed operation it is the last known report, which may be
  from an earlier operation or None; the ``reason`` carries the
  failure detail.
* ``recorded`` is a snapshot of the job's money fields after the
  operation (status, capture ids/amounts, voided amount, review flag).

This module imports nothing from the app: the service passes
already-sanitized payloads in, so there is no import cycle with
app/escrow.py.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


OUTCOME_COMPLETED = "completed"
OUTCOME_RETRIED = "retried"
OUTCOME_ABANDONED = "abandoned"
OUTCOME_WEDGED = "wedged"
OUTCOME_FAILED = "failed"

OUTCOMES = frozenset({
    OUTCOME_COMPLETED,
    OUTCOME_RETRIED,
    OUTCOME_ABANDONED,
    OUTCOME_WEDGED,
    OUTCOME_FAILED,
})

# Cap for file-backed stores (JsonFileStore): insertion-ordered, oldest
# first evicted — the same bounded-file rationale as the webhook event
# id cap in app/store.py. SQLite needs no cap (real database; prune
# with SQL when operations wants it).
AUDIT_EVENT_MAX = 5000


@dataclass
class AuditEvent:
    """One structured audit event for a financial operation."""
    event_id: str
    ts: int
    job_id: str
    operation: str
    outcome: str
    idempotency_key: str | None = None
    paypal_reported: dict | None = None
    recorded: dict | None = None
    reason: str | None = None

    def __post_init__(self):
        if self.outcome not in OUTCOMES:
            raise ValueError(
                f"unknown audit outcome {self.outcome!r}; "
                f"expected one of {sorted(OUTCOMES)}")

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "ts": self.ts,
            "job_id": self.job_id,
            "operation": self.operation,
            "outcome": self.outcome,
            "idempotency_key": self.idempotency_key,
            "paypal_reported": self.paypal_reported,
            "recorded": self.recorded,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AuditEvent":
        return cls(
            event_id=data["event_id"],
            ts=int(data["ts"]),
            job_id=data["job_id"],
            operation=data["operation"],
            outcome=data["outcome"],
            idempotency_key=data.get("idempotency_key"),
            paypal_reported=data.get("paypal_reported"),
            recorded=data.get("recorded"),
            reason=data.get("reason"),
        )


class AuditLog:
    """Recorder for audit events, backed by the job store.

    The store holds the events durably alongside the jobs (a new
    table for SqliteStore, a capped list in the JSON payload for
    JsonFileStore, an in-memory list for MemoryStore). The store
    interface is duck-typed: ``append_audit_event(dict)`` and
    ``audit_events(job_id=None, limit=None)``.
    """

    def __init__(self, store):
        self._store = store

    def record(self, *, job_id: str, operation: str,
               idempotency_key: str | None = None,
               paypal_reported: dict | None = None,
               recorded: dict | None = None,
               outcome: str = OUTCOME_COMPLETED,
               reason: str | None = None) -> AuditEvent:
        """Build one event and append it to the store. Lets store
        errors propagate — the service wraps every record call so
        audit can never break a financial operation."""
        event = AuditEvent(
            event_id=uuid.uuid4().hex,
            ts=int(time.time()),
            job_id=job_id,
            operation=operation,
            outcome=outcome,
            idempotency_key=idempotency_key,
            paypal_reported=paypal_reported,
            recorded=recorded,
            reason=reason,
        )
        self._store.append_audit_event(event.to_dict())
        return event

    def events_for_job(self, job_id: str,
                       limit: int | None = None) -> list[AuditEvent]:
        """All events for one job, oldest first."""
        return [AuditEvent.from_dict(d)
                for d in self._store.audit_events(job_id=job_id,
                                                 limit=limit)]

    def all_events(self, limit: int | None = None) -> list[AuditEvent]:
        """All events across jobs, oldest first."""
        return [AuditEvent.from_dict(d)
                for d in self._store.audit_events(job_id=None,
                                                 limit=limit)]

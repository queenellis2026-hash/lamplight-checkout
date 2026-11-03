"""PayPal escrow — state transition dispatcher (Phase 2 skeleton).

Design doc: TRANSITION-MATRIX-MERGED.md (Rev 3, 13 states).
Phase 2 notes: PHASE2-NOTES.md.

This module is the single dispatch point for ALL escrow state changes.
Per §1.1 ("the matrix IS the implementation"), every (state, event) pair
in the §5 table becomes a branch in ``transition()``. There are no ad-hoc
if/elif chains outside the dispatcher.

Phase 2 scope (skeleton only):
  - Exceptions: UnhandledTransition, GuardRejection
  - State enum (13 states, Rev 3 numbering)
  - Event enum (49 events)
  - Action value objects (dataclasses)
  - resolve_state() — the §7.1 precedence waterfall (pure function)
  - make_intent_key() — idempotency keys tied to (job_id, kind, attempt)
  - transition() — signature + docstring + smoke-test cells only

Phase 3 (NOT here): the full §5 table cells.

The dispatcher NEVER performs network I/O (§1.4). It is a pure function:
(state, event) -> (new_state, actions). The caller persists intents,
issues PayPal HTTP calls, and executes the returned actions.

Gate 1: APPROVED (Gemini, 2026-11-01). Rev 3.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from dataclasses import dataclass, field
from enum import Enum

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class UnhandledTransition(Exception):
    """Raised for any (state, event) pair not in the §5 table.

    This is a matrix-completeness defect — an unrecoverable bug, not a
    business refusal. Loud, logged, test-visible. HTTP 500 in production.

    Per I5 (Totality): the table is the complete specification, and
    incompleteness is a bug, never a default. The dispatcher's default
    arm is ``raise``, never ``pass``.
    """

    def __init__(self, state, event):
        self.state = state
        self.event = event
        super().__init__(
            f"UnhandledTransition: no table cell for "
            f"(state={state}, event={event}). "
            f"This is a dispatcher bug — add the cell or fix the caller."
        )
        log.error("UnhandledTransition(state=%r, event=%r)", state, event)

    #: HTTP status when this escapes to the API layer.
    http_status = 500


class GuardRejection(Exception):
    """Raised for domain guard violations — expected, explicit refusals.

    Examples: non-payer calling capture(), capture after the deadline,
    submit_delivery on a job that is not AUTHORIZED.

    DISTINCT from UnhandledTransition: a GuardRejection is a legal
    business refusal. An UnhandledTransition is a matrix-completeness
    defect. Per I5, every cell in the table evaluates to exactly one of:
    state mutation, GuardRejection, or explicit idempotent no-op.

    Architectural (new reviewer): this is a PURE domain exception — no
    HTTP concepts. The API layer (app/server.py) maps `category` to HTTP
    status codes via HTTP_STATUS_FOR_CATEGORY. `retry_after_seconds` is
    a domain hint (wait before retrying), not an HTTP header directive.
    """

    #: Domain categories. server.py maps these to HTTP status codes.
    VALIDATION = "validation"    # -> 400 Bad Request
    FORBIDDEN = "forbidden"      # -> 403 Forbidden (default)
    CONFLICT = "conflict"        # -> 409 Conflict
    GONE = "gone"                # -> 410 Gone
    RETRYABLE = "retryable"      # -> 503 Service Unavailable

    def __init__(self, reason: str, category: str = "forbidden",
                 retry_after_seconds: int | None = None):
        self.reason = reason
        self.category = category
        # Domain hint: caller should wait this many seconds before retrying.
        # server.py translates this to the Retry-After HTTP header.
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"GuardRejection({category}): {reason}")


# ---------------------------------------------------------------------------
# States — §3.2 (Rev 3, 13 discrete operational states)
# ---------------------------------------------------------------------------

class State(str, Enum):
    """Discrete operational states. Each has different legal transitions.

    Rev 3 numbering (S5 AUTHORIZED_PENDING eliminated as a ghost state;
    old S4 QUOTED_DENIED_RESET merged into S1 per the history-flag rule).
    """

    S1_QUOTED_FRESH = "S1_QUOTED_FRESH"
    S2_QUOTED_ORDERED = "S2_QUOTED_ORDERED"
    S3_QUOTED_ATTEMPTED = "S3_QUOTED_ATTEMPTED"
    S4_AUTHORIZED_LIVE = "S4_AUTHORIZED_LIVE"
    S5_AUTHORIZED_WEDGED = "S5_AUTHORIZED_WEDGED"
    S6_DELIVERED_OPEN = "S6_DELIVERED_OPEN"
    S7_DELIVERED_PENDING = "S7_DELIVERED_PENDING"
    S8_DELIVERED_PARTIAL_RECORDED = "S8_DELIVERED_PARTIAL_RECORDED"
    S9_DELIVERED_WEDGED = "S9_DELIVERED_WEDGED"
    S10a_CAPTURED_FULL = "S10a_CAPTURED_FULL"
    S10b_CAPTURED_SPLIT = "S10b_CAPTURED_SPLIT"
    S10c_CAPTURED_UNAUDITED = "S10c_CAPTURED_UNAUDITED"
    S11_VOIDED_SETTLED = "S11_VOIDED_SETTLED"

    @property
    def is_terminal(self) -> bool:
        """I2: terminal states are strictly absorbing sinks."""
        return self in (
            State.S10a_CAPTURED_FULL,
            State.S10b_CAPTURED_SPLIT,
            State.S10c_CAPTURED_UNAUDITED,
            State.S11_VOIDED_SETTLED,
        )

    @property
    def is_wedged(self) -> bool:
        return self in (
            State.S5_AUTHORIZED_WEDGED,
            State.S9_DELIVERED_WEDGED,
        )


# ---------------------------------------------------------------------------
# Events — §4 (Rev 3)
# ---------------------------------------------------------------------------

class Event(str, Enum):
    """All events the dispatcher can receive.

    Categories (§4):
      E_*   client API calls (Phase 1 intents) and timers
      R_*   PayPal synchronous responses (Phase 2 outcomes)
      READ_* PayPal read results (reconcile evidence)
      ENUM_* capture-enumeration outcomes
      WH_*  webhooks — only the 4 dispatchable ones reach transition();
            the 6 routing-layer ones are handled in the webhook gateway (§4.7)
      T_*   timer events
    """

    # §4.1 Client API calls (Phase 1 intents — persist intent, then POST)
    E_CREATE_JOB = "E_CREATE_JOB"
    E_CREATE_ORDER = "E_CREATE_ORDER"
    E_START_AUTHORIZE = "E_START_AUTHORIZE"
    E_SUBMIT_DELIVERY = "E_SUBMIT_DELIVERY"
    E_START_CAPTURE = "E_START_CAPTURE"
    E_START_CAPTURE_PARTIAL = "E_START_CAPTURE_PARTIAL"
    E_START_RESIDUAL_VOID = "E_START_RESIDUAL_VOID"  # Rev 3 (Gemini Defect 2)
    E_CANCEL_PENDING = "E_CANCEL_PENDING"
    E_VOID_DEADLINE = "E_VOID_DEADLINE"
    E_FORCE_SETTLE = "E_FORCE_SETTLE"
    E_RECONCILE = "E_RECONCILE"

    # §4.2 PayPal synchronous responses (Phase 2 outcomes)
    R_AUTH_CREATED = "R_AUTH_CREATED"
    R_AUTH_DENIED = "R_AUTH_DENIED"
    R_AUTH_500 = "R_AUTH_500"
    R_AUTH_MALFORMED = "R_AUTH_MALFORMED"
    R_CAP_COMPLETED = "R_CAP_COMPLETED"
    R_CAP_DECLINED = "R_CAP_DECLINED"
    R_CAP_500 = "R_CAP_500"
    R_PARTIAL_CAP_COMPLETED = "R_PARTIAL_CAP_COMPLETED"
    # Meta-review (explicit unaudited path): PayPal reports COMPLETED but
    # provides no capture id. The caller dispatches this instead of
    # R_CAP_COMPLETED so the unaudited path is explicit, not a silent
    # filter of empty ids.
    E_UNAUDITED_CAPTURE = "E_UNAUDITED_CAPTURE"
    R_VOID_OK = "R_VOID_OK"
    R_VOID_422 = "R_VOID_422"
    R_VOID_500 = "R_VOID_500"
    # NOTE: R_VOID_FAIL is used in the §5.8 table ("R_VOID_FAIL → S8, keep
    # for retry") but is missing from the §4.2 event list — a doc
    # inconsistency. Treated here as the generic "residual void POST
    # failed" outcome (covers R_VOID_422/R_VOID_500 for the residual leg).
    R_VOID_FAIL = "R_VOID_FAIL"
    R_OLD_ORDER_ACTIVE = "R_OLD_ORDER_ACTIVE"
    R_OLD_ORDER_DEAD = "R_OLD_ORDER_DEAD"

    # §4.3 PayPal read results (reconcile evidence, carried on E_RECONCILE)
    READ_CREATED = "READ_CREATED"
    READ_PENDING = "READ_PENDING"
    READ_PARTIAL = "READ_PARTIAL"
    READ_CAPTURED = "READ_CAPTURED"
    READ_RELEASED = "READ_RELEASED"
    READ_500 = "READ_500"

    # §4.4 Capture-enumeration outcomes (from CaptureEnumeration, r54)
    ENUM_FOUND = "ENUM_FOUND"
    ENUM_AMBIGUOUS = "ENUM_AMBIGUOUS"
    ENUM_UNATTRIBUTABLE = "ENUM_UNATTRIBUTABLE"
    ENUM_NONE = "ENUM_NONE"
    ENUM_UNREAD = "ENUM_UNREAD"
    ENUM_ORDER_UNREAD = "ENUM_ORDER_UNREAD"

    # §4.5 Webhooks — dispatchable subset (resolved, non-terminal job only)
    WH_AUTH_CREATED = "WH_AUTH_CREATED"
    WH_AUTH_VOIDED = "WH_AUTH_VOIDED"
    WH_CAP_COMPLETED = "WH_CAP_COMPLETED"
    WH_CAP_DENIED = "WH_CAP_DENIED"

    # §4.7 Webhooks — routing-layer subset (handled in the webhook gateway
    # BEFORE resolve_state(); these NEVER reach transition()).
    WH_NO_JOB = "WH_NO_JOB"
    WH_NO_AUTH_ID = "WH_NO_AUTH_ID"
    WH_UNKNOWN_TYPE = "WH_UNKNOWN_TYPE"
    WH_JOB_SETTLED = "WH_JOB_SETTLED"
    WH_DUPLICATE = "WH_DUPLICATE"
    WH_READ_FAIL = "WH_READ_FAIL"

    # §4.6 Timer events
    T_QUOTE_EXPIRY = "T_QUOTE_EXPIRY"
    T_DEADLINE = "T_DEADLINE"

    @property
    def reaches_dispatcher(self) -> bool:
        """False for routing-layer webhooks (§4.7) — they never enter §5."""
        return self not in (
            Event.WH_NO_JOB,
            Event.WH_NO_AUTH_ID,
            Event.WH_UNKNOWN_TYPE,
            Event.WH_JOB_SETTLED,
            Event.WH_DUPLICATE,
            Event.WH_READ_FAIL,
        )


# ---------------------------------------------------------------------------
# Actions — value objects returned by transition() (§7)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlaceHold:
    """Phase 1 authorize intent persisted; caller POSTs the authorize."""
    order_id: str


def _as_capture_ids(ids) -> tuple[str, ...]:
    """Capture ids are a tuple of non-empty strings. A bare string must
    not become one id per character (`tuple("CAP1")` → `('C','A','P','1')`).

    Strict validation (meta-review): empty or blank ids raise ValueError.
    An unaudited capture (PayPal COMPLETED with no id) must use the
    explicit E_UNAUDITED_CAPTURE event — silently filtering empty ids
    hides bad data.
    """
    if isinstance(ids, str) or not isinstance(ids, (tuple, list)):
        raise ValueError(
            "capture_ids must be a tuple of capture id strings, not "
            f"{type(ids).__name__}")
    out = tuple(str(c).strip() for c in ids)
    for cap_id in out:
        if not cap_id:
            raise ValueError(
                f"capture id must be a non-empty string, got {cap_id!r}")
    return out


@dataclass(frozen=True)
class RecordCapture:
    """Book a capture on the job. IDs are a tuple, compared as sets
    (§6.4 fix — the synthetic "A+B" join is gone)."""
    capture_ids: tuple[str, ...] = ()
    amount_cents: int | None = None

    def __post_init__(self):
        # None amount = unknown (malformed/absent from PayPal) — the
        # ID is recorded, the amount stays unknown. The executor
        # (_apply_dispatcher_actions) books the ID with None and
        # leaves job.capture_amount_cents alone. This mirrors legacy
        # _record_capture_amount's "absent amount leaves the books
        # alone" rule.
        # Gemini v5 review (Fix 4, LOW): reject non-int and negative amounts.
        if self.amount_cents is not None:
            if isinstance(self.amount_cents, bool) or not isinstance(self.amount_cents, int):
                raise ValueError("RecordCapture.amount_cents must be an int")
            if self.amount_cents < 0:
                raise ValueError("RecordCapture.amount_cents must be >= 0")
        object.__setattr__(self, "capture_ids", _as_capture_ids(self.capture_ids))
        # Meta-review: blank/empty-string ids are NOT allowed — they raise.
        # An unaudited capture (PayPal COMPLETED with no id) uses the
        # explicit E_UNAUDITED_CAPTURE event, whose handler constructs
        # RecordCapture with capture_ids=() (empty tuple, no blank ids).


@dataclass(frozen=True)
class PostVoid:
    """Caller POSTs a void against this authorization."""
    authorization_id: str

    def __post_init__(self):
        if not self.authorization_id or not str(self.authorization_id).strip():
            raise ValueError("PostVoid.authorization_id must be non-empty")


@dataclass(frozen=True)
class RecordVoid:
    """Book a confirmed void.

    ``resolve_state`` returns S10b only when ``voided_amount_cents`` is
    not None. A NoOp note does not book that figure. 0 is a real amount.
    """
    amount_cents: int
    authorization_id: str = ""

    def __post_init__(self):
        if self.amount_cents is None:
            raise ValueError("RecordVoid.amount_cents must not be None")
        if isinstance(self.amount_cents, bool) or not isinstance(self.amount_cents, int):
            raise ValueError("RecordVoid.amount_cents must be an int")
        if self.amount_cents < 0:
            raise ValueError("RecordVoid.amount_cents must be >= 0")


@dataclass(frozen=True)
class SetReviewFlag:
    """Wedge the job: most operations refused until force_settle.

    amount_cents carries the display amount (e.g. the pending intent's
    attempted amount) so the wedge reason and amount never drift apart.
    """
    reason: str
    amount_cents: int | None = None


@dataclass(frozen=True)
class ClearReviewFlag:
    """Unwedge the job."""


@dataclass(frozen=True)
class SaveIntent:
    """Persist a Phase 1 intent BEFORE any PayPal POST leaves (§1.4).

    The key comes from make_intent_key() — cryptographically tied to
    (job_id, kind, attempt). Crash-recovery anchor: if the server dies
    between the POST and the response, the persisted intent proves the
    operation is in flight.
    """
    key: str
    amount_cents: int | None
    kind: str  # "authorize" | "capture" | "capture_partial" | "residual_void"

    def __post_init__(self):
        # Gemini Phase 3 review: a None amount would silently persist an
        # intent with no amount, breaking crash recovery. Fail loud —
        # EXCEPT for kind="capture" (full capture): the legacy
        # convention is amount_cents=None for full captures, int for
        # partials (refined Fix 1).
        if self.amount_cents is None and self.kind != "capture":
            raise ValueError(
                f"SaveIntent.amount_cents must not be None (kind={self.kind})")
        # Gemini v5 review (Fix 4, LOW): reject non-int and negative amounts.
        if self.amount_cents is not None:
            if isinstance(self.amount_cents, bool) or not isinstance(self.amount_cents, int):
                raise ValueError("SaveIntent.amount_cents must be an int")
            if self.amount_cents < 0:
                raise ValueError("SaveIntent.amount_cents must be >= 0")


@dataclass(frozen=True)
class ClearIntent:
    """Clear the pending intent after settlement or decline."""


@dataclass(frozen=True)
class SaveResidualVoidIntent:
    """Persist the residual-void intent for a partial capture (S8).

    Separate from SaveIntent because the residual void is the second
    network hop — partial capture (S8) or a deadline/force void that
    must not be marked settled until R_VOID_OK. Crash-recovery anchor
    (§1.4). The caller POSTs the void; the key proves it is in flight.
    """
    key: str


@dataclass(frozen=True)
class SaveVoidIntent:
    """Persist a void intent for a full authorization void.

    Gemini Fix 3 (HIGH): distinct from SaveResidualVoidIntent, which is
    strictly for partial-capture residual voids (S8). Reusing the
    residual action for plain authorization voids corrupts crash-recovery
    logs — workers cannot tell an in-flight residual balance void from
    a full authorization cancellation.
    """
    key: str
    authorization_id: str


@dataclass(frozen=True)
class ReadOldOrder:
    """Phase 1 of the S3 E_CREATE_ORDER read-guard (§6.5).

    The caller reads the old PayPal order, then dispatches R_OLD_ORDER_ACTIVE
    (adopt the live hold → S4, never double-authorize) or R_OLD_ORDER_DEAD
    (safe to issue a fresh order → S2).
    """
    order_id: str


@dataclass(frozen=True)
class DeleteJob:
    """Delete an expired quote with nothing attempted (S1/S2 T_QUOTE_EXPIRY)."""


@dataclass(frozen=True)
class EmitReceipt:
    """Render the settlement receipt."""
    kind: str  # "full" | "split" | "unaudited"


@dataclass(frozen=True)
class NoOp:
    """Explicit idempotent no-op with read-only audit logging (I5).

    This is NOT silent fallthrough — it is a deliberate, documented cell
    in the table. Used for defensive rows such as the S1 webhook cells
    (unreachable per §4.7, kept for table completeness).
    """
    note: str = ""


#: Union of all action types transition() may return.
Action = (
    PlaceHold | RecordCapture | PostVoid | RecordVoid | SetReviewFlag
    | ClearReviewFlag | SaveIntent | ClearIntent | SaveResidualVoidIntent
    | SaveVoidIntent
    | ReadOldOrder | DeleteJob | EmitReceipt | NoOp
)


# ---------------------------------------------------------------------------
# Evidence — carrier for compound-cell data (§7, Phase 3)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Evidence:
    """Evidence and caller context for transition() cells.

    The dispatcher is a pure function: (state, event, evidence) ->
    (new_state, actions). Compound table cells like
    ``E_RECONCILE + READ_PARTIAL + ENUM_FOUND`` are expressed as
    ``transition(S4, E_RECONCILE, Evidence(read=READ_PARTIAL,
    enum=ENUM_FOUND))``. The caller attaches the PayPal read result
    and enum outcome BEFORE dispatching (§1.4: I/O never happens inside
    transition()).

    Guard fields (is_payer, deadline_passed, delivery_verified) let the
    table enforce domain guards as explicit cells per §7 rule 3 —
    a failed guard raises GuardRejection (HTTP 4xx), never silently
    passes.
    """

    # Caller context (for intent-key generation)
    job_id: str = ""
    attempt: int = 1
    order_id: str = ""
    authorization_id: str = ""

    # Read evidence (E_RECONCILE, E_VOID_DEADLINE compound cells)
    read: Event | None = None          # READ_* result
    enum: Event | None = None          # ENUM_* outcome

    # Force-settle evidence (E_FORCE_SETTLE compound cells)
    readable: bool = False             # PayPal order was readable
    has_capture: bool = False          # evidence shows a capture
    is_partial: bool = False           # the capture was partial
    void_confirmed: bool = False       # residual void confirmed/abandoned

    # Domain guards (§7 rule 3 — failed guard → GuardRejection)
    # is_payer defaults to False (fail-closed): a caller that forgets
    # the field must not get payer rights by accident.
    is_payer: bool = False             # caller is the job payer
    deadline_passed: bool = False      # wall-clock passed job.deadline
    delivery_verified: bool = False    # delivery hash verified

    # Amounts (for RecordCapture / SaveIntent / RecordVoid)
    amount_cents: int | None = None
    voided_amount_cents: int | None = None
    # I1 fund conservation: authorized amount for captured+voided check.
    authorized_cents: int | None = None
    capture_ids: tuple = ()            # capture IDs from the read

    # Pending capture intent (v8.2 production-readiness item 2): the FSM
    # models the in-flight intent lifecycle on E_RECONCILE instead of
    # falling back to legacy. Background reconcile is READ-ONLY — it
    # never replays the key; only the payer's explicit retry does.
    pending_intent_id: str | None = None      # pending_capture_request_id
    pending_intent_amount_cents: int | None = None
    # True when the caller knows the intent is stale (the sweeper only
    # processes intents past the staleness threshold). A stale intent's
    # key had time to land — if no capture is listed, it's dead.
    pending_intent_stale: bool = False

    def __post_init__(self):
        object.__setattr__(self, "capture_ids", _as_capture_ids(self.capture_ids))


# ---------------------------------------------------------------------------
# JobSnapshot — strict typed view of a job record for resolve_state()
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class JobSnapshot:
    """Strict, typed snapshot of the job fields resolve_state() needs.

    Replaces duck-typing with explicit validation: from_job() raises
    AttributeError if a required field is missing, so malformed jobs
    fail loud at the boundary instead of silently resolving wrong.
    """
    job_id: str
    status: str  # "QUOTED" | "AUTHORIZED" | "DELIVERED" | "CAPTURED" | "VOIDED"
    needs_review: bool = False
    pending_capture_request_id: str | None = None
    capture_id: str = ""
    capture_amount_cents: int | None = None
    voided_amount_cents: int | None = None
    settlement_method: str | None = None
    authorize_attempted: bool = False
    order_id: str | None = None

    @classmethod
    def from_job(cls, job) -> "JobSnapshot":
        """Build a snapshot from any job-like object.

        Required fields (status, capture_id, capture_amount_cents,
        voided_amount_cents, pending_capture_request_id, order_id)
        raise AttributeError if missing. Optional fields default
        sensibly if absent.
        """
        # Required — loud AttributeError if missing.
        status = job.status
        capture_id = job.capture_id
        capture_amount_cents = job.capture_amount_cents
        voided_amount_cents = job.voided_amount_cents
        pending_capture_request_id = job.pending_capture_request_id
        order_id = job.order_id
        # Optional — getattr with defaults.
        return cls(
            job_id=getattr(job, "job_id", ""),
            status=status,
            needs_review=bool(getattr(job, "needs_review", False)),
            pending_capture_request_id=pending_capture_request_id,
            capture_id=capture_id,
            capture_amount_cents=capture_amount_cents,
            voided_amount_cents=voided_amount_cents,
            settlement_method=getattr(job, "settlement_method", None),
            authorize_attempted=bool(getattr(job, "authorize_attempted", False)),
            order_id=order_id,
        )


# ---------------------------------------------------------------------------
# resolve_state() — §7.1 precedence waterfall (pure function, no I/O)
# ---------------------------------------------------------------------------

def resolve_state(job) -> State:
    """Map a job record to exactly one of the 13 discrete states.

    Pure function of the job record — no I/O, no PayPal calls. The
    underlying flags (needs_review, pending intent, recorded capture,
    authorize_attempted, order_id) are independent booleans; this
    waterfall evaluates top-down, first match wins (§7.1, Rev 3).

    Accepts either a JobSnapshot or any job-like object (backward
    compat — converted via JobSnapshot.from_job(), which validates
    required fields). Internally operates on the snapshot.

    Raises:
      AttributeError: if a required job field is missing (from_job).
      UnhandledTransition: for a status/flag combination the waterfall
        cannot resolve (data invariant violation — loud, not silent).
      UnhandledTransition: for combinations the design proves impossible
        (§3.5: pending/recorded capture while AUTHORIZED) — raised
        explicitly, never assert (python -O safety).
    """
    # Normalize to a strict snapshot — validates required fields.
    snap = job if isinstance(job, JobSnapshot) else JobSnapshot.from_job(job)
    status = snap.status

    # 1. Terminal check — terminality dominates (a settled job is settled
    #    regardless of stale flags).
    if status == "CAPTURED":
        # v8 money-safety fix: a review flag dominates the split
        # shortcut. A void-amount disagreement (or any operator flag)
        # means a human must look — the job resolves S10c (unaudited),
        # never S10b, while the flag stands. Clean splits (no flag)
        # still resolve S10b below.
        if snap.needs_review:
            return State.S10c_CAPTURED_UNAUDITED
        # S10b: partial capture + residual void confirmed.
        if snap.voided_amount_cents is not None:
            return State.S10b_CAPTURED_SPLIT
        # S10c: audit trail incomplete — force-settled from a wedge, a
        # stale wedge flag on a settled job, or an unauditable capture
        # (amount on the books, no capture id).
        if snap.settlement_method == "force_settled":
            return State.S10c_CAPTURED_UNAUDITED
        if not snap.capture_id and snap.capture_amount_cents is not None:
            return State.S10c_CAPTURED_UNAUDITED
        # S10a: full capture, clean audit trail.
        return State.S10a_CAPTURED_FULL
    if status == "VOIDED":
        # v8 money-safety fix: a flagged void is not a clean settlement.
        # Surface the wedge instead of the silent terminal so the
        # operator sees the job (a wedged job never auto-settles).
        if snap.needs_review:
            return State.S9_DELIVERED_WEDGED
        return State.S11_VOIDED_SETTLED

    needs_review = snap.needs_review
    has_pending = snap.pending_capture_request_id is not None
    has_recorded = bool(snap.capture_id) or snap.capture_amount_cents is not None

    # 2. Wedged — the wedge dominates pending/recorded (a wedged job needs
    #    a human before any intent resolves).
    if needs_review:
        if status == "AUTHORIZED":
            return State.S5_AUTHORIZED_WEDGED
        if status == "DELIVERED":
            return State.S9_DELIVERED_WEDGED
        raise UnhandledTransition(f"WEDGED/status={status}", "RESOLVE")

    # 3. Pending intent — pending dominates recorded (an in-flight intent
    #    is newer evidence than a recorded capture).
    if has_pending:
        # §3.5: a capture intent cannot exist while AUTHORIZED — the
        # client API guards forbid starting capture before delivery.
        # Explicit raise (not assert) so python -O cannot silently
        # resolve this to S7.
        if status != "DELIVERED":
            raise UnhandledTransition(
                f"pending capture intent in status={status}: impossible per §3.5",
                "RESOLVE")
        return State.S7_DELIVERED_PENDING

    # 4. Recorded capture.
    if has_recorded:
        # §3.5: a pre-delivery partial capture always wedges to S5 on
        # discovery — there is no AUTHORIZED_PARTIAL_RECORDED state.
        # Explicit raise (not assert) so python -O cannot silently
        # resolve this to S8.
        if status != "DELIVERED":
            # Amount-only bookkeeping record (no capture ID, amount
            # already on the books, reconciled) is not a §3.5
            # violation — it's already-processed state, not a newly
            # discovered pre-delivery capture. The §3.5 raise applies
            # only when there's an actual capture ID (a real PayPal
            # capture discovered pre-delivery).
            if not snap.capture_id:
                return State.S4_AUTHORIZED_LIVE
            raise UnhandledTransition(
                f"recorded capture in status={status}: should have wedged "
                f"to S5_AUTHORIZED_WEDGED per §3.5",
                "RESOLVE")
        # Distinguish full vs partial: a full-cover capture has no
        # residual, so it is not S8 (partial recorded). The bridge
        # (_capture_with_reconcile) may record a full capture without
        # yet updating job.status — resolve to S10a so the FSM tracks
        # the settlement correctly.
        # (Defensive: test FakeJobs may lack quote.)
        _quote = getattr(job, "quote", None) or {}
        hold = _quote.get("price_cents")
        captured = getattr(job, "capture_amount_cents", None)
        if (hold is not None and captured is not None
                and captured >= hold):
            return State.S10a_CAPTURED_FULL
        return State.S8_DELIVERED_PARTIAL_RECORDED

    # 5-7. Quoted variants and clean live states.
    if status == "QUOTED":
        # History-flag note (§1.2): authorize_attempted is the weakest
        # signal — checked last among the QUOTED variants. S3 means
        # "unknown server state", not a permission bit.
        if snap.authorize_attempted:
            return State.S3_QUOTED_ATTEMPTED
        if snap.order_id:
            return State.S2_QUOTED_ORDERED
        return State.S1_QUOTED_FRESH
    if status == "AUTHORIZED":
        return State.S4_AUTHORIZED_LIVE
    if status == "DELIVERED":
        return State.S6_DELIVERED_OPEN

    raise UnhandledTransition(f"status={status}", "RESOLVE")


# ---------------------------------------------------------------------------
# make_intent_key() — idempotency keys (Phase 2 notes, edge case #3)
# ---------------------------------------------------------------------------

_INTENT_SECRET_ENV = "DISPATCHER_INTENT_SECRET"


def validate_config() -> None:
    """Fail fast at startup if required config is missing.

    Call this once during application boot, before serving traffic.
    Raises RuntimeError with a clear message if DISPATCHER_INTENT_SECRET
    is unset — we refuse to run financial code with an insecure default.
    Also refuses the literal "test-secret" (Claude LOW #8): tests use
    it, production must not.
    """
    secret = os.environ.get(_INTENT_SECRET_ENV)
    if not secret:
        raise RuntimeError(
            f"{_INTENT_SECRET_ENV} must be set — refusing to start with "
            "insecure fallback. Generate a per-deployment secret "
            "(e.g. `openssl rand -hex 32`) and export it before boot."
        )
    if secret == "test-secret":
        raise RuntimeError(
            f"{_INTENT_SECRET_ENV} is the test value — refusing to start "
            "production with it. Generate a real per-deployment secret."
        )


def _intent_secret() -> str:
    """Per-deployment HMAC secret for intent keys.

    Source: the DISPATCHER_INTENT_SECRET environment variable. There is
    NO fallback — if the env var is unset, this raises RuntimeError.
    Two deployments sharing a secret would generate identical keys for
    identical (job_id, kind, attempt) triples; keep the secret
    per-deployment. Call validate_config() at startup to fail fast.
    """
    secret = os.environ.get(_INTENT_SECRET_ENV)
    if not secret:
        raise RuntimeError(
            f"{_INTENT_SECRET_ENV} must be set — refusing to generate "
            "intent keys with insecure fallback. Set a per-deployment "
            "secret (see validate_config())."
        )
    return secret


def make_intent_key(job_id: str, kind: str, attempt: int) -> str:
    """Build the idempotency key for a Phase 1 intent.

    The key is HMAC-SHA256(secret, "job_id:kind:attempt") — cryptographically
    tied to all three inputs (edge case #3):
      - Same (job_id, kind, attempt)  → same key (safe replay, I4).
      - Different attempt             → different key (no cross-attempt
        collision on R_CAP_500 replays in S7).
      - Different job or kind         → different key.

    Args:
      job_id:  the escrow job id.
      kind:    "authorize" | "capture" | "capture_partial" | "residual_void" | "void".
      attempt: 1-based attempt counter. MUST increment on every new
        Phase 1 intent for the same (job, kind) — reusing an attempt
        number reuses the key, which is only safe for exact replays.

    Returns:
      64-char lowercase hex digest.

    Raises:
      ValueError: job_id or kind is empty. Empty job ids must not alias
        onto a shared "unknown-job" key.
      ValueError: attempt is not an int >= 1.
    """
    if not job_id or not str(job_id).strip():
        raise ValueError("job_id must be non-empty")
    if not kind or not str(kind).strip():
        raise ValueError("kind must be non-empty")
    # Claude review (LOW): attempt must be a real int >= 1. None, 0,
    # negative, or bool would produce colliding or confusing keys.
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("attempt must be an int >= 1")
    # Claude review (LOW): length-prefixed encoding defeats the colon
    # collision — ("a:b", "c") vs ("a", "b:c") now hash differently.
    msg = f"{len(job_id)}:{job_id}{len(kind)}:{kind}{attempt}".encode("utf-8")
    return hmac.new(
        _intent_secret().encode("utf-8"), msg, hashlib.sha256
    ).hexdigest()


# ---------------------------------------------------------------------------
# transition() — the single dispatch point (§7)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# transition() — the single dispatch point (§7, Phase 3: full §5 table)
# ---------------------------------------------------------------------------
#
# Table organization: _TABLE maps (State, Event) -> handler function.
# Each handler takes (Evidence | None) and returns (State, [Action]).
# Handlers that need evidence raise UnhandledTransition if the required
# evidence fields are missing — the caller must attach read/enum results
# BEFORE dispatching (§1.4).
#
# Guard convention (§7 rule 3): domain guards (is_payer, deadline_passed,
# delivery_verified) are checked INSIDE the cell. A failed guard raises
# GuardRejection (HTTP 4xx) — an explicit, typed refusal. This is NOT
# UnhandledTransition (HTTP 500, matrix-completeness bug).
#
# Webhook policy (§4.7): WH_CAP_COMPLETED / WH_AUTH_VOIDED are HINTS, not
# commands — the cell returns (state, [NoOp("trigger fresh read")]) and
# the CALLER issues E_RECONCILE. The dispatcher never trusts a webhook
# payload for money movement (I1).

def _ev(evidence: Evidence | None) -> Evidence:
    """Normalize None evidence to an empty Evidence."""
    return evidence if evidence is not None else Evidence()


def _require_payer(ev: Evidence) -> None:
    if not ev.is_payer:
        raise GuardRejection("payer only", category="forbidden")


def _validate_amount(amount_cents, field_name="amount_cents") -> None:
    """Shared amount validator — raises GuardRejection(400), not ValueError.

    Claude review (MEDIUM): None → GuardRejection was done, but
    negative/float/bool still raised ValueError (HTTP 500) from the
    action constructors. All amount validation now happens here, at the
    handler boundary, with the correct 4xx contract.
    """
    if amount_cents is None:
        raise GuardRejection(f"{field_name} required", category="validation")
    if isinstance(amount_cents, bool) or not isinstance(amount_cents, int):
        raise GuardRejection(f"{field_name} must be an int", category="validation")
    if amount_cents < 0:
        raise GuardRejection(f"{field_name} must be >= 0", category="validation")


def _validate_capture_amount(amount_cents) -> None:
    """Amount validator for captures — also rejects zero.

    A zero-amount capture is not a real capture; it would book S10a
    with no money moved. Voids may be zero (residual fully captured),
    but captures must be positive.
    """
    _validate_amount(amount_cents)
    if amount_cents == 0:
        raise GuardRejection("capture amount must be > 0", category="validation")


def _require_capture_ids(ev) -> None:
    """Raise GuardRejection(400) if capture_ids is empty.

    An empty capture_ids would book S10a (clean) while resolve_state
    says S10c (unaudited). This is a caller bug (forgot the IDs),
    not a dispatcher bug — so 400, not 500.
    """
    if not ev.capture_ids or len(ev.capture_ids) == 0:
        raise GuardRejection("capture_ids required for RecordCapture",
                             category="validation")


def _check_fund_conservation(ev: Evidence) -> None:
    """I1: captured + voided must not exceed authorized.

    Only enforced when the caller provides authorized_cents. When set,
    any money-booking handler must call this after validating amounts.
    """
    if ev.voided_amount_cents is not None and ev.voided_amount_cents < 0:
        raise GuardRejection("voided_amount_cents must be >= 0",
                             category="validation")
    if ev.authorized_cents is not None and ev.amount_cents is not None:
        total = ev.amount_cents + (ev.voided_amount_cents or 0)
        if total > ev.authorized_cents:
            raise GuardRejection(
                f"captured + voided ({total}) exceeds authorized "
                f"({ev.authorized_cents})",
                category="validation")


def _intent_key(ev: Evidence, kind: str) -> str:
    """Build the idempotency key for a SaveIntent action.

    An empty job id must not collapse onto one shared key — two jobs
    that forgot the id would replay each other's PayPal posts.
    """
    if not ev.job_id or not str(ev.job_id).strip():
        raise GuardRejection(
            "job_id is required to build an intent key", category="validation")
    return make_intent_key(ev.job_id, kind, ev.attempt)


def _begin_void(ev: Evidence, stay: State):
    """Start a void without settling.

    Persist the intent, instruct the POST, stay in ``stay``. The job
    becomes S11 only on R_VOID_OK. Marking it voided before PayPal
    accepts the POST is an absorbing sink with the hold still live.

    Gemini Fix 3 (HIGH): emits SaveVoidIntent (full authorization void),
    NOT SaveResidualVoidIntent (partial-capture residual only).
    """
    if not ev.authorization_id or not str(ev.authorization_id).strip():
        raise GuardRejection(
            "authorization_id required to void", category="validation")
    key = _intent_key(ev, "void")
    return (stay, [
        SaveVoidIntent(key=key, authorization_id=ev.authorization_id),
        PostVoid(authorization_id=ev.authorization_id),
    ])


def _record_void(ev: Evidence) -> RecordVoid:
    if ev.voided_amount_cents is None:
        raise GuardRejection(
            "R_VOID_OK requires voided_amount_cents", category="validation")
    return RecordVoid(
        amount_cents=ev.voided_amount_cents,
        authorization_id=ev.authorization_id,
    )


_SAFE_UNATTRIBUTABLE_READS = (
    Event.READ_CAPTURED, Event.READ_RELEASED, Event.READ_PARTIAL,
)


# ---------------------------------------------------------------------------
# I3 static edge metadata — target-state declarations for the liveness graph
# ---------------------------------------------------------------------------

def _targets(*states):
    """Declare the set of states this handler may return.

    Used by TestLiveness._build_graph to construct the directed graph
    WITHOUT evaluating handlers (which need evidence). Each handler
    lists every State it can return across all evidence branches.
    A handler that always raises (guard-only cell) declares no targets.
    """
    def deco(fn):
        fn.target_states = frozenset(states)
        return fn
    return deco


# -- S1 (QUOTED_FRESH) -------------------------------------------------------

@_targets(State.S2_QUOTED_ORDERED)
def _s1_create_order(ev: Evidence):
    return (State.S2_QUOTED_ORDERED,
            [NoOp(note="caller creates PayPal order, saves order_id")])

@_targets(State.S1_QUOTED_FRESH)
def _s1_reconcile(ev: Evidence):
    return (State.S1_QUOTED_FRESH, [NoOp(note="no authorization to read")])

@_targets(State.S1_QUOTED_FRESH)
def _s1_webhook_defensive(ev: Evidence):
    # Edge case #1: unreachable per §4.7 (S1 has no order_id), kept as
    # explicit NoOp for table completeness (I5).
    return (State.S1_QUOTED_FRESH,
            [NoOp(note="unreachable per §4.7 — defensive explicit no-op (I5)")])

@_targets(State.S1_QUOTED_FRESH)
def _s1_r_auth_denied_replay(ev: Evidence):
    # Gemini Fix 6 (MEDIUM): idempotent replay. The denial was already
    # processed (job returned to S1); a duplicate R_AUTH_DENIED is a
    # no-op, not a 500.
    return (State.S1_QUOTED_FRESH,
            [NoOp(note="denial already processed — idempotent replay")])

@_targets(State.S1_QUOTED_FRESH)
def _s1_quote_expiry(ev: Evidence):
    return (State.S1_QUOTED_FRESH, [DeleteJob()])


# -- S2 (QUOTED_ORDERED) -----------------------------------------------------

@_targets(State.S3_QUOTED_ATTEMPTED)
def _s2_start_authorize(ev: Evidence):
    key = _intent_key(ev, "authorize")
    # The authorize intent records the quoted amount — crash recovery
    # needs to know what was authorized. ev.amount_cents must be set
    # by the caller (loud ValueError if None, per Gemini Phase 3).
    # Gemini v5 review (Fix 3): GuardRejection, not ValueError.
    _validate_amount(ev.amount_cents)
    return (State.S3_QUOTED_ATTEMPTED,
            [SaveIntent(key=key, amount_cents=ev.amount_cents,
                        kind="authorize")])

@_targets(State.S2_QUOTED_ORDERED)
def _s2_reconcile(ev: Evidence):
    return (State.S2_QUOTED_ORDERED, [NoOp(note="no-op")])

@_targets(State.S2_QUOTED_ORDERED)
def _s2_create_order_replay(ev: Evidence):
    # Gemini Fix 6 (MEDIUM): idempotent replay. An order already exists
    # for this job; creating another is a caller error, not a 500.
    ev = _ev(ev)
    raise GuardRejection("order already exists for this job",
                         category="conflict")

@_targets(State.S2_QUOTED_ORDERED)
def _s2_webhook(ev: Evidence):
    return (State.S2_QUOTED_ORDERED,
            [NoOp(note="webhook is hint — caller issues E_RECONCILE")])

@_targets(State.S2_QUOTED_ORDERED)
def _s2_quote_expiry(ev: Evidence):
    return (State.S2_QUOTED_ORDERED, [DeleteJob()])


# -- S3 (QUOTED_ATTEMPTED) ---------------------------------------------------

@_targets(State.S4_AUTHORIZED_LIVE)
def _s3_r_auth_created(ev: Evidence):
    return (State.S4_AUTHORIZED_LIVE,
            [ClearIntent(),
             NoOp(note="caller saves authorization_id; status=AUTHORIZED")])

@_targets(State.S1_QUOTED_FRESH)
def _s3_r_auth_denied(ev: Evidence):
    # Rev 2: clean reset (old S4 absorbed into S1).
    return (State.S1_QUOTED_FRESH,
            [ClearIntent(),
             NoOp(note="clear order_id, authorize_attempted=False")])

@_targets(State.S3_QUOTED_ATTEMPTED)
def _s3_r_auth_retryable(ev: Evidence):
    return (State.S3_QUOTED_ATTEMPTED,
            [NoOp(note="intent kept for replay; retry via E_RECONCILE")])

@_targets(State.S3_QUOTED_ATTEMPTED)
def _s3_create_order(ev: Evidence):
    # Rev 2 (§6.5): do NOT issue a fresh order blind — read the old one.
    return (State.S3_QUOTED_ATTEMPTED,
            [ReadOldOrder(order_id=ev.order_id)])

@_targets(State.S4_AUTHORIZED_LIVE)
def _s3_r_old_order_active(ev: Evidence):
    # Adopt, don't duplicate — prevents double-hold on the payer's card.
    return (State.S4_AUTHORIZED_LIVE,
            [NoOp(note="adopt authorization_id from old order; status=AUTHORIZED")])

@_targets(State.S2_QUOTED_ORDERED)
def _s3_r_old_order_dead(ev: Evidence):
    return (State.S2_QUOTED_ORDERED,
            [NoOp(note="old order dead — safe to issue fresh order")])

@_targets(State.S10a_CAPTURED_FULL, State.S11_VOIDED_SETTLED, State.S3_QUOTED_ATTEMPTED, State.S5_AUTHORIZED_WEDGED)
def _s3_reconcile(ev: Evidence):
    ev = _ev(ev)
    read, enum = ev.read, ev.enum
    if read in (Event.READ_CREATED, Event.READ_PENDING, None):
        return (State.S3_QUOTED_ATTEMPTED, [NoOp(note="still pending")])
    if read == Event.READ_CAPTURED:
        if enum == Event.ENUM_FOUND:
            # Malformed/unknown amount (None): record the ID with
            # unknown amount as unaudited — do NOT wedge. (Claude
            # finding 2: legacy settles with ID recorded, amount
            # unknown.)
            if ev.amount_cents is None:
                return (State.S10c_CAPTURED_UNAUDITED,
                        [RecordCapture(capture_ids=tuple(ev.capture_ids),
                                      amount_cents=None),
                         EmitReceipt(kind="unaudited")])
            # Gemini v5 review (Fix 3): GuardRejection, not ValueError.
            _validate_capture_amount(ev.amount_cents)
            _check_fund_conservation(ev)
            _require_capture_ids(ev)
            return (State.S10a_CAPTURED_FULL,
                    [RecordCapture(capture_ids=tuple(ev.capture_ids),
                                  amount_cents=ev.amount_cents),
                     ClearIntent(),
                     EmitReceipt(kind="full")])
        return (State.S3_QUOTED_ATTEMPTED, [NoOp(note="capture read unclear")])
    if read == Event.READ_RELEASED:
        # Gemini Fix 1 (CRITICAL): clear the dangling authorize intent.
        # S3 never held funds, so book the void amount only when the
        # caller provides one.
        actions = [ClearIntent()]
        if ev.voided_amount_cents is not None:
            actions.insert(0, _record_void(ev))
        else:
            actions.append(NoOp(note="hold released"))
        return (State.S11_VOIDED_SETTLED, actions)
    if read == Event.READ_500:
        return (State.S3_QUOTED_ATTEMPTED, [NoOp(note="read failed — retry")])
    if read == Event.READ_PARTIAL:
        # Unusual pre-authorization partial — wedge for review.
        return (State.S5_AUTHORIZED_WEDGED,
                [SetReviewFlag(reason="PARTIALLY_CAPTURED in S3")])
    raise UnhandledTransition(State.S3_QUOTED_ATTEMPTED, Event.E_RECONCILE)

@_targets(State.S3_QUOTED_ATTEMPTED)
def _s3_webhook(ev: Evidence):
    return (State.S3_QUOTED_ATTEMPTED,
            [NoOp(note="webhook is hint — caller issues E_RECONCILE")])

@_targets(State.S3_QUOTED_ATTEMPTED, State.S4_AUTHORIZED_LIVE)
def _s3_quote_expiry(ev: Evidence):
    # Edge case #2 (documented): if the expiry read discovers a live
    # hold, the hold is REAL — quote expiry does not invalidate it.
    # Transition to S4; T_DEADLINE → E_VOID_DEADLINE handles cleanup.
    # The caller attaches read evidence; without it we stay in S3.
    ev = _ev(ev)
    if ev.read == Event.READ_CREATED or ev.read == Event.READ_PENDING:
        return (State.S4_AUTHORIZED_LIVE,
                [NoOp(note="expiry read found live hold — hold is real; "
                           "T_DEADLINE enables void")])
    return (State.S3_QUOTED_ATTEMPTED, [NoOp(note="expiry — no hold found")])


# -- S4 (AUTHORIZED_LIVE) ----------------------------------------------------

@_targets(State.S6_DELIVERED_OPEN)
def _s4_submit_delivery(ev: Evidence):
    ev = _ev(ev)
    if ev.deadline_passed:
        raise GuardRejection("deadline passed — delivery refused",
                             category="gone")
    return (State.S6_DELIVERED_OPEN,
            [NoOp(note="caller saves delivery_hash; status=DELIVERED")])

@_targets(State.S4_AUTHORIZED_LIVE, State.S5_AUTHORIZED_WEDGED,
          State.S10c_CAPTURED_UNAUDITED, State.S11_VOIDED_SETTLED)
def _s4_void_deadline(ev: Evidence):
    ev = _ev(ev)
    _require_payer(ev)
    if not ev.deadline_passed:
        raise GuardRejection("deadline has not passed", category="forbidden")
    read, enum = ev.read, ev.enum
    # Missing read is a caller bug (they dispatched the void without
    # looking), not a hole in the matrix.
    if read is None:
        raise GuardRejection(
            "E_VOID_DEADLINE requires a PayPal read result",
            category="validation")
    if read == Event.READ_500:
        raise GuardRejection("refusing to void blind — order unreadable",
                             category="conflict")
    if read == Event.READ_PENDING:
        return (State.S4_AUTHORIZED_LIVE,
                [NoOp(note="authorization still pending — retry read before void")])
    if read == Event.READ_RELEASED:
        # Claude review (MEDIUM): this path is reachable after _begin_void
        # saved the intent and R_VOID_OK was lost. Clear the dangling
        # intent — same as _s4_reconcile.
        if ev.is_partial:
            return (State.S10c_CAPTURED_UNAUDITED,
                    [ClearIntent(), EmitReceipt(kind="unaudited")])
        return (State.S11_VOIDED_SETTLED,
                [ClearIntent(), NoOp(note="hold already released")])
    # Pre-delivery partial or capture — never a routine void (§3.5).
    if read == Event.READ_PARTIAL:
        return (State.S5_AUTHORIZED_WEDGED,
                [SetReviewFlag(
                    reason="PARTIALLY_CAPTURED pre-delivery — operator review")])
    if read == Event.READ_CAPTURED:
        # Gemini v5 review (Fix 3): GuardRejection, not ValueError.
        _validate_capture_amount(ev.amount_cents)
        _check_fund_conservation(ev)
        _require_capture_ids(ev)
        return (State.S5_AUTHORIZED_WEDGED,
                [RecordCapture(capture_ids=ev.capture_ids,
                              amount_cents=ev.amount_cents),
                 SetReviewFlag(reason="captured pre-delivery — operator review")])
    # Live hold (READ_CREATED). Enumeration decides whether a void is safe.
    if enum is None:
        raise GuardRejection(
            "E_VOID_DEADLINE requires an enum outcome to void a live hold",
            category="validation")
    if enum in (Event.ENUM_UNREAD, Event.ENUM_ORDER_UNREAD):
        raise GuardRejection("refusing to void blind — order unreadable",
                             category="conflict")
    if enum in (Event.ENUM_AMBIGUOUS, Event.ENUM_UNATTRIBUTABLE, Event.ENUM_FOUND):
        return (State.S5_AUTHORIZED_WEDGED,
                [SetReviewFlag(reason=f"void blocked: {enum}")])
    if read == Event.READ_CREATED and enum == Event.ENUM_NONE:
        # Two-phase: intent + POST now, S11 only after R_VOID_OK.
        return _begin_void(ev, State.S4_AUTHORIZED_LIVE)
    raise GuardRejection(
        f"cannot void on evidence read={read} enum={enum}",
        category="conflict")

def _pending_intent_reconcile(ev: Evidence, current_state: State):
    """Decide E_RECONCILE for a job with an in-flight capture intent.

    v8.2 (production-readiness item 2): the FSM models the pending
    intent lifecycle instead of falling back to legacy. Background
    reconcile is READ-ONLY (Round 28 C3) — it never replays the key;
    only the payer's explicit retry (capture/capture_partial) does.

    Policy — what the READ proves about the in-flight key:
    - ENUM_FOUND: return None — fall through to the normal arm, which
      records the listed COMPLETED capture and clears the spent intent.
    - ENUM_UNATTRIBUTABLE: wedge — money moved but cannot be attributed.
    - ENUM_AMBIGUOUS: a non-terminal capture is listed; the key may
      still land. Non-released reads stay open (the bridge reports
      awaiting); released reads wedge (money may still move).
    - ENUM_NONE / ENUM_UNREAD / ENUM_ORDER_UNREAD / None: nothing
      visible. Non-released, non-captured reads keep the intent (the
      bridge reports intent_kept_unconfirmed). Released reads wedge
      with the key kept. CAPTURED reads settle unaudited — PayPal
      reports the hold gone, so the key is spent-or-dead.

    The NoOp notes carry the legacy-compatible return-contract text;
    the bridge surfaces them as the error with awaiting /
    intent_kept_unconfirmed flags.

    Returns (new_state, actions), or None to fall through to the
    normal arm.
    """
    read, enum = ev.read, ev.enum
    if current_state in (State.S4_AUTHORIZED_LIVE,
                         State.S5_AUTHORIZED_WEDGED):
        wedged = State.S5_AUTHORIZED_WEDGED
    else:
        wedged = State.S9_DELIVERED_WEDGED
    if enum == Event.ENUM_FOUND:
        return None
    if enum == Event.ENUM_UNATTRIBUTABLE:
        return (wedged, [SetReviewFlag(
            reason="captures unattributable (operator review needed)")])
    if enum == Event.ENUM_AMBIGUOUS:
        if read == Event.READ_RELEASED:
            return (wedged, [SetReviewFlag(
                reason="non-terminal capture listed on released hold "
                       "with pending capture key — money may still move; "
                       "key is kept (operator review needed)")])
        if read == Event.READ_PARTIAL:
            note = ("partial capture pending and a non-terminal capture "
                    "is listed — awaiting the webhook or a payer retry")
        elif read == Event.READ_CAPTURED:
            note = ("authorization is CAPTURED but the listed capture is "
                    "PENDING — awaiting COMPLETED; will retry")
        else:
            note = ("capture pending and a non-terminal capture is "
                    "listed — awaiting the webhook or a payer retry")
        return (current_state, [NoOp(note=note)])
    # ENUM_NONE / ENUM_UNREAD / ENUM_ORDER_UNREAD / None: no capture visible.
    if read == Event.READ_RELEASED:
        # Stale intent + definitive absence (ENUM_NONE = both resources
        # read, nothing listed): the key had time to land and didn't —
        # it's dead. Fall through to the normal arm's clean void (or
        # books-settle). A non-stale intent might still be in flight —
        # wedge it for operator review (Claude round-28 finding 1).
        # An incomplete read (unread/None) always wedges: a capture
        # could be hiding.
        if enum == Event.ENUM_NONE and ev.pending_intent_stale:
            return None
        # Distinguish "the order could not be read" from "no capture
        # listed" — legacy wedges both, with different reasons, and
        # tests pin the distinction. The attempted amount rides along
        # (legacy surfaced it for the operator).
        attempted = ev.pending_intent_amount_cents
        attempted_note = (f" (attempted ${attempted/100:.2f})"
                          if attempted is not None else "")
        if enum is None or enum in (Event.ENUM_UNREAD,
                                    Event.ENUM_ORDER_UNREAD):
            return (wedged, [SetReviewFlag(
                reason="released hold with pending capture key and the "
                       "order could not be read" + attempted_note +
                       " (operator review needed)",
                amount_cents=attempted)])
        return (wedged, [SetReviewFlag(
            reason="released hold with pending capture key and no "
                   "capture listed — the key is kept" + attempted_note +
                   " (operator review needed)",
            amount_cents=attempted)])
    if read == Event.READ_CAPTURED:
        # PayPal reports the hold gone: settle unaudited and clear the
        # spent-or-dead key. (enum=None — the enumeration itself raised —
        # wedges instead: never settle on an unreadable read.)
        if enum is None:
            return (wedged, [SetReviewFlag(
                reason="order could not be read — cannot confirm no "
                       "capture moved (operator review needed)")])
        return (State.S10c_CAPTURED_UNAUDITED,
                [RecordCapture(capture_ids=(), amount_cents=None),
                 ClearIntent(),
                 EmitReceipt(kind="unaudited")])
    # CREATED / PENDING / PARTIAL / unknown reads: the key may never
    # have executed — keep it for the payer's retry or the webhook.
    status_str = {
        Event.READ_CREATED: "CREATED",
        Event.READ_PENDING: "PENDING",
        Event.READ_PARTIAL: "PARTIALLY_CAPTURED",
    }.get(read, "UNKNOWN")
    if read == Event.READ_PARTIAL:
        attempted = ev.pending_intent_amount_cents
        note = ("partial capture pending but no capture is listed"
                + (f" (attempted ${attempted/100:.2f})"
                   if attempted is not None else "")
                + " — retry capture_partial with the same amount to "
                  "replay the key")
    elif enum in (Event.ENUM_UNREAD, Event.ENUM_ORDER_UNREAD) or enum is None:
        note = (f"capture pending but the authorization is {status_str} "
                "and the order could not be read — retry the capture to "
                "replay the key")
    else:
        note = (f"capture pending but the authorization is {status_str} "
                "and no capture is listed — retry the capture to replay "
                "the key")
    return (current_state, [NoOp(note=note)])


@_targets(State.S10a_CAPTURED_FULL, State.S10b_CAPTURED_SPLIT, State.S10c_CAPTURED_UNAUDITED, State.S11_VOIDED_SETTLED, State.S4_AUTHORIZED_LIVE, State.S5_AUTHORIZED_WEDGED)
def _s4_reconcile(ev: Evidence):
    ev = _ev(ev)
    read, enum = ev.read, ev.enum
    # v8.2 (production-readiness item 2): the FSM models the pending
    # capture intent lifecycle — no legacy fallback for in-flight keys.
    if ev.pending_intent_id is not None:
        decided = _pending_intent_reconcile(ev, State.S4_AUTHORIZED_LIVE)
        if decided is not None:
            return decided
    if read in (Event.READ_CREATED, Event.READ_PENDING, None):
        return (State.S4_AUTHORIZED_LIVE, [NoOp(note="hold live")])
    if read == Event.READ_PARTIAL:
        # §3.5: pre-delivery partial is never routine — wedge.
        if ev.has_capture or ev.capture_ids:
            # Gemini v5 review (Fix 3): GuardRejection, not ValueError.
            _validate_capture_amount(ev.amount_cents)
            _check_fund_conservation(ev)
            _require_capture_ids(ev)
            return (State.S5_AUTHORIZED_WEDGED,
                    [RecordCapture(capture_ids=tuple(ev.capture_ids),
                                  amount_cents=ev.amount_cents),
                     SetReviewFlag(reason="pre-delivery partial — review")])
        return (State.S5_AUTHORIZED_WEDGED,
                [SetReviewFlag(
                    reason="PARTIALLY_CAPTURED but no capture listed and "
                           "none in flight")])
    if read == Event.READ_CAPTURED:
        if enum == Event.ENUM_FOUND:
            # Malformed/unknown amount (None): record the ID with
            # unknown amount as unaudited — do NOT wedge. Legacy does
            # this via _record_capture_amount's "absent amount leaves
            # the books alone" rule (Claude finding 2).
            if ev.amount_cents is None:
                return (State.S10c_CAPTURED_UNAUDITED,
                        [RecordCapture(capture_ids=tuple(ev.capture_ids),
                                      amount_cents=None),
                         EmitReceipt(kind="unaudited")])
            _validate_capture_amount(ev.amount_cents)
            _check_fund_conservation(ev)
            if not ev.capture_ids:
                return (State.S10c_CAPTURED_UNAUDITED,
                        [RecordCapture(capture_ids=(),
                                      amount_cents=ev.amount_cents),
                         EmitReceipt(kind="unaudited")])
            # Partial capture → S10b split with residual.
            if ev.is_partial:
                hold = ev.authorized_cents or 0
                residual = hold - (ev.amount_cents or 0)
                actions = [RecordCapture(capture_ids=tuple(ev.capture_ids),
                                         amount_cents=ev.amount_cents),
                           ClearIntent()]
                if residual > 0:
                    return (State.S10b_CAPTURED_SPLIT,
                            actions + [RecordVoid(amount_cents=residual,
                                                  authorization_id=ev.authorization_id),
                                       EmitReceipt(kind="split")])
            return (State.S10a_CAPTURED_FULL,
                    [RecordCapture(capture_ids=tuple(ev.capture_ids),
                                  amount_cents=ev.amount_cents),
                     ClearIntent(), EmitReceipt(kind="full")])
        if enum == Event.ENUM_UNREAD:
            # PayPal says CAPTURED but the order can't be read for
            # details. If the books already have a capture, leave open
            # (don't overwrite). Otherwise, trust the CAPTURED status
            # and mark unaudited. Legacy does this via
            # _record_captured_authorization's "unread" branch.
            if ev.has_capture:
                return (State.S4_AUTHORIZED_LIVE,
                        [NoOp(note="order unread but capture on books — "
                                   "will retry")])
            return (State.S10c_CAPTURED_UNAUDITED,
                    [RecordCapture(capture_ids=(),
                                  amount_cents=None),
                     EmitReceipt(kind="unaudited")])
        if enum == Event.ENUM_ORDER_UNREAD:
            # §6.3 fix: valid capture on the books — "will retry", no wedge.
            return (State.S4_AUTHORIZED_LIVE,
                    [NoOp(note="order unread but capture on books — will retry")])
        if enum == Event.ENUM_NONE:
            # PayPal reports CAPTURED but lists no capture — the
            # "reconciled fallback": money moved but the details are
            # not visible. Record as unaudited CAPTURED, not a wedge.
            # (Legacy _reconcile_with_auth does the same via
            # _mark_unaudited.) No amount validation — the amount is
            # legitimately unknown here. Clear any existing review flag
            # — the CAPTURED arm settles definitively.
            return (State.S10c_CAPTURED_UNAUDITED,
                    [RecordCapture(capture_ids=(),
                                  amount_cents=None),
                     ClearReviewFlag(),
                     EmitReceipt(kind="unaudited")])
        # ENUM_AMBIGUOUS / ENUM_UNATTRIBUTABLE → wedge with descriptive
        # reasons (legacy uses "non-terminal" and "cannot be attributed"
        # language; the dispatcher should too).
        if enum == Event.ENUM_AMBIGUOUS:
            # Round 45: PENDING (non-terminal) does NOT wedge. The job
            # stays open awaiting COMPLETED — PENDING never settles but
            # is not a conflict.
            return (State.S4_AUTHORIZED_LIVE,
                    [NoOp(note="awaiting COMPLETED (PENDING listed)")])
        return (State.S5_AUTHORIZED_WEDGED,
                [SetReviewFlag(
                    reason="reconcile blocked: captures cannot be attributed "
                           "(amount, currency, or id problem — operator "
                           "review needed)")])
    if read == Event.READ_RELEASED:
        # P0 fix (19-case void gap): check for captures BEFORE declaring
        # void. A released hold with a listed capture means money moved —
        # settling as VOIDED would lose it.
        # v8 money-safety fix: ENUM_NONE ("proved absent") with a capture
        # already on the books settles from the books too — the recorded
        # capture came from an earlier PayPal read, and voiding it would
        # erase real money.
        if ev.has_capture and ev.enum in (Event.ENUM_FOUND, Event.ENUM_NONE):
            _validate_capture_amount(ev.amount_cents)
            _check_fund_conservation(ev)
            if ev.is_partial:
                actions = [RecordCapture(capture_ids=tuple(ev.capture_ids),
                                         amount_cents=ev.amount_cents),
                           ClearIntent()]
                if ev.voided_amount_cents is not None:
                    return (State.S10b_CAPTURED_SPLIT,
                            actions + [_record_void(ev),
                                       EmitReceipt(kind="split")])
                return (State.S10c_CAPTURED_UNAUDITED,
                        actions + [EmitReceipt(kind="unaudited")])
            return (State.S10a_CAPTURED_FULL,
                    [RecordCapture(capture_ids=tuple(ev.capture_ids),
                                  amount_cents=ev.amount_cents),
                     ClearIntent(), EmitReceipt(kind="full")])
        # v8 money-safety fix 1: the capture enumeration itself failed
        # (enum=None — PayPalError in the bridge). The order is
        # unreadable, so a capture could be hiding behind the released
        # hold. Wedge; never void blind.
        if ev.enum is None:
            return (State.S5_AUTHORIZED_WEDGED,
                    [SetReviewFlag(
                        reason="order could not be read — cannot confirm "
                               "no capture moved (operator review needed)")])
        # AMBIGUOUS / UNATTRIBUTABLE always wedge: a non-terminal or
        # unattributable capture may still move money.
        if ev.enum in (Event.ENUM_AMBIGUOUS, Event.ENUM_UNATTRIBUTABLE):
            if ev.enum == Event.ENUM_AMBIGUOUS:
                reason = ("capture pending on released hold: non-terminal "
                          "capture is listed — money may still move "
                          "(operator review needed)")
            else:
                reason = ("capture pending on released hold: captures "
                          "cannot be attributed (amount, currency, or id "
                          "problem — operator review needed)")
            return (State.S5_AUTHORIZED_WEDGED,
                    [SetReviewFlag(reason=reason)])
        # v8 money-safety fix 3: UNREAD / ORDER_UNREAD wedge only on a
        # real capture risk. With no capture on the books and none
        # listed there is nothing to lose — fall through to the clean
        # void below instead of wedging.
        if ev.enum in (Event.ENUM_UNREAD, Event.ENUM_ORDER_UNREAD):
            if ev.has_capture:
                return (State.S5_AUTHORIZED_WEDGED,
                        [SetReviewFlag(
                            reason="capture pending on released hold: order "
                                   "could not be read — cannot confirm no "
                                   "other capture moved (operator review "
                                   "needed)")])
        # Gemini v5 review (Fix 2, MEDIUM): clear the dangling intent.
        if ev.is_partial:
            return (State.S10c_CAPTURED_UNAUDITED,
                    [ClearIntent(), EmitReceipt(kind="unaudited")])
        # Round 35: the full hold was released — book the void so
        # voided_amount_cents is populated (receipt balance
        # assertions). Without RecordVoid the job settles S11 with
        # voided_amount_cents=None.
        if ev.authorized_cents is None:
            return (State.S5_AUTHORIZED_WEDGED,
                    [SetReviewFlag(
                        reason="authorization released but hold amount "
                               "unknown — operator review needed")])
        return (State.S11_VOIDED_SETTLED,
                [RecordVoid(amount_cents=ev.authorized_cents,
                            authorization_id=ev.authorization_id),
                 ClearIntent(),
                 NoOp(note="hold released")])
    if read == Event.READ_500:
        return (State.S4_AUTHORIZED_LIVE, [NoOp(note="read failed — retry")])
    raise UnhandledTransition(State.S4_AUTHORIZED_LIVE, Event.E_RECONCILE)

@_targets(State.S4_AUTHORIZED_LIVE)
def _s4_webhook_voided_or_completed(ev: Evidence):
    return (State.S4_AUTHORIZED_LIVE,
            [NoOp(note="webhook is hint — caller issues E_RECONCILE")])

@_targets(State.S4_AUTHORIZED_LIVE)
def _s4_webhook_denied(ev: Evidence):
    return (State.S4_AUTHORIZED_LIVE, [NoOp(note="denial recorded; stay open")])

@_targets(State.S4_AUTHORIZED_LIVE)
def _s4_deadline(ev: Evidence):
    return (State.S4_AUTHORIZED_LIVE,
            [NoOp(note="deadline passed — enables E_VOID_DEADLINE")])


@_targets(State.S11_VOIDED_SETTLED)
def _s4_r_void_ok(ev: Evidence):
    ev = _ev(ev)
    return (State.S11_VOIDED_SETTLED, [_record_void(ev), ClearIntent()])


@_targets(State.S4_AUTHORIZED_LIVE)
def _s4_r_void_fail(ev: Evidence):
    # Intent stays. The caller retries the same key; the job is not voided.
    return (State.S4_AUTHORIZED_LIVE,
            [NoOp(note="void failed — intent kept for retry")])


@_targets(State.S4_AUTHORIZED_LIVE)
def _s4_r_void_422(ev: Evidence):
    # Gemini Fix 4 (HIGH): HTTP 422 = AUTHORIZATION_ALREADY_VOIDED or
    # STATUS_NOT_VOIDABLE. Retrying the same idempotency key returns 422
    # forever — clear the intent and tell the caller to reconcile.
    return (State.S4_AUTHORIZED_LIVE,
            [ClearIntent(),
             NoOp(note="void already processed at PayPal (422) — "
                       "caller should reconcile")])


# -- S5 (AUTHORIZED_WEDGED) --------------------------------------------------

@_targets(State.S10a_CAPTURED_FULL, State.S10b_CAPTURED_SPLIT,
          State.S10c_CAPTURED_UNAUDITED, State.S5_AUTHORIZED_WEDGED)
def _s5_force_settle(ev: Evidence):
    ev = _ev(ev)
    _require_payer(ev)
    enum = ev.enum
    # The caller MUST attach read evidence before dispatching E_FORCE_SETTLE.
    # Missing evidence is a caller bug (GuardRejection), not a matrix hole.
    if enum is None:
        raise GuardRejection(
            "E_FORCE_SETTLE requires read evidence (enum outcome)",
            category="validation")
    if not ev.readable or enum in (Event.ENUM_UNREAD, Event.ENUM_ORDER_UNREAD):
        raise GuardRejection(
            "cannot force-settle: the order could not be read",
            category="conflict")
    if enum == Event.ENUM_AMBIGUOUS:
        raise GuardRejection("money may still move", category="conflict")
    # Partial beats ENUM_FOUND. Booking a partial as a full receipt
    # drops the residual. Unconfirmed residual stays a refusal, not a 500.
    if ev.is_partial and ev.void_confirmed:
        if enum != Event.ENUM_FOUND:
            raise GuardRejection(
                "split settle requires an attributed capture (ENUM_FOUND)",
                category="conflict")
        # Gemini v5 review (Fix 3): GuardRejection, not ValueError.
        _validate_capture_amount(ev.amount_cents)
        _check_fund_conservation(ev)
        _require_capture_ids(ev)
        return (State.S10b_CAPTURED_SPLIT,
                [RecordCapture(capture_ids=ev.capture_ids,
                              amount_cents=ev.amount_cents),
                 ClearReviewFlag(), EmitReceipt(kind="split")])
    if ev.is_partial and not ev.void_confirmed:
        if (enum == Event.ENUM_UNATTRIBUTABLE
                and ev.read in _SAFE_UNATTRIBUTABLE_READS):
            return (State.S10c_CAPTURED_UNAUDITED,
                    [ClearReviewFlag(), EmitReceipt(kind="unaudited")])
        raise GuardRejection(
            "residual void not confirmed — refusing to force-settle",
            category="conflict")
    if enum == Event.ENUM_FOUND:
        # Claude review (MEDIUM): S9 cross-checks flags against enum, S5
        # must too. ENUM_FOUND with no capture evidence is contradictory —
        # refuse rather than booking S10a with an empty RecordCapture.
        if not ev.has_capture and not ev.is_partial:
            raise GuardRejection(
                "ENUM_FOUND requires has_capture or is_partial",
                category="conflict")
        # Gemini v5 review (Fix 3): GuardRejection, not ValueError.
        _validate_capture_amount(ev.amount_cents)
        _check_fund_conservation(ev)
        _require_capture_ids(ev)
        return (State.S10a_CAPTURED_FULL,
                [RecordCapture(capture_ids=ev.capture_ids,
                              amount_cents=ev.amount_cents),
                 ClearReviewFlag(), EmitReceipt(kind="full")])
    if enum == Event.ENUM_NONE:
        # Two-phase void. S11 only on R_VOID_OK — the wedge stays until
        # PayPal confirms, so a failed POST cannot land in a sink.
        return _begin_void(ev, State.S5_AUTHORIZED_WEDGED)
    if enum == Event.ENUM_UNATTRIBUTABLE:
        if ev.read in _SAFE_UNATTRIBUTABLE_READS:
            return (State.S10c_CAPTURED_UNAUDITED,
                    [ClearReviewFlag(), EmitReceipt(kind="unaudited")])
        return (State.S5_AUTHORIZED_WEDGED,
                [NoOp(note="unattributable — wedge persists")])
    raise GuardRejection(
        f"cannot force-settle on enum={enum}", category="conflict")

@_targets(State.S5_AUTHORIZED_WEDGED, State.S10a_CAPTURED_FULL, State.S10c_CAPTURED_UNAUDITED, State.S11_VOIDED_SETTLED)
def _s5_reconcile(ev: Evidence):
    ev = _ev(ev)
    # v8.2: a wedged job with a pending intent gets its read
    # re-evaluated — the key's fate may have changed since the wedge.
    # (A listed capture on a wedged job stays wedged; force-settle is
    # the exit — the pending branch returns None for ENUM_FOUND.)
    if ev.pending_intent_id is not None:
        decided = _pending_intent_reconcile(ev, State.S5_AUTHORIZED_WEDGED)
        if decided is not None:
            return decided
    # A definitive CAPTURED read clears the wedge — PayPal confirms money
    # moved. This mirrors the S4 CAPTURED arm (legacy _reconcile_with_auth
    # clears the flag via _record_captured_authorization).
    if ev.read == Event.READ_CAPTURED:
        if ev.enum == Event.ENUM_NONE:
            return (State.S10c_CAPTURED_UNAUDITED,
                    [RecordCapture(capture_ids=(),
                                  amount_cents=None),
                     ClearReviewFlag(),
                     EmitReceipt(kind="unaudited")])
        if ev.enum == Event.ENUM_FOUND and ev.has_capture:
            if ev.amount_cents is None:
                return (State.S10c_CAPTURED_UNAUDITED,
                        [RecordCapture(capture_ids=tuple(ev.capture_ids),
                                      amount_cents=None),
                         ClearReviewFlag(),
                         EmitReceipt(kind="unaudited")])
            return (State.S10a_CAPTURED_FULL,
                    [RecordCapture(capture_ids=tuple(ev.capture_ids),
                                  amount_cents=ev.amount_cents),
                     ClearReviewFlag(),
                     EmitReceipt(kind="full")])
    return (State.S5_AUTHORIZED_WEDGED, [NoOp(note="wedge persists")])

@_targets(State.S5_AUTHORIZED_WEDGED)
def _s5_webhook(ev: Evidence):
    return (State.S5_AUTHORIZED_WEDGED,
            [NoOp(note="wedge persists; caller may issue E_RECONCILE")])

@_targets(State.S5_AUTHORIZED_WEDGED)
def _s5_cancel_pending(ev: Evidence):
    # S5 has no pending-intent state in Rev 3 (ghost S5 eliminated) —
    # cancelling here is a no-op. Kept as an explicit cell for I5.
    ev = _ev(ev)
    _require_payer(ev)
    return (State.S5_AUTHORIZED_WEDGED, [NoOp(note="no pending intent in S5")])


@_targets(State.S11_VOIDED_SETTLED)
def _s5_r_void_ok(ev: Evidence):
    ev = _ev(ev)
    return (State.S11_VOIDED_SETTLED,
            [_record_void(ev), ClearReviewFlag(), ClearIntent()])


@_targets(State.S5_AUTHORIZED_WEDGED)
def _s5_r_void_fail(ev: Evidence):
    return (State.S5_AUTHORIZED_WEDGED,
            [NoOp(note="void failed — wedge persists, intent kept")])


@_targets(State.S5_AUTHORIZED_WEDGED)
def _s5_r_void_422(ev: Evidence):
    # Gemini Fix 4 (HIGH): 422 means PayPal already voided — do not
    # retry forever. Clear the intent; caller reconciles.
    return (State.S5_AUTHORIZED_WEDGED,
            [ClearIntent(),
             NoOp(note="void already processed at PayPal (422) — "
                       "caller should reconcile")])

# NOTE: E_VOID_DEADLINE is not in S5's table — a wedged job must be
# force-settled, not voided around the wedge. Callers in S5 with
# E_VOID_DEADLINE get UnhandledTransition (loud, per I5).


# -- S6 (DELIVERED_OPEN) -----------------------------------------------------

@_targets(State.S7_DELIVERED_PENDING)
def _s6_start_capture(ev: Evidence):
    ev = _ev(ev)
    _require_payer(ev)
    if not ev.delivery_verified:
        raise GuardRejection("delivery not verified", category="forbidden")
    key = _intent_key(ev, "capture")
    # Refined Fix 1: a FULL capture intent carries amount_cents=None
    # (legacy convention). Skip the amount validation for None — the
    # authorized_cents still bounds the capture server-side.
    if ev.amount_cents is not None:
        # Gemini v5 review (Fix 3): GuardRejection, not ValueError.
        _validate_amount(ev.amount_cents)
    return (State.S7_DELIVERED_PENDING,
            [SaveIntent(key=key, amount_cents=ev.amount_cents, kind="capture")])

@_targets(State.S7_DELIVERED_PENDING)
def _s6_start_capture_partial(ev: Evidence):
    ev = _ev(ev)
    _require_payer(ev)
    if not ev.delivery_verified:
        raise GuardRejection("delivery not verified", category="forbidden")
    key = _intent_key(ev, "capture_partial")
    # Gemini v5 review (Fix 3): GuardRejection, not ValueError.
    _validate_amount(ev.amount_cents)
    return (State.S7_DELIVERED_PENDING,
            [SaveIntent(key=key, amount_cents=ev.amount_cents,
                        kind="capture_partial")])

@_targets(State.S10a_CAPTURED_FULL, State.S10b_CAPTURED_SPLIT, State.S10c_CAPTURED_UNAUDITED, State.S11_VOIDED_SETTLED, State.S6_DELIVERED_OPEN, State.S8_DELIVERED_PARTIAL_RECORDED, State.S9_DELIVERED_WEDGED)
def _s6_reconcile(ev: Evidence):
    # Same read arms as S4, but DELIVERED variants of the targets.
    ev = _ev(ev)
    read, enum = ev.read, ev.enum
    # v8.2 (production-readiness item 2): the FSM models the pending
    # capture intent lifecycle — no legacy fallback for in-flight keys.
    if ev.pending_intent_id is not None:
        decided = _pending_intent_reconcile(ev, State.S6_DELIVERED_OPEN)
        if decided is not None:
            return decided
    if read in (Event.READ_CREATED, Event.READ_PENDING, None):
        return (State.S6_DELIVERED_OPEN, [NoOp(note="hold live")])
    if read == Event.READ_PARTIAL:
        # PARTIALLY_CAPTURED with no capture listed and none recorded is a
        # wedge — the partial's ID was never observed (legacy
        # _reconcile_with_auth sets a review flag here).
        if ev.enum == Event.ENUM_NONE and not ev.has_capture:
            return (State.S9_DELIVERED_WEDGED,
                    [SetReviewFlag(
                        reason="authorization is PARTIALLY_CAPTURED but no "
                               "capture is recorded and none is in flight "
                               "— operator review needed")])
        # A partial capture is listed — record it and stay open on the
        # residual (S8), not a wedge. Legacy _reconcile_with_auth does
        # the same: records the capture via _record_listed_capture and
        # stays DELIVERED. Only wedge if there's no capture to record.
        if ev.has_capture or ev.capture_ids:
            # Gemini v5 review (Fix 3): GuardRejection, not ValueError.
            # (None amount = malformed: record ID, amount unknown.)
            if ev.amount_cents is not None:
                _validate_capture_amount(ev.amount_cents)
                _check_fund_conservation(ev)
            _require_capture_ids(ev)
            return (State.S8_DELIVERED_PARTIAL_RECORDED,
                    [RecordCapture(capture_ids=tuple(ev.capture_ids),
                                  amount_cents=ev.amount_cents)])
        return (State.S9_DELIVERED_WEDGED,
                [SetReviewFlag(reason="PARTIALLY_CAPTURED in DELIVERED")])
    if read == Event.READ_CAPTURED:
        if enum == Event.ENUM_FOUND:
            # Malformed/unknown amount (None): record the ID with
            # unknown amount as unaudited — do NOT wedge. (Claude
            # finding 2: legacy settles with ID recorded, amount
            # unknown.)
            if ev.amount_cents is None:
                return (State.S10c_CAPTURED_UNAUDITED,
                        [RecordCapture(capture_ids=tuple(ev.capture_ids),
                                      amount_cents=None),
                         EmitReceipt(kind="unaudited")])
            # Gemini v5 review (Fix 3): GuardRejection, not ValueError.
            _validate_capture_amount(ev.amount_cents)
            _check_fund_conservation(ev)
            _require_capture_ids(ev)
            # Partial capture on a CAPTURED auth → S10b split with
            # residual. Legacy computes voided_amount = hold - captured.
            if ev.is_partial:
                hold = ev.authorized_cents or 0
                residual = hold - (ev.amount_cents or 0)
                actions = [RecordCapture(capture_ids=tuple(ev.capture_ids),
                                         amount_cents=ev.amount_cents),
                           ClearIntent()]
                if residual > 0:
                    # Book the residual via a synthetic voided amount.
                    return (State.S10b_CAPTURED_SPLIT,
                            actions + [RecordVoid(amount_cents=residual,
                                                  authorization_id=ev.authorization_id),
                                       EmitReceipt(kind="split")])
            return (State.S10a_CAPTURED_FULL,
                    [RecordCapture(capture_ids=tuple(ev.capture_ids),
                                  amount_cents=ev.amount_cents),
                     ClearIntent(), EmitReceipt(kind="full")])
        if enum == Event.ENUM_UNREAD:
            # PayPal says CAPTURED but order unreadable. If books have
            # a capture, leave open. Otherwise trust CAPTURED and mark
            # unaudited (legacy _record_captured_authorization).
            if ev.has_capture:
                return (State.S6_DELIVERED_OPEN, [NoOp(note="retry")])
            return (State.S10c_CAPTURED_UNAUDITED,
                    [RecordCapture(capture_ids=(),
                                  amount_cents=None),
                     EmitReceipt(kind="unaudited")])
        if enum == Event.ENUM_ORDER_UNREAD:
            # §6.3 fix — "will retry", no wedge.
            return (State.S6_DELIVERED_OPEN,
                    [NoOp(note="order unread but capture on books — will retry")])
        if enum == Event.ENUM_NONE:
            # PayPal reports CAPTURED but lists no capture — the
            # "reconciled fallback": money moved but the details are
            # not visible. Record as unaudited CAPTURED, not a wedge.
            return (State.S10c_CAPTURED_UNAUDITED,
                    [RecordCapture(capture_ids=(),
                                  amount_cents=None),
                     EmitReceipt(kind="unaudited")])
        if enum == Event.ENUM_AMBIGUOUS:
            # Round 45: PENDING (non-terminal) does NOT wedge. The job
            # stays open awaiting COMPLETED — PENDING never settles but
            # is not a conflict. (Applies in S4/S6/S7/S8 alike.)
            return (State.S6_DELIVERED_OPEN,
                    [NoOp(note="awaiting COMPLETED (PENDING listed)")])
        return (State.S9_DELIVERED_WEDGED,
                [SetReviewFlag(
                    reason="reconcile blocked: captures cannot be attributed "
                           "(amount, currency, or id problem — operator "
                           "review needed)")])
    if read == Event.READ_RELEASED:
        # P0 fix (19-case void gap): check for captures BEFORE declaring
        # void. A released hold with a listed capture means money moved —
        # settling as VOIDED would lose it.
        # v8 money-safety fix: ENUM_NONE ("proved absent") with a capture
        # already on the books settles from the books too — the recorded
        # capture came from an earlier PayPal read, and voiding it would
        # erase real money.
        if ev.has_capture and ev.enum in (Event.ENUM_FOUND, Event.ENUM_NONE):
            _validate_capture_amount(ev.amount_cents)
            _check_fund_conservation(ev)
            if ev.is_partial:
                actions = [RecordCapture(capture_ids=tuple(ev.capture_ids),
                                         amount_cents=ev.amount_cents),
                           ClearIntent()]
                if ev.voided_amount_cents is not None:
                    return (State.S10b_CAPTURED_SPLIT,
                            actions + [_record_void(ev),
                                       EmitReceipt(kind="split")])
                return (State.S10c_CAPTURED_UNAUDITED,
                        actions + [EmitReceipt(kind="unaudited")])
            return (State.S10a_CAPTURED_FULL,
                    [RecordCapture(capture_ids=tuple(ev.capture_ids),
                                  amount_cents=ev.amount_cents),
                     ClearIntent(), EmitReceipt(kind="full")])
        # v8 money-safety fix 1: the capture enumeration itself failed
        # (enum=None — PayPalError in the bridge). The order is
        # unreadable, so a capture could be hiding behind the released
        # hold. Wedge; never void blind.
        if ev.enum is None:
            return (State.S9_DELIVERED_WEDGED,
                    [SetReviewFlag(
                        reason="order could not be read — cannot confirm "
                               "no capture moved (operator review needed)")])
        # AMBIGUOUS / UNATTRIBUTABLE always wedge: a non-terminal or
        # unattributable capture may still move money.
        if ev.enum in (Event.ENUM_AMBIGUOUS, Event.ENUM_UNATTRIBUTABLE):
            if ev.enum == Event.ENUM_AMBIGUOUS:
                reason = ("capture pending on released hold: non-terminal "
                          "capture is listed — money may still move "
                          "(operator review needed)")
            else:
                reason = ("capture pending on released hold: captures "
                          "cannot be attributed (amount, currency, or id "
                          "problem — operator review needed)")
            return (State.S9_DELIVERED_WEDGED,
                    [SetReviewFlag(reason=reason)])
        # v8 money-safety fix 3: UNREAD / ORDER_UNREAD wedge only on a
        # real capture risk. With no capture on the books and none
        # listed there is nothing to lose — fall through to the clean
        # void below instead of wedging.
        if ev.enum in (Event.ENUM_UNREAD, Event.ENUM_ORDER_UNREAD):
            if ev.has_capture:
                return (State.S9_DELIVERED_WEDGED,
                        [SetReviewFlag(
                            reason="capture pending on released hold: order "
                                   "could not be read — cannot confirm no "
                                   "other capture moved (operator review "
                                   "needed)")])
        if ev.is_partial:
            return (State.S10c_CAPTURED_UNAUDITED,
                    [ClearIntent(), EmitReceipt(kind="unaudited")])
        # v8 money-safety fix 4: one ledger for the void — book it
        # exactly like S4 (Round 35), not a bare NoOp.
        if ev.authorized_cents is None:
            return (State.S9_DELIVERED_WEDGED,
                    [SetReviewFlag(
                        reason="authorization released but hold amount "
                               "unknown — operator review needed")])
        return (State.S11_VOIDED_SETTLED,
                [RecordVoid(amount_cents=ev.authorized_cents,
                            authorization_id=ev.authorization_id),
                 ClearIntent(),
                 NoOp(note="hold released")])
    if read == Event.READ_500:
        return (State.S6_DELIVERED_OPEN, [NoOp(note="read failed — retry")])
    raise UnhandledTransition(State.S6_DELIVERED_OPEN, Event.E_RECONCILE)

@_targets(State.S6_DELIVERED_OPEN)
def _s6_webhook_hint(ev: Evidence):
    return (State.S6_DELIVERED_OPEN,
            [NoOp(note="webhook is hint — caller issues E_RECONCILE")])

@_targets(State.S6_DELIVERED_OPEN)
def _s6_webhook_denied(ev: Evidence):
    return (State.S6_DELIVERED_OPEN, [NoOp(note="denial recorded; stay open")])


# -- S7 (DELIVERED_PENDING) --------------------------------------------------

@_targets(State.S10a_CAPTURED_FULL)
def _s7_r_cap_completed(ev: Evidence):
    ev = _ev(ev)
    # Intent was full OR PayPal returned a full capture on a partial
    # intent — both settle to S10a (§5.7).
    # Gemini v5 review (Fix 3): GuardRejection, not ValueError.
    # Legacy policy (Claude finding 2, Medium): a malformed/absent
    # amount does NOT wedge — the money moved, so the capture settles
    # with the ID recorded and the amount unknown (None). Only a
    # zero amount is rejected (not a real capture).
    if ev.amount_cents is None:
        # Amount unknown — record ID, settle, leave amount None.
        # Skip fund conservation (nothing to check).
        pass
    else:
        _validate_capture_amount(ev.amount_cents)
    # Legacy policy (round 11, mirrored): an over-hold capture is
    # RECORDED, not refused — the money moved, so refusing would leave
    # it unrecorded. Flag for operator review instead of raising
    # GuardRejection from _check_fund_conservation.
    extra_actions = []
    if (ev.authorized_cents is not None and ev.amount_cents is not None
            and ev.amount_cents > ev.authorized_cents):
        extra_actions.append(SetReviewFlag(
            reason=f"capture returned {ev.amount_cents} cents, more than "
                   f"the {ev.authorized_cents}-cent hold "
                   f"(operator review needed)"))
    elif ev.amount_cents is not None:
        _check_fund_conservation(ev)
    # Meta-review: strict — an unaudited capture (no id) must use the
    # explicit E_UNAUDITED_CAPTURE event, not R_CAP_COMPLETED with
    # empty ids. _require_capture_ids raises GuardRejection(400) on
    # missing/blank ids so bad data is explicit, never silent.
    _require_capture_ids(ev)
    return (State.S10a_CAPTURED_FULL,
            [RecordCapture(capture_ids=tuple(ev.capture_ids),
                          amount_cents=ev.amount_cents),
             ClearIntent(), EmitReceipt(kind="full")] + extra_actions)

@_targets(State.S10c_CAPTURED_UNAUDITED, State.S8_DELIVERED_PARTIAL_RECORDED)
def _s7_unaudited_capture(ev: Evidence):
    """Explicit unaudited capture handling with partial-capture protection."""
    ev = _ev(ev)
    # None amount = unknown (malformed PayPal response). Legacy records
    # the capture with amount unknown and settles unaudited — never wedge
    # on a missing amount here. Skip validation when nothing to validate.
    if ev.amount_cents is not None:
        _validate_capture_amount(ev.amount_cents)
        _check_fund_conservation(ev)
    is_partial = (
        ev.authorized_cents is not None
        and ev.amount_cents is not None
        and ev.amount_cents < ev.authorized_cents
    )
    if is_partial:
        key = _intent_key(ev, "residual_void")
        return (State.S8_DELIVERED_PARTIAL_RECORDED,
                [RecordCapture(capture_ids=(),
                              amount_cents=ev.amount_cents),
                 ClearIntent(),
                 SaveResidualVoidIntent(key=key)])
    return (State.S10c_CAPTURED_UNAUDITED,
            [RecordCapture(capture_ids=(),
                          amount_cents=ev.amount_cents),
             ClearIntent(),
             EmitReceipt(kind="unaudited")])

@_targets(State.S6_DELIVERED_OPEN)
def _s7_r_cap_declined(ev: Evidence):
    return (State.S6_DELIVERED_OPEN,
            [ClearIntent(),
             NoOp(note="declined — caller increments attempt counter")])

@_targets(State.S7_DELIVERED_PENDING)
def _s7_r_cap_500(ev: Evidence):
    # I4: keep intent for replay with the same idempotency key.
    return (State.S7_DELIVERED_PENDING,
            [NoOp(note="keep intent for replay — caller retries same key")])

@_targets(State.S8_DELIVERED_PARTIAL_RECORDED)
def _s7_r_partial_cap_completed(ev: Evidence):
    # Rev 2 (two-phase): record the partial capture, clear the capture
    # intent, save the residual-void intent. The caller POSTs the void.
    # If the server crashes before the void POST, S8's persisted state
    # proves the residual is open (§1.4).
    # Phase 2 Method 2: a malformed/unparseable amount arrives as None
    # (legacy _record_capture_amount leaves the field alone instead of
    # wedging — the ID is recorded, the amount stays unknown). Skip
    # amount validation for None; the executor books the ID with an
    # unknown amount.
    ev = _ev(ev)
    key = _intent_key(ev, "residual_void")
    if ev.amount_cents is not None:
        # Gemini v5 review (Fix 3): GuardRejection, not ValueError.
        _validate_capture_amount(ev.amount_cents)
        _check_fund_conservation(ev)
    _require_capture_ids(ev)
    return (State.S8_DELIVERED_PARTIAL_RECORDED,
            [RecordCapture(capture_ids=tuple(ev.capture_ids),
                          amount_cents=ev.amount_cents),
             ClearIntent(), SaveResidualVoidIntent(key=key)])

@_targets(State.S6_DELIVERED_OPEN)
def _s7_cancel_pending(ev: Evidence):
    ev = _ev(ev)
    _require_payer(ev)
    return (State.S6_DELIVERED_OPEN, [ClearIntent()])

@_targets(State.S10a_CAPTURED_FULL, State.S10b_CAPTURED_SPLIT, State.S10c_CAPTURED_UNAUDITED, State.S11_VOIDED_SETTLED, State.S7_DELIVERED_PENDING, State.S8_DELIVERED_PARTIAL_RECORDED, State.S9_DELIVERED_WEDGED)
def _s7_reconcile(ev: Evidence):
    ev = _ev(ev)
    read = ev.read
    # v8.2 (production-readiness item 2): the FSM models the pending
    # capture intent lifecycle — no legacy fallback for in-flight keys.
    if ev.pending_intent_id is not None:
        decided = _pending_intent_reconcile(ev, State.S7_DELIVERED_PENDING)
        if decided is not None:
            return decided
    if read == Event.READ_CAPTURED:
        # ENUM_NONE (no capture listed, amount unknown): the reconciled
        # fallback — record as unaudited without amount validation.
        # Legacy _reconcile_with_auth does the same via _mark_unaudited.
        if ev.enum == Event.ENUM_NONE and ev.amount_cents is None:
            return (State.S10c_CAPTURED_UNAUDITED,
                    [RecordCapture(capture_ids=(),
                                  amount_cents=None),
                     ClearIntent(),
                     EmitReceipt(kind="unaudited")])
        # UNATTRIBUTABLE (malformed amount): wedge, don't crash.
        # Legacy wedges via _wedge_on_unattributable.
        if ev.enum == Event.ENUM_UNATTRIBUTABLE:
            return (State.S9_DELIVERED_WEDGED,
                    [SetReviewFlag(
                        reason="reconcile blocked: captures cannot be "
                               "attributed (amount, currency, or id problem "
                               "— operator review needed)")])
        if ev.enum == Event.ENUM_AMBIGUOUS:
            # Round 45: PENDING (non-terminal) does NOT wedge. The job
            # stays open awaiting COMPLETED — PENDING never settles but
            # is not a conflict. (Applies in S4/S6/S7/S8 alike.)
            return (State.S7_DELIVERED_PENDING,
                    [NoOp(note="awaiting COMPLETED (PENDING listed)")])
        if ev.enum in (Event.ENUM_UNREAD, Event.ENUM_ORDER_UNREAD):
            # Order unreadable — leave open for retry, don't wedge.
            return (State.S7_DELIVERED_PENDING,
                    [NoOp(note="order unread — retry")])
        _validate_capture_amount(ev.amount_cents)
        _check_fund_conservation(ev)
        if not ev.capture_ids:
            return (State.S10c_CAPTURED_UNAUDITED,
                    [RecordCapture(capture_ids=(),
                                  amount_cents=ev.amount_cents),
                     ClearIntent(),
                     EmitReceipt(kind="unaudited")])
        return (State.S10a_CAPTURED_FULL,
                [RecordCapture(capture_ids=tuple(ev.capture_ids),
                              amount_cents=ev.amount_cents),
                 ClearIntent(), EmitReceipt(kind="full")])
    if read == Event.READ_PARTIAL:
        key = _intent_key(ev, "residual_void")
        # PARTIALLY_CAPTURED with no capture listed and none recorded is a
        # wedge — the partial's ID was never observed (legacy
        # _reconcile_with_auth sets a review flag here). Don't book it as
        # an unaudited partial.
        if ev.enum == Event.ENUM_NONE and not ev.has_capture:
            return (State.S9_DELIVERED_WEDGED,
                    [SetReviewFlag(
                        reason="authorization is PARTIALLY_CAPTURED but no "
                               "capture is recorded and none is in flight "
                               "— operator review needed")])
        # Gemini v5 review (Fix 3): GuardRejection, not ValueError.
        _validate_capture_amount(ev.amount_cents)
        _check_fund_conservation(ev)
        if not ev.capture_ids:
            # Grok fix order #5: a partial with no capture IDs is an
            # unaudited partial — route to S8 (not a 400). The residual
            # void intent is armed so the remainder can be released.
            return (State.S8_DELIVERED_PARTIAL_RECORDED,
                    [RecordCapture(capture_ids=(),
                                  amount_cents=ev.amount_cents),
                     ClearIntent(),
                     SaveResidualVoidIntent(key=key)])
        _require_capture_ids(ev)
        return (State.S8_DELIVERED_PARTIAL_RECORDED,
                [RecordCapture(capture_ids=ev.capture_ids,
                              amount_cents=ev.amount_cents),
                 ClearIntent(),
                 SaveResidualVoidIntent(key=key)])
    if read in (Event.READ_CREATED, Event.READ_PENDING, None):
        return (State.S7_DELIVERED_PENDING, [NoOp(note="intent in flight")])
    if read == Event.READ_500:
        return (State.S7_DELIVERED_PENDING, [NoOp(note="read failed — retry")])
    if read == Event.READ_RELEASED:
        # P0 fix (19-case void gap): check for captures BEFORE declaring
        # void. A released hold with a listed capture means money moved —
        # settling as VOIDED would lose it.
        # v8 money-safety fix: ENUM_NONE ("proved absent") with a capture
        # already on the books settles from the books too — the recorded
        # capture came from an earlier PayPal read, and voiding it would
        # erase real money.
        if ev.has_capture and ev.enum in (Event.ENUM_FOUND, Event.ENUM_NONE):
            _validate_capture_amount(ev.amount_cents)
            _check_fund_conservation(ev)
            if ev.is_partial:
                actions = [RecordCapture(capture_ids=tuple(ev.capture_ids),
                                         amount_cents=ev.amount_cents),
                           ClearIntent()]
                if ev.voided_amount_cents is not None:
                    return (State.S10b_CAPTURED_SPLIT,
                            actions + [_record_void(ev),
                                       EmitReceipt(kind="split")])
                return (State.S10c_CAPTURED_UNAUDITED,
                        actions + [EmitReceipt(kind="unaudited")])
            return (State.S10a_CAPTURED_FULL,
                    [RecordCapture(capture_ids=tuple(ev.capture_ids),
                                  amount_cents=ev.amount_cents),
                     ClearIntent(), EmitReceipt(kind="full")])
        # v8 money-safety fix 1: the capture enumeration itself failed
        # (enum=None — PayPalError in the bridge). The order is
        # unreadable, so a capture could be hiding behind the released
        # hold. Wedge; never void blind.
        if ev.enum is None:
            return (State.S9_DELIVERED_WEDGED,
                    [SetReviewFlag(
                        reason="order could not be read — cannot confirm "
                               "no capture moved (operator review needed)")])
        # AMBIGUOUS / UNATTRIBUTABLE always wedge: a non-terminal or
        # unattributable capture may still move money.
        if ev.enum in (Event.ENUM_AMBIGUOUS, Event.ENUM_UNATTRIBUTABLE):
            if ev.enum == Event.ENUM_AMBIGUOUS:
                reason = ("capture pending on released hold: non-terminal "
                          "capture is listed — money may still move "
                          "(operator review needed)")
            else:
                reason = ("capture pending on released hold: captures "
                          "cannot be attributed (amount, currency, or id "
                          "problem — operator review needed)")
            return (State.S9_DELIVERED_WEDGED,
                    [SetReviewFlag(reason=reason)])
        # v8 money-safety fix 3: UNREAD / ORDER_UNREAD wedge only on a
        # real capture risk. With no capture on the books and none
        # listed there is nothing to lose — fall through to the clean
        # void below instead of wedging.
        if ev.enum in (Event.ENUM_UNREAD, Event.ENUM_ORDER_UNREAD):
            if ev.has_capture:
                return (State.S9_DELIVERED_WEDGED,
                        [SetReviewFlag(
                            reason="capture pending on released hold: order "
                                   "could not be read — cannot confirm no "
                                   "other capture moved (operator review "
                                   "needed)")])
        # v8 money-safety fix 4: one ledger for the void — book it
        # exactly like S4 (Round 35), not a bare intent clear.
        if ev.authorized_cents is None:
            return (State.S9_DELIVERED_WEDGED,
                    [SetReviewFlag(
                        reason="authorization released but hold amount "
                               "unknown — operator review needed")])
        return (State.S11_VOIDED_SETTLED,
                [RecordVoid(amount_cents=ev.authorized_cents,
                            authorization_id=ev.authorization_id),
                 ClearIntent(),
                 NoOp(note="hold released")])
    raise UnhandledTransition(State.S7_DELIVERED_PENDING, Event.E_RECONCILE)

@_targets(State.S7_DELIVERED_PENDING)
def _s7_webhook_hint(ev: Evidence):
    return (State.S7_DELIVERED_PENDING,
            [NoOp(note="webhook is hint — caller issues E_RECONCILE")])

@_targets(State.S6_DELIVERED_OPEN)
def _s7_webhook_denied(ev: Evidence):
    return (State.S6_DELIVERED_OPEN,
            [ClearIntent(), NoOp(note="denial recorded; intent cleared")])

@_targets(State.S6_DELIVERED_OPEN)
def _s6_deadline(ev: Evidence):
    # Gemini Fix 2 (CRITICAL): the deadline timer can fire after delivery
    # was submitted (S4 → S6). Delivery is recorded; the buyer cannot void
    # the deadline. Enables the buyer-inspection window / auto-capture.
    return (State.S6_DELIVERED_OPEN,
            [NoOp(note="deadline passed post-delivery — delivery recorded; "
                       "awaiting capture")])

@_targets(State.S7_DELIVERED_PENDING)
def _s7_webhook_auth_created(ev: Evidence):
    return (State.S7_DELIVERED_PENDING, [NoOp(note="unexpected; intent persists")])


# -- S8 (DELIVERED_PARTIAL_RECORDED) -----------------------------------------

@_targets(State.S8_DELIVERED_PARTIAL_RECORDED)
def _s8_start_residual_void(ev: Evidence):
    # Rev 3 (Gemini Defect 2): two-phase residual void. Save the intent;
    # the caller POSTs the void; R_VOID_OK / R_VOID_FAIL settles.
    ev = _ev(ev)
    key = _intent_key(ev, "residual_void")
    return (State.S8_DELIVERED_PARTIAL_RECORDED,
            [SaveResidualVoidIntent(key=key)])

@_targets(State.S10b_CAPTURED_SPLIT)
def _s8_r_void_ok(ev: Evidence):
    ev = _ev(ev)
    return (State.S10b_CAPTURED_SPLIT,
            [_record_void(ev), ClearIntent(), EmitReceipt(kind="split")])

@_targets(State.S8_DELIVERED_PARTIAL_RECORDED)
def _s8_r_void_fail(ev: Evidence):
    return (State.S8_DELIVERED_PARTIAL_RECORDED,
            [NoOp(note="keep for retry")])


@_targets(State.S8_DELIVERED_PARTIAL_RECORDED)
def _s8_r_void_422(ev: Evidence):
    # Gemini Fix 4 (HIGH): 422 = already voided at PayPal. Clear the
    # intent; caller reconciles to confirm.
    return (State.S8_DELIVERED_PARTIAL_RECORDED,
            [ClearIntent(),
             NoOp(note="void already processed at PayPal (422) — "
                       "caller should reconcile")])

@_targets(State.S10a_CAPTURED_FULL, State.S10b_CAPTURED_SPLIT,
          State.S10c_CAPTURED_UNAUDITED, State.S8_DELIVERED_PARTIAL_RECORDED,
          State.S9_DELIVERED_WEDGED)
def _s8_reconcile(ev: Evidence):
    ev = _ev(ev)
    read = ev.read
    if read == Event.READ_PARTIAL:
        # Rev 2 (§6.1, Critical Flaw 2): wedge to S9 (DELIVERED_WEDGED),
        # NOT S5. Demoting to AUTHORIZED would erase the delivery.
        return (State.S9_DELIVERED_WEDGED,
                [SetReviewFlag(reason="residual void unconfirmed — review")])
    if read == Event.READ_CAPTURED:
        if ev.enum == Event.ENUM_FOUND:
            # Gemini v5 review (Fix 3): GuardRejection, not ValueError.
            _validate_capture_amount(ev.amount_cents)
            _check_fund_conservation(ev)
            _require_capture_ids(ev)
            return (State.S10a_CAPTURED_FULL,
                    [RecordCapture(capture_ids=ev.capture_ids,
                                  amount_cents=ev.amount_cents),
                     EmitReceipt(kind="full")])
        if ev.enum in (Event.ENUM_UNREAD, Event.ENUM_ORDER_UNREAD):
            # Order unreadable with a partial on the books — stay open
            # for retry, don't wedge. (Test r31_c2: the partial is
            # already recorded, we just can't confirm the rest.)
            return (State.S8_DELIVERED_PARTIAL_RECORDED,
                    [NoOp(note="order unread — retry")])
        if ev.enum == Event.ENUM_AMBIGUOUS:
            # Round 45: PENDING listed on a CAPTURED arm does NOT wedge.
            # PENDING never settles but is not a conflict — stay open
            # awaiting COMPLETED, no review flag.
            return (State.S8_DELIVERED_PARTIAL_RECORDED,
                    [NoOp(note="awaiting COMPLETED (PENDING listed)")])
        elif ev.enum == Event.ENUM_UNATTRIBUTABLE:
            reason = ("capture read unclear: captures cannot be attributed "
                      "(amount, currency, or id problem — operator review "
                      "needed)")
        else:
            reason = ("capture read unclear: captures could not be "
                      f"enumerated ({ev.enum} — operator review needed)")
        return (State.S9_DELIVERED_WEDGED,
                [SetReviewFlag(reason=reason)])
    if read in (Event.READ_CREATED, Event.READ_PENDING, None):
        return (State.S8_DELIVERED_PARTIAL_RECORDED,
                [NoOp(note="residual still held")])
    if read == Event.READ_RELEASED:
        # S8 already has a partial on the books. A released residual is
        # never a pure void (S11) — that would drop the booked capture.
        # Confirmed void amount → split; otherwise unaudited.
        # Gemini v5 review (Fix 2, MEDIUM): clear the dangling intent.
        if ev.void_confirmed and ev.voided_amount_cents is not None:
            return (State.S10b_CAPTURED_SPLIT,
                    [_record_void(ev), ClearIntent(),
                     EmitReceipt(kind="split")])
        return (State.S10c_CAPTURED_UNAUDITED,
                [ClearIntent(), EmitReceipt(kind="unaudited")])
    if read == Event.READ_500:
        return (State.S8_DELIVERED_PARTIAL_RECORDED,
                [NoOp(note="read failed — retry")])
    raise UnhandledTransition(State.S8_DELIVERED_PARTIAL_RECORDED,
                              Event.E_RECONCILE)

@_targets(State.S8_DELIVERED_PARTIAL_RECORDED)
def _s8_webhook_hint(ev: Evidence):
    return (State.S8_DELIVERED_PARTIAL_RECORDED,
            [NoOp(note="webhook is hint — caller issues E_RECONCILE")])

@_targets(State.S8_DELIVERED_PARTIAL_RECORDED)
def _s8_webhook_other(ev: Evidence):
    return (State.S8_DELIVERED_PARTIAL_RECORDED,
            [NoOp(note="residual still open")])

@_targets()  # guard-only: always raises, no transitions
def _s8_force_settle(ev: Evidence):
    # Not wedged — use the S8→S9 reconcile arm first.
    raise GuardRejection("not wedged — reconcile to S9 first",
                         category="forbidden")

@_targets()  # guard-only: always raises, no transitions
def _s8_void_deadline(ev: Evidence):
    raise GuardRejection("wrong state — needs AUTHORIZED", category="forbidden")


# -- S9 (DELIVERED_WEDGED) ---------------------------------------------------

@_targets(State.S10a_CAPTURED_FULL, State.S10b_CAPTURED_SPLIT,
          State.S10c_CAPTURED_UNAUDITED, State.S9_DELIVERED_WEDGED)
def _s9_force_settle(ev: Evidence):
    ev = _ev(ev)
    _require_payer(ev)
    enum = ev.enum
    # Gemini v5 review (Fix 1, CRITICAL): S5 checks enum None, S9 must too.
    # A caller with readable=True, has_capture=False, enum=None used to
    # fall through to _begin_void — a blind void.
    if enum is None:
        raise GuardRejection(
            "E_FORCE_SETTLE requires read evidence (enum outcome)",
            category="validation")
    # Never settle or void without a read. Defaults (has_capture False,
    # enum None) used to look like "nothing captured" and void blind.
    if not ev.readable or enum in (Event.ENUM_UNREAD, Event.ENUM_ORDER_UNREAD):
        raise GuardRejection(
            "cannot force-settle: the order could not be read",
            category="conflict")
    if enum == Event.ENUM_AMBIGUOUS:
        raise GuardRejection("money may still move", category="conflict")
    # Flags and enum disagree → refuse. Do not let a false has_capture
    # void a capture ENUM_FOUND already saw, or the reverse.
    if enum == Event.ENUM_FOUND and not ev.has_capture and not ev.is_partial:
        raise GuardRejection(
            "enum found a capture but has_capture is false — refusing to void",
            category="conflict")
    if enum == Event.ENUM_NONE and (ev.has_capture or ev.is_partial):
        raise GuardRejection(
            "enum found no capture but flags say money moved — refusing to settle",
            category="conflict")
    if ev.is_partial and ev.void_confirmed:
        if not (ev.has_capture or ev.capture_ids):
            raise GuardRejection(
                "split settle requires a recorded capture", category="conflict")
        # Gemini v5 review (Fix 3): GuardRejection, not ValueError.
        _validate_capture_amount(ev.amount_cents)
        _check_fund_conservation(ev)
        _require_capture_ids(ev)
        return (State.S10b_CAPTURED_SPLIT,
                [RecordCapture(capture_ids=ev.capture_ids,
                              amount_cents=ev.amount_cents),
                 ClearReviewFlag(), EmitReceipt(kind="split")])
    if ev.has_capture and not ev.is_partial:
        # Gemini Fix 5 (HIGH): unattributable funds must not settle on
        # the audited books — route to S10c, not S10a.
        if enum == Event.ENUM_UNATTRIBUTABLE:
            # Gemini v5 review (Fix 3): GuardRejection, not ValueError.
            _validate_capture_amount(ev.amount_cents)
            _check_fund_conservation(ev)
            _require_capture_ids(ev)
            return (State.S10c_CAPTURED_UNAUDITED,
                    [RecordCapture(capture_ids=ev.capture_ids,
                                  amount_cents=ev.amount_cents),
                     ClearReviewFlag(), EmitReceipt(kind="unaudited")])
        if enum != Event.ENUM_FOUND:
            raise GuardRejection(
                f"force-settle full capture requires ENUM_FOUND, got {enum}",
                category="conflict")
        # Gemini v5 review (Fix 3): GuardRejection, not ValueError.
        _validate_capture_amount(ev.amount_cents)
        _check_fund_conservation(ev)
        _require_capture_ids(ev)
        return (State.S10a_CAPTURED_FULL,
                [RecordCapture(capture_ids=ev.capture_ids,
                              amount_cents=ev.amount_cents),
                 ClearReviewFlag(), EmitReceipt(kind="full")])
    if ev.is_partial and not ev.void_confirmed:
        if (enum == Event.ENUM_UNATTRIBUTABLE
                and ev.read in _SAFE_UNATTRIBUTABLE_READS):
            return (State.S10c_CAPTURED_UNAUDITED,
                    [ClearReviewFlag(), EmitReceipt(kind="unaudited")])
        raise GuardRejection(
            "residual void not confirmed — refusing to force-settle",
            category="conflict")
    if not ev.has_capture and not ev.is_partial:
        if enum == Event.ENUM_UNATTRIBUTABLE:
            return (State.S9_DELIVERED_WEDGED,
                    [NoOp(note="unattributable — wedge persists")])
        # readable, no capture. Two-phase void; S11 waits for R_VOID_OK.
        return _begin_void(ev, State.S9_DELIVERED_WEDGED)
    raise GuardRejection(
        "cannot force-settle on this evidence", category="conflict")

@_targets(State.S9_DELIVERED_WEDGED, State.S10c_CAPTURED_UNAUDITED)
def _s9_reconcile(ev: Evidence):
    ev = _ev(ev)
    # v8.2: a wedged job with a pending intent gets its read
    # re-evaluated — the key's fate may have changed since the wedge.
    # (A listed capture on a wedged job stays wedged; force-settle is
    # the exit — the pending branch returns None for ENUM_FOUND.)
    if ev.pending_intent_id is not None:
        decided = _pending_intent_reconcile(ev, State.S9_DELIVERED_WEDGED)
        if decided is not None:
            return decided
    return (State.S9_DELIVERED_WEDGED, [NoOp(note="wedge persists")])

@_targets(State.S9_DELIVERED_WEDGED)
def _s9_cancel_pending(ev: Evidence):
    # Rev 3 (Gemini Defect 3): clear the intent, wedge persists. NEVER
    # demote to S5 (AUTHORIZED_WEDGED) — cancelling in DELIVERED_WEDGED
    # must not erase the delivery.
    ev = _ev(ev)
    _require_payer(ev)
    return (State.S9_DELIVERED_WEDGED, [ClearIntent()])


@_targets(State.S11_VOIDED_SETTLED)
def _s9_r_void_ok(ev: Evidence):
    ev = _ev(ev)
    return (State.S11_VOIDED_SETTLED,
            [_record_void(ev), ClearReviewFlag(), ClearIntent()])


@_targets(State.S9_DELIVERED_WEDGED)
def _s9_r_void_fail(ev: Evidence):
    return (State.S9_DELIVERED_WEDGED,
            [NoOp(note="void failed — wedge persists, intent kept")])


@_targets(State.S9_DELIVERED_WEDGED)
def _s9_r_void_422(ev: Evidence):
    # Gemini Fix 4 (HIGH): 422 = already voided at PayPal. Clear the
    # intent; caller reconciles to confirm.
    return (State.S9_DELIVERED_WEDGED,
            [ClearIntent(),
             NoOp(note="void already processed at PayPal (422) — "
                       "caller should reconcile")])

@_targets(State.S9_DELIVERED_WEDGED)
def _s9_webhook(ev: Evidence):
    return (State.S9_DELIVERED_WEDGED,
            [NoOp(note="wedge persists; caller may issue E_RECONCILE")])


# -- S10a / S10b / S10c / S11 (terminal) -------------------------------------

@_targets(State.S10a_CAPTURED_FULL)
def _t10a_reconcile(ev: Evidence):
    return (State.S10a_CAPTURED_FULL, [NoOp(note="terminal — no-op (I2)")])

@_targets(State.S10b_CAPTURED_SPLIT)
def _t10b_reconcile(ev: Evidence):
    return (State.S10b_CAPTURED_SPLIT, [NoOp(note="terminal — no-op (I2)")])

@_targets(State.S10c_CAPTURED_UNAUDITED)
def _t10c_reconcile(ev: Evidence):
    return (State.S10c_CAPTURED_UNAUDITED, [NoOp(note="terminal — no-op (I2)")])

@_targets(State.S11_VOIDED_SETTLED)
def _t11_reconcile(ev: Evidence):
    return (State.S11_VOIDED_SETTLED, [NoOp(note="terminal — no-op (I2)")])


@_targets()  # guard-only: always raises, no transitions
def _terminal_client_call(ev: Evidence):
    """Client mutation on a terminal job — explicit 409, not a 500.

    Claude review (LOW-MEDIUM): terminal + E_START_CAPTURE /
    E_FORCE_SETTLE / E_SUBMIT_DELIVERY / E_START_AUTHORIZE used to raise
    UnhandledTransition (HTTP 500, "matrix bug"). A client calling a
    mutating event on a settled job is a caller error (HTTP 409), not
    a dispatcher defect. The routing layer should prevent this, but the
    table is explicit per I5.
    """
    raise GuardRejection(
        "job is terminal — no client mutations allowed", category="conflict")


def _make_late_timer(state: State):
    """Factory for late-timer NoOp self-loop cells.

    Claude review (LOW-MEDIUM): 21 of 26 timer pairs had no cell. If a
    timer fires after the job already progressed past the state where
    the timer is meaningful, the dispatcher returns a NoOp self-loop
    instead of raising UnhandledTransition (HTTP 500). The caller is
    expected to cancel timers on progress; this is the backstop.
    """
    def _handler(ev: Evidence):
        return (state, [NoOp(note="timer fired late — job already progressed")])
    _handler.target_states = frozenset([state])
    _handler.__name__ = f"_late_timer_{state.name.lower()}"
    return _handler


# ---------------------------------------------------------------------------
# The full §5 table — (State, Event) -> handler
# ---------------------------------------------------------------------------

_TABLE: dict[tuple[State, Event], object] = {
    # -- S1 (QUOTED_FRESH) --
    (State.S1_QUOTED_FRESH, Event.E_CREATE_ORDER): _s1_create_order,
    (State.S1_QUOTED_FRESH, Event.E_RECONCILE): _s1_reconcile,
    (State.S1_QUOTED_FRESH, Event.WH_AUTH_CREATED): _s1_webhook_defensive,
    (State.S1_QUOTED_FRESH, Event.WH_AUTH_VOIDED): _s1_webhook_defensive,
    (State.S1_QUOTED_FRESH, Event.WH_CAP_COMPLETED): _s1_webhook_defensive,
    (State.S1_QUOTED_FRESH, Event.WH_CAP_DENIED): _s1_webhook_defensive,
    (State.S1_QUOTED_FRESH, Event.T_QUOTE_EXPIRY): _s1_quote_expiry,
    (State.S1_QUOTED_FRESH, Event.R_AUTH_DENIED): _s1_r_auth_denied_replay,

    # -- S2 (QUOTED_ORDERED) --
    (State.S2_QUOTED_ORDERED, Event.E_START_AUTHORIZE): _s2_start_authorize,
    (State.S2_QUOTED_ORDERED, Event.E_RECONCILE): _s2_reconcile,
    (State.S2_QUOTED_ORDERED, Event.WH_AUTH_CREATED): _s2_webhook,
    (State.S2_QUOTED_ORDERED, Event.WH_AUTH_VOIDED): _s2_webhook,
    (State.S2_QUOTED_ORDERED, Event.WH_CAP_COMPLETED): _s2_webhook,
    (State.S2_QUOTED_ORDERED, Event.WH_CAP_DENIED): _s2_webhook,
    (State.S2_QUOTED_ORDERED, Event.T_QUOTE_EXPIRY): _s2_quote_expiry,
    (State.S2_QUOTED_ORDERED, Event.E_CREATE_ORDER): _s2_create_order_replay,

    # -- S3 (QUOTED_ATTEMPTED) --
    (State.S3_QUOTED_ATTEMPTED, Event.R_AUTH_CREATED): _s3_r_auth_created,
    (State.S3_QUOTED_ATTEMPTED, Event.R_AUTH_DENIED): _s3_r_auth_denied,
    (State.S3_QUOTED_ATTEMPTED, Event.R_AUTH_500): _s3_r_auth_retryable,
    (State.S3_QUOTED_ATTEMPTED, Event.R_AUTH_MALFORMED): _s3_r_auth_retryable,
    (State.S3_QUOTED_ATTEMPTED, Event.E_CREATE_ORDER): _s3_create_order,
    (State.S3_QUOTED_ATTEMPTED, Event.R_OLD_ORDER_ACTIVE): _s3_r_old_order_active,
    (State.S3_QUOTED_ATTEMPTED, Event.R_OLD_ORDER_DEAD): _s3_r_old_order_dead,
    (State.S3_QUOTED_ATTEMPTED, Event.E_RECONCILE): _s3_reconcile,
    (State.S3_QUOTED_ATTEMPTED, Event.WH_AUTH_CREATED): _s3_webhook,
    (State.S3_QUOTED_ATTEMPTED, Event.WH_AUTH_VOIDED): _s3_webhook,
    (State.S3_QUOTED_ATTEMPTED, Event.WH_CAP_COMPLETED): _s3_webhook,
    (State.S3_QUOTED_ATTEMPTED, Event.WH_CAP_DENIED): _s3_webhook,
    (State.S3_QUOTED_ATTEMPTED, Event.T_QUOTE_EXPIRY): _s3_quote_expiry,

    # -- S4 (AUTHORIZED_LIVE) --
    (State.S4_AUTHORIZED_LIVE, Event.E_SUBMIT_DELIVERY): _s4_submit_delivery,
    (State.S4_AUTHORIZED_LIVE, Event.E_VOID_DEADLINE): _s4_void_deadline,
    (State.S4_AUTHORIZED_LIVE, Event.R_VOID_OK): _s4_r_void_ok,
    (State.S4_AUTHORIZED_LIVE, Event.R_VOID_FAIL): _s4_r_void_fail,
    (State.S4_AUTHORIZED_LIVE, Event.R_VOID_422): _s4_r_void_422,
    (State.S4_AUTHORIZED_LIVE, Event.R_VOID_500): _s4_r_void_fail,
    (State.S4_AUTHORIZED_LIVE, Event.E_RECONCILE): _s4_reconcile,
    (State.S4_AUTHORIZED_LIVE, Event.WH_AUTH_VOIDED): _s4_webhook_voided_or_completed,
    (State.S4_AUTHORIZED_LIVE, Event.WH_CAP_COMPLETED): _s4_webhook_voided_or_completed,
    (State.S4_AUTHORIZED_LIVE, Event.WH_CAP_DENIED): _s4_webhook_denied,
    (State.S4_AUTHORIZED_LIVE, Event.WH_AUTH_CREATED): _s4_webhook_voided_or_completed,
    (State.S4_AUTHORIZED_LIVE, Event.T_DEADLINE): _s4_deadline,

    # -- S5 (AUTHORIZED_WEDGED) --
    (State.S5_AUTHORIZED_WEDGED, Event.E_FORCE_SETTLE): _s5_force_settle,
    (State.S5_AUTHORIZED_WEDGED, Event.R_VOID_OK): _s5_r_void_ok,
    (State.S5_AUTHORIZED_WEDGED, Event.R_VOID_FAIL): _s5_r_void_fail,
    (State.S5_AUTHORIZED_WEDGED, Event.R_VOID_422): _s5_r_void_422,
    (State.S5_AUTHORIZED_WEDGED, Event.R_VOID_500): _s5_r_void_fail,
    (State.S5_AUTHORIZED_WEDGED, Event.E_RECONCILE): _s5_reconcile,
    (State.S5_AUTHORIZED_WEDGED, Event.E_CANCEL_PENDING): _s5_cancel_pending,
    (State.S5_AUTHORIZED_WEDGED, Event.WH_AUTH_VOIDED): _s5_webhook,
    (State.S5_AUTHORIZED_WEDGED, Event.WH_CAP_COMPLETED): _s5_webhook,
    (State.S5_AUTHORIZED_WEDGED, Event.WH_CAP_DENIED): _s5_webhook,
    (State.S5_AUTHORIZED_WEDGED, Event.WH_AUTH_CREATED): _s5_webhook,

    # -- S6 (DELIVERED_OPEN) --
    (State.S6_DELIVERED_OPEN, Event.E_START_CAPTURE): _s6_start_capture,
    (State.S6_DELIVERED_OPEN, Event.E_START_CAPTURE_PARTIAL): _s6_start_capture_partial,
    (State.S6_DELIVERED_OPEN, Event.E_RECONCILE): _s6_reconcile,
    (State.S6_DELIVERED_OPEN, Event.WH_CAP_COMPLETED): _s6_webhook_hint,
    (State.S6_DELIVERED_OPEN, Event.WH_AUTH_VOIDED): _s6_webhook_hint,
    (State.S6_DELIVERED_OPEN, Event.WH_CAP_DENIED): _s6_webhook_denied,
    (State.S6_DELIVERED_OPEN, Event.WH_AUTH_CREATED): _s6_webhook_hint,
    (State.S6_DELIVERED_OPEN, Event.T_DEADLINE): _s6_deadline,

    # -- S7 (DELIVERED_PENDING) --
    (State.S7_DELIVERED_PENDING, Event.R_CAP_COMPLETED): _s7_r_cap_completed,
    # Meta-review: explicit unaudited path — PayPal COMPLETED with no id.
    (State.S7_DELIVERED_PENDING, Event.E_UNAUDITED_CAPTURE): _s7_unaudited_capture,
    # Reconciled unaudited: _capture_with_reconcile may clear the intent
    # before the bridge dispatches, leaving S6. Same handler applies.
    (State.S6_DELIVERED_OPEN, Event.E_UNAUDITED_CAPTURE): _s7_unaudited_capture,
    (State.S7_DELIVERED_PENDING, Event.R_CAP_DECLINED): _s7_r_cap_declined,
    (State.S7_DELIVERED_PENDING, Event.R_CAP_500): _s7_r_cap_500,
    (State.S7_DELIVERED_PENDING, Event.R_PARTIAL_CAP_COMPLETED): _s7_r_partial_cap_completed,
    (State.S7_DELIVERED_PENDING, Event.E_CANCEL_PENDING): _s7_cancel_pending,
    (State.S7_DELIVERED_PENDING, Event.E_RECONCILE): _s7_reconcile,
    (State.S7_DELIVERED_PENDING, Event.WH_CAP_COMPLETED): _s7_webhook_hint,
    (State.S7_DELIVERED_PENDING, Event.WH_AUTH_VOIDED): _s7_webhook_hint,
    (State.S7_DELIVERED_PENDING, Event.WH_CAP_DENIED): _s7_webhook_denied,
    (State.S7_DELIVERED_PENDING, Event.WH_AUTH_CREATED): _s7_webhook_auth_created,

    # -- S8 (DELIVERED_PARTIAL_RECORDED) --
    (State.S8_DELIVERED_PARTIAL_RECORDED, Event.E_START_RESIDUAL_VOID): _s8_start_residual_void,
    (State.S8_DELIVERED_PARTIAL_RECORDED, Event.R_VOID_OK): _s8_r_void_ok,
    (State.S8_DELIVERED_PARTIAL_RECORDED, Event.R_VOID_FAIL): _s8_r_void_fail,
    (State.S8_DELIVERED_PARTIAL_RECORDED, Event.R_VOID_422): _s8_r_void_422,
    (State.S8_DELIVERED_PARTIAL_RECORDED, Event.R_VOID_500): _s8_r_void_fail,
    (State.S8_DELIVERED_PARTIAL_RECORDED, Event.E_RECONCILE): _s8_reconcile,
    (State.S8_DELIVERED_PARTIAL_RECORDED, Event.E_FORCE_SETTLE): _s8_force_settle,
    (State.S8_DELIVERED_PARTIAL_RECORDED, Event.E_VOID_DEADLINE): _s8_void_deadline,
    (State.S8_DELIVERED_PARTIAL_RECORDED, Event.WH_CAP_COMPLETED): _s8_webhook_hint,
    (State.S8_DELIVERED_PARTIAL_RECORDED, Event.WH_AUTH_VOIDED): _s8_webhook_hint,
    (State.S8_DELIVERED_PARTIAL_RECORDED, Event.WH_CAP_DENIED): _s8_webhook_other,
    (State.S8_DELIVERED_PARTIAL_RECORDED, Event.WH_AUTH_CREATED): _s8_webhook_other,

    # -- S9 (DELIVERED_WEDGED) --
    (State.S9_DELIVERED_WEDGED, Event.E_FORCE_SETTLE): _s9_force_settle,
    (State.S9_DELIVERED_WEDGED, Event.R_VOID_OK): _s9_r_void_ok,
    (State.S9_DELIVERED_WEDGED, Event.R_VOID_FAIL): _s9_r_void_fail,
    (State.S9_DELIVERED_WEDGED, Event.R_VOID_422): _s9_r_void_422,
    (State.S9_DELIVERED_WEDGED, Event.R_VOID_500): _s9_r_void_fail,
    (State.S9_DELIVERED_WEDGED, Event.E_RECONCILE): _s9_reconcile,
    (State.S9_DELIVERED_WEDGED, Event.E_CANCEL_PENDING): _s9_cancel_pending,
    (State.S9_DELIVERED_WEDGED, Event.WH_AUTH_VOIDED): _s9_webhook,
    (State.S9_DELIVERED_WEDGED, Event.WH_CAP_COMPLETED): _s9_webhook,
    (State.S9_DELIVERED_WEDGED, Event.WH_CAP_DENIED): _s9_webhook,
    (State.S9_DELIVERED_WEDGED, Event.WH_AUTH_CREATED): _s9_webhook,

    # -- S10a / S10b / S10c / S11 (terminal — I2) --
    (State.S10a_CAPTURED_FULL, Event.E_RECONCILE): _t10a_reconcile,
    (State.S10b_CAPTURED_SPLIT, Event.E_RECONCILE): _t10b_reconcile,
    (State.S10c_CAPTURED_UNAUDITED, Event.E_RECONCILE): _t10c_reconcile,
    (State.S11_VOIDED_SETTLED, Event.E_RECONCILE): _t11_reconcile,

    # -- Terminal client mutations → 409 (not 500) --
    # Claude review (LOW-MEDIUM): a client calling a mutating event on a
    # settled job is a caller error, not a matrix defect.
    (State.S10a_CAPTURED_FULL, Event.E_START_CAPTURE): _terminal_client_call,
    (State.S10a_CAPTURED_FULL, Event.E_FORCE_SETTLE): _terminal_client_call,
    (State.S10a_CAPTURED_FULL, Event.E_SUBMIT_DELIVERY): _terminal_client_call,
    (State.S10a_CAPTURED_FULL, Event.E_START_AUTHORIZE): _terminal_client_call,
    (State.S10b_CAPTURED_SPLIT, Event.E_START_CAPTURE): _terminal_client_call,
    (State.S10b_CAPTURED_SPLIT, Event.E_FORCE_SETTLE): _terminal_client_call,
    (State.S10b_CAPTURED_SPLIT, Event.E_SUBMIT_DELIVERY): _terminal_client_call,
    (State.S10b_CAPTURED_SPLIT, Event.E_START_AUTHORIZE): _terminal_client_call,
    (State.S10c_CAPTURED_UNAUDITED, Event.E_START_CAPTURE): _terminal_client_call,
    (State.S10c_CAPTURED_UNAUDITED, Event.E_FORCE_SETTLE): _terminal_client_call,
    (State.S10c_CAPTURED_UNAUDITED, Event.E_SUBMIT_DELIVERY): _terminal_client_call,
    (State.S10c_CAPTURED_UNAUDITED, Event.E_START_AUTHORIZE): _terminal_client_call,
    (State.S11_VOIDED_SETTLED, Event.E_START_CAPTURE): _terminal_client_call,
    (State.S11_VOIDED_SETTLED, Event.E_FORCE_SETTLE): _terminal_client_call,
    (State.S11_VOIDED_SETTLED, Event.E_SUBMIT_DELIVERY): _terminal_client_call,
    (State.S11_VOIDED_SETTLED, Event.E_START_AUTHORIZE): _terminal_client_call,

    # -- Late timers → NoOp self-loop (not 500) --
    # Claude review (LOW-MEDIUM): timers that fire after the job has
    # progressed past the state where they're meaningful get an explicit
    # NoOp instead of UnhandledTransition. The caller should cancel
    # timers on progress; these cells are the backstop.
    # T_QUOTE_EXPIRY is meaningful in S1/S2/S3 only.
    (State.S4_AUTHORIZED_LIVE, Event.T_QUOTE_EXPIRY): _make_late_timer(State.S4_AUTHORIZED_LIVE),
    (State.S5_AUTHORIZED_WEDGED, Event.T_QUOTE_EXPIRY): _make_late_timer(State.S5_AUTHORIZED_WEDGED),
    (State.S6_DELIVERED_OPEN, Event.T_QUOTE_EXPIRY): _make_late_timer(State.S6_DELIVERED_OPEN),
    (State.S7_DELIVERED_PENDING, Event.T_QUOTE_EXPIRY): _make_late_timer(State.S7_DELIVERED_PENDING),
    (State.S8_DELIVERED_PARTIAL_RECORDED, Event.T_QUOTE_EXPIRY): _make_late_timer(State.S8_DELIVERED_PARTIAL_RECORDED),
    (State.S9_DELIVERED_WEDGED, Event.T_QUOTE_EXPIRY): _make_late_timer(State.S9_DELIVERED_WEDGED),
    (State.S10a_CAPTURED_FULL, Event.T_QUOTE_EXPIRY): _make_late_timer(State.S10a_CAPTURED_FULL),
    (State.S10b_CAPTURED_SPLIT, Event.T_QUOTE_EXPIRY): _make_late_timer(State.S10b_CAPTURED_SPLIT),
    (State.S10c_CAPTURED_UNAUDITED, Event.T_QUOTE_EXPIRY): _make_late_timer(State.S10c_CAPTURED_UNAUDITED),
    (State.S11_VOIDED_SETTLED, Event.T_QUOTE_EXPIRY): _make_late_timer(State.S11_VOIDED_SETTLED),
    # T_DEADLINE is meaningful in S4/S6 only (S6 already has a cell).
    (State.S1_QUOTED_FRESH, Event.T_DEADLINE): _make_late_timer(State.S1_QUOTED_FRESH),
    (State.S2_QUOTED_ORDERED, Event.T_DEADLINE): _make_late_timer(State.S2_QUOTED_ORDERED),
    (State.S3_QUOTED_ATTEMPTED, Event.T_DEADLINE): _make_late_timer(State.S3_QUOTED_ATTEMPTED),
    (State.S5_AUTHORIZED_WEDGED, Event.T_DEADLINE): _make_late_timer(State.S5_AUTHORIZED_WEDGED),
    (State.S7_DELIVERED_PENDING, Event.T_DEADLINE): _make_late_timer(State.S7_DELIVERED_PENDING),
    (State.S8_DELIVERED_PARTIAL_RECORDED, Event.T_DEADLINE): _make_late_timer(State.S8_DELIVERED_PARTIAL_RECORDED),
    (State.S9_DELIVERED_WEDGED, Event.T_DEADLINE): _make_late_timer(State.S9_DELIVERED_WEDGED),
    (State.S10a_CAPTURED_FULL, Event.T_DEADLINE): _make_late_timer(State.S10a_CAPTURED_FULL),
    (State.S10b_CAPTURED_SPLIT, Event.T_DEADLINE): _make_late_timer(State.S10b_CAPTURED_SPLIT),
    (State.S10c_CAPTURED_UNAUDITED, Event.T_DEADLINE): _make_late_timer(State.S10c_CAPTURED_UNAUDITED),
    (State.S11_VOIDED_SETTLED, Event.T_DEADLINE): _make_late_timer(State.S11_VOIDED_SETTLED),
}

# Keep the old name as an alias for backward compatibility with any
# Phase 2 test that references _SKELETON_CELLS directly.
_SKELETON_CELLS = _TABLE


def transition(state: State, event: Event,
              evidence: Evidence | None = None) -> tuple[State, list]:
    """Single dispatch point for ALL escrow state changes (§7).

    - Takes the current discrete state (§3.2, via resolve_state) + event (§4)
      + optional evidence (read results, enum outcomes, guard flags).
    - Returns (new_state, actions_to_execute).
    - Raises UnhandledTransition(state, event) for any pair not in the
      table — loud, logged, test-visible. NEVER silently falls through (I5).
    - Raises GuardRejection for domain guard violations (e.g. non-payer
      capture) — typed client error, HTTP 4xx. DISTINCT from
      UnhandledTransition (HTTP 500).
    - NEVER performs network I/O (§1.4). Actions are value objects; the
      caller persists intents, issues PayPal HTTP calls, and executes the
      returned actions. The dispatcher decides WHAT; the caller executes HOW.

    Phase 3: the full §5 table (all 13 states × events) is populated in
    _TABLE. Compound cells (e.g. E_RECONCILE + READ_PARTIAL + ENUM_FOUND)
    are expressed via the evidence parameter — the caller attaches the
    PayPal read result and enum outcome BEFORE dispatching.

    NOTE (edge case #2): the S3 T_QUOTE_EXPIRY cell documents — if the
    expiry read discovers a live hold, transition to S4_AUTHORIZED_LIVE.
    The hold is real; quote expiry does not invalidate it. T_DEADLINE →
    E_VOID_DEADLINE handles cleanup.

    Args:
      state: the resolved discrete state (from resolve_state).
      event: the event being dispatched (Phase 1 intent, Phase 2
        response, read evidence, enum outcome, dispatchable webhook,
        or timer).
      evidence: optional Evidence carrying read results, enum outcomes,
        guard flags (is_payer, deadline_passed, delivery_verified),
        and caller context (job_id, attempt, order_id) for intent keys.

    Returns:
      (new_state, actions): the post-transition state and the ordered
      list of Action value objects for the caller to execute.

    Raises:
      UnhandledTransition: no table cell for (state, event).
      GuardRejection: domain guard violation, or the cell was invoked
        without the evidence it requires (missing read, amount, job id).
    """
    handler = _TABLE.get((state, event))
    if handler is None:
        raise UnhandledTransition(state, event)
    return handler(_ev(evidence))



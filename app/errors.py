"""Phase 5: centralized PayPal error taxonomy.

Every PayPalError is classified into exactly one class. The classification
drives what the service does next — it is not documentation, it is control
flow.

The three classes (deep dive §3 — "a PSP timeout is neither success nor
failure"):

    RETRYABLE   The request definitely did NOT execute server-side, or the
                server explicitly rejected it without acting. Safe to retry
                with the SAME idempotency key. Examples: transport failure
                before the request left, HTTP 429 (rate limited — the server
                said "not now", not "I did it"), HTTP 408 when the request
                was never sent.

    DEFINITIVE  PayPal answered with finality. Retrying will get the same
                answer. Do NOT retry — transition the job accordingly.
                Examples: 404 (order gone), 422 (void too late), 401/403
                (auth — the answer won't change), PayPalConfigError
                (nothing was sent AND nothing will be until config is fixed).

    AMBIGUOUS   The request may or may not have executed. The response was
                lost after the request was (possibly) sent. NEVER blind-retry
                — always verify-then-act: re-query PayPal with the ORIGINAL
                idempotency key first, then decide (record / retry-same-key /
                wedge-with-alert). Examples: transport failure after the
                request was sent, 5xx after send, 408 after send.

The critical parameter is `request_was_sent`: did the request leave our
process? A timeout BEFORE the request left is RETRYABLE. A timeout AFTER
it was sent is AMBIGUOUS. Callers must know which side of the send they
were on — this forces the Pre/RPC/Post-RPC discipline from Airbnb's
Orpheus (deep dive §1: "no network calls in the DB phases, no DB work
in the network phase").

Mapping to the legacy helpers in app/escrow.py:
    _is_transient_paypal_error(e) == True
        ⟺ classify(e, request_was_sent=False) is RETRYABLE
        (the legacy helper assumes pre-send; the taxonomy makes the
        assumption explicit and handles the post-send case)
    _is_order_absent(e) == True
        ⟹ classify(e, request_was_sent=*) is DEFINITIVE
        (404/RESOURCE_NOT_FOUND is final regardless of send state)
"""

from __future__ import annotations

from enum import Enum

from app.paypal_client import PayPalError, PayPalConfigError


class ErrorClass(Enum):
    """The three-way classification for every PayPal failure."""
    RETRYABLE = "retryable"
    DEFINITIVE = "definitive"
    AMBIGUOUS = "ambiguous"


# HTTP statuses that are always definitive: PayPal answered with
# finality, and the answer will not change on retry.
_DEFINITIVE_STATUSES = frozenset({400, 401, 403, 404, 422})

# HTTP statuses that are always retryable: the server explicitly
# declined to act (429) or never saw a well-formed request.
# Note: 429 is retryable EVEN IF the request was sent — "rate limited"
# means "I did not process this", not "I might have processed this".
_ALWAYS_RETRYABLE_STATUSES = frozenset({429})


def classify_paypal_error(error: PayPalError, *,
                          request_was_sent: bool) -> ErrorClass:
    """Classify a PayPal failure.

    Args:
        error: the PayPalError raised.
        request_was_sent: True if the request left our process (we are
            in or past the RPC phase). False if the failure happened
            before the request was sent (Pre-RPC, or a local error).

    The `request_was_sent` distinction is the whole point: it is what
    turns "transient" into either "safe to retry" or "must verify
    first". When in doubt, pass True — AMBIGUOUS fails closed
    (verify-then-act), RETRYABLE fails open (blind retry).
    """
    # Config errors never left the process AND will never succeed
    # until fixed — definitive, not retryable.
    if isinstance(error, PayPalConfigError):
        return ErrorClass.DEFINITIVE

    status = error.status

    # Definitive statuses: PayPal answered with finality.
    if status in _DEFINITIVE_STATUSES:
        return ErrorClass.DEFINITIVE

    # 429: the server said "not now" — it did not act.
    if status in _ALWAYS_RETRYABLE_STATUSES:
        return ErrorClass.RETRYABLE

    # Transport failure (status None): the connection died.
    # Before send: nothing left, safe to retry.
    # After send: the request may have executed — ambiguous.
    if status is None:
        return (ErrorClass.AMBIGUOUS if request_was_sent
                else ErrorClass.RETRYABLE)

    # 408 Request Timeout: same logic as transport — before send it's
    # a clean retry; after send the server may have acted.
    if status == 408:
        return (ErrorClass.AMBIGUOUS if request_was_sent
                else ErrorClass.RETRYABLE)

    # 5xx Server Error: the server failed. Before send, it never saw
    # the request. After send, it may have acted before failing.
    if status >= 500:
        return (ErrorClass.AMBIGUOUS if request_was_sent
                else ErrorClass.RETRYABLE)

    # Any other 4xx not in the definitive set: PayPal answered, and
    # the answer is final for this request shape. Definitive.
    # (This is the fail-closed default for unknown 4xx.)
    if status is not None and 400 <= status < 500:
        return ErrorClass.DEFINITIVE

    # Unreachable with the current PayPalError shapes, but fail closed:
    # unknown means verify first.
    return ErrorClass.AMBIGUOUS


def is_retryable(error: PayPalError, *, request_was_sent: bool) -> bool:
    """Convenience: True iff classify() is RETRYABLE."""
    return classify_paypal_error(
        error, request_was_sent=request_was_sent) is ErrorClass.RETRYABLE


def is_definitive(error: PayPalError, *, request_was_sent: bool) -> bool:
    """Convenience: True iff classify() is DEFINITIVE."""
    return classify_paypal_error(
        error, request_was_sent=request_was_sent) is ErrorClass.DEFINITIVE


def is_ambiguous(error: PayPalError, *, request_was_sent: bool) -> bool:
    """Convenience: True iff classify() is AMBIGUOUS."""
    return classify_paypal_error(
        error, request_was_sent=request_was_sent) is ErrorClass.AMBIGUOUS

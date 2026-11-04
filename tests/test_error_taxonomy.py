"""Phase 5: error taxonomy tests.

Proves every PayPalError maps to exactly one ErrorClass, and that the
request_was_sent distinction is what flips RETRYABLE <-> AMBIGUOUS.
The taxonomy is control flow, not documentation — these tests lock in
the classification contract.
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.errors import (ErrorClass, classify_paypal_error, is_ambiguous,
                        is_definitive, is_retryable)
from app.paypal_client import PayPalError, PayPalConfigError


def err(status, body=None):
    return PayPalError("test error", status=status, body=body)


# ---------------------------------------------------------------------------
# Exhaustive classification matrix.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,was_sent,expected", [
    # 429: always retryable — "not now" means "did not act".
    (429, True, ErrorClass.RETRYABLE),
    (429, False, ErrorClass.RETRYABLE),
    # Transport failure: depends entirely on send state.
    (None, True, ErrorClass.AMBIGUOUS),
    (None, False, ErrorClass.RETRYABLE),
    # 408: same as transport.
    (408, True, ErrorClass.AMBIGUOUS),
    (408, False, ErrorClass.RETRYABLE),
    # 5xx: same as transport.
    (500, True, ErrorClass.AMBIGUOUS),
    (500, False, ErrorClass.RETRYABLE),
    (502, True, ErrorClass.AMBIGUOUS),
    (503, False, ErrorClass.RETRYABLE),
    # Definitive 4xx: final regardless of send state.
    (400, True, ErrorClass.DEFINITIVE),
    (400, False, ErrorClass.DEFINITIVE),
    (401, True, ErrorClass.DEFINITIVE),
    (403, False, ErrorClass.DEFINITIVE),
    (404, True, ErrorClass.DEFINITIVE),
    (404, False, ErrorClass.DEFINITIVE),
    (422, True, ErrorClass.DEFINITIVE),
    (422, False, ErrorClass.DEFINITIVE),
    # Unknown 4xx: fail closed (definitive).
    (418, True, ErrorClass.DEFINITIVE),
    (418, False, ErrorClass.DEFINITIVE),
])
def test_classification_matrix(status, was_sent, expected):
    assert classify_paypal_error(
        err(status), request_was_sent=was_sent) is expected


def test_config_error_is_definitive():
    # Nothing was sent AND nothing will be until config is fixed —
    # retrying is pointless. Definitive, not retryable.
    e = PayPalConfigError("credentials missing")
    assert classify_paypal_error(e, request_was_sent=False) is ErrorClass.DEFINITIVE
    # Even if somehow "sent", config errors are local.
    assert classify_paypal_error(e, request_was_sent=True) is ErrorClass.DEFINITIVE


def test_request_was_sent_is_the_flip():
    # The same transport failure flips class on send state alone.
    # This is the Pre/RPC/Post-RPC discipline made testable.
    e = PayPalError("connection reset", status=None)
    assert is_retryable(e, request_was_sent=False)
    assert is_ambiguous(e, request_was_sent=True)
    assert not is_definitive(e, request_was_sent=True)

    e500 = PayPalError("internal error", status=500)
    assert is_retryable(e500, request_was_sent=False)
    assert is_ambiguous(e500, request_was_sent=True)


def test_429_never_ambiguous():
    # 429 means "I did not process this" — safe to retry even after send.
    # This is the one status where send state does NOT matter.
    e = PayPalError("rate limited", status=429,
                    headers={"Retry-After": "2"})
    assert is_retryable(e, request_was_sent=True)
    assert is_retryable(e, request_was_sent=False)
    assert e.retry_after_seconds == 2.0


def test_convenience_helpers_agree():
    e = err(503)
    cls = classify_paypal_error(e, request_was_sent=True)
    assert cls is ErrorClass.AMBIGUOUS
    assert is_ambiguous(e, request_was_sent=True)
    assert not is_retryable(e, request_was_sent=True)
    assert not is_definitive(e, request_was_sent=True)


def test_every_class_has_a_member():
    # The taxonomy is exactly three classes — no fourth hiding.
    assert len(ErrorClass) == 3
    assert {c.value for c in ErrorClass} == {
        "retryable", "definitive", "ambiguous"}


# ---------------------------------------------------------------------------
# Exhaustiveness: every error the test doubles raise must classify.
# ---------------------------------------------------------------------------

def _collect_fake_errors():
    """Gather representative PayPalErrors from the fake's shapes."""
    return [
        PayPalError("order gone", status=404),
        PayPalError("auth gone", status=404),
        PayPalError("conflict", status=422,
                    body={"name": "INVALID_REQUEST"}),
        PayPalError("transport", status=None),
        PayPalError("server", status=500),
        PayPalError("timeout", status=408),
        PayPalError("limited", status=429),
        PayPalError("forbidden", status=403),
        PayPalError("bad", status=400),
        PayPalConfigError("no creds"),
    ]


@pytest.mark.parametrize("was_sent", [True, False])
def test_no_unclassified_errors(was_sent):
    # Every error shape classifies without raising — there is no
    # fourth outcome, no None, no exception from the classifier.
    for e in _collect_fake_errors():
        cls = classify_paypal_error(e, request_was_sent=was_sent)
        assert isinstance(cls, ErrorClass), f"{e!r} unclassified"


def test_legacy_transient_maps_to_retryable_presend():
    # _is_transient_paypal_error assumed pre-send. The taxonomy makes
    # the assumption explicit: transient + not-sent == RETRYABLE.
    from app.escrow import _is_transient_paypal_error
    for status in (None, 408, 429, 500, 502, 503):
        e = err(status)
        if _is_transient_paypal_error(e):
            assert is_retryable(e, request_was_sent=False), status


def test_legacy_order_absent_maps_to_definitive():
    # _is_order_absent (404/RESOURCE_NOT_FOUND) is DEFINITIVE in both
    # send states — finality doesn't depend on send.
    from app.escrow import _is_order_absent
    for e in (err(404),
              PayPalError("x", status=400,
                          body={"name": "RESOURCE_NOT_FOUND"}),
              PayPalError("x", status=400, body="RESOURCE_NOT_FOUND")):
        assert _is_order_absent(e)
        assert is_definitive(e, request_was_sent=True)
        assert is_definitive(e, request_was_sent=False)

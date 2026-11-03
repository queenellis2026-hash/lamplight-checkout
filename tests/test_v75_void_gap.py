"""Tests for the P0 19-case void gap fix (v7.5).

When PayPal reports a hold as released (VOIDED/EXPIRED/DENIED) but a
capture is actually listed, the dispatcher must NOT settle as VOIDED.
It must record the capture and settle as CAPTURED.

This is the sweeper's crash-recovery path: if a worker crashes after
PayPal captures funds and PayPal cancels the remaining hold, settling
as VOIDED would lose real customer money.
"""
import pytest

from app.dispatcher import (
    Event,
    Evidence,
    State,
    resolve_state,
    transition,
)


def _ev(**kwargs):
    """Build Evidence with sensible defaults for reconcile tests."""
    defaults = dict(
        job_id="job-1",
        authorization_id="AUTH-1",
        read=Event.READ_RELEASED,
        authorized_cents=1250,
    )
    defaults.update(kwargs)
    return Evidence(**defaults)


class TestVoidGapP0:
    """The dispatcher must check for captures before declaring void."""

    def test_s4_released_with_capture_goes_captured_not_voided(self):
        # S4 (AUTHORIZED_LIVE), hold released, but a capture is listed.
        # Must go to S10a_CAPTURED_FULL, NOT S11_VOIDED_SETTLED.
        ev = _ev(
            enum=Event.ENUM_FOUND,
            has_capture=True,
            capture_ids=("CAP-1",),
            amount_cents=1250,
        )
        new_state, actions = transition(
            State.S4_AUTHORIZED_LIVE, Event.E_RECONCILE, ev)
        assert new_state == State.S10a_CAPTURED_FULL, (
            f"released hold with capture must settle CAPTURED, got {new_state}")
        # Verify a RecordCapture action was emitted
        action_names = [a.__class__.__name__ for a in actions]
        assert "RecordCapture" in action_names

    def test_s6_released_with_capture_goes_captured_not_voided(self):
        # S6 (DELIVERED_OPEN), hold released, capture listed.
        ev = _ev(
            enum=Event.ENUM_FOUND,
            has_capture=True,
            capture_ids=("CAP-1",),
            amount_cents=1250,
        )
        new_state, actions = transition(
            State.S6_DELIVERED_OPEN, Event.E_RECONCILE, ev)
        assert new_state == State.S10a_CAPTURED_FULL, (
            f"released hold with capture must settle CAPTURED, got {new_state}")

    def test_s7_released_with_capture_goes_captured_not_voided(self):
        # S7 (DELIVERED_PENDING), hold released, capture listed.
        ev = _ev(
            enum=Event.ENUM_FOUND,
            has_capture=True,
            capture_ids=("CAP-1",),
            amount_cents=1250,
        )
        new_state, actions = transition(
            State.S7_DELIVERED_PENDING, Event.E_RECONCILE, ev)
        assert new_state == State.S10a_CAPTURED_FULL, (
            f"released hold with capture must settle CAPTURED, got {new_state}")

    def test_s7_released_with_partial_capture_goes_unaudited(self):
        # Partial capture on a released hold → S10c (unaudited), not void.
        ev = _ev(
            enum=Event.ENUM_FOUND,
            has_capture=True,
            is_partial=True,
            capture_ids=("CAP-1",),
            amount_cents=500,
        )
        new_state, actions = transition(
            State.S7_DELIVERED_PENDING, Event.E_RECONCILE, ev)
        assert new_state == State.S10c_CAPTURED_UNAUDITED, (
            f"released hold with partial capture must go unaudited, got {new_state}")

    def test_s7_released_with_split_capture_goes_split(self):
        # Partial capture + voided amount → S10b (split).
        ev = _ev(
            enum=Event.ENUM_FOUND,
            has_capture=True,
            is_partial=True,
            capture_ids=("CAP-1",),
            amount_cents=500,
            voided_amount_cents=750,
        )
        new_state, actions = transition(
            State.S7_DELIVERED_PENDING, Event.E_RECONCILE, ev)
        assert new_state == State.S10b_CAPTURED_SPLIT, (
            f"released hold with partial + voided must go split, got {new_state}")

    def test_released_with_no_capture_still_voids(self):
        # No capture listed (ENUM_NONE) → clean void to S11.
        # This is the normal case; the fix must not break it.
        for state, expected_wedge in [
            (State.S4_AUTHORIZED_LIVE, State.S5_AUTHORIZED_WEDGED),
            (State.S6_DELIVERED_OPEN, State.S9_DELIVERED_WEDGED),
            (State.S7_DELIVERED_PENDING, State.S9_DELIVERED_WEDGED),
        ]:
            ev = _ev(enum=Event.ENUM_NONE, has_capture=False)
            new_state, actions = transition(state, Event.E_RECONCILE, ev)
            assert new_state == State.S11_VOIDED_SETTLED, (
                f"{state} with no capture must void, got {new_state}")

    def test_released_with_ambiguous_capture_wedges(self):
        # Ambiguous capture on released hold → wedge for operator review.
        ev = _ev(
            enum=Event.ENUM_AMBIGUOUS,
            has_capture=False,  # ambiguous = uncertain
        )
        new_state, actions = transition(
            State.S7_DELIVERED_PENDING, Event.E_RECONCILE, ev)
        assert new_state == State.S9_DELIVERED_WEDGED, (
            f"ambiguous capture on released hold must wedge, got {new_state}")
        action_names = [a.__class__.__name__ for a in actions]
        assert "SetReviewFlag" in action_names

    def test_released_with_unread_order_and_no_capture_voids_clean(self):
        # v8 money-safety fix: UNREAD/ORDER_UNREAD wedge only on a real
        # capture risk. With no capture on the books and none listed,
        # there is nothing to lose — clean void, not a wedge.
        ev = _ev(
            enum=Event.ENUM_UNREAD,
            has_capture=False,
        )
        new_state, actions = transition(
            State.S7_DELIVERED_PENDING, Event.E_RECONCILE, ev)
        assert new_state == State.S11_VOIDED_SETTLED, (
            f"unread order with no capture risk must void clean, "
            f"got {new_state}")
        action_names = [a.__class__.__name__ for a in actions]
        assert "RecordVoid" in action_names, (
            "clean void must book the void (one ledger)")

    def test_released_with_unread_order_and_capture_wedges(self):
        # Order unreadable AND auth listed a capture → wedge.
        ev = _ev(
            enum=Event.ENUM_UNREAD,
            has_capture=True,
            capture_ids=("CAP-1",),
            amount_cents=1250,
        )
        new_state, actions = transition(
            State.S7_DELIVERED_PENDING, Event.E_RECONCILE, ev)
        assert new_state == State.S9_DELIVERED_WEDGED, (
            f"unread order with capture must wedge, got {new_state}")

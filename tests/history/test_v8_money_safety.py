"""Regression tests for the v8 money-safety holes (verified Nov 2, 2026).

Each test here FAILS on the unfixed code and PASSES after the fix.
The honest architecture: the dispatcher decides the routine paths;
legacy remains the fallback for genuinely unhandled cases.
"""
import pytest

from app.dispatcher import (
    Event,
    Evidence,
    JobSnapshot,
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


def _reasons(actions):
    return [getattr(a, "reason", "") for a in actions
            if a.__class__.__name__ == "SetReviewFlag"]


# ---------------------------------------------------------------------------
# Fix 1: VOID-ON-UNREADABLE — enum=None on a released hold must WEDGE,
# never void. (escrow.py set enum_res=None on PayPalError and dispatched
# anyway; the FSM fell through to S11.)
# ---------------------------------------------------------------------------

class TestVoidOnUnreadable:
    @pytest.mark.parametrize("state,wedge", [
        (State.S4_AUTHORIZED_LIVE, State.S5_AUTHORIZED_WEDGED),
        (State.S6_DELIVERED_OPEN, State.S9_DELIVERED_WEDGED),
        (State.S7_DELIVERED_PENDING, State.S9_DELIVERED_WEDGED),
    ])
    def test_released_with_enum_none_wedges(self, state, wedge):
        # enum=None = the capture enumeration itself failed (PayPalError).
        # An unreadable order must never void — wedge for the operator.
        ev = _ev(enum=None)
        new_state, actions = transition(state, Event.E_RECONCILE, ev)
        assert new_state == wedge, (
            f"{state}: unreadable order on released hold must wedge, "
            f"got {new_state}")
        reasons = _reasons(actions)
        assert reasons, "wedge must carry a SetReviewFlag"
        assert any("could not be read" in r for r in reasons), (
            f"wedge reason must say the order could not be read, got {reasons}")


# ---------------------------------------------------------------------------
# Fix 2: ENUM_NONE must not void a booked capture.
# ---------------------------------------------------------------------------

class TestEnumNoneWithBookedCapture:
    def test_s4_enum_none_with_full_capture_settles_from_books(self):
        # ENUM_NONE ("proved absent") with a full capture already on the
        # books: the books are the evidence — settle CAPTURED, not VOIDED.
        ev = _ev(
            enum=Event.ENUM_NONE,
            has_capture=True,
            capture_ids=("CAP-1",),
            amount_cents=1250,
        )
        new_state, actions = transition(
            State.S4_AUTHORIZED_LIVE, Event.E_RECONCILE, ev)
        assert new_state == State.S10a_CAPTURED_FULL, (
            f"ENUM_NONE with booked full capture must settle CAPTURED, "
            f"got {new_state}")

    def test_s6_enum_none_with_partial_capture_goes_split(self):
        ev = _ev(
            enum=Event.ENUM_NONE,
            has_capture=True,
            is_partial=True,
            capture_ids=("CAP-1",),
            amount_cents=500,
            voided_amount_cents=750,
        )
        new_state, actions = transition(
            State.S6_DELIVERED_OPEN, Event.E_RECONCILE, ev)
        assert new_state == State.S10b_CAPTURED_SPLIT, (
            f"ENUM_NONE with booked partial capture must go split, "
            f"got {new_state}")

    def test_s7_enum_none_with_empty_books_still_voids(self):
        # Books truly empty AND enum confirms none listed -> clean void.
        ev = _ev(enum=Event.ENUM_NONE, has_capture=False)
        new_state, _ = transition(
            State.S7_DELIVERED_PENDING, Event.E_RECONCILE, ev)
        assert new_state == State.S11_VOIDED_SETTLED


# ---------------------------------------------------------------------------
# Fix 3: UNREAD/ORDER_UNREAD wedge only on real capture risk.
# ---------------------------------------------------------------------------

class TestUnreadWedgeOnlyOnCaptureRisk:
    @pytest.mark.parametrize("enum", [Event.ENUM_UNREAD, Event.ENUM_ORDER_UNREAD])
    @pytest.mark.parametrize("state", [
        State.S4_AUTHORIZED_LIVE,
        State.S6_DELIVERED_OPEN,
        State.S7_DELIVERED_PENDING,
    ])
    def test_unread_with_no_capture_voids_clean(self, enum, state):
        # No capture on the books and none listed: no capture risk, so
        # a clean void is correct — not a wedge.
        ev = _ev(enum=enum, has_capture=False)
        new_state, _ = transition(state, Event.E_RECONCILE, ev)
        assert new_state == State.S11_VOIDED_SETTLED, (
            f"{state} {enum}: no capture risk must void clean, "
            f"got {new_state}")

    @pytest.mark.parametrize("enum", [Event.ENUM_UNREAD, Event.ENUM_ORDER_UNREAD])
    @pytest.mark.parametrize("state,wedge", [
        (State.S4_AUTHORIZED_LIVE, State.S5_AUTHORIZED_WEDGED),
        (State.S6_DELIVERED_OPEN, State.S9_DELIVERED_WEDGED),
        (State.S7_DELIVERED_PENDING, State.S9_DELIVERED_WEDGED),
    ])
    def test_unread_with_capture_wedges(self, enum, state, wedge):
        # Real capture risk (capture on the books) -> wedge.
        ev = _ev(
            enum=enum,
            has_capture=True,
            capture_ids=("CAP-1",),
            amount_cents=1250,
        )
        new_state, actions = transition(state, Event.E_RECONCILE, ev)
        assert new_state == wedge, (
            f"{state} {enum}: capture risk must wedge, got {new_state}")
        assert _reasons(actions), "wedge must carry a SetReviewFlag"


# ---------------------------------------------------------------------------
# Fix 4: one ledger for the released-hold void — S6/S7 book the void
# exactly like S4 (RecordVoid + ClearIntent), not a bare NoOp.
# ---------------------------------------------------------------------------

class TestOneLedgerForVoid:
    @pytest.mark.parametrize("state", [
        State.S4_AUTHORIZED_LIVE,
        State.S6_DELIVERED_OPEN,
        State.S7_DELIVERED_PENDING,
    ])
    def test_clean_void_books_record_void_and_clears_intent(self, state):
        ev = _ev(enum=Event.ENUM_NONE, has_capture=False)
        new_state, actions = transition(state, Event.E_RECONCILE, ev)
        assert new_state == State.S11_VOIDED_SETTLED
        kinds = [a.__class__.__name__ for a in actions]
        assert "RecordVoid" in kinds, (
            f"{state}: void must book RecordVoid, got {kinds}")
        assert "ClearIntent" in kinds, (
            f"{state}: void must clear the intent, got {kinds}")
        voids = [a for a in actions
                 if a.__class__.__name__ == "RecordVoid"]
        assert voids[0].amount_cents == 1250, (
            "void must book the full authorized hold")


# ---------------------------------------------------------------------------
# Fix 5: Round 45 in S6/S7 — PENDING stays open, everywhere.
# ---------------------------------------------------------------------------

class TestRound45Everywhere:
    @pytest.mark.parametrize("state,open_state", [
        (State.S4_AUTHORIZED_LIVE, State.S4_AUTHORIZED_LIVE),
        (State.S6_DELIVERED_OPEN, State.S6_DELIVERED_OPEN),
        (State.S7_DELIVERED_PENDING, State.S7_DELIVERED_PENDING),
        (State.S8_DELIVERED_PARTIAL_RECORDED,
         State.S8_DELIVERED_PARTIAL_RECORDED),
    ])
    def test_ambiguous_pending_stays_open(self, state, open_state):
        ev = _ev(
            read=Event.READ_CAPTURED,
            enum=Event.ENUM_AMBIGUOUS,
        )
        new_state, actions = transition(state, Event.E_RECONCILE, ev)
        assert new_state == open_state, (
            f"{state}: PENDING (ambiguous) must stay open, got {new_state}")
        notes = [getattr(a, "note", "") for a in actions
                 if a.__class__.__name__ == "NoOp"]
        assert any("PENDING" in n for n in notes), (
            f"stay-open must note awaiting COMPLETED, got {notes}")


# ---------------------------------------------------------------------------
# Fix 8: review flag must dominate terminality in resolve_state.
# ---------------------------------------------------------------------------

class TestReviewFlagDominatesTerminality:
    def _snap(self, **kw):
        defaults = dict(
            job_id="job-1",
            status="CAPTURED",
            needs_review=False,
            pending_capture_request_id=None,
            capture_id="CAP-1",
            capture_amount_cents=1250,
            voided_amount_cents=None,
            settlement_method=None,
            authorize_attempted=True,
            order_id="ORD-1",
        )
        defaults.update(kw)
        return JobSnapshot(**defaults)

    def test_flagged_split_resolves_unaudited_not_split(self):
        # Void-amount disagreement flagged the job: even with
        # voided_amount_cents populated, it needs an operator, not S10b.
        snap = self._snap(
            needs_review=True,
            voided_amount_cents=750,
            capture_amount_cents=500,
        )
        assert resolve_state(snap) == State.S10c_CAPTURED_UNAUDITED, (
            "flagged split must resolve S10c (operator), not S10b")

    def test_clean_split_still_resolves_split(self):
        # No flag: a legitimate clean split still resolves S10b.
        snap = self._snap(
            needs_review=False,
            voided_amount_cents=750,
            capture_amount_cents=500,
        )
        assert resolve_state(snap) == State.S10b_CAPTURED_SPLIT

    def test_flagged_void_with_recorded_capture_wedges(self):
        # A review flag on a VOIDED job with a recorded capture must be
        # visible as a wedge — not a silent S11 terminal.
        snap = self._snap(
            status="VOIDED",
            needs_review=True,
        )
        resolved = resolve_state(snap)
        assert resolved in (State.S5_AUTHORIZED_WEDGED,
                            State.S9_DELIVERED_WEDGED), (
            f"flagged VOIDED job must wedge, got {resolved}")

    def test_clean_void_still_terminal(self):
        snap = self._snap(status="VOIDED", needs_review=False)
        assert resolve_state(snap) == State.S11_VOIDED_SETTLED


"""Phase 3 table tests for app/dispatcher.py.

Covers every §5 cell in the Rev 3 transition matrix:
  1. Terminal states (S10a/b/c, S11) — refusals and no-ops only (I2).
  2. QUOTED states (S1, S2, S3) — order creation, authorize intent, read guards.
  3. AUTHORIZED_LIVE (S4) — delivery submission, void deadline, reconcile arms.
  4. DELIVERED states (S6, S7, S8) — capture flows, two-phase.
  5. WEDGED states (S5, S9) — force-settle logic.

Each test dispatches (state, event, evidence) and asserts the exact
(new_state, actions) from the §5 table.
"""

import pytest

from app.dispatcher import (
    ClearIntent,
    ClearReviewFlag,
    DeleteJob,
    EmitReceipt,
    Event,
    Evidence,
    GuardRejection,
    NoOp,
    PostVoid,
    ReadOldOrder,
    RecordCapture,
    RecordVoid,
    SaveIntent,
    SaveResidualVoidIntent,
    SaveVoidIntent,
    SetReviewFlag,
    State,
    UnhandledTransition,
    transition,
)


def ev(**kw):
    """Build Evidence with sensible defaults for table tests."""
    defaults = {"job_id": "job-1", "attempt": 1}
    defaults.update(kw)
    return Evidence(**defaults)


# ---------------------------------------------------------------------------
# 1. Terminal states (I2: strictly absorbing sinks)
# ---------------------------------------------------------------------------

class TestTerminalStates:
    @pytest.mark.parametrize("state", [
        State.S10a_CAPTURED_FULL,
        State.S10b_CAPTURED_SPLIT,
        State.S10c_CAPTURED_UNAUDITED,
        State.S11_VOIDED_SETTLED,
    ])
    def test_reconcile_is_noop(self, state):
        new_state, actions = transition(state, Event.E_RECONCILE)
        assert new_state == state
        assert len(actions) == 1
        assert isinstance(actions[0], NoOp)

    @pytest.mark.parametrize("state", [
        State.S10a_CAPTURED_FULL,
        State.S10b_CAPTURED_SPLIT,
        State.S10c_CAPTURED_UNAUDITED,
        State.S11_VOIDED_SETTLED,
    ])
    def test_client_calls_raise_unhandled(self, state):
        # Terminal states refuse all client operations via GuardRejection
        # (HTTP 409), not UnhandledTransition. A client calling a mutating
        # event on a settled job is a caller error, not a matrix defect.
        # (Claude review, LOW-MEDIUM.)
        for event in (Event.E_START_CAPTURE, Event.E_FORCE_SETTLE,
                      Event.E_SUBMIT_DELIVERY, Event.E_START_AUTHORIZE):
            with pytest.raises(GuardRejection) as exc_info:
                transition(state, event)
            assert exc_info.value.category == "conflict"


# ---------------------------------------------------------------------------
# 2. QUOTED states
# ---------------------------------------------------------------------------

class TestS1QuotedFresh:
    def test_create_order(self):
        s, actions = transition(State.S1_QUOTED_FRESH, Event.E_CREATE_ORDER)
        assert s == State.S2_QUOTED_ORDERED
        assert len(actions) == 1

    def test_reconcile_noop(self):
        s, actions = transition(State.S1_QUOTED_FRESH, Event.E_RECONCILE)
        assert s == State.S1_QUOTED_FRESH

    def test_quote_expiry_deletes(self):
        s, actions = transition(State.S1_QUOTED_FRESH, Event.T_QUOTE_EXPIRY)
        assert any(isinstance(a, DeleteJob) for a in actions)

    def test_webhook_defensive_noop(self):
        # Edge case #1: unreachable per §4.7, explicit NoOp for I5.
        for wh in (Event.WH_AUTH_CREATED, Event.WH_AUTH_VOIDED,
                   Event.WH_CAP_COMPLETED, Event.WH_CAP_DENIED):
            s, actions = transition(State.S1_QUOTED_FRESH, wh)
            assert s == State.S1_QUOTED_FRESH
            assert isinstance(actions[0], NoOp)


class TestS2QuotedOrdered:
    def test_start_authorize_saves_intent(self):
        s, actions = transition(
            State.S2_QUOTED_ORDERED, Event.E_START_AUTHORIZE,
            ev(job_id="j1", attempt=1, amount_cents=2500))
        assert s == State.S3_QUOTED_ATTEMPTED
        assert len(actions) == 1
        assert isinstance(actions[0], SaveIntent)
        assert actions[0].kind == "authorize"
        assert actions[0].key  # non-empty key
        assert actions[0].amount_cents == 2500

    def test_reconcile_noop(self):
        s, _ = transition(State.S2_QUOTED_ORDERED, Event.E_RECONCILE)
        assert s == State.S2_QUOTED_ORDERED

    def test_quote_expiry_deletes(self):
        s, actions = transition(State.S2_QUOTED_ORDERED, Event.T_QUOTE_EXPIRY)
        assert any(isinstance(a, DeleteJob) for a in actions)


class TestS3QuotedAttempted:
    def test_r_auth_created(self):
        s, actions = transition(State.S3_QUOTED_ATTEMPTED,
                                Event.R_AUTH_CREATED)
        assert s == State.S4_AUTHORIZED_LIVE
        assert any(isinstance(a, ClearIntent) for a in actions)

    def test_r_auth_denied_resets_to_s1(self):
        # Rev 2: clean reset (old S4 absorbed).
        s, actions = transition(State.S3_QUOTED_ATTEMPTED,
                                Event.R_AUTH_DENIED)
        assert s == State.S1_QUOTED_FRESH
        assert any(isinstance(a, ClearIntent) for a in actions)

    def test_r_auth_500_stays(self):
        s, _ = transition(State.S3_QUOTED_ATTEMPTED, Event.R_AUTH_500)
        assert s == State.S3_QUOTED_ATTEMPTED

    def test_r_auth_malformed_stays(self):
        s, _ = transition(State.S3_QUOTED_ATTEMPTED, Event.R_AUTH_MALFORMED)
        assert s == State.S3_QUOTED_ATTEMPTED

    def test_create_order_reads_old_first(self):
        # Rev 2 (§6.5): read old order, don't blindly create.
        s, actions = transition(
            State.S3_QUOTED_ATTEMPTED, Event.E_CREATE_ORDER,
            ev(order_id="old-order-123"))
        assert s == State.S3_QUOTED_ATTEMPTED
        assert len(actions) == 1
        assert isinstance(actions[0], ReadOldOrder)
        assert actions[0].order_id == "old-order-123"

    def test_r_old_order_active_adopts(self):
        # Adopt, don't duplicate — prevents double-hold.
        s, _ = transition(State.S3_QUOTED_ATTEMPTED,
                          Event.R_OLD_ORDER_ACTIVE)
        assert s == State.S4_AUTHORIZED_LIVE

    def test_r_old_order_dead_goes_to_s2(self):
        s, _ = transition(State.S3_QUOTED_ATTEMPTED, Event.R_OLD_ORDER_DEAD)
        assert s == State.S2_QUOTED_ORDERED

    def test_reconcile_read_created_stays(self):
        s, _ = transition(State.S3_QUOTED_ATTEMPTED, Event.E_RECONCILE,
                          ev(read=Event.READ_CREATED))
        assert s == State.S3_QUOTED_ATTEMPTED

    def test_reconcile_read_captured_found(self):
        s, actions = transition(
            State.S3_QUOTED_ATTEMPTED, Event.E_RECONCILE,
            ev(read=Event.READ_CAPTURED, enum=Event.ENUM_FOUND,
               capture_ids=("CAP-1",), amount_cents=1000))
        assert s == State.S10a_CAPTURED_FULL
        assert any(isinstance(a, RecordCapture) for a in actions)

    def test_reconcile_read_released(self):
        s, _ = transition(State.S3_QUOTED_ATTEMPTED, Event.E_RECONCILE,
                          ev(read=Event.READ_RELEASED))
        assert s == State.S11_VOIDED_SETTLED


# ---------------------------------------------------------------------------
# 3. AUTHORIZED_LIVE (S4)
# ---------------------------------------------------------------------------

class TestS4AuthorizedLive:
    def test_r_void_422_clears_intent_no_retry(self):
        # Gemini Fix 4: 422 = already voided at PayPal. Must clear the
        # intent and tell the caller to reconcile — not retry forever.
        s, actions = transition(
            State.S4_AUTHORIZED_LIVE, Event.R_VOID_422,
            ev(is_payer=True))
        assert s == State.S4_AUTHORIZED_LIVE
        assert any(isinstance(a, ClearIntent) for a in actions)
        assert not any(isinstance(a, PostVoid) for a in actions)

    def test_submit_delivery(self):
        s, _ = transition(State.S4_AUTHORIZED_LIVE, Event.E_SUBMIT_DELIVERY,
                          ev(deadline_passed=False))
        assert s == State.S6_DELIVERED_OPEN

    def test_submit_delivery_after_deadline_rejected(self):
        with pytest.raises(GuardRejection) as exc_info:
            transition(State.S4_AUTHORIZED_LIVE, Event.E_SUBMIT_DELIVERY,
                       ev(deadline_passed=True))
        assert exc_info.value.category == "gone"

    def test_void_deadline_clean_does_not_settle(self):
        # Two-phase: save the intent and POST. S11 waits for R_VOID_OK.
        s, actions = transition(
            State.S4_AUTHORIZED_LIVE, Event.E_VOID_DEADLINE,
            ev(deadline_passed=True, is_payer=True,
               read=Event.READ_CREATED, enum=Event.ENUM_NONE,
               authorization_id="auth-1"))
        assert s == State.S4_AUTHORIZED_LIVE
        assert any(isinstance(a, SaveVoidIntent) for a in actions)
        voids = [a for a in actions if isinstance(a, PostVoid)]
        assert len(voids) == 1
        assert voids[0].authorization_id == "auth-1"

    def test_void_ok_settles(self):
        s, actions = transition(
            State.S4_AUTHORIZED_LIVE, Event.R_VOID_OK,
            ev(voided_amount_cents=2500, authorization_id="auth-1"))
        assert s == State.S11_VOIDED_SETTLED
        booked = [a for a in actions if isinstance(a, RecordVoid)]
        assert len(booked) == 1
        assert booked[0].amount_cents == 2500

    def test_void_fail_stays_live(self):
        s, _ = transition(State.S4_AUTHORIZED_LIVE, Event.R_VOID_FAIL)
        assert s == State.S4_AUTHORIZED_LIVE

    def test_void_deadline_missing_read_is_client_error(self):
        with pytest.raises(GuardRejection) as exc_info:
            transition(State.S4_AUTHORIZED_LIVE, Event.E_VOID_DEADLINE,
                       ev(deadline_passed=True, is_payer=True))
        assert exc_info.value.category == "validation"
        assert not isinstance(exc_info.value, UnhandledTransition)

    def test_void_deadline_pending_does_not_void(self):
        s, actions = transition(
            State.S4_AUTHORIZED_LIVE, Event.E_VOID_DEADLINE,
            ev(deadline_passed=True, is_payer=True,
               read=Event.READ_PENDING, enum=Event.ENUM_NONE,
               authorization_id="auth-1"))
        assert s == State.S4_AUTHORIZED_LIVE
        assert not any(isinstance(a, PostVoid) for a in actions)

    def test_void_deadline_order_unread_refused(self):
        with pytest.raises(GuardRejection) as exc_info:
            transition(State.S4_AUTHORIZED_LIVE, Event.E_VOID_DEADLINE,
                       ev(deadline_passed=True, is_payer=True,
                          read=Event.READ_CREATED,
                          enum=Event.ENUM_ORDER_UNREAD,
                          authorization_id="auth-1"))
        assert exc_info.value.category == "conflict"

    def test_void_deadline_requires_authorization_id(self):
        with pytest.raises(GuardRejection) as exc_info:
            transition(State.S4_AUTHORIZED_LIVE, Event.E_VOID_DEADLINE,
                       ev(deadline_passed=True, is_payer=True,
                          read=Event.READ_CREATED, enum=Event.ENUM_NONE))
        assert exc_info.value.category == "validation"

    def test_void_deadline_requires_payer(self):
        with pytest.raises(GuardRejection):
            transition(State.S4_AUTHORIZED_LIVE, Event.E_VOID_DEADLINE,
                       ev(deadline_passed=True, is_payer=False,
                          read=Event.READ_CREATED, enum=Event.ENUM_NONE))

    def test_void_deadline_requires_deadline(self):
        # Claude review (HIGH): valid evidence otherwise, so the deadline
        # guard is the one under test (not payer/read/enum).
        with pytest.raises(GuardRejection) as exc_info:
            transition(State.S4_AUTHORIZED_LIVE, Event.E_VOID_DEADLINE,
                       ev(deadline_passed=False, is_payer=True,
                          read=Event.READ_CREATED, enum=Event.ENUM_NONE,
                          authorization_id="auth-1", job_id="j1", attempt=1))
        assert exc_info.value.category == "forbidden"
        assert "deadline" in exc_info.value.reason.lower()

    def test_void_deadline_partial_wedges(self):
        # §3.5: pre-delivery partial is never routine.
        s, actions = transition(
            State.S4_AUTHORIZED_LIVE, Event.E_VOID_DEADLINE,
            ev(deadline_passed=True, is_payer=True,
               read=Event.READ_PARTIAL))
        assert s == State.S5_AUTHORIZED_WEDGED
        assert any(isinstance(a, SetReviewFlag) for a in actions)

    def test_void_deadline_blind_refused(self):
        # "Refusing to void blind" — GuardRejection, not a wedge.
        with pytest.raises(GuardRejection) as exc_info:
            transition(State.S4_AUTHORIZED_LIVE, Event.E_VOID_DEADLINE,
                       ev(deadline_passed=True, is_payer=True,
                          read=Event.READ_500))
        assert exc_info.value.category == "conflict"

    def test_reconcile_hold_live(self):
        s, _ = transition(State.S4_AUTHORIZED_LIVE, Event.E_RECONCILE,
                          ev(read=Event.READ_CREATED))
        assert s == State.S4_AUTHORIZED_LIVE

    def test_reconcile_captured_found(self):
        s, actions = transition(
            State.S4_AUTHORIZED_LIVE, Event.E_RECONCILE,
            ev(read=Event.READ_CAPTURED, enum=Event.ENUM_FOUND,
               capture_ids=("CAP-1",), amount_cents=1000))
        assert s == State.S10a_CAPTURED_FULL

    def test_reconcile_order_unread_no_wedge(self):
        # §6.3 fix: valid capture on books — "will retry", no wedge.
        s, _ = transition(
            State.S4_AUTHORIZED_LIVE, Event.E_RECONCILE,
            ev(read=Event.READ_CAPTURED, enum=Event.ENUM_ORDER_UNREAD,
               capture_ids=("CAP-1",), amount_cents=1000))
        assert s == State.S4_AUTHORIZED_LIVE

    def test_reconcile_ambiguous_stays_open(self):
        # Round 45: PENDING (AMBIGUOUS) does NOT wedge — stays open
        # awaiting COMPLETED.
        s, _ = transition(
            State.S4_AUTHORIZED_LIVE, Event.E_RECONCILE,
            ev(read=Event.READ_CAPTURED, enum=Event.ENUM_AMBIGUOUS))
        assert s == State.S4_AUTHORIZED_LIVE

    def test_deadline_enables_void(self):
        s, _ = transition(State.S4_AUTHORIZED_LIVE, Event.T_DEADLINE)
        assert s == State.S4_AUTHORIZED_LIVE


# ---------------------------------------------------------------------------
# 4. DELIVERED states
# ---------------------------------------------------------------------------

class TestS6DeliveredOpen:
    def test_deadline_after_delivery_is_noop(self):
        # Gemini Fix 2 (CRITICAL): the deadline timer can fire after
        # delivery (S4 → S6). Must not 500 — stays in S6.
        s, actions = transition(
            State.S6_DELIVERED_OPEN, Event.T_DEADLINE, ev())
        assert s == State.S6_DELIVERED_OPEN
        assert len(actions) == 1
        assert isinstance(actions[0], NoOp)

    def test_start_capture(self):
        s, actions = transition(
            State.S6_DELIVERED_OPEN, Event.E_START_CAPTURE,
            ev(is_payer=True, delivery_verified=True,
               amount_cents=1000, job_id="j1", attempt=1))
        assert s == State.S7_DELIVERED_PENDING
        assert len(actions) == 1
        assert isinstance(actions[0], SaveIntent)
        assert actions[0].kind == "capture"

    def test_start_capture_requires_payer(self):
        # Claude review (HIGH): must pass a valid amount_cents so the test
        # exercises the payer guard, not the amount guard.
        with pytest.raises(GuardRejection) as exc_info:
            transition(State.S6_DELIVERED_OPEN, Event.E_START_CAPTURE,
                       ev(is_payer=False, delivery_verified=True,
                          amount_cents=2500, job_id="j1", attempt=1))
        assert exc_info.value.category == "forbidden"
        assert "payer" in exc_info.value.reason.lower()

    def test_start_capture_requires_verified_delivery(self):
        # Claude review (HIGH): valid amount so the delivery guard is
        # the one under test.
        with pytest.raises(GuardRejection) as exc_info:
            transition(State.S6_DELIVERED_OPEN, Event.E_START_CAPTURE,
                       ev(is_payer=True, delivery_verified=False,
                          amount_cents=2500, job_id="j1", attempt=1))
        assert exc_info.value.category == "forbidden"
        assert "delivery" in exc_info.value.reason.lower()

    def test_start_capture_requires_job_id(self):
        with pytest.raises(GuardRejection) as exc_info:
            transition(State.S6_DELIVERED_OPEN, Event.E_START_CAPTURE,
                       ev(job_id="", is_payer=True, delivery_verified=True,
                          amount_cents=1000))
        assert exc_info.value.category == "validation"

    def test_start_capture_partial(self):
        s, actions = transition(
            State.S6_DELIVERED_OPEN, Event.E_START_CAPTURE_PARTIAL,
            ev(is_payer=True, delivery_verified=True,
               amount_cents=500, job_id="j1", attempt=1))
        assert s == State.S7_DELIVERED_PENDING
        assert actions[0].kind == "capture_partial"

    def test_reconcile_captured_found(self):
        s, _ = transition(
            State.S6_DELIVERED_OPEN, Event.E_RECONCILE,
            ev(read=Event.READ_CAPTURED, enum=Event.ENUM_FOUND,
               capture_ids=("CAP-1",), amount_cents=1000))
        assert s == State.S10a_CAPTURED_FULL

    def test_reconcile_order_unread_no_wedge(self):
        s, _ = transition(
            State.S6_DELIVERED_OPEN, Event.E_RECONCILE,
            ev(read=Event.READ_CAPTURED, enum=Event.ENUM_ORDER_UNREAD,
               capture_ids=("CAP-1",)))
        assert s == State.S6_DELIVERED_OPEN

    def test_reconcile_partial_records_capture_before_wedge(self):
        # A partial capture listed on DELIVERED is recorded and the job
        # stays open on the residual (S8), not wedged. Legacy
        # _reconcile_with_auth does the same via _record_listed_capture.
        # (Test name is historical — the "wedge" no longer happens.)
        s, actions = transition(
            State.S6_DELIVERED_OPEN, Event.E_RECONCILE,
            ev(read=Event.READ_PARTIAL, capture_ids=("CAP-1",),
               amount_cents=400, has_capture=True))
        assert s == State.S8_DELIVERED_PARTIAL_RECORDED
        booked = [a for a in actions if isinstance(a, RecordCapture)]
        assert len(booked) == 1
        assert booked[0].capture_ids == ("CAP-1",)
        assert booked[0].amount_cents == 400
        assert not any(isinstance(a, SetReviewFlag) for a in actions)


class TestS7DeliveredPending:
    def test_r_cap_completed_full(self):
        s, actions = transition(
            State.S7_DELIVERED_PENDING, Event.R_CAP_COMPLETED,
            ev(capture_ids=("CAP-1",), amount_cents=1000))
        assert s == State.S10a_CAPTURED_FULL
        assert any(isinstance(a, RecordCapture) for a in actions)
        assert any(isinstance(a, ClearIntent) for a in actions)
        assert any(isinstance(a, EmitReceipt) for a in actions)

    def test_r_cap_declined(self):
        s, actions = transition(State.S7_DELIVERED_PENDING,
                                Event.R_CAP_DECLINED)
        assert s == State.S6_DELIVERED_OPEN
        assert any(isinstance(a, ClearIntent) for a in actions)

    def test_r_cap_500_keeps_intent(self):
        # I4: keep intent for replay with the same key.
        s, _ = transition(State.S7_DELIVERED_PENDING, Event.R_CAP_500)
        assert s == State.S7_DELIVERED_PENDING

    def test_r_partial_cap_completed(self):
        s, actions = transition(
            State.S7_DELIVERED_PENDING, Event.R_PARTIAL_CAP_COMPLETED,
            ev(capture_ids=("CAP-1",), amount_cents=500,
               job_id="j1", attempt=1))
        assert s == State.S8_DELIVERED_PARTIAL_RECORDED
        assert any(isinstance(a, RecordCapture) for a in actions)
        assert any(isinstance(a, ClearIntent) for a in actions)
        assert any(isinstance(a, SaveResidualVoidIntent) for a in actions)

    def test_cancel_pending(self):
        s, actions = transition(
            State.S7_DELIVERED_PENDING, Event.E_CANCEL_PENDING,
            ev(is_payer=True))
        assert s == State.S6_DELIVERED_OPEN
        assert any(isinstance(a, ClearIntent) for a in actions)

    def test_cancel_pending_requires_payer(self):
        with pytest.raises(GuardRejection):
            transition(State.S7_DELIVERED_PENDING, Event.E_CANCEL_PENDING,
                       ev(is_payer=False))

    def test_reconcile_partial_saves_residual_intent(self):
        s, actions = transition(
            State.S7_DELIVERED_PENDING, Event.E_RECONCILE,
            ev(read=Event.READ_PARTIAL, capture_ids=("CAP-1",),
               amount_cents=500, job_id="j1", attempt=1))
        assert s == State.S8_DELIVERED_PARTIAL_RECORDED
        assert any(isinstance(a, RecordCapture) for a in actions)
        assert any(isinstance(a, ClearIntent) for a in actions)
        assert any(isinstance(a, SaveResidualVoidIntent) for a in actions)


class TestS8DeliveredPartialRecorded:
    def test_start_residual_void(self):
        # Rev 3 (Gemini Defect 2): two-phase, no compound events.
        s, actions = transition(
            State.S8_DELIVERED_PARTIAL_RECORDED,
            Event.E_START_RESIDUAL_VOID,
            ev(job_id="j1", attempt=2))
        assert s == State.S8_DELIVERED_PARTIAL_RECORDED
        assert len(actions) == 1
        assert isinstance(actions[0], SaveResidualVoidIntent)

    def test_r_void_ok_settles_split(self):
        s, actions = transition(
            State.S8_DELIVERED_PARTIAL_RECORDED, Event.R_VOID_OK,
            ev(voided_amount_cents=500, authorization_id="auth-1"))
        assert s == State.S10b_CAPTURED_SPLIT
        assert any(isinstance(a, EmitReceipt) for a in actions)
        booked = [a for a in actions if isinstance(a, RecordVoid)]
        assert len(booked) == 1 and booked[0].amount_cents == 500

    def test_r_void_ok_requires_amount(self):
        with pytest.raises(GuardRejection) as exc_info:
            transition(State.S8_DELIVERED_PARTIAL_RECORDED, Event.R_VOID_OK)
        assert exc_info.value.category == "validation"

    def test_reconcile_released_is_not_a_pure_void(self):
        # Default is_partial=False must not drop the booked partial into S11.
        s, actions = transition(
            State.S8_DELIVERED_PARTIAL_RECORDED, Event.E_RECONCILE,
            ev(read=Event.READ_RELEASED))
        assert s == State.S10c_CAPTURED_UNAUDITED
        assert any(isinstance(a, EmitReceipt) for a in actions)

    def test_reconcile_released_confirmed_is_split(self):
        s, actions = transition(
            State.S8_DELIVERED_PARTIAL_RECORDED, Event.E_RECONCILE,
            ev(read=Event.READ_RELEASED, void_confirmed=True,
               voided_amount_cents=500))
        assert s == State.S10b_CAPTURED_SPLIT
        assert any(isinstance(a, RecordVoid) for a in actions)

    def test_reconcile_pending_stays(self):
        s, _ = transition(
            State.S8_DELIVERED_PARTIAL_RECORDED, Event.E_RECONCILE,
            ev(read=Event.READ_PENDING))
        assert s == State.S8_DELIVERED_PARTIAL_RECORDED

    def test_r_void_fail_stays(self):
        s, _ = transition(State.S8_DELIVERED_PARTIAL_RECORDED,
                          Event.R_VOID_FAIL)
        assert s == State.S8_DELIVERED_PARTIAL_RECORDED

    def test_reconcile_partial_wedges_to_s9(self):
        # Rev 2 (Critical Flaw 2): S9, NOT S5. No demotion.
        s, actions = transition(
            State.S8_DELIVERED_PARTIAL_RECORDED, Event.E_RECONCILE,
            ev(read=Event.READ_PARTIAL))
        assert s == State.S9_DELIVERED_WEDGED
        assert any(isinstance(a, SetReviewFlag) for a in actions)

    def test_force_settle_rejected_not_wedged(self):
        with pytest.raises(GuardRejection):
            transition(State.S8_DELIVERED_PARTIAL_RECORDED,
                       Event.E_FORCE_SETTLE, ev(is_payer=True))

    def test_void_deadline_rejected_wrong_state(self):
        with pytest.raises(GuardRejection):
            transition(State.S8_DELIVERED_PARTIAL_RECORDED,
                       Event.E_VOID_DEADLINE, ev(is_payer=True))


# ---------------------------------------------------------------------------
# 5. WEDGED states
# ---------------------------------------------------------------------------

class TestS5AuthorizedWedged:
    def test_force_settle_found(self):
        s, actions = transition(
            State.S5_AUTHORIZED_WEDGED, Event.E_FORCE_SETTLE,
            ev(is_payer=True, readable=True, enum=Event.ENUM_FOUND,
               has_capture=True, capture_ids=("CAP-1",), amount_cents=1000))
        assert s == State.S10a_CAPTURED_FULL
        assert any(isinstance(a, ClearReviewFlag) for a in actions)

    def test_force_settle_found_without_flags_rejected(self):
        # Claude review (MEDIUM): S5 vs S9 asymmetry. ENUM_FOUND with no
        # capture flags is contradictory — refuse (409), don't book S10a
        # with an empty RecordCapture.
        with pytest.raises(GuardRejection) as exc_info:
            transition(
                State.S5_AUTHORIZED_WEDGED, Event.E_FORCE_SETTLE,
                ev(is_payer=True, readable=True, enum=Event.ENUM_FOUND,
                   amount_cents=1000))
        assert exc_info.value.category == "conflict"

    def test_force_settle_none_starts_void(self):
        # Not S11 yet — the POST has not been confirmed.
        s, actions = transition(
            State.S5_AUTHORIZED_WEDGED, Event.E_FORCE_SETTLE,
            ev(is_payer=True, readable=True, enum=Event.ENUM_NONE,
               authorization_id="auth-1"))
        assert s == State.S5_AUTHORIZED_WEDGED
        assert any(isinstance(a, PostVoid) for a in actions)
        assert any(isinstance(a, SaveVoidIntent) for a in actions)

    def test_s5_force_settle_requires_enum(self):
        # Claude review (HIGH): S5's enum-is-None guard needs a regression
        # test too, for parity with S9.
        with pytest.raises(GuardRejection) as exc_info:
            transition(
                State.S5_AUTHORIZED_WEDGED, Event.E_FORCE_SETTLE,
                ev(is_payer=True, readable=True,
                   authorization_id="a"))
        assert exc_info.value.category == "validation"
        assert "enum" in exc_info.value.reason.lower()

    def test_void_ok_leaves_wedge(self):
        s, actions = transition(
            State.S5_AUTHORIZED_WEDGED, Event.R_VOID_OK,
            ev(voided_amount_cents=2500, authorization_id="auth-1"))
        assert s == State.S11_VOIDED_SETTLED
        assert any(isinstance(a, RecordVoid) for a in actions)
        assert any(isinstance(a, ClearReviewFlag) for a in actions)

    def test_force_settle_unread_rejected(self):
        with pytest.raises(GuardRejection) as exc_info:
            transition(State.S5_AUTHORIZED_WEDGED, Event.E_FORCE_SETTLE,
                       ev(is_payer=True, readable=False,
                          enum=Event.ENUM_UNREAD))
        assert exc_info.value.category == "conflict"

    def test_force_settle_ambiguous_rejected(self):
        with pytest.raises(GuardRejection):
            transition(State.S5_AUTHORIZED_WEDGED, Event.E_FORCE_SETTLE,
                       ev(is_payer=True, readable=True,
                          enum=Event.ENUM_AMBIGUOUS))

    def test_force_settle_requires_evidence(self):
        with pytest.raises(GuardRejection):
            transition(State.S5_AUTHORIZED_WEDGED, Event.E_FORCE_SETTLE,
                       ev(is_payer=True))

    def test_force_settle_requires_payer(self):
        with pytest.raises(GuardRejection):
            transition(State.S5_AUTHORIZED_WEDGED, Event.E_FORCE_SETTLE,
                       ev(is_payer=False, readable=True,
                          enum=Event.ENUM_FOUND))

    def test_force_settle_split(self):
        # Partial + void confirmed beats ENUM_FOUND. A full receipt here
        # would drop the residual.
        s, actions = transition(
            State.S5_AUTHORIZED_WEDGED, Event.E_FORCE_SETTLE,
            ev(is_payer=True, readable=True, enum=Event.ENUM_FOUND,
               is_partial=True, void_confirmed=True,
               capture_ids=("CAP-1",), amount_cents=500))
        assert s == State.S10b_CAPTURED_SPLIT
        assert any(isinstance(a, EmitReceipt) and a.kind == "split"
                   for a in actions)

    def test_force_settle_partial_without_void_refused(self):
        with pytest.raises(GuardRejection) as exc_info:
            transition(
                State.S5_AUTHORIZED_WEDGED, Event.E_FORCE_SETTLE,
                ev(is_payer=True, readable=True, enum=Event.ENUM_FOUND,
                   is_partial=True, void_confirmed=False,
                   capture_ids=("CAP-1",), amount_cents=500))
        assert exc_info.value.category == "conflict"

    def test_reconcile_wedge_persists(self):
        s, _ = transition(State.S5_AUTHORIZED_WEDGED, Event.E_RECONCILE)
        assert s == State.S5_AUTHORIZED_WEDGED


class TestS9DeliveredWedged:
    def test_force_settle_full_capture(self):
        s, actions = transition(
            State.S9_DELIVERED_WEDGED, Event.E_FORCE_SETTLE,
            ev(is_payer=True, readable=True, has_capture=True,
               is_partial=False, capture_ids=("CAP-1",),
               amount_cents=1000, enum=Event.ENUM_FOUND))
        assert s == State.S10a_CAPTURED_FULL
        assert any(isinstance(a, ClearReviewFlag) for a in actions)

    def test_force_settle_full_capture_unattributable_goes_unaudited(self):
        # Gemini Fix 5: unattributable funds must not settle on the
        # audited books.
        s, actions = transition(
            State.S9_DELIVERED_WEDGED, Event.E_FORCE_SETTLE,
            ev(is_payer=True, readable=True, has_capture=True,
               is_partial=False, capture_ids=("CAP-1",),
               amount_cents=1000, enum=Event.ENUM_UNATTRIBUTABLE))
        assert s == State.S10c_CAPTURED_UNAUDITED
        assert any(isinstance(a, ClearReviewFlag) for a in actions)

    def test_force_settle_full_capture_wrong_enum_rejected(self):
        # Gemini Fix 5: S10a requires ENUM_FOUND.
        with pytest.raises(GuardRejection):
            transition(
                State.S9_DELIVERED_WEDGED, Event.E_FORCE_SETTLE,
                ev(is_payer=True, readable=True, has_capture=True,
                   is_partial=False, capture_ids=("CAP-1",),
                   amount_cents=1000, enum=Event.ENUM_NONE))

    def test_force_settle_split(self):
        # Rev 2 (Critical Flaw 2): split settlement is a legal wedge exit.
        s, actions = transition(
            State.S9_DELIVERED_WEDGED, Event.E_FORCE_SETTLE,
            ev(is_payer=True, readable=True, has_capture=True,
               enum=Event.ENUM_FOUND,
               is_partial=True, void_confirmed=True,
               capture_ids=("CAP-1",), amount_cents=500))
        assert s == State.S10b_CAPTURED_SPLIT

    def test_force_settle_no_capture_starts_void(self):
        s, actions = transition(
            State.S9_DELIVERED_WEDGED, Event.E_FORCE_SETTLE,
            ev(is_payer=True, readable=True, has_capture=False,
               enum=Event.ENUM_NONE,
               authorization_id="auth-1"))
        assert s == State.S9_DELIVERED_WEDGED
        assert any(isinstance(a, PostVoid) for a in actions)
        assert any(isinstance(a, SaveVoidIntent) for a in actions)

    def test_s9_force_settle_requires_enum(self):
        # Claude review (HIGH): the enum-is-None guard has no regression
        # test. Removing it would allow a blind void; this test kills
        # that mutant.
        with pytest.raises(GuardRejection) as exc_info:
            transition(
                State.S9_DELIVERED_WEDGED, Event.E_FORCE_SETTLE,
                ev(is_payer=True, readable=True, has_capture=False,
                   authorization_id="a"))
        assert exc_info.value.category == "validation"
        assert "enum" in exc_info.value.reason.lower()

    def test_void_ok_leaves_wedge(self):
        s, actions = transition(
            State.S9_DELIVERED_WEDGED, Event.R_VOID_OK,
            ev(voided_amount_cents=2500, authorization_id="auth-1"))
        assert s == State.S11_VOIDED_SETTLED
        assert any(isinstance(a, ClearReviewFlag) for a in actions)

    def test_force_settle_blind_refused(self):
        with pytest.raises(GuardRejection) as exc_info:
            transition(
                State.S9_DELIVERED_WEDGED, Event.E_FORCE_SETTLE,
                ev(is_payer=True, readable=False, has_capture=False,
                   enum=Event.ENUM_UNREAD))
        assert exc_info.value.category == "conflict"

    def test_force_settle_found_capture_not_voided_by_false_flag(self):
        with pytest.raises(GuardRejection) as exc_info:
            transition(
                State.S9_DELIVERED_WEDGED, Event.E_FORCE_SETTLE,
                ev(is_payer=True, readable=True, enum=Event.ENUM_FOUND,
                   has_capture=False, is_partial=False,
                   authorization_id="auth-1"))
        assert exc_info.value.category == "conflict"

    def test_force_settle_partial_unconfirmed_refused(self):
        with pytest.raises(GuardRejection) as exc_info:
            transition(
                State.S9_DELIVERED_WEDGED, Event.E_FORCE_SETTLE,
                ev(is_payer=True, readable=True, enum=Event.ENUM_FOUND,
                   has_capture=True, is_partial=True, void_confirmed=False,
                   capture_ids=("CAP-1",), amount_cents=500))
        assert exc_info.value.category == "conflict"

    def test_force_settle_unread_rejected(self):
        with pytest.raises(GuardRejection):
            transition(State.S9_DELIVERED_WEDGED, Event.E_FORCE_SETTLE,
                       ev(is_payer=True, readable=False,
                          enum=Event.ENUM_UNREAD))

    def test_cancel_pending_no_demotion(self):
        # Rev 3 (Gemini Defect 3): NEVER demote to S5.
        s, actions = transition(
            State.S9_DELIVERED_WEDGED, Event.E_CANCEL_PENDING,
            ev(is_payer=True))
        assert s == State.S9_DELIVERED_WEDGED
        assert any(isinstance(a, ClearIntent) for a in actions)

    def test_reconcile_wedge_persists(self):
        s, _ = transition(State.S9_DELIVERED_WEDGED, Event.E_RECONCILE)
        assert s == State.S9_DELIVERED_WEDGED


# ---------------------------------------------------------------------------
# I3 static check: every non-terminal state reaches a terminal (BFS)
# ---------------------------------------------------------------------------

class TestLiveness:
    """I3: no reachable non-terminal state may be a sink or trap.

    Builds the directed graph from _TABLE cells (ignoring evidence-gated
    branches — an edge exists if ANY evidence leads from S to S') and
    asserts every non-terminal state has a path to a terminal state.
    """

    def _build_graph(self):
        """Build the directed state graph from static handler metadata.

        Uses each handler's `target_states` attribute (declared via the
        @_targets decorator) — no handler evaluation needed, so no
        evidence required. An edge S -> S' exists if ANY cell from S
        can transition to S' under some evidence.
        """
        from app.dispatcher import _TABLE, State
        graph: dict = {s: set() for s in State}
        for (s, _), handler in _TABLE.items():
            targets = getattr(handler, "target_states", None)
            assert targets is not None, (
                f"handler {handler.__name__} missing @_targets annotation (I3)")
            for t in targets:
                graph[s].add(t)
        return graph

    def _reachable_terminals(self, graph, start):
        """BFS from start; return the set of reachable terminal states."""
        from app.dispatcher import State
        seen = set()
        queue = [start]
        terminals = set()
        while queue:
            s = queue.pop(0)
            if s in seen:
                continue
            seen.add(s)
            if s.is_terminal:
                terminals.add(s)
                continue  # terminals are sinks — don't follow (no outgoing)
            for t in graph.get(s, ()):
                if t not in seen:
                    queue.append(t)
        return terminals

    def test_every_nonterminal_reaches_a_terminal(self):
        # I3 (formal): for every non-terminal state S, BFS over the
        # static edge graph must find a path to at least one terminal.
        # This FAILS if any state is a trap or sink.
        from app.dispatcher import State
        graph = self._build_graph()
        for s in State:
            if s.is_terminal:
                continue
            terminals = self._reachable_terminals(graph, s)
            assert len(terminals) > 0, (
                f"I3 VIOLATION: {s.value} cannot reach any terminal state")

    def test_all_handlers_have_target_annotations(self):
        # Completeness: every handler in _TABLE must declare targets.
        from app.dispatcher import _TABLE
        for (s, e), handler in _TABLE.items():
            assert hasattr(handler, "target_states"), (
                f"{handler.__name__} ({s.value}, {e.value}) missing @_targets")

    def test_nonterminal_states_have_table_cells(self):
        # Every non-terminal state has at least one outgoing cell.
        from app.dispatcher import _TABLE
        non_terminal = [s for s in State if not s.is_terminal]
        states_with_cells = {s for (s, _) in _TABLE}
        for s in non_terminal:
            assert s in states_with_cells, f"{s} has no table cells (I3)"

    def test_wedged_states_have_force_settle(self):
        # S5 and S9 must have E_FORCE_SETTLE → terminal exits (I3).
        from app.dispatcher import _TABLE
        assert (State.S5_AUTHORIZED_WEDGED, Event.E_FORCE_SETTLE) in _TABLE
        assert (State.S9_DELIVERED_WEDGED, Event.E_FORCE_SETTLE) in _TABLE

    def test_s8_has_wedge_exit(self):
        # S8 (the former deadlock) must have an explicit wedge exit.
        from app.dispatcher import _TABLE
        assert (State.S8_DELIVERED_PARTIAL_RECORDED,
                Event.E_RECONCILE) in _TABLE

    def test_s3_has_fresh_order_exit(self):
        # S3 (the former lockout) must have the read-guarded order exit.
        from app.dispatcher import _TABLE
        assert (State.S3_QUOTED_ATTEMPTED, Event.E_CREATE_ORDER) in _TABLE
        assert (State.S3_QUOTED_ATTEMPTED,
                Event.R_OLD_ORDER_DEAD) in _TABLE




class TestNewReviewerFixes:
    """Tests for the new reviewer's 5 fixes (review 20)."""

    def test_fix2_unaudited_capture_goes_to_s10c(self):
        """Meta-review: unaudited capture uses the explicit
        E_UNAUDITED_CAPTURE event → S10c, not R_CAP_COMPLETED with
        silently-filtered empty ids."""
        from app.dispatcher import transition, State, Event, Evidence
        ev = Evidence(
            job_id="JOB-1",
            amount_cents=1250,
            authorized_cents=1250,
        )
        new_state, actions = transition(
            State.S7_DELIVERED_PENDING, Event.E_UNAUDITED_CAPTURE, ev)
        assert new_state == State.S10c_CAPTURED_UNAUDITED
        # RecordCapture should have empty IDs (explicit, not filtered)
        from app.dispatcher import RecordCapture
        rc = [a for a in actions if isinstance(a, RecordCapture)][0]
        assert rc.capture_ids == ()

    def test_fix2_empty_string_raises_not_filtered(self):
        """Meta-review: _as_capture_ids is strict — blank ids raise
        ValueError instead of being silently filtered. Bad data must
        be explicit."""
        from app.dispatcher import _as_capture_ids
        import pytest
        with pytest.raises(ValueError):
            _as_capture_ids(("",))
        with pytest.raises(ValueError):
            _as_capture_ids(["", "  "])
        with pytest.raises(ValueError):
            _as_capture_ids(("CAP-1", "", "CAP-2"))
        # Empty tuple (no ids at all) is fine — that's the explicit
        # unaudited path, not bad data.
        assert _as_capture_ids(()) == ()

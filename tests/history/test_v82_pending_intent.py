"""Item 2a (production-readiness): the FSM models the pending capture
intent lifecycle — no legacy fallback for in-flight keys.

Background reconcile is READ-ONLY: it never replays the key. The FSM
decides what the read proves about the in-flight key; the bridge
surfaces the legacy-compatible return contract (awaiting /
intent_kept_unconfirmed).
"""
import pytest

from app.dispatcher import (
    Event, Evidence, State, ClearIntent, NoOp, SetReviewFlag,
    _pending_intent_reconcile,
)


def _ev(**kw):
    base = dict(job_id="j1", authorization_id="AUTH-1",
                pending_intent_id="key-123",
                pending_intent_amount_cents=500)
    base.update(kw)
    return Evidence(**base)


class TestPendingIntentBranch:
    def test_found_falls_through_to_normal_arm(self):
        # A listed COMPLETED capture: the normal arm records it and
        # clears the spent intent.
        ev = _ev(read=Event.READ_CREATED, enum=Event.ENUM_FOUND,
                 has_capture=True, capture_ids=("C-1",), amount_cents=500)
        assert _pending_intent_reconcile(ev, State.S4_AUTHORIZED_LIVE) is None

    def test_created_none_keeps_intent(self):
        ev = _ev(read=Event.READ_CREATED, enum=Event.ENUM_NONE)
        state, actions = _pending_intent_reconcile(
            ev, State.S4_AUTHORIZED_LIVE)
        assert state == State.S4_AUTHORIZED_LIVE
        assert not any(isinstance(a, ClearIntent) for a in actions)
        note = next(a.note for a in actions if isinstance(a, NoOp))
        assert "retry the capture to replay the key" in note

    def test_created_unread_keeps_intent_with_unread_note(self):
        ev = _ev(read=Event.READ_PENDING, enum=Event.ENUM_UNREAD)
        state, actions = _pending_intent_reconcile(
            ev, State.S4_AUTHORIZED_LIVE)
        assert state == State.S4_AUTHORIZED_LIVE
        note = next(a.note for a in actions if isinstance(a, NoOp))
        assert "order could not be read" in note

    def test_created_ambiguous_awaits(self):
        ev = _ev(read=Event.READ_CREATED, enum=Event.ENUM_AMBIGUOUS)
        state, actions = _pending_intent_reconcile(
            ev, State.S4_AUTHORIZED_LIVE)
        assert state == State.S4_AUTHORIZED_LIVE
        note = next(a.note for a in actions if isinstance(a, NoOp))
        assert "awaiting" in note

    def test_partial_ambiguous_awaits_with_partial_note(self):
        ev = _ev(read=Event.READ_PARTIAL, enum=Event.ENUM_AMBIGUOUS)
        state, actions = _pending_intent_reconcile(
            ev, State.S6_DELIVERED_OPEN)
        assert state == State.S6_DELIVERED_OPEN
        note = next(a.note for a in actions if isinstance(a, NoOp))
        assert "partial capture pending" in note

    def test_partial_none_keeps_intent_with_amount(self):
        ev = _ev(read=Event.READ_PARTIAL, enum=Event.ENUM_NONE)
        state, actions = _pending_intent_reconcile(
            ev, State.S6_DELIVERED_OPEN)
        assert state == State.S6_DELIVERED_OPEN
        note = next(a.note for a in actions if isinstance(a, NoOp))
        assert "retry capture_partial" in note
        assert "$5.00" in note

    def test_released_none_stale_falls_through_to_clean_void(self):
        # Stale intent + ENUM_NONE (both resources read, nothing listed):
        # the key had time to land and didn't — it's dead. Fall through
        # to the normal arm (clean void), don't wedge.
        ev = _ev(read=Event.READ_RELEASED, enum=Event.ENUM_NONE,
                 pending_intent_stale=True)
        assert _pending_intent_reconcile(
            ev, State.S6_DELIVERED_OPEN) is None

    def test_released_none_fresh_wedges_keeps_key(self):
        # Non-stale intent: the key might still be in flight — wedge.
        ev = _ev(read=Event.READ_RELEASED, enum=Event.ENUM_NONE,
                 pending_intent_stale=False)
        state, actions = _pending_intent_reconcile(
            ev, State.S6_DELIVERED_OPEN)
        assert state == State.S9_DELIVERED_WEDGED
        flag = next(a for a in actions if isinstance(a, SetReviewFlag))
        assert "key is kept" in flag.reason
        assert "$5.00" in flag.reason
        assert flag.amount_cents == 500
        assert not any(isinstance(a, ClearIntent) for a in actions)

    def test_released_unread_wedges_with_unread_reason(self):
        ev = _ev(read=Event.READ_RELEASED, enum=Event.ENUM_UNREAD)
        state, actions = _pending_intent_reconcile(
            ev, State.S4_AUTHORIZED_LIVE)
        assert state == State.S5_AUTHORIZED_WEDGED
        flag = next(a for a in actions if isinstance(a, SetReviewFlag))
        assert "order could not be read" in flag.reason

    def test_released_enum_none_wedges(self):
        # enum=None (enumeration raised): never void blind.
        ev = _ev(read=Event.READ_RELEASED, enum=None)
        state, actions = _pending_intent_reconcile(
            ev, State.S4_AUTHORIZED_LIVE)
        assert state == State.S5_AUTHORIZED_WEDGED

    def test_released_ambiguous_wedges(self):
        ev = _ev(read=Event.READ_RELEASED, enum=Event.ENUM_AMBIGUOUS)
        state, actions = _pending_intent_reconcile(
            ev, State.S7_DELIVERED_PENDING)
        assert state == State.S9_DELIVERED_WEDGED

    def test_unattributable_wedges(self):
        ev = _ev(read=Event.READ_CREATED,
                 enum=Event.ENUM_UNATTRIBUTABLE)
        state, actions = _pending_intent_reconcile(
            ev, State.S4_AUTHORIZED_LIVE)
        assert state == State.S5_AUTHORIZED_WEDGED

    def test_captured_none_settles_unaudited_clears_intent(self):
        ev = _ev(read=Event.READ_CAPTURED, enum=Event.ENUM_NONE)
        state, actions = _pending_intent_reconcile(
            ev, State.S6_DELIVERED_OPEN)
        assert state == State.S10c_CAPTURED_UNAUDITED
        assert any(isinstance(a, ClearIntent) for a in actions)

    def test_captured_enum_none_wedges(self):
        # The enumeration itself raised: never settle on an unreadable
        # read, even when PayPal reports CAPTURED.
        ev = _ev(read=Event.READ_CAPTURED, enum=None)
        state, actions = _pending_intent_reconcile(
            ev, State.S4_AUTHORIZED_LIVE)
        assert state == State.S5_AUTHORIZED_WEDGED

    def test_s5_wedge_mapping(self):
        # Re-evaluation from a wedged AUTHORIZED job with an unreadable
        # order stays in S5.
        ev = _ev(read=Event.READ_RELEASED, enum=Event.ENUM_UNREAD)
        state, _ = _pending_intent_reconcile(ev, State.S5_AUTHORIZED_WEDGED)
        assert state == State.S5_AUTHORIZED_WEDGED

    def test_s9_wedge_mapping(self):
        ev = _ev(read=Event.READ_RELEASED, enum=Event.ENUM_UNREAD)
        state, _ = _pending_intent_reconcile(ev, State.S9_DELIVERED_WEDGED)
        assert state == State.S9_DELIVERED_WEDGED


class TestPendingIntentBridge:
    """The bridge no longer diverts in-flight keys to legacy."""

    def test_no_fallback1_in_bridge(self):
        import inspect
        from app import escrow
        src = inspect.getsource(
            escrow.EscrowService._reconcile_job_via_dispatcher)
        # The legacy diversion branch is gone (the comment noting its
        # removal is fine); the FSM decides pending-intent cases now.
        assert "if self._has_pending_capture(job):" not in src

    def test_pending_kept_unconfirmed_contract(self):
        from test_escrow_paypal import _StubPayPal, _stub_authorized_job
        stub = _StubPayPal()
        svc, job = _stub_authorized_job(stub)
        svc.submit_delivery(job, agent="agent@example",
                            delivery_text="report")
        svc._set_pending_capture(
            job, 500, f"lamplight-capture-partial-500-{job.job_id}-attempt-0")
        # CREATED hold, nothing listed: the key is kept, not retired.
        result = svc.reconcile_job(job)
        assert result["intent_kept_unconfirmed"] is True
        assert "retry the capture to replay the key" in result["error"]
        assert job.pending_capture_request_id is not None

    def test_pending_ambiguous_awaiting_contract(self):
        from test_escrow_paypal import _StubPayPal, _stub_authorized_job
        stub = _StubPayPal()
        svc, job = _stub_authorized_job(stub)
        svc.submit_delivery(job, agent="agent@example",
                            delivery_text="report")
        svc._set_pending_capture(
            job, 500, f"lamplight-capture-partial-500-{job.job_id}-attempt-0")
        stub.auths["AUTH-STUB"]["captures"] = [{
            "id": "CAP-PENDING", "status": "PENDING", "amount": "5.00"}]
        result = svc.reconcile_job(job)
        assert result.get("awaiting") is True
        assert job.pending_capture_request_id is not None

    def test_pending_found_clears_intent(self):
        from test_escrow_paypal import _StubPayPal, _stub_authorized_job
        stub = _StubPayPal()
        svc, job = _stub_authorized_job(stub)
        svc.submit_delivery(job, agent="agent@example",
                            delivery_text="report")
        key = f"lamplight-capture-{job.job_id}-attempt-0"
        svc._set_pending_capture(job, None, key)
        stub.auths["AUTH-STUB"]["status"] = "CAPTURED"
        stub.auths["AUTH-STUB"]["captures"] = [{
            "id": "CAP-1", "status": "COMPLETED",
            "amount": {"currency_code": "USD", "value": "12.50"}}]
        result = svc.reconcile_job(job)
        assert result["to_status"] == "CAPTURED", result
        assert job.pending_capture_request_id is None

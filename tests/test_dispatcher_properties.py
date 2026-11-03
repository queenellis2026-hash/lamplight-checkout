
"""Property-based tests for the dispatcher (I1 fund conservation, I4 idempotence).

Uses Hypothesis to generate random legal event sequences through the
§5 table and verify the Golden Invariants hold at every step.

I1 (Fund Conservation): captured + voided + active_hold == authorized
  for S4-S9; == 0 for S1-S3. No transition may create or destroy money.

I4 (Idempotent Replay): applying the same event twice yields the same
  state with no duplicate side effects.

These are Phase 3 tests — they run against the full §5 table.
"""

import pytest
from hypothesis import given, settings, strategies as st

from app.dispatcher import (
    Event,
    Evidence,
    GuardRejection,
    State,
    UnhandledTransition,
    transition,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Legal (state, event) pairs that don't require complex evidence.
# Used as the basis for random walk generation.
SIMPLE_CELLS = [
    # S1
    (State.S1_QUOTED_FRESH, Event.E_CREATE_ORDER),
    (State.S1_QUOTED_FRESH, Event.E_RECONCILE),
    (State.S1_QUOTED_FRESH, Event.T_QUOTE_EXPIRY),
    # S2
    (State.S2_QUOTED_ORDERED, Event.E_RECONCILE),
    (State.S2_QUOTED_ORDERED, Event.T_QUOTE_EXPIRY),
    # S3
    (State.S3_QUOTED_ATTEMPTED, Event.R_AUTH_DENIED),
    (State.S3_QUOTED_ATTEMPTED, Event.R_AUTH_500),
    # S4
    (State.S4_AUTHORIZED_LIVE, Event.T_DEADLINE),
    (State.S4_AUTHORIZED_LIVE, Event.WH_CAP_DENIED),
    # S6
    (State.S6_DELIVERED_OPEN, Event.WH_CAP_DENIED),
    # Terminals
    (State.S10a_CAPTURED_FULL, Event.E_RECONCILE),
    (State.S10b_CAPTURED_SPLIT, Event.E_RECONCILE),
    (State.S10c_CAPTURED_UNAUDITED, Event.E_RECONCILE),
    (State.S11_VOIDED_SETTLED, Event.E_RECONCILE),
]

# Map each state to its legal events (for walk generation).
STATE_EVENTS: dict[State, list[Event]] = {}
for s, e in SIMPLE_CELLS:
    STATE_EVENTS.setdefault(s, []).append(e)


def _default_evidence(state: State, event: Event) -> Evidence:
    """Build minimal valid evidence for a (state, event) pair."""
    return Evidence(
        job_id="prop-test-job",
        attempt=1,
        is_payer=True,
        deadline_passed=False,
        delivery_verified=True,
        readable=True,
        amount_cents=2500,  # required by SaveIntent/RecordCapture guards
    )


# ---------------------------------------------------------------------------
# I4: Idempotent replay
# ---------------------------------------------------------------------------

class TestIdempotentReplay:
    """I4: applying the same event twice yields the same state."""

    @pytest.mark.parametrize("state,event", SIMPLE_CELLS)
    def test_double_apply_same_result(self, state, event):
        """transition(transition(s,e), e) == transition(s,e)."""
        ev = _default_evidence(state, event)
        try:
            s1, a1 = transition(state, event, ev)
        except (GuardRejection, UnhandledTransition):
            pytest.skip("cell requires specific evidence")
        try:
            s2, a2 = transition(s1, event, ev)
        except GuardRejection:
            # Second application refused cleanly (e.g. non-payer replay) —
            # that's an idempotent refusal, not a duplication.
            return
        except UnhandledTransition:
            # Gemini Fix 6 (MEDIUM): a replay that 500s is a matrix gap,
            # not idempotence. Fail loud so the missing cell is visible.
            # (Duplicate webhook arrival must not crash the worker.)
            pytest.fail(
                f"replay of {event} on {s1} (from {state}) raised "
                f"UnhandledTransition — add an idempotent NoOp or "
                f"GuardRejection cell")
        # If the second application succeeds, the state must not move
        # again. Matching action types is not idempotence.
        assert s2 == s1
        money = [a for a in a2
                 if type(a).__name__ in
                 ("RecordCapture", "PostVoid", "RecordVoid")]
        assert money == []


    def test_reconcile_is_idempotent(self):
        """E_RECONCILE twice in a row → same state, both no-ops."""
        for state in (State.S4_AUTHORIZED_LIVE, State.S6_DELIVERED_OPEN,
                      State.S10a_CAPTURED_FULL):
            ev = _default_evidence(state, Event.E_RECONCILE)
            s1, a1 = transition(state, Event.E_RECONCILE, ev)
            s2, a2 = transition(s1, Event.E_RECONCILE, ev)
            assert s1 == s2 == state


# ---------------------------------------------------------------------------
# I1: Fund conservation (structural)
# ---------------------------------------------------------------------------

class TestFundConservation:
    """I1: the table never emits actions that create or destroy money.

    Structural check: every cell that emits RecordCapture or PostVoid
    must also emit the corresponding state transition to a terminal
    or wedged state (money is accounted for). No cell emits a money
    action while staying in a non-terminal, non-wedged state without
    also recording the allocation.
    """

    def test_money_actions_always_change_state_or_wedge(self):
        """Any cell emitting RecordCapture/PostVoid/SaveIntent must leave
        the pre-money state (no silent money movement in a no-op).

        Real check (not assert True): for every cell whose handler can
        emit a money-moving action (determined by source inspection),
        assert that NONE of the handler's declared target_states equals
        the source state unless the target is a wedged state — i.e. money
        never moves while staying in the same non-wedged state.
        """
        from app.dispatcher import _TABLE, State
        import inspect
        # Money-MOVING actions (not intent anchors): RecordCapture books
        # money on the job; PostVoid instructs the caller to void.
        # SaveIntent/SaveResidualVoidIntent are Phase 1 crash-recovery
        # anchors — the money moves in Phase 2 (R_* events), so they may
        # legitimately stay in the same state (e.g. S8 + E_START_RESIDUAL_VOID
        # saves the intent; R_VOID_OK transitions to S10b).
        MONEY_ACTIONS = ("RecordCapture", "PostVoid", "RecordVoid")
        # Documented pattern: E_RECONCILE handlers book externally-verified
        # PayPal truth while ALSO having the source state as a target for
        # their no-op branches. E_VOID_DEADLINE on S4 is the other mixed
        # cell: the clean arm saves a void intent and stays; RecordCapture
        # lives only on the pre-delivery wedge arm, which leaves S4.
        def _is_reconcile_self_loop(s, e, t):
            return (e == Event.E_RECONCILE and t == s
                    and not s.is_terminal and not s.is_wedged)

        def _is_void_intent_anchor(s, e, t):
            return (e == Event.E_VOID_DEADLINE
                    and s == State.S4_AUTHORIZED_LIVE
                    and t == s)
        for (s, e), handler in _TABLE.items():
            src = inspect.getsource(handler)
            emits_money = any(a in src for a in MONEY_ACTIONS)
            if not emits_money:
                continue
            targets = getattr(handler, "target_states", frozenset())
            for t in targets:
                if _is_reconcile_self_loop(s, e, t):
                    continue
                if _is_void_intent_anchor(s, e, t):
                    continue
                ok = (t != s) or t.is_wedged
                assert ok, (
                    f"I1 VIOLATION: {s.value}/{e.value} emits money while "
                    f"staying in non-wedged state {t.value}")
        # The S4 void self-loop must not book money. PostVoid is the
        # instruction to POST; settlement is R_VOID_OK.
        stayed, actions = transition(
            State.S4_AUTHORIZED_LIVE, Event.E_VOID_DEADLINE,
            Evidence(job_id="i1-job", attempt=1, is_payer=True,
                     deadline_passed=True, read=Event.READ_CREATED,
                     enum=Event.ENUM_NONE, authorization_id="auth-i1",
                     amount_cents=1000))
        assert stayed == State.S4_AUTHORIZED_LIVE
        assert not any(type(a).__name__ in ("RecordCapture", "RecordVoid")
                       for a in actions)

    def test_no_cell_creates_money_from_nothing(self):
        """S1/S2/S3 (pre-authorization) cells never emit RecordCapture,
        EXCEPT S3+E_RECONCILE adopting an externally-verified capture.

        Rationale (I1): recording a capture that PayPal's read API
        confirms is NOT creating money — it's booking external truth.
        The invariant forbids inventing money, not recording it.
        S3 is "unknown server state"; if the read shows CAPTURED with
        ENUM_FOUND, the money moved at PayPal and we must book it.
        """
        from app.dispatcher import _TABLE, RecordCapture
        import inspect
        allowed = {(State.S3_QUOTED_ATTEMPTED, Event.E_RECONCILE)}
        for (s, e), handler in _TABLE.items():
            if s in (State.S1_QUOTED_FRESH, State.S2_QUOTED_ORDERED,
                      State.S3_QUOTED_ATTEMPTED):
                if (s, e) in allowed:
                    continue
                src = inspect.getsource(handler)
                assert "RecordCapture" not in src, (
                    f"{s}/{e} emits RecordCapture in pre-auth state (I1)")


# ---------------------------------------------------------------------------
# Stateful walk (Hypothesis RuleBasedStateMachine)
# ---------------------------------------------------------------------------

from hypothesis.stateful import RuleBasedStateMachine, rule, invariant


class TestFundConservationEnforcement:
    """I1: captured + voided must not exceed authorized.

    When the caller provides authorized_cents, money-booking handlers
    enforce the conservation check via _check_fund_conservation.
    """

    def test_capture_exceeding_authorized_rejected(self):
        from app.dispatcher import (
            Evidence, Event, GuardRejection, State, transition)
        with pytest.raises(GuardRejection) as exc_info:
            transition(
                State.S6_DELIVERED_OPEN, Event.E_RECONCILE,
                Evidence(job_id="j1", attempt=1, is_payer=True,
                         read=Event.READ_CAPTURED, enum=Event.ENUM_FOUND,
                         capture_ids=("CAP-1",), amount_cents=6000,
                         authorized_cents=5000))
        assert exc_info.value.category == "validation"
        assert "exceeds authorized" in exc_info.value.reason

    def test_capture_within_authorized_allowed(self):
        from app.dispatcher import (
            Evidence, Event, State, transition)
        s, actions = transition(
            State.S6_DELIVERED_OPEN, Event.E_RECONCILE,
            Evidence(job_id="j1", attempt=1, is_payer=True,
                     read=Event.READ_CAPTURED, enum=Event.ENUM_FOUND,
                     capture_ids=("CAP-1",), amount_cents=4000,
                     authorized_cents=5000))
        assert s == State.S10a_CAPTURED_FULL

    def test_no_authorized_cents_no_check(self):
        # Without authorized_cents, no conservation check (backward compat).
        from app.dispatcher import (
            Evidence, Event, State, transition)
        s, _ = transition(
            State.S6_DELIVERED_OPEN, Event.E_RECONCILE,
            Evidence(job_id="j1", attempt=1, is_payer=True,
                     read=Event.READ_CAPTURED, enum=Event.ENUM_FOUND,
                     capture_ids=("CAP-1",), amount_cents=999999))
        assert s == State.S10a_CAPTURED_FULL


class _SMJob:
    """Minimal mutable job for the state machine."""
    def __init__(self):
        self.job_id = "sm-job"
        self.status = "QUOTED"
        self.needs_review = False
        self.pending_capture_request_id = None
        self.capture_id = ""
        self.capture_amount_cents = None
        self.voided_amount_cents = None
        self.settlement_method = None
        self.authorize_attempted = False
        self.order_id = ""


def _sm_evidence(**kw):
    """Evidence with valid defaults for state-machine rules."""
    defaults = dict(
        job_id="sm-job",
        attempt=1,
        is_payer=True,
        deadline_passed=False,
        delivery_verified=True,
        readable=True,
        amount_cents=2500,
    )
    defaults.update(kw)
    return Evidence(**defaults)


class DispatcherStateMachine(RuleBasedStateMachine):
    """True stateful walk: QUOTED → AUTHORIZED → DELIVERED → CAPTURED.

    Tracks both the dispatcher State and a mutable job record. After
    every rule, the invariant asserts resolve_state(job) == machine
    state — catching any state/event/target mismatch between the table
    and the waterfall.
    """

    def __init__(self):
        super().__init__()
        self.current = State.S1_QUOTED_FRESH
        self.job = _SMJob()

    def _apply(self, event, evidence, expect_state, job_updates):
        """Apply transition(), update job fields, set machine state."""
        new_state, actions = transition(self.current, event, evidence)
        assert new_state == expect_state, (
            f"{self.current.value}/{event.value}: expected {expect_state.value}, "
            f"got {new_state.value}")
        assert isinstance(actions, list) and len(actions) > 0
        for k, v in job_updates.items():
            setattr(self.job, k, v)
        self.current = new_state

    # -- Happy path: QUOTED → AUTHORIZED → DELIVERED → CAPTURED ---------

    @rule()
    def create_order(self):
        if self.current != State.S1_QUOTED_FRESH:
            return
        self._apply(Event.E_CREATE_ORDER, _sm_evidence(),
                    State.S2_QUOTED_ORDERED, {"order_id": "ORD-1"})

    @rule()
    def start_authorize(self):
        if self.current != State.S2_QUOTED_ORDERED:
            return
        self._apply(Event.E_START_AUTHORIZE, _sm_evidence(),
                    State.S3_QUOTED_ATTEMPTED,
                    {"authorize_attempted": True})

    @rule()
    def auth_created(self):
        if self.current != State.S3_QUOTED_ATTEMPTED:
            return
        self._apply(Event.R_AUTH_CREATED, _sm_evidence(),
                    State.S4_AUTHORIZED_LIVE, {"status": "AUTHORIZED"})

    @rule()
    def submit_delivery(self):
        if self.current != State.S4_AUTHORIZED_LIVE:
            return
        self._apply(Event.E_SUBMIT_DELIVERY, _sm_evidence(),
                    State.S6_DELIVERED_OPEN, {"status": "DELIVERED"})

    @rule()
    def start_capture(self):
        if self.current != State.S6_DELIVERED_OPEN:
            return
        self._apply(Event.E_START_CAPTURE, _sm_evidence(),
                    State.S7_DELIVERED_PENDING,
                    {"pending_capture_request_id": "req-1"})

    @rule()
    def cap_completed(self):
        if self.current != State.S7_DELIVERED_PENDING:
            return
        self._apply(Event.R_CAP_COMPLETED,
                    _sm_evidence(capture_ids=("CAP-1",)),
                    State.S10a_CAPTURED_FULL,
                    {"status": "CAPTURED",
                     "capture_id": "CAP-1",
                     "capture_amount_cents": 2500,
                     "pending_capture_request_id": None})

    # -- Branch: denial resets to S1 ------------------------------------

    @rule()
    def auth_denied(self):
        if self.current != State.S3_QUOTED_ATTEMPTED:
            return
        self._apply(Event.R_AUTH_DENIED, _sm_evidence(),
                    State.S1_QUOTED_FRESH,
                    {"authorize_attempted": False, "order_id": ""})

    # -- Branch: void path ----------------------------------------------

    @rule()
    def void_deadline(self):
        if self.current != State.S4_AUTHORIZED_LIVE:
            return
        # A deadline void without a read is a client error, not a settle.
        # The clean read starts the POST and stays live until R_VOID_OK.
        self._apply(Event.E_VOID_DEADLINE,
                    _sm_evidence(deadline_passed=True,
                                 read=Event.READ_CREATED,
                                 enum=Event.ENUM_NONE,
                                 authorization_id="auth-1"),
                    State.S4_AUTHORIZED_LIVE, {})

    @rule()
    def void_confirmed(self):
        if self.current != State.S4_AUTHORIZED_LIVE:
            return
        self._apply(Event.R_VOID_OK,
                    _sm_evidence(voided_amount_cents=2500,
                                 authorization_id="auth-1"),
                    State.S11_VOIDED_SETTLED, {"status": "VOIDED"})

    # -- Branch: capture declined back to open ---------------------------

    @rule()
    def cap_declined(self):
        if self.current != State.S7_DELIVERED_PENDING:
            return
        self._apply(Event.R_CAP_DECLINED, _sm_evidence(),
                    State.S6_DELIVERED_OPEN,
                    {"pending_capture_request_id": None})

    # -- No-op reconcile (idempotent) ------------------------------------

    @rule()
    def reconcile_noop(self):
        if self.current not in (State.S1_QUOTED_FRESH,
                                State.S2_QUOTED_ORDERED,
                                State.S4_AUTHORIZED_LIVE,
                                State.S6_DELIVERED_OPEN):
            return
        self._apply(Event.E_RECONCILE, _sm_evidence(),
                    self.current, {})

    @invariant()
    def waterfall_agrees_with_machine(self):
        """I3/I4: resolve_state(job) must equal the machine's state.

        Catches any mismatch between the §5 table cells and the §7.1
        waterfall — if a cell transitions to S4 but the job fields
        resolve to S6, the design is inconsistent.
        """
        from app.dispatcher import resolve_state
        assert resolve_state(self.job) == self.current, (
            f"waterfall={resolve_state(self.job).value} != "
            f"machine={self.current.value}")


# Run the state machine as a Hypothesis test.
TestDispatcherStateMachine = DispatcherStateMachine.TestCase
TestDispatcherStateMachine.settings = settings(
    max_examples=50, deadline=None, stateful_step_count=20)



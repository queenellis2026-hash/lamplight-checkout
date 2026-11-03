
"""Phase 2 skeleton tests for app/dispatcher.py.

Covers:
  1. resolve_state() waterfall — every flag combination resolves to
     exactly one state (§7.1).
  2. make_intent_key() — determinism + attempt-uniqueness (edge case #3).
  3. transition() skeleton — smoke cells work; unlisted pairs raise
     UnhandledTransition.
  4. GuardRejection vs UnhandledTransition — distinct types, distinct
     HTTP codes (I5).
"""

import pytest

from app.dispatcher import (
    Action,
    ClearIntent,
    ClearReviewFlag,
    DeleteJob,
    EmitReceipt,
    Event,
    GuardRejection,
    NoOp,
    PlaceHold,
    PostVoid,
    ReadOldOrder,
    RecordCapture,
    SaveIntent,
    SaveResidualVoidIntent,
    SetReviewFlag,
    State,
    UnhandledTransition,
    make_intent_key,
    resolve_state,
    transition,
)


# ---------------------------------------------------------------------------
# Test double — duck-typed job record (no import of app.escrow.Job)
# ---------------------------------------------------------------------------

class FakeJob:
    def __init__(self, **kw):
        self.job_id = kw.get("job_id", "job-1")
        self.status = kw.get("status", "QUOTED")
        self.needs_review = kw.get("needs_review", False)
        self.pending_capture_request_id = kw.get("pending_capture_request_id", None)
        self.capture_id = kw.get("capture_id", "")
        self.capture_amount_cents = kw.get("capture_amount_cents", None)
        self.voided_amount_cents = kw.get("voided_amount_cents", None)
        self.settlement_method = kw.get("settlement_method", None)
        self.authorize_attempted = kw.get("authorize_attempted", False)
        self.order_id = kw.get("order_id", "")


# ---------------------------------------------------------------------------
# 1. resolve_state() waterfall
# ---------------------------------------------------------------------------

class TestResolveState:
    def test_quoted_fresh(self):
        assert resolve_state(FakeJob()) == State.S1_QUOTED_FRESH

    def test_quoted_ordered(self):
        assert resolve_state(FakeJob(order_id="ORD-1")) == State.S2_QUOTED_ORDERED

    def test_quoted_attempted(self):
        # authorize_attempted dominates order_id (history is weakest signal,
        # but an attempted POST means "unknown server state").
        j = FakeJob(order_id="ORD-1", authorize_attempted=True)
        assert resolve_state(j) == State.S3_QUOTED_ATTEMPTED

    def test_authorized_live(self):
        assert resolve_state(FakeJob(status="AUTHORIZED")) == State.S4_AUTHORIZED_LIVE

    def test_authorized_wedged(self):
        j = FakeJob(status="AUTHORIZED", needs_review=True)
        assert resolve_state(j) == State.S5_AUTHORIZED_WEDGED

    def test_delivered_open(self):
        assert resolve_state(FakeJob(status="DELIVERED")) == State.S6_DELIVERED_OPEN

    def test_delivered_pending(self):
        j = FakeJob(status="DELIVERED", pending_capture_request_id="key-1")
        assert resolve_state(j) == State.S7_DELIVERED_PENDING

    def test_delivered_partial_recorded_by_id(self):
        j = FakeJob(status="DELIVERED", capture_id="CAP-1")
        assert resolve_state(j) == State.S8_DELIVERED_PARTIAL_RECORDED

    def test_delivered_partial_recorded_by_amount(self):
        # Amount-only record counts as recorded money (round 28/43).
        j = FakeJob(status="DELIVERED", capture_amount_cents=500)
        assert resolve_state(j) == State.S8_DELIVERED_PARTIAL_RECORDED

    def test_delivered_wedged(self):
        j = FakeJob(status="DELIVERED", needs_review=True)
        assert resolve_state(j) == State.S9_DELIVERED_WEDGED

    def test_wedge_dominates_pending(self):
        # needs_review beats has_pending_capture in the waterfall.
        j = FakeJob(status="DELIVERED", needs_review=True,
                    pending_capture_request_id="key-1")
        assert resolve_state(j) == State.S9_DELIVERED_WEDGED

    def test_wedge_dominates_recorded(self):
        j = FakeJob(status="DELIVERED", needs_review=True, capture_id="CAP-1")
        assert resolve_state(j) == State.S9_DELIVERED_WEDGED

    def test_pending_dominates_recorded(self):
        j = FakeJob(status="DELIVERED", pending_capture_request_id="key-1",
                    capture_id="CAP-1")
        assert resolve_state(j) == State.S7_DELIVERED_PENDING

    def test_pending_in_authorized_is_impossible(self):
        # §3.5: capture intent cannot exist while AUTHORIZED.
        j = FakeJob(status="AUTHORIZED", pending_capture_request_id="key-1")
        with pytest.raises(UnhandledTransition):
            resolve_state(j)

    def test_recorded_in_authorized_is_impossible(self):
        # §3.5: pre-delivery partials wedge on discovery — no
        # AUTHORIZED_PARTIAL_RECORDED state exists.
        j = FakeJob(status="AUTHORIZED", capture_id="CAP-1")
        with pytest.raises(UnhandledTransition):
            resolve_state(j)

    # --- terminal disambiguation ---

    def test_captured_full(self):
        j = FakeJob(status="CAPTURED", capture_id="CAP-1",
                    capture_amount_cents=1250)
        assert resolve_state(j) == State.S10a_CAPTURED_FULL

    def test_captured_split(self):
        j = FakeJob(status="CAPTURED", capture_id="CAP-1",
                    capture_amount_cents=800, voided_amount_cents=450)
        assert resolve_state(j) == State.S10b_CAPTURED_SPLIT

    def test_captured_unaudited_force_settled(self):
        j = FakeJob(status="CAPTURED", capture_id="CAP-1",
                    settlement_method="force_settled")
        assert resolve_state(j) == State.S10c_CAPTURED_UNAUDITED

    def test_captured_unaudited_stale_wedge(self):
        # Wedge flag still set on a settled job → audit incomplete.
        j = FakeJob(status="CAPTURED", capture_id="CAP-1",
                    needs_review=True)
        assert resolve_state(j) == State.S10c_CAPTURED_UNAUDITED

    def test_captured_unaudited_amount_only(self):
        # Amount on the books, no capture id → unauditable.
        j = FakeJob(status="CAPTURED", capture_amount_cents=1250)
        assert resolve_state(j) == State.S10c_CAPTURED_UNAUDITED

    def test_terminal_dominates_stale_flags(self):
        # A settled job is settled regardless of stale pending intent.
        j = FakeJob(status="CAPTURED", capture_id="CAP-1",
                    capture_amount_cents=1250,
                    pending_capture_request_id="stale-key")
        assert resolve_state(j) == State.S10a_CAPTURED_FULL

    def test_voided(self):
        assert resolve_state(FakeJob(status="VOIDED")) == State.S11_VOIDED_SETTLED

    def test_unknown_status_raises(self):
        with pytest.raises(UnhandledTransition):
            resolve_state(FakeJob(status="BOGUS"))

    def test_waterfall_is_total_over_flag_space(self):
        """Every combination of the independent booleans resolves to
        exactly one state, a documented impossible-state assertion,
        or a loud data-corruption signal."""
        count_resolved = 0
        count_impossible = 0
        count_corrupt = 0
        for status in ("QUOTED", "AUTHORIZED", "DELIVERED"):
            for needs_review in (False, True):
                for pending in (None, "k"):
                    for cap_id in ("", "CAP"):
                        for attempted in (False, True):
                            for order in ("", "ORD"):
                                j = FakeJob(
                                    status=status, needs_review=needs_review,
                                    pending_capture_request_id=pending,
                                    capture_id=cap_id,
                                    authorize_attempted=attempted,
                                    order_id=order,
                                )
                                try:
                                    s = resolve_state(j)
                                    assert isinstance(s, State)
                                    count_resolved += 1
                                except UnhandledTransition:
                                    # Two kinds of loud failure:
                                    # - Impossible combos (§3.5): AUTHORIZED or QUOTED
                                    #   with pending/recorded capture (needs_review=False).
                                    # - Corrupt combos: QUOTED + needs_review (wedge
                                    #   undefined for QUOTED).
                                    if status == "QUOTED" and needs_review:
                                        count_corrupt += 1
                                    else:
                                        # Documented impossible combos (§3.5).
                                        count_impossible += 1
        assert count_resolved > 0
        # Impossible combos (§3.5 — a capture intent/record cannot exist
        # before delivery; the wedge check in step 2 catches the
        # needs_review=True cases first, so these all have needs_review=False):
        #   AUTHORIZED + pending: 1 × 1 × 1(pending) × 2(cap) × 2(att) × 2(ord) = 8
        #   AUTHORIZED + recorded: 1 × 1 × 1(no pend) × 1(cap) × 2 × 2 = 4
        #   QUOTED + pending:     1 × 1 × 1(pending) × 2(cap) × 2(att) × 2(ord) = 8
        #   QUOTED + recorded:    1 × 1 × 1(no pend) × 1(cap) × 2 × 2 = 4
        assert count_impossible == 24
        # Corrupt combos: QUOTED + needs_review (wedge undefined for QUOTED):
        #   1(status) × 1(wedge) × 2(pending) × 2(cap) × 2(att) × 2(ord) = 16
        assert count_corrupt == 16
        assert count_resolved + count_impossible + count_corrupt == 96

# ---------------------------------------------------------------------------
# 2. make_intent_key()
# ---------------------------------------------------------------------------

class TestMakeIntentKey:
    def test_deterministic(self):
        assert (make_intent_key("j1", "capture", 1)
                == make_intent_key("j1", "capture", 1))

    def test_attempt_uniqueness(self):
        # Edge case #3: different attempts MUST produce different keys.
        k1 = make_intent_key("j1", "capture", 1)
        k2 = make_intent_key("j1", "capture", 2)
        assert k1 != k2

    def test_job_uniqueness(self):
        assert (make_intent_key("j1", "capture", 1)
                != make_intent_key("j2", "capture", 1))

    def test_kind_uniqueness(self):
        assert (make_intent_key("j1", "capture", 1)
                != make_intent_key("j1", "authorize", 1))

    def test_format(self):
        k = make_intent_key("j1", "capture", 1)
        assert len(k) == 64
        assert all(c in "0123456789abcdef" for c in k)

    def test_empty_job_id_rejected(self):
        with pytest.raises(ValueError, match="job_id"):
            make_intent_key("", "capture", 1)


# ---------------------------------------------------------------------------
# 3. transition() skeleton
# ---------------------------------------------------------------------------

class TestTransitionSkeleton:
    def test_smoke_cell_create_order(self):
        new_state, actions = transition(State.S1_QUOTED_FRESH,
                                       Event.E_CREATE_ORDER)
        assert new_state == State.S2_QUOTED_ORDERED
        assert len(actions) == 1
        assert isinstance(actions[0], NoOp)

    def test_s1_webhook_defensives_are_explicit_noops(self):
        # Edge case #1: unreachable per §4.7, kept as explicit NoOp (I5).
        for ev in (Event.WH_AUTH_CREATED, Event.WH_AUTH_VOIDED,
                   Event.WH_CAP_COMPLETED, Event.WH_CAP_DENIED):
            new_state, actions = transition(State.S1_QUOTED_FRESH, ev)
            assert new_state == State.S1_QUOTED_FRESH
            assert len(actions) == 1
            assert isinstance(actions[0], NoOp)
            assert actions[0].note != ""

    def test_unlisted_pair_raises(self):
        with pytest.raises(UnhandledTransition):
            transition(State.S4_AUTHORIZED_LIVE, Event.E_CREATE_ORDER)

    def test_unhandled_carries_state_and_event(self):
        # (S6, E_START_AUTHORIZE): a QUOTED-phase event in DELIVERED —
        # no cell, so UnhandledTransition (matrix defect, loud per I5).
        try:
            transition(State.S6_DELIVERED_OPEN, Event.E_START_AUTHORIZE)
        except UnhandledTransition as e:
            assert e.state == State.S6_DELIVERED_OPEN
            assert e.event == Event.E_START_AUTHORIZE
        else:
            pytest.fail("expected UnhandledTransition")

    def test_transition_never_returns_none(self):
        # I5: every handled cell returns (state, actions) — never None.
        # Phase 3: cells may raise GuardRejection for missing guard evidence
        # (e.g. E_VOID_DEADLINE without deadline_passed=True). That's an
        # explicit outcome, not silent — acceptable. UnhandledTransition
        # for a TABLE cell would be a bug.
        from app.dispatcher import _SKELETON_CELLS
        assert len(_SKELETON_CELLS) > 0
        for (s, e) in _SKELETON_CELLS:
            try:
                new_state, actions = transition(s, e)
            except GuardRejection:
                continue  # explicit guard refusal — not silent
            except ValueError:
                # Missing required evidence (e.g. amount_cents for
                # SaveIntent) — loud failure, not silent. Acceptable.
                continue
            assert isinstance(new_state, State)
            assert isinstance(actions, list)
            assert len(actions) > 0

    def test_routing_layer_webhooks_flagged(self):
        # §4.7 events must never reach transition() — the enum flags them.
        for ev in (Event.WH_NO_JOB, Event.WH_NO_AUTH_ID,
                   Event.WH_UNKNOWN_TYPE, Event.WH_JOB_SETTLED,
                   Event.WH_DUPLICATE, Event.WH_READ_FAIL):
            assert ev.reaches_dispatcher is False
        for ev in (Event.WH_AUTH_CREATED, Event.WH_AUTH_VOIDED,
                   Event.WH_CAP_COMPLETED, Event.WH_CAP_DENIED):
            assert ev.reaches_dispatcher is True


# ---------------------------------------------------------------------------
# 4. GuardRejection vs UnhandledTransition (I5)
# ---------------------------------------------------------------------------

class TestExceptionSeparation:
    def test_distinct_types(self):
        assert GuardRejection is not UnhandledTransition
        assert not issubclass(GuardRejection, UnhandledTransition)
        assert not issubclass(UnhandledTransition, GuardRejection)

    def test_distinct_http_codes(self):
        assert UnhandledTransition.http_status == 500
        g = GuardRejection("non-payer called capture")
        assert g.category == "forbidden"
        assert g.category != "gone"

    def test_guard_rejection_custom_status(self):
        g = GuardRejection("deadline passed", category="gone")
        assert g.category == "gone"
        assert "deadline passed" in str(g)

    def test_unhandled_logs_and_names_pair(self):
        with pytest.raises(UnhandledTransition) as ei:
            raise UnhandledTransition(State.S9_DELIVERED_WEDGED,
                                      Event.E_SUBMIT_DELIVERY)
        assert "S9_DELIVERED_WEDGED" in str(ei.value)
        assert "E_SUBMIT_DELIVERY" in str(ei.value)


# ---------------------------------------------------------------------------
# 5. State / Event / Action taxonomy sanity
# ---------------------------------------------------------------------------

class TestTaxonomy:
    def test_thirteen_states(self):
        assert len(State) == 13

    def test_terminal_states(self):
        assert State.S10a_CAPTURED_FULL.is_terminal
        assert State.S10b_CAPTURED_SPLIT.is_terminal
        assert State.S10c_CAPTURED_UNAUDITED.is_terminal
        assert State.S11_VOIDED_SETTLED.is_terminal
        assert not State.S1_QUOTED_FRESH.is_terminal
        assert not State.S8_DELIVERED_PARTIAL_RECORDED.is_terminal

    def test_wedged_states(self):
        assert State.S5_AUTHORIZED_WEDGED.is_wedged
        assert State.S9_DELIVERED_WEDGED.is_wedged
        assert not State.S4_AUTHORIZED_LIVE.is_wedged

    def test_forty_nine_events(self):
        # Meta-review: E_UNAUDITED_CAPTURE added (explicit unaudited
        # path) → 50 events.
        assert len(Event) == 50

    def test_start_residual_void_present(self):
        # Rev 3 (Gemini Defect 2): the two-phase residual-void intent event.
        assert Event.E_START_RESIDUAL_VOID.value == "E_START_RESIDUAL_VOID"

    def test_r_void_fail_present(self):
        # Used in the §5.8 table; noted as missing from §4.2's list.
        assert Event.R_VOID_FAIL.value == "R_VOID_FAIL"

    def test_actions_are_frozen_dataclasses(self):
        a = SaveIntent(key="k", amount_cents=100, kind="capture")
        with pytest.raises(Exception):
            a.key = "other"  # frozen
        r = RecordCapture(capture_ids=("CAP-1", "CAP-2"), amount_cents=500)
        assert set(r.capture_ids) == {"CAP-1", "CAP-2"}  # §6.4: set compare

    def test_record_capture_allows_none_amount_for_unknown(self):
        # Phase 2 (Method 1): None amount = unknown (malformed/absent
        # from PayPal). The ID is recorded, the amount stays unknown.
        # This mirrors legacy _record_capture_amount's "absent amount
        # leaves the books alone" rule (Claude finding 2).
        r = RecordCapture(capture_ids=("CAP-1",), amount_cents=None)
        assert r.capture_ids == ("CAP-1",)
        assert r.amount_cents is None

    def test_record_capture_allows_empty_ids_for_unaudited(self):
        # Fix 2 (new reviewer): empty capture IDs are allowed — they mean
        # an unaudited capture (PayPal COMPLETED with no id). Handlers
        # route empty IDs to S10c_CAPTURED_UNAUDITED explicitly.
        r = RecordCapture(capture_ids=(), amount_cents=1000)
        assert r.capture_ids == ()
        assert r.amount_cents == 1000

    def test_save_intent_rejects_none_amount(self):
        # Gemini Phase 3 review: intent without amount breaks recovery —
        # EXCEPT kind="capture" (full capture), where None is the legacy
        # convention (refined Fix 1).
        with pytest.raises(ValueError, match="amount_cents must not be None"):
            SaveIntent(key="k", amount_cents=None, kind="capture_partial")
        # Full capture allows None.
        s = SaveIntent(key="k", amount_cents=None, kind="capture")
        assert s.amount_cents is None

    def test_zero_amount_is_valid(self):
        # 0 is a valid amount (not None) — truthiness trap guard.
        r = RecordCapture(capture_ids=("CAP-1",), amount_cents=0)
        assert r.amount_cents == 0
        s = SaveIntent(key="k", amount_cents=0, kind="capture")
        assert s.amount_cents == 0

    def test_string_capture_id_rejected(self):
        with pytest.raises(ValueError, match="capture_ids"):
            RecordCapture(capture_ids="CAP1", amount_cents=1000)

    def test_post_void_rejects_empty_authorization(self):
        with pytest.raises(ValueError, match="authorization_id"):
            PostVoid(authorization_id="")


class TestJobSnapshot:
    """JobSnapshot: strict typed view for resolve_state()."""

    def test_from_job_builds_snapshot(self):
        from app.dispatcher import JobSnapshot
        job = FakeJob(status="AUTHORIZED", order_id="ORD-1")
        snap = JobSnapshot.from_job(job)
        assert snap.status == "AUTHORIZED"
        assert snap.order_id == "ORD-1"
        assert snap.needs_review is False
        # resolve_state accepts the snapshot directly
        assert resolve_state(snap) == State.S4_AUTHORIZED_LIVE

    def test_from_job_missing_required_field_raises(self):
        from app.dispatcher import JobSnapshot
        class MalformedJob:
            # Missing: status, capture_id, etc.
            job_id = "bad-1"
        with pytest.raises(AttributeError):
            JobSnapshot.from_job(MalformedJob())

    def test_from_job_missing_status_raises(self):
        from app.dispatcher import JobSnapshot
        class NoStatusJob:
            job_id = "bad-2"
            capture_id = ""
            capture_amount_cents = None
            voided_amount_cents = None
            pending_capture_request_id = None
            order_id = ""
        with pytest.raises(AttributeError):
            JobSnapshot.from_job(NoStatusJob())

    def test_snapshot_is_frozen(self):
        from app.dispatcher import JobSnapshot
        snap = JobSnapshot(job_id="j1", status="QUOTED")
        with pytest.raises(Exception):
            snap.status = "AUTHORIZED"  # frozen

    def test_resolve_state_backward_compat_duck_typed(self):
        # Plain duck-typed objects still work (converted internally).
        assert resolve_state(FakeJob(status="DELIVERED")) == State.S6_DELIVERED_OPEN


# ---------------------------------------------------------------------------
# Phase 3 hardening — Gemini review additions (2026-11-01)
# ---------------------------------------------------------------------------

class TestZeroValueAmounts:
    """Gemini Phase 2 review: truthiness trap.

    resolve_state() uses `is not None` (not truthiness) to distinguish
    S10a (full capture) from S10b (split). A zero-value capture or void
    must not be dropped by a `if amount:` check. These tests lock in
    the correct behavior with explicit 0 values.
    """

    def test_zero_capture_amount_resolves_to_s10a(self):
        # Full capture of 0 cents is still a full capture (S10a),
        # not a split (S10b). The `is not None` check must hold.
        job = FakeJob(
            status="CAPTURED",
            capture_id="CAP-0",
            capture_amount_cents=0,
            voided_amount_cents=None,
        )
        assert resolve_state(job) == State.S10a_CAPTURED_FULL

    def test_zero_void_amount_resolves_to_s10b(self):
        # Split with 0 voided cents is still a split (S10b).
        # `voided_amount_cents=0` is not None → S10b, not S10a.
        job = FakeJob(
            status="CAPTURED",
            capture_id="CAP-1",
            capture_amount_cents=500,
            voided_amount_cents=0,
        )
        assert resolve_state(job) == State.S10b_CAPTURED_SPLIT

    def test_zero_capture_amount_not_dropped(self):
        # Ensure no truthiness check drops a 0-cent capture.
        # If resolve_state used `if job.capture_amount_cents:`
        # instead of `is not None`, this would misroute.
        job = FakeJob(
            status="CAPTURED",
            capture_id="CAP-1",
            capture_amount_cents=0,
            voided_amount_cents=None,
        )
        state = resolve_state(job)
        # Must be S10a (has capture_id, no void) — not S10c or an error.
        assert state == State.S10a_CAPTURED_FULL


class TestIntentSecretConfig:
    """Secret must be set — no insecure fallback (financial code).

    If DISPATCHER_INTENT_SECRET is unset, make_intent_key() raises
    RuntimeError (loud) instead of silently using a dev default.
    validate_config() lets callers fail fast at startup.
    """

    def test_missing_secret_raises_runtime_error(self, monkeypatch):
        import app.dispatcher as disp

        monkeypatch.delenv("DISPATCHER_INTENT_SECRET", raising=False)

        with pytest.raises(RuntimeError, match="DISPATCHER_INTENT_SECRET"):
            make_intent_key("job-1", "capture", 1)

    def test_validate_config_raises_when_unset(self, monkeypatch):
        from app.dispatcher import validate_config

        monkeypatch.delenv("DISPATCHER_INTENT_SECRET", raising=False)
        with pytest.raises(RuntimeError, match="DISPATCHER_INTENT_SECRET"):
            validate_config()

    def test_validate_config_passes_when_set(self, monkeypatch):
        from app.dispatcher import validate_config

        monkeypatch.setenv("DISPATCHER_INTENT_SECRET", "test-secret-123")
        validate_config()  # must not raise

    def test_env_secret_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("DISPATCHER_INTENT_SECRET", "test-secret-123")
        key1 = make_intent_key("job-1", "capture", 1)

        monkeypatch.setenv("DISPATCHER_INTENT_SECRET", "different-secret-456")
        key2 = make_intent_key("job-1", "capture", 1)

        # Different secrets → different keys
        assert key1 != key2



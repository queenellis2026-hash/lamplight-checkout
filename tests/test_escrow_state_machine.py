"""Phase 1: Property-based state machine testing of EscrowService.

A Hypothesis RuleBasedStateMachine drives the public EscrowService API
through random operation sequences against the in-process FakePayPalClient
(tests/fake_paypal.py) and asserts money invariants after every step.

The 7 invariants (from the game plan):
  1. Conservation: captured + voided <= authorized, per job.
  2. No negative money: amounts are non-negative int cents, never float.
  3. N retries = one effect: the fake's call record shows at most one
     executed (non-replay) PSP call per idempotency key.
  4. Terminal sinks: CAPTURED/VOIDED never transition out.
  5. Dispute/pause integrity: the v8.13/v8.14 lessons encoded as law.
  6. Legal transitions: fsm_divergence_count() stays 0.
  7. Audit completeness: every state change produces an audit row.

Time model: self.clock starts at real time.time(). svc._now() is
overridden to return it. Rules that accept a `now` hook get
int(self.clock). The advance_time rule moves the clock forward
(1s..96h), driving dispute deadlines, honor windows, and quote/job
deadlines deterministically.

Run: pytest tests/test_escrow_state_machine.py
Full: 100 examples (~3-8 min). Smoke: 25 examples (CI).
"""
import pathlib
import sys
import time

import pytest
from hypothesis import assume, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.escrow import (
    EscrowError,
    EscrowService,
    fsm_divergence_count,
    reset_fsm_divergences,
)
from app.quote import new_quote
from app.store import MemoryStore
from tests.fake_paypal import FakePayPalClient

PAYER = "payer@sm.example"
AGENT = "agent@sm.example"

TERMINAL = ("CAPTURED", "VOIDED")
# v8.14: honor = 3 days from authorization, minus 1h margin.
HONOR_PERIOD = 3 * 24 * 3600
HONOR_MARGIN = 3600

REASONS = st.sampled_from([
    "need more time to review",
    "delivery looks incomplete",
    "want to double-check the work",
    "pause for inspection",
])


class EscrowMachine(RuleBasedStateMachine):
    """Drives EscrowService through random operation sequences."""

    jobs = Bundle("jobs")

    def __init__(self):
        super().__init__()
        self.paypal = FakePayPalClient()
        self.store = MemoryStore()
        self.svc = EscrowService(self.paypal, store=self.store)
        # Controllable clock, starting at real now so Job.created_at /
        # authorized_at (which use time.time()) stay consistent.
        self.clock = time.time()
        self.svc._now = lambda: self.clock
        # Per-job tracking for invariants.
        self.texts: dict[str, str] = {}
        self.prices: dict[str, int] = {}
        self.prev_status: dict[str, str] = {}
        self.prev_review: dict[str, bool] = {}
        self.prev_audit_n: dict[str, int] = {}
        reset_fsm_divergences()

    # -- helpers ------------------------------------------------------
    def _now_i(self) -> int:
        return int(self.clock)

    def _get(self, job_id):
        return self.store.get(job_id)

    def _audit_n(self, job_id) -> int:
        return len(self.store.audit_events(job_id=job_id))

    def _sync_tracking(self, job_id):
        """Refresh per-job tracking after a rule ran."""
        job = self._get(job_id)
        if job is None:
            return
        self.prev_status[job_id] = job.status
        self.prev_review[job_id] = job.needs_review
        self.prev_audit_n[job_id] = self._audit_n(job_id)

    @initialize(target=jobs)
    def init_job(self):
        """Every example starts with one job driven to DELIVERED.

        Without this, random rule selection rarely assembles the
        create->order->authorize->deliver chain, and most steps are
        no-ops. The initialize rule runs once per example, giving the
        machine interesting state to mutate from step one.
        """
        quote = new_quote(service="sm", price_cents=5000,
                          pay_to="agent@sm.example",
                          ttl_seconds=30 * 24 * 3600)
        job = self.svc.create_job(quote=quote, payer=PAYER, agent=AGENT,
                                  deadline_seconds=3600)
        self.prices[job.job_id] = 5000
        order = self.svc.create_order(job)
        self.paypal.approve_order(order["id"])
        self.svc.authorize(self.svc.store.get(job.job_id))
        text = "init delivery"
        self.svc.submit_delivery(self.svc.store.get(job.job_id), agent=AGENT,
                                 delivery_text=text, now=self._now_i())
        self.texts[job.job_id] = text
        self._sync_tracking(job.job_id)
        return job.job_id

    # -- rules ----------------------------------------------------------
    @rule(target=jobs, price=st.integers(min_value=100, max_value=100_000))
    def create(self, price):
        quote = new_quote(service="sm", price_cents=price,
                          pay_to="agent@sm.example",
                          ttl_seconds=30 * 24 * 3600)
        job = self.svc.create_job(quote=quote, payer=PAYER, agent=AGENT,
                                  deadline_seconds=120)
        self.prices[job.job_id] = price
        self._sync_tracking(job.job_id)
        return job.job_id

    @rule(job_id=jobs)
    def do_order(self, job_id):
        job = self._get(job_id)
        if job is None or job.status != "QUOTED" or job.order_id:
            return
        order = self.svc.create_order(job)
        self.paypal.approve_order(order["id"])
        self._sync_tracking(job_id)

    @rule(job_id=jobs)
    def do_authorize(self, job_id):
        job = self._get(job_id)
        if job is None or job.status != "QUOTED" or not job.order_id:
            return
        self.svc.authorize(job)
        self._sync_tracking(job_id)

    @rule(job_id=jobs, text=st.text(min_size=1, max_size=200))
    def do_deliver(self, job_id, text):
        job = self._get(job_id)
        if job is None or job.status != "AUTHORIZED":
            return
        if self._now_i() >= job.deadline:
            return  # delivery at/after deadline is refused
        try:
            self.svc.submit_delivery(job, agent=AGENT, delivery_text=text,
                                     now=self._now_i())
        except EscrowError:
            return  # guard refused; precondition not met
        self.texts[job_id] = text
        self._sync_tracking(job_id)

    @rule(job_id=jobs)
    def do_capture(self, job_id):
        job = self._get(job_id)
        if job is None or job.status != "DELIVERED":
            return
        if job.needs_review:
            return  # paused; capture is refused
        text = self.texts.get(job_id)
        if text is None:
            return
        try:
            self.svc.capture(job, payer=PAYER, delivery_text=text)
        except EscrowError:
            return
        self._sync_tracking(job_id)

    @rule(job_id=jobs,
          fraction=st.floats(min_value=0.1, max_value=0.9))
    def do_capture_partial(self, job_id, fraction):
        job = self._get(job_id)
        if job is None or job.status != "DELIVERED":
            return
        if job.needs_review:
            return
        text = self.texts.get(job_id)
        if text is None:
            return
        hold = self.prices.get(job_id, 0)
        amount = max(1, int(hold * fraction))
        if amount >= hold:
            return  # degenerate to full capture; skip
        try:
            self.svc.capture_partial(job, payer=PAYER, delivery_text=text,
                                     amount_cents=amount)
        except EscrowError:
            return
        self._sync_tracking(job_id)

    @rule(job_id=jobs)
    def do_void(self, job_id):
        job = self._get(job_id)
        if job is None or job.status not in ("AUTHORIZED", "DELIVERED"):
            return
        if self._now_i() <= job.deadline:
            return  # void only after the deadline
        try:
            self.svc.void_after_deadline(job, payer=PAYER,
                                         now=self._now_i())
        except EscrowError:
            return
        self._sync_tracking(job_id)

    @rule(job_id=jobs, reason=REASONS)
    def do_dispute(self, job_id, reason):
        job = self._get(job_id)
        if job is None or job.status != "DELIVERED":
            return
        if job.needs_review:
            # The 409: a second pause while the flag is set must raise,
            # and the deadline must not move.
            before = job.dispute_deadline
            with pytest.raises(EscrowError, match="already under review"):
                self.svc.dispute_job(job, payer=PAYER, reason=reason,
                                     now=self._now_i())
            after = self._get(job_id).dispute_deadline
            assert before == after, "deadline moved on refused re-dispute"
            return
        try:
            self.svc.dispute_job(job, payer=PAYER, reason=reason,
                                 now=self._now_i())
        except EscrowError:
            # Honor refused (fail-closed) or other guard.
            pass
        self._sync_tracking(job_id)

    @rule(job_id=jobs)
    def do_force_settle(self, job_id):
        job = self._get(job_id)
        if job is None or not job.needs_review:
            return
        # v8.14 fix (Nov 2026): force_settle on an active payer pause
        # now CAPTURES (pays the worker) per the documented
        # "force_settle pays the worker now". The machine exercises
        # it and the FSM tracking validates the transition.
        try:
            self.svc.force_settle(job, payer=PAYER)
        except EscrowError:
            return
        self._sync_tracking(job_id)

    @rule()
    def do_sweep(self):
        # The sweep drives expired pauses and inspection deadlines.
        # It may capture, so it can change job state.
        before = {j.job_id: (j.status, j.needs_review)
                  for j in self.store.all()}
        self.svc.check_inspection_deadlines(now=self._now_i())
        for job_id in before:
            self._sync_tracking(job_id)

    @rule(job_id=jobs)
    def do_reconcile(self, job_id):
        job = self._get(job_id)
        if job is None or job.status not in ("AUTHORIZED", "DELIVERED"):
            return
        try:
            self.svc.reconcile_job(job)
        except EscrowError:
            return
        self._sync_tracking(job_id)

    @rule(delta=st.integers(min_value=1, max_value=345_600))
    def advance_time(self, delta):
        # 1s .. 96h. Drives deadlines, honor windows, dispute windows.
        self.clock += delta

    # -- invariants (checked after every step) --------------------------
    @invariant()
    def money_conservation(self):
        """I1: captured + voided <= authorized, per job."""
        for job in self.store.all():
            authorized = (job.quote or {}).get("price_cents", 0)
            captured = job.capture_amount_cents or 0
            voided = job.voided_amount_cents or 0
            assert captured + voided <= authorized, (
                f"job {job.job_id}: captured({captured}) + voided({voided}) "
                f"> authorized({authorized})")

    @invariant()
    def no_negative_money(self):
        """I2: amounts are non-negative int cents, never float."""
        for job in self.store.all():
            for field in ("capture_amount_cents", "voided_amount_cents",
                          "pending_capture_amount_cents",
                          "needs_review_amount_cents"):
                v = getattr(job, field, None)
                if v is not None:
                    assert isinstance(v, int) and not isinstance(v, bool), (
                        f"job {job.job_id}.{field}={v!r} is not int cents")
                    assert v >= 0, (
                        f"job {job.job_id}.{field}={v} is negative")
            for i, a in enumerate(job.capture_amounts or []):
                if a is not None:
                    assert isinstance(a, int) and a >= 0

    @invariant()
    def retries_have_one_effect(self):
        """I3: at most one executed PSP call per idempotency key."""
        seen: dict[tuple[str, str | None], int] = {}
        for op, key, _fp, was_replay in self.paypal.calls:
            if not was_replay:
                seen[(op, key)] = seen.get((op, key), 0) + 1
        for (op, key), n in seen.items():
            assert n <= 1, (
                f"PSP {op} key={key} executed {n} times (not idempotent)")

    @invariant()
    def terminal_sinks(self):
        """I4: CAPTURED/VOIDED never transition out."""
        for job in self.store.all():
            prev = self.prev_status.get(job.job_id)
            if prev in TERMINAL:
                assert job.status == prev, (
                    f"job {job.job_id} left terminal state {prev} "
                    f"-> {job.status}")
            if job.status in TERMINAL:
                self.prev_status[job.job_id] = job.status

    @invariant()
    def pause_integrity(self):
        """I5: the v8.13/v8.14 lessons, encoded as law."""
        now = self._now_i()
        for job in self.store.all():
            # needs_review only on open jobs, never terminal.
            if job.needs_review:
                assert job.status in ("AUTHORIZED", "DELIVERED"), (
                    f"job {job.job_id}: needs_review on {job.status}")
            # Dispute deadline clamped to the honor window.
            if job.dispute_deadline is not None and \
                    job.authorized_at is not None:
                honor_limit = job.authorized_at + HONOR_PERIOD - HONOR_MARGIN
                assert job.dispute_deadline <= honor_limit, (
                    f"job {job.job_id}: dispute_deadline "
                    f"{job.dispute_deadline} > honor_limit {honor_limit}")
            # The flag clears only with a terminal capture in the same
            # write: if the flag went True->False, the job must now be
            # terminal CAPTURED (the v8.14 clear-then-capture fix).
            was = self.prev_review.get(job.job_id)
            if was is True and job.needs_review is False:
                assert job.status == "CAPTURED", (
                    f"job {job.job_id}: needs_review cleared without "
                    f"terminal capture (status={job.status})")

    @invariant()
    def no_fsm_divergence(self):
        """I6: the service never disagrees with the dispatcher table."""
        assert fsm_divergence_count() == 0, "FSM divergence detected"

    @invariant()
    def audit_completeness(self):
        """I7: every state change produces an audit row."""
        for job in self.store.all():
            prev = self.prev_status.get(job.job_id)
            if prev is not None and prev != job.status:
                n = self._audit_n(job.job_id)
                assert n > self.prev_audit_n.get(job.job_id, 0), (
                    f"job {job.job_id}: {prev} -> {job.status} "
                    f"with no new audit row")


# -- test entry points ---------------------------------------------------
# Each needs its own machine subclass: RuleBasedStateMachine.TestCase is
# a single class per machine, so two settings need two machines.


class EscrowMachineFull(EscrowMachine):
    """100-example nightly run."""
    pass


TestEscrowStateMachineFull = EscrowMachineFull.TestCase
TestEscrowStateMachineFull.settings = settings(
    max_examples=100, deadline=None, stateful_step_count=15)


class EscrowMachineSmoke(EscrowMachine):
    """25-example CI smoke run."""
    pass


TestEscrowStateMachineSmoke = EscrowMachineSmoke.TestCase
TestEscrowStateMachineSmoke.settings = settings(
    max_examples=25, deadline=None, stateful_step_count=10)

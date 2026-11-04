"""v7.2 regression tests for the issues found in the v7.1 review.

1. The USE_DISPATCHER flag must actually change which code path runs
   (v7.1: all tests passed identically with the flag ON and OFF).
2. FSM/legacy divergence must be reported loudly and counted.
3. force-settle's 403 message must not ship a literal "<redacted>".
4. ttl_seconds / deadline_seconds are bounded.
"""
import logging

import pytest

import app.escrow as escrow_mod
from app.auth import DEMO_PAYER_TOKEN
from app.escrow import EscrowService
from app.quote import new_quote
from app.server import MAX_SECONDS, app as flask_app


# ---------------------------------------------------------------- 1. flag canary
class _NoPayPal:
    """Any PayPal call from the legacy path raises AttributeError; the
    canary only cares which branch was *entered*."""


# public method -> private dispatcher implementation it must route to
ROUTES = {
    "create_order": ("_create_order_via_dispatcher", lambda s, j: s.create_order(j)),
    "authorize": ("_authorize_via_dispatcher", lambda s, j: s.authorize(j)),
    "submit_delivery": ("_submit_delivery_via_dispatcher",
                        lambda s, j: s.submit_delivery(j, agent=j.agent, delivery_text="x")),
    "capture": ("_capture_via_dispatcher",
                lambda s, j: s.capture(j, payer=j.payer, delivery_text="x")),
    "capture_partial": ("_capture_partial_via_dispatcher",
                        lambda s, j: s.capture_partial(j, payer=j.payer,
                                                       delivery_text="x", amount_cents=1)),
    "reconcile_job": ("_reconcile_job_via_dispatcher", lambda s, j: s.reconcile_job(j)),
    "void_after_deadline": ("_void_after_deadline_via_dispatcher",
                            lambda s, j: s.void_after_deadline(j, payer=j.payer)),
    "force_settle": ("_force_settle_legacy_with_fsm_tracking",
                     lambda s, j: s.force_settle(j, payer=j.payer)),
}


def _svc_and_job():
    svc = EscrowService(_NoPayPal())
    job = svc.create_job(
        quote=new_quote(service="audit", price_cents=1250, pay_to="agent@example"),
        payer="payer@example", agent="agent@example", deadline_seconds=3600)
    return svc, job


@pytest.mark.parametrize("flag", [True, False])
@pytest.mark.parametrize("method", sorted(ROUTES))
def test_flag_selects_code_path(monkeypatch, method, flag):
    impl_name, call = ROUTES[method]
    entered = []
    monkeypatch.setattr(EscrowService, impl_name,
                        lambda self, *a, **k: entered.append(impl_name) or {})
    monkeypatch.setattr(escrow_mod, "USE_DISPATCHER", flag)
    svc, job = _svc_and_job()
    try:
        call(svc, job)
    except Exception:  # legacy path may refuse/raise on the stub; irrelevant
        pass
    assert (impl_name in entered) is flag, (
        f"{method}: USE_DISPATCHER={flag} but dispatcher path "
        f"{'was' if entered else 'was not'} entered")


# ------------------------------------------------------- 2. divergence reporting
def test_divergence_is_counted_and_logged_at_error():
    records = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(record)

    h = _H()
    lg = logging.getLogger(escrow_mod.__name__)
    lg.addHandler(h)
    escrow_mod.reset_fsm_divergences()
    try:
        escrow_mod._report_fsm_divergence("reconcile", "job-1", "legacy A, FSM B")
    finally:
        lg.removeHandler(h)
    assert escrow_mod.fsm_divergence_count() == 1
    assert [r.levelno for r in records] == [logging.ERROR]
    assert "job-1" in records[0].getMessage()
    escrow_mod.reset_fsm_divergences()
    assert escrow_mod.fsm_divergence_count() == 0


def test_shadow_tracker_reports_mismatch_without_raising(monkeypatch):
    """A tracker disagreement must be counted but never raise (legacy has
    already moved money)."""
    escrow_mod.reset_fsm_divergences()
    svc, job = _svc_and_job()
    import app.dispatcher as d
    # FSM claims a state that cannot match the real job.
    monkeypatch.setattr(d, "transition",
                        lambda *a, **k: (d.State.S11_VOIDED_SETTLED, []))
    svc._fsm_track_reconcile(job, d.resolve_state(job),
                             {"status": "VOIDED"}, None)
    assert escrow_mod.fsm_divergence_count() == 1
    escrow_mod.reset_fsm_divergences()


# ---------------------------------------------------------- 3. <redacted> message
def test_force_settle_mismatch_message_is_not_redacted_placeholder():
    flask_app.config["TESTING"] = True
    c = flask_app.test_client()
    r = c.post("/jobs/any/force-settle",
               headers={"Authorization": f"Bearer {DEMO_PAYER_TOKEN}"},
               json={"payer": "someone-else@example"})
    assert r.status_code == 403
    msg = r.get_json()["error"]
    assert "<redacted>" not in msg
    assert msg.endswith("bearer token")


# --------------------------------------------------------------- 4. bounded ttl
@pytest.mark.parametrize("field", ["ttl_seconds", "deadline_seconds"])
def test_seconds_fields_are_bounded(field):
    flask_app.config["TESTING"] = True
    c = flask_app.test_client()
    r = c.post("/jobs", json={"brief": "audit the holders, budget $50",
                              field: MAX_SECONDS + 1})
    assert r.status_code == 400
    assert field in r.get_json()["error"]
    r = c.post("/jobs", json={"brief": "audit the holders, budget $50",
                              field: 10**12})
    assert r.status_code == 400

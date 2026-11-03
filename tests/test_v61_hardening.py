"""v6.1 hardening tests. Self-contained: stubs the modules that are not part
of the consolidated bundle (paypal_client, quote) so it runs standalone.
Run: DISPATCHER_INTENT_SECRET=test-secret python -m pytest tests/test_v61_hardening.py -q
"""
import inspect
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone

import pytest


def _stub(name, **attrs):
    if name not in sys.modules:
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m


class _PayPalError(Exception):
    pass


_stub("app.paypal_client", PayPalClient=type("PayPalClient", (), {}),
      PayPalError=_PayPalError, amount_to_cents=lambda *a, **k: 0,
      PayPalConfigError=type("PayPalConfigError", (Exception,), {}))
_stub("app.quote", quote_expired=lambda *a, **k: False)

from app import dispatcher as d  # noqa: E402
from app import webhooks as wh  # noqa: E402
from app.dispatcher import Event, Evidence, GuardRejection, State, transition  # noqa: E402


# ---------------------------------------------------------------- webhooks
def _hdrs(sent_at):
    return {
        "PAYPAL-AUTH-ALGO": "SHA256withRSA",
        "PAYPAL-TRANSMISSION-SIG": "sig",
        "PAYPAL-CERT-URL": "https://api.paypal.com/cert.pem",
        "PAYPAL-TRANSMISSION-ID": "t-1",
        "PAYPAL-TRANSMISSION-TIME": sent_at.isoformat(),
    }


def _ok(delta):
    sent = datetime.now(timezone.utc) + delta
    return wh.verify_paypal_signature(_hdrs(sent), b"{}", "WH-1")


def test_recent_and_72h_redelivery_accepted():
    assert _ok(timedelta(seconds=-5))
    assert _ok(timedelta(hours=-71))


def test_older_than_72h_rejected():
    assert not _ok(timedelta(hours=-73))


def test_small_forward_clock_skew_tolerated():
    assert _ok(timedelta(seconds=120))


def test_future_dated_transmission_rejected():
    # v6.1: abs() used to accept timestamps up to 72h in the FUTURE.
    assert not _ok(timedelta(minutes=10))
    assert not _ok(timedelta(hours=48))


def test_non_paypal_cert_host_rejected():
    h = _hdrs(datetime.now(timezone.utc))
    h["PAYPAL-CERT-URL"] = "https://evil.example.com/paypal.com"
    assert not wh.verify_paypal_signature(h, b"{}", "WH-1")


# ------------------------------------------------------------------- store
def _job(job_id="j1"):
    from app.escrow import Job
    return Job(job_id=job_id, quote={"price_cents": 1000, "quote_id": "q"},
               payer="p", agent="a")


def test_store_sees_other_process_writes(tmp_path):
    from app.store import JsonFileStore
    path = tmp_path / "jobs.json"
    a = JsonFileStore(path)
    b = JsonFileStore(path)           # second "worker"
    a.save(_job("j1"))
    assert b.get("j1") is not None    # v6.1: stale before this fix
    assert [j.job_id for j in b.all()] == ["j1"]


def test_store_sees_other_process_deletes(tmp_path):
    from app.store import JsonFileStore
    path = tmp_path / "jobs.json"
    a, b = JsonFileStore(path), JsonFileStore(path)
    a.save(_job("j1"))
    assert b.get("j1") is not None
    a.delete("j1")
    assert b.get("j1") is None


def test_store_keeps_stable_objects_between_own_writes(tmp_path):
    from app.store import JsonFileStore
    s = JsonFileStore(tmp_path / "jobs.json")
    j = _job("j1")
    s.save(j)
    assert s.get("j1") is j
    assert s.get("j1") is s.get("j1")   # no re-read churn without a foreign write


def test_store_webhook_event_visibility_across_workers(tmp_path):
    from app.store import JsonFileStore
    path = tmp_path / "jobs.json"
    a, b = JsonFileStore(path), JsonFileStore(path)
    a.mark_webhook_event("evt-1")
    assert b.webhook_event_seen("evt-1")


# --------------------------------------------------------------- sweeper
def test_sweeper_thread_runs_and_survives_failures(monkeypatch):
    from app import sweeper
    calls = []

    def fake_sweep(service, threshold):
        calls.append(time.time())
        if len(calls) == 1:
            raise RuntimeError("boom")      # first pass fails; loop must continue
        return []

    monkeypatch.setattr(sweeper, "sweep_stale_intents", fake_sweep)
    t = sweeper.start_sweeper_thread(object(), 1)
    deadline = time.time() + 5
    while len(calls) < 2 and time.time() < deadline:
        time.sleep(0.05)
    t.stop_event.set()
    t.join(3)
    assert len(calls) >= 2 and not t.is_alive()


@pytest.mark.parametrize("bad", [0, -1, True, "5", None])
def test_sweeper_thread_rejects_bad_interval(bad):
    from app import sweeper
    with pytest.raises(ValueError):
        sweeper.start_sweeper_thread(object(), bad)


# ------------------------------------------------- escrow v6.1 pins
def test_grace_window_constant_and_injectable_clock():
    from app.escrow import EscrowService
    assert EscrowService.IN_FLIGHT_READ_LAG_GRACE_SECONDS == 15 * 60
    assert callable(EscrowService._now)


def _run_webhook(result):
    import json
    cache = wh.WebhookIdempotencyCache()
    body = json.dumps({"event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {
        "supplementary_data": {"related_ids": {"authorization_id": "AUTH-1"}}}}).encode()
    h = _hdrs(datetime.now(timezone.utc))

    class Svc:
        def find_job_by_resource_id(self, r): return object()
        def job_is_settled(self, j): return False
        # Grok v7.1: gateway now calls handle_webhook_event, not reconcile_job.
        # Map the old result dict to the new action format.
        def handle_webhook_event(self, event):
            if result.get("awaiting"):
                return {"action": "pending", "error": result.get("error", "")}
            return {"action": "reconciled", "error": result.get("error", "")}

    r = wh.handle_webhook(Svc(), h, body, "WH-1", cache, allow_unverified=True)
    return r["http_status"], cache.seen("t-1")


def test_gateway_503s_and_does_not_mark_dedupe_on_awaiting():
    status, marked = _run_webhook({"job_id": "j", "updated": False, "awaiting": True,
                                   "error": "awaiting PayPal's read to catch up"})
    assert status == 503 and marked is False


def test_escrow_grace_return_carries_nonempty_error():
    """Source pin for the v7 bug: the grace-window return must set a truthy
    error alongside awaiting=True."""
    import app.escrow as esc
    full = open(inspect.getsourcefile(esc)).read()
    src = full
    assert src.count("age < self.IN_FLIGHT_READ_LAG_GRACE_SECONDS") == 1
    k = src.index("age < self.IN_FLIGHT_READ_LAG_GRACE_SECONDS")
    window = src[k:k + 1200]
    end = window.index('"awaiting": True')
    block = window[:end]
    assert '"error": ""' not in block
    assert '"error":' in block


# ------------------------------------------------------------- dispatcher
def test_full_capture_intent_allows_none_amount_only_for_capture():
    d.SaveIntent(key="k", amount_cents=None, kind="capture")
    with pytest.raises(ValueError):
        d.SaveIntent(key="k", amount_cents=None, kind="capture_partial")


def test_start_capture_none_amount_is_full_capture():
    ev = Evidence(job_id="j", is_payer=True, delivery_verified=True, amount_cents=None)
    state, actions = transition(State.S6_DELIVERED_OPEN, Event.E_START_CAPTURE, ev)
    assert state == State.S7_DELIVERED_PENDING
    assert actions[0].kind == "capture" and actions[0].amount_cents is None


def test_partial_capture_requires_amount():
    ev = Evidence(job_id="j", is_payer=True, delivery_verified=True, amount_cents=None)
    with pytest.raises(GuardRejection):
        transition(State.S6_DELIVERED_OPEN, Event.E_START_CAPTURE_PARTIAL, ev)


def test_force_settle_void_is_two_phase():
    """ENUM_NONE on a wedged job arms the void but stays wedged; only
    R_VOID_OK reaches S11."""
    ev = Evidence(job_id="j", is_payer=True, readable=True, enum=Event.ENUM_NONE,
                  read=Event.READ_CREATED, authorization_id="AUTH-1")
    state, actions = transition(State.S5_AUTHORIZED_WEDGED, Event.E_FORCE_SETTLE, ev)
    assert state == State.S5_AUTHORIZED_WEDGED
    kinds = [type(a).__name__ for a in actions]
    assert kinds == ["SaveVoidIntent", "PostVoid"]
    ok = Evidence(job_id="j", authorization_id="AUTH-1", voided_amount_cents=1000)
    state, actions = transition(State.S5_AUTHORIZED_WEDGED, Event.R_VOID_OK, ok)
    assert state == State.S11_VOIDED_SETTLED
    assert any(isinstance(a, d.RecordVoid) and a.amount_cents == 1000 for a in actions)


def test_r_void_ok_without_amount_is_refused():
    with pytest.raises(GuardRejection):
        transition(State.S5_AUTHORIZED_WEDGED, Event.R_VOID_OK, Evidence(job_id="j"))


def test_released_authorization_reconcile_books_void_amount():
    # ENUM_NONE = the enumeration ran and proved no capture listed
    # (a bare Evidence with enum=None now means the enumeration itself
    # failed — v8 money-safety fix — which wedges, never voids).
    ev = Evidence(job_id="j", read=Event.READ_RELEASED, authorized_cents=2500,
                  authorization_id="AUTH-1", enum=Event.ENUM_NONE)
    state, actions = transition(State.S4_AUTHORIZED_LIVE, Event.E_RECONCILE, ev)
    assert state == State.S11_VOIDED_SETTLED
    assert any(isinstance(a, d.RecordVoid) and a.amount_cents == 2500 for a in actions)


def test_terminal_states_absorb_reconcile_and_reject_client_calls():
    for s in (State.S10a_CAPTURED_FULL, State.S10b_CAPTURED_SPLIT,
              State.S10c_CAPTURED_UNAUDITED, State.S11_VOIDED_SETTLED):
        assert transition(s, Event.E_RECONCILE, Evidence())[0] == s
        with pytest.raises(GuardRejection):
            transition(s, Event.E_START_CAPTURE, Evidence(is_payer=True))


def test_table_cells_are_total_or_explicitly_refused():
    """Every table cell either returns (State, actions) or raises one of the
    sanctioned exception types — never an unexpected error type — on empty
    evidence, and a returned state is in the cell's declared target set."""
    allowed = (GuardRejection, d.UnhandledTransition, ValueError)
    for (state, event), handler in d._TABLE.items():
        try:
            new_state, actions = transition(state, event, Evidence())
        except allowed:
            continue
        assert new_state in handler.target_states, (state, event, new_state)
        assert isinstance(actions, list) and actions
        if state.is_terminal:
            assert new_state == state


def test_resolve_state_terminal_precedence():
    S = d.JobSnapshot
    assert d.resolve_state(S(job_id="j", status="VOIDED")) == State.S11_VOIDED_SETTLED
    assert d.resolve_state(S(job_id="j", status="CAPTURED", capture_id="c",
                             capture_amount_cents=5)) == State.S10a_CAPTURED_FULL
    assert d.resolve_state(S(job_id="j", status="CAPTURED", capture_id="c",
                             capture_amount_cents=5,
                             voided_amount_cents=5)) == State.S10b_CAPTURED_SPLIT
    assert d.resolve_state(S(job_id="j", status="CAPTURED", capture_id="c",
                             capture_amount_cents=5,
                             settlement_method="force_settled")) == State.S10c_CAPTURED_UNAUDITED

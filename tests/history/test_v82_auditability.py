"""Item 6 (production-readiness): operational auditability.

Structured, correlated audit events for every financial operation, plus
a ledger-vs-PayPal reconciliation report. Each test fails before the
implementation (no app.audit module, no store methods, no hooks) and
passes after:

- an event is recorded per financial operation with the idempotency
  key attached,
- the reconciliation report flags a divergence between books and
  PayPal,
- wedge reasons are captured.
"""
import pytest

from app.audit import AuditLog
from app.paypal_client import PayPalError
from test_escrow_paypal import (EscrowError, EscrowService, _StubPayPal,
                                _quote, _stub_authorized_job)


def _events(svc, job):
    return AuditLog(svc.store).events_for_job(job.job_id)


def test_authorize_records_completed_event_with_idempotency_key():
    """authorize() records one completed event carrying the exact
    idempotency key it POSTed with."""
    stub = _StubPayPal()
    svc = EscrowService(stub)
    job = svc.create_job(quote=_quote(), payer="payer@example",
                         agent="agent@example", deadline_seconds=3600)
    svc.create_order(job)
    svc.authorize(job)
    auth_events = [e for e in _events(svc, job)
                   if e.operation == "authorize"]
    assert len(auth_events) == 1
    ev = auth_events[0]
    assert ev.outcome == "completed"
    assert ev.idempotency_key == (
        f"lamplight-authorize-{job.job_id}-{job.order_id}")
    assert ev.recorded["status"] == "AUTHORIZED"
    assert ev.paypal_reported["operation"] == "authorize"


def test_create_order_records_event():
    """create_order() records its own event (no idempotency key — the
    order POST carries none)."""
    stub = _StubPayPal()
    svc = EscrowService(stub)
    job = svc.create_job(quote=_quote(), payer="payer@example",
                         agent="agent@example", deadline_seconds=3600)
    svc.create_order(job)
    order_events = [e for e in _events(svc, job)
                    if e.operation == "create_order"]
    assert len(order_events) == 1
    assert order_events[0].outcome == "completed"


def test_failed_operation_records_failed_event_with_reason():
    """A refused operation records a failed event carrying the
    refusal reason."""
    stub = _StubPayPal()
    svc, job = _stub_authorized_job(stub)
    with pytest.raises(EscrowError):
        svc.create_order(job)  # order already exists — refused
    failed = [e for e in _events(svc, job)
              if e.operation == "create_order" and e.outcome == "failed"]
    assert len(failed) == 1
    assert "cannot create order in state AUTHORIZED" in failed[0].reason


def test_capture_records_completed_event():
    """A directly-observed capture records one completed event and the
    booked CAPTURED state."""
    stub = _StubPayPal()
    stub.capture_raises = False  # capture observed, no lost response
    svc, job = _stub_authorized_job(stub)
    svc.submit_delivery(job, agent="agent@example",
                        delivery_text="report v1")
    svc.capture(job, payer="payer@example", delivery_text="report v1")
    cap_events = [e for e in _events(svc, job)
                  if e.operation == "capture" and e.outcome == "completed"]
    assert len(cap_events) == 1
    assert cap_events[0].recorded["status"] == "CAPTURED"
    assert cap_events[0].recorded["capture_amount_cents"] == 1250


def test_lost_capture_response_records_retried_event_with_exact_key():
    """Lost-response capture: the replay records a retried event with
    the exact idempotency key PayPal saw, then the operation completes."""
    stub = _StubPayPal()  # default: applies server-side, response lost,
    # retry with the remembered key replays the original
    svc, job = _stub_authorized_job(stub)
    svc.submit_delivery(job, agent="agent@example",
                        delivery_text="report v1")
    result = svc.capture(job, payer="payer@example",
                         delivery_text="report v1")
    assert result["status"] == "COMPLETED"
    events = _events(svc, job)
    retried = [e for e in events if e.outcome == "retried"]
    assert len(retried) >= 1
    for e in retried:
        # The retried event names the exact key PayPal remembers —
        # replaying any other key would risk a second capture.
        assert e.idempotency_key in stub.seen_keys, e.idempotency_key
    completed = [e for e in events
                 if e.operation == "capture" and e.outcome == "completed"]
    assert len(completed) == 1


def test_abandoned_intent_records_event_with_declined_key():
    """A definitively declined key: the intent is abandoned and the
    abandoned event names the poisoned key."""
    stub = _StubPayPal()
    stub.capture_applies = False
    stub.capture_raises = False
    stub.capture_status = "DECLINED"
    svc, job = _stub_authorized_job(stub)
    svc.submit_delivery(job, agent="agent@example",
                        delivery_text="report v1")
    with pytest.raises(EscrowError):
        svc.capture(job, payer="payer@example", delivery_text="report v1")
    events = _events(svc, job)
    abandoned = [e for e in events if e.outcome == "abandoned"]
    assert len(abandoned) == 1
    assert abandoned[0].idempotency_key in stub.seen_keys
    assert "declined" in abandoned[0].reason


def test_wedge_records_reason():
    """A wedge records a wedged event carrying the operator-facing
    review reason: a non-terminal capture listed on the order refuses
    the void and wedges for a human."""
    stub = _StubPayPal()
    svc, job = _stub_authorized_job(stub)
    stub.orders["ORDER-STUB"]["purchase_units"] = [{
        "payments": {"captures": [{
            "id": "CAP-PENDING", "status": "PENDING",
            "amount": {"currency_code": "USD", "value": "12.50"}}]}}]
    with pytest.raises(EscrowError):
        svc.void_after_deadline(job, payer="payer@example",
                                now=job.deadline + 1)
    assert job.needs_review
    wedged = [e for e in _events(svc, job) if e.outcome == "wedged"]
    assert len(wedged) >= 1
    void_wedge = [e for e in wedged
                  if e.operation == "void_after_deadline"]
    assert len(void_wedge) == 1
    assert "non-terminal capture is listed" in void_wedge[0].reason
    assert void_wedge[0].recorded["needs_review"] is True


def test_reconcile_job_records_event():
    """reconcile_job() records its own event, so the periodic
    reconcile leaves an audit trail too."""
    stub = _StubPayPal()
    svc, job = _stub_authorized_job(stub)
    svc.reconcile_job(job)
    rec_events = [e for e in _events(svc, job)
                  if e.operation == "reconcile_job"]
    assert len(rec_events) == 1
    assert rec_events[0].outcome == "completed"


def test_reconciliation_report_flags_divergence():
    """Books record CAPTURE-BOOKS; PayPal lists CAPTURE-PAYPAL. The
    report flags the job divergent with both directions named."""
    stub = _StubPayPal()
    svc, job = _stub_authorized_job(stub)
    job.status = "CAPTURED"
    job.capture_id = "CAPTURE-BOOKS"
    job.capture_ids = ["CAPTURE-BOOKS"]
    job.capture_amount_cents = 1250
    svc.store.save(job)
    stub.auths["AUTH-STUB"] = {
        "id": "AUTH-STUB", "status": "CAPTURED",
        "captures": [{"id": "CAPTURE-PAYPAL", "status": "COMPLETED",
                      "amount": {"currency_code": "USD", "value": "12.50"}}]}
    report = svc.ledger_reconciliation_report(job.job_id)
    assert report["jobs_checked"] == 1
    assert report["divergent_count"] == 1
    entry = report["entries"][0]
    assert entry["divergent"] is True
    assert any("book_capture_not_listed_by_paypal: CAPTURE-BOOKS" in i
               for i in entry["issues"]), entry["issues"]
    assert any("paypal_capture_not_on_books: CAPTURE-PAYPAL" in i
               for i in entry["issues"]), entry["issues"]


def test_reconciliation_report_clean_when_books_agree():
    """Books and PayPal agree on the capture: not divergent, no
    issues."""
    stub = _StubPayPal()
    stub.capture_raises = False
    svc, job = _stub_authorized_job(stub)
    svc.submit_delivery(job, agent="agent@example",
                        delivery_text="report v1")
    svc.capture(job, payer="payer@example", delivery_text="report v1")
    assert job.status == "CAPTURED"
    report = svc.ledger_reconciliation_report(job.job_id)
    assert report["jobs_checked"] == 1
    assert report["divergent_count"] == 0
    entry = report["entries"][0]
    assert entry["divergent"] is False
    assert entry["issues"] == []
    assert entry["paypal_status"] == "CAPTURED"


def test_reconciliation_report_failed_read_is_issue_not_divergence():
    """A PayPal read failure is reported as an issue — absence of
    evidence is not evidence of divergence."""
    stub = _StubPayPal()
    stub.fail_get_authorization = True
    svc, job = _stub_authorized_job(stub)
    report = svc.ledger_reconciliation_report(job.job_id)
    assert report["jobs_checked"] == 1
    assert report["divergent_count"] == 0
    entry = report["entries"][0]
    assert entry["divergent"] is False
    assert any(i.startswith("paypal_read_failed") for i in entry["issues"])


def test_reconciliation_report_amount_mismatch():
    """Same capture id, different amounts on books vs PayPal: the
    report flags the amount mismatch."""
    stub = _StubPayPal()
    svc, job = _stub_authorized_job(stub)
    job.status = "CAPTURED"
    job.capture_id = "CAPTURE-STUB"
    job.capture_ids = ["CAPTURE-STUB"]
    job.capture_amount_cents = 1250
    svc.store.save(job)
    stub.auths["AUTH-STUB"] = {
        "id": "AUTH-STUB", "status": "CAPTURED",
        "captures": [{"id": "CAPTURE-STUB", "status": "COMPLETED",
                      "amount": {"currency_code": "USD", "value": "10.00"}}]}
    report = svc.ledger_reconciliation_report(job.job_id)
    entry = report["entries"][0]
    assert entry["divergent"] is True
    assert any(i.startswith("amount_mismatch") for i in entry["issues"]), \
        entry["issues"]


def test_audit_events_durable_in_sqlite(tmp_path):
    """Audit events survive a SqliteStore close/reopen with payloads
    intact."""
    from app.store import SqliteStore
    path = str(tmp_path / "audit.db")
    log = AuditLog(SqliteStore(path))
    log.record(job_id="J1", operation="authorize",
               idempotency_key="k1", outcome="completed",
               paypal_reported={"status": "CREATED"},
               recorded={"status": "AUTHORIZED"})
    log.record(job_id="J1", operation="capture", outcome="wedged",
               reason="operator review needed")
    reopened = AuditLog(SqliteStore(path)).events_for_job("J1")
    assert [(e.operation, e.outcome) for e in reopened] == [
        ("authorize", "completed"), ("capture", "wedged")]
    assert reopened[0].idempotency_key == "k1"
    assert reopened[0].paypal_reported == {"status": "CREATED"}
    assert reopened[0].recorded == {"status": "AUTHORIZED"}
    assert reopened[1].reason == "operator review needed"


def test_audit_events_persist_in_json_store(tmp_path):
    """Audit events survive a JsonFileStore close/reopen."""
    from app.store import JsonFileStore
    path = str(tmp_path / "jobs.json")
    AuditLog(JsonFileStore(path)).record(
        job_id="J2", operation="void_after_deadline", outcome="completed")
    reopened = AuditLog(JsonFileStore(path)).events_for_job("J2")
    assert len(reopened) == 1
    assert reopened[0].operation == "void_after_deadline"


def test_audit_event_outcome_validated():
    """A bad outcome string is a programming error and raises at
    event-build time."""
    from app.audit import AuditEvent
    with pytest.raises(ValueError):
        AuditEvent(event_id="x", ts=1, job_id="J", operation="op",
                   outcome="bogus")

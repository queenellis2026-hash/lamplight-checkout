"""Phase 3: programmable fault injection on top of the Phase 1 fake.

ChaosPayPalClient extends FakePayPalClient (tests/fake_paypal.py) with
deterministic, one-shot faults that fire at exact kill-windows from the
postmortems (deep dive §6):

  crash_after_accept  -- the fake ACCEPTS the call (state mutated,
                         idempotency response stored), THEN raises.
                         Simulates a process kill between the PSP call
                         and our DB write. The response is lost; the
                         money moved.
  slow_next           -- sleep N seconds, then proceed normally.
                         Simulates a slow PSP (not a timeout — the
                         service has no client-side timeout on the
                         direct call; the ambiguous-timeout case is
                         crash_after_accept with a 408 PayPalError).
  fail_next           -- inherited from the fake: raise before any
                         state change (transport/5xx/429). The PSP
                         never saw the request.

Faults are one-shot: each fires once, then disarms. No randomness in
the fault itself (randomness belongs in Phase 1's operation
generator). Thread-safe arming/disarming for the concurrent tests.

Helper:
  forget_idempotency_keys() -- PayPal drops idempotency records but
                         keeps money state. Simulates 24h key expiry
                         mid-retry.
"""
from __future__ import annotations

import threading
import time

from app.paypal_client import PayPalError
from tests.fake_paypal import FakePayPalClient


class ChaosPayPalClient(FakePayPalClient):
    """FakePayPalClient with programmable kill-window faults."""

    def __init__(self):
        super().__init__()
        self._chaos_guard = threading.Lock()
        # operations whose next EXECUTION is accepted, then crashed.
        self.crash_after_accept: set[str] = set()
        # the error raised after the accept (default: raw transport
        # crash, which the service does NOT catch as PayPalError).
        self.crash_error: BaseException = ConnectionError(
            "simulated crash after PSP accepted the call")
        # operation -> seconds to sleep before proceeding.
        self.slow_ops: dict[str, float] = {}

    # -- arming -------------------------------------------------------
    def crash_after(self, operation: str,
                    error: BaseException | None = None) -> None:
        """Accept the next `operation` call, then raise `error`."""
        with self._chaos_guard:
            self.crash_after_accept.add(operation)
            if error is not None:
                self.crash_error = error

    def slow_next(self, operation: str, seconds: float) -> None:
        """Sleep `seconds` before the next `operation` call, then proceed."""
        with self._chaos_guard:
            self.slow_ops[operation] = seconds

    def forget_idempotency_keys(self) -> None:
        """PayPal forgets idempotency records; money state is kept.

        Simulates key expiry mid-retry. A retry that blindly re-POSTs
        would double-execute; the service must read-first
        (verify-then-act) instead.
        """
        with self._chaos_guard:
            self._idempotency.clear()

    # -- fault engine ---------------------------------------------------
    # _chaos_armed() is called at the top of each overridden mutating
    # call: it atomically disarms one-shot faults and returns
    # (crash: bool, slow_seconds: float). The crash fires only when
    # the parent call RETURNED (the fake accepted: state mutated and
    # the idempotency response stored). If the parent raised (422,
    # 404, fail_next), the PSP never accepted anything — no crash.
    def _chaos_armed(self, operation: str) -> tuple[bool, float]:
        with self._chaos_guard:
            crash = operation in self.crash_after_accept
            if crash:
                self.crash_after_accept.discard(operation)
            slow = self.slow_ops.pop(operation, 0.0)
        return crash, slow

    # -- overridden mutating calls ---------------------------------------
    # NOTE: parent methods are invoked as FakePayPalClient.m(self, ...)
    # rather than via super() in a lambda — zero-arg super() has no
    # __class__ cell inside the nested lambda.
    def capture_authorization(self, authorization_id: str,
                              amount_cents: int | None = None,
                              currency: str = "USD",
                              request_id: str | None = None,
                              final_capture: bool = False) -> dict:
        crash, slow = self._chaos_armed("capture_authorization")
        if slow:
            time.sleep(slow)
        result = FakePayPalClient.capture_authorization(
            self, authorization_id, amount_cents=amount_cents,
            currency=currency, request_id=request_id,
            final_capture=final_capture)
        if crash:
            raise self.crash_error
        return result

    def void_authorization(self, authorization_id: str,
                           request_id: str | None = None) -> dict:
        crash, slow = self._chaos_armed("void_authorization")
        if slow:
            time.sleep(slow)
        result = FakePayPalClient.void_authorization(
            self, authorization_id, request_id=request_id)
        if crash:
            raise self.crash_error
        return result

    def authorize_order(self, order_id: str,
                        request_id: str | None = None) -> dict:
        crash, slow = self._chaos_armed("authorize_order")
        if slow:
            time.sleep(slow)
        result = FakePayPalClient.authorize_order(
            self, order_id, request_id=request_id)
        if crash:
            raise self.crash_error
        return result

    # -- introspection ----------------------------------------------------
    def executed_keys(self, operation: str) -> set[str | None]:
        """Distinct idempotency keys that actually EXECUTED (not replayed)."""
        return {key for op, key, _fp, was_replay in self.calls
                if op == operation and not was_replay}

    def executed_count(self, operation: str) -> int:
        return sum(1 for op, _k, _fp, was_replay in self.calls
                   if op == operation and not was_replay)

    def replayed_count(self, operation: str) -> int:
        return sum(1 for op, _k, _fp, was_replay in self.calls
                   if op == operation and was_replay)

    # -- read tracking ------------------------------------------------------
    # The fake only records mutating calls in self.calls. For
    # verify-then-act assertions ("the first action after the timeout
    # was a READ, not a second capture POST") the chaos client also
    # logs reads, in order, interleaved with nothing else — tests
    # compare positions across self.reads and self.calls.
    def get_authorization(self, authorization_id: str) -> dict:
        if not hasattr(self, "_read_log"):
            self._read_log = []
        self._read_log.append(authorization_id)
        return super().get_authorization(authorization_id)

    @property
    def read_log(self) -> list[str]:
        return list(getattr(self, "_read_log", []))

    def reset(self) -> None:
        super().reset()
        self._read_log = []
        with self._chaos_guard:
            self.crash_after_accept.clear()
            self.slow_ops.clear()

"""Crash-recovery sweeper for in-flight capture intents (suggestion #1).

A process can die between POSTing a capture to PayPal and receiving the
response: the persisted intent (``pending_capture_request_id``) is then
the only local record that money may have moved. This module is the
background daemon that finds those stranded jobs, re-reads PayPal's
actual authorization state, and reconciles through the dispatcher —
so money never strands silently.

Design rules, inherited from the codebase:

* READ-ONLY on the PayPal side (round 28, C3): the sweep never POSTs.
  An intent whose key may never have reached PayPal stays in flight for
  the payer's explicit retry (capture/capture_partial) or the webhook —
  the sweep only records what PayPal's state proves.
* The dispatcher decides WHAT; the caller executes HOW (§1.4). The
  sweep builds ``Evidence`` via the shared
  ``_build_reconcile_evidence`` (the same builder the reconcile bridge
  uses — v8.2 item 2b), dispatches ``E_RECONCILE`` via
  ``transition()``, applies the returned actions, and saves —
  mirroring ``_void_after_deadline_via_dispatcher``'s Phase-1 action
  loop.
* Fail closed everywhere: a failed read, an unrecognized PayPal
  status, a guard rejection, or an unexpected action leaves the job
  untouched for the next sweep (or the operator) — never settled, never
  cleared blind.
* One bad job never stops the sweep: per-job exceptions are logged and
  the sweep continues.

Note: the in-flight intents persisted on a Job are the capture
intent (``pending_capture_request_id`` / ``pending_capture_created_at``,
round 11 / round 28 C3) and the void intent (``pending_void_request_id``,
Grok fix order #2). The sweeper scans only capture intents — a stale
void intent is harmless (it is just an idempotency key; the void path
re-reads PayPal and reconciles on retry, and settlement clears it via
ClearIntent), so there is nothing for the sweeper to recover there.
"""
from __future__ import annotations

import logging
import os
import time

from .dispatcher import (Event, GuardRejection, State,
                         UnhandledTransition, resolve_state, transition)
from .paypal_client import PayPalError

log = logging.getLogger(__name__)

#: Default staleness bar: an intent older than this is swept. Five
#: minutes is well past a capture POST's round trip — an intent this
#: old survived a crash, not a slow network.
DEFAULT_STALE_THRESHOLD_SECONDS = 300

#: PayPal authorization status -> READ_* evidence carried on E_RECONCILE.
_AUTH_STATUS_TO_READ = {
    "CREATED": Event.READ_CREATED,
    "PENDING": Event.READ_PENDING,
    "PARTIALLY_CAPTURED": Event.READ_PARTIAL,
    "CAPTURED": Event.READ_CAPTURED,
    "VOIDED": Event.READ_RELEASED,
    "EXPIRED": Event.READ_RELEASED,   # an expired hold never moves money
    "DENIED": Event.READ_RELEASED,    # a denied authorization can never move money
}

#: (Removed v8.2, item 2b: the sweeper now calls the shared
#: _build_reconcile_evidence instead of mapping outcomes inline.)


class _FileLock:
    """Cross-process per-job mutex (Gemini Q1, Claude MEDIUM #5).

    The in-process RLock (service._delivery_lock) coordinates threads;
    this coordinates separate worker PROCESSES on the same host via
    an exclusive fcntl lock on a per-job file.

    Claude MEDIUM #5: the lock file is NEVER unlinked on release.
    Unlinking creates a POSIX race — process A unlinks just as process
    B opens the path, B locks a dangling inode while process C creates
    a new file at the same path and also locks it. Two holders, one
    path. The file persists; the kernel handles locking on the
    existing inode with no disk overhead. A crashed holder's lock is
    released by the OS when its file descriptor closes.

    Single-process / single-host only (Claude Q4): /tmp is not shared
    across containers, and fcntl does not exist on Windows. For
    multi-host deployments, run the sweeper as a single-replica job
    or replace this with a Redis Redlock. Production refuses to start
    with more than one worker unless a shared lock is configured
    (see app/server.py startup check).

    Non-blocking acquire: if another process holds the job's lock,
    this sweep skips the job (the holder will reconcile it).
    """

    _DIR = None

    def __init__(self, job_id: str):
        import tempfile
        if _FileLock._DIR is None:
            _FileLock._DIR = os.path.join(
                tempfile.gettempdir(), "paypal-sweeper-locks")
            os.makedirs(_FileLock._DIR, exist_ok=True)
        # Sanitize: the job id becomes a filename — strip anything
        # that is not alphanumeric, dash, or underscore. (Claude
        # MEDIUM #5: open() with "w" follows symlinks; the dedicated
        # subdirectory + sanitized names keep this out of shared /tmp.)
        safe = "".join(
            c if (c.isalnum() or c in "-_") else "_" for c in job_id)
        self._path = os.path.join(_FileLock._DIR, f"{safe}.lock")
        self._fh = None

    def __enter__(self):
        import fcntl
        self._fh = open(self._path, "a+")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Another process holds it — skip this job this run.
            self._fh.close()
            self._fh = None
            raise _LockBusy()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._fh is not None:
            import fcntl
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None
                # NEVER unlink (Claude MEDIUM #5): see class docstring.
        return False


class _LockBusy(Exception):
    """Another process holds the job's sweep lock — skip it this run."""


def sweep_stale_intents(service, stale_threshold_seconds: int = 300,
                        *, now: int | None = None) -> list[str]:
    """Find jobs stuck with stale in-flight capture intents and reconcile
    each with PayPal's actual state.

    Returns the ids of the jobs whose stuck intent was resolved (the
    intent cleared — the job settled, the hold proved released, or a
    listed capture recorded and accounted for). Jobs the sweep could
    not resolve (failed read, unrecognized state, guard rejection) keep
    their intent for the next sweep; one bad job never stops the sweep.

    ``now`` is a test-only clock hook (same convention as
    ``cancel_pending_capture``).
    """
    effective_now = now if now is not None else int(time.time())
    recovered: list[str] = []
    for job in service.store.all():
        if job.pending_capture_request_id is None:
            continue
        created_at = job.pending_capture_created_at
        if created_at is None:
            # Round 31 (C7) lesson: a missing timestamp (pre-round-28
            # store, or a hand-edited file) used to read as 0 — decades
            # old — so an age check would sweep it immediately. Treat
            # it as "unknown age" and fail closed: leave it alone.
            log.debug("sweeper: job %s has an intent with no timestamp — "
                      "skipping", job.job_id)
            continue
        if effective_now - created_at < stale_threshold_seconds:
            continue
        try:
            if _sweep_one(service, job, effective_now,
                           stale_threshold_seconds):
                recovered.append(job.job_id)
        except _LockBusy:
            # Another worker holds this job — it will reconcile it.
            log.debug("sweeper: job %s locked by another worker — "
                      "skipping", job.job_id)
        except Exception as e:  # noqa: BLE001 — one bad job never stops the sweep
            log.warning("sweeper: job %s skipped: %s: %s",
                        job.job_id, type(e).__name__, e)
    return recovered


def _sweep_one(service, job, now: int,
               stale_threshold_seconds: int) -> bool:
    """Reconcile one stale capture intent. Returns True when the stuck
    intent was resolved (cleared or settled); False leaves it in place
    for the next sweep."""
    # Grok v7.1: the deployment is single-process only. The in-process
    # per-job RLock coordinates threads; the file lock coordinates the
    # (single) process's writes. Multi-process or multi-host deployments
    # are NOT supported — the server refuses to boot with >1 worker,
    # and multi-host needs SQLite/Postgres plus a distributed lock
    # (e.g. Redis Redlock).
    # Lock ordering is always file-then-memory: every path that takes
    # both takes them in this order, so no deadlock.
    with _FileLock(job.job_id):
        # Same per-job lock as the settlement paths (round 10): the status
        # check and the state change happen inside it, so a live capture
        # and the sweep can never interleave on one job.
        with service._delivery_lock(job.job_id):
            return _sweep_one_locked(service, job, now,
                                     stale_threshold_seconds)


def _sweep_one_locked(service, job, now: int,
                      stale_threshold_seconds: int) -> bool:
    """The sweep body, run with both locks held."""
    fresh = service.store.get(job.job_id)
    if fresh is None:
        return False
    job = fresh
    if job.pending_capture_request_id is None:
        # Raced another path that cleared the intent first.
        return True
    # Gemini Q1 (TOCTOU): the sweep iterates store.all() OUTSIDE the
    # lock — by the time this job is reached, its intent may have
    # been refreshed (a live retry re-armed it). Re-validate BOTH the
    # intent's presence AND its staleness inside the lock.
    created_at = job.pending_capture_created_at
    if created_at is None or now - created_at < stale_threshold_seconds:
        return False
    try:
        auth = service.paypal.get_authorization(job.authorization_id)
    except PayPalError as e:
        log.warning("sweeper: job %s: authorization read failed — "
                    "leaving intent in place: %s", job.job_id, e)
        return False
    read = _AUTH_STATUS_TO_READ.get(auth.get("status"))
    if read is None:
        # An unrecognized PayPal status is not evidence for any
        # dispatcher cell — refuse to settle on it (fail closed).
        log.warning("sweeper: job %s: unrecognized authorization "
                    "status %r — leaving intent in place",
                    job.job_id, auth.get("status"))
        return False
    enum_res = service._first_completed_capture_including_order(
        job, auth)
    cap = enum_res.capture
    # v8.2 (production-readiness item 2b): ONE evidence builder. The
    # sweeper calls the same _build_reconcile_evidence the bridge uses —
    # has_capture comes from the BOOKS (stored ids + listed ids), not
    # just the current read's listing. The old inline fork is deleted.
    # MEDIUM #4: a missing deadline is unknown, not 0 — never treat it
    # as passed.
    ev = service._build_reconcile_evidence(
        job, auth, enum_res,
        deadline_passed=(False if job.deadline is None
                         else now >= job.deadline),
        # The sweeper only processes intents past the staleness
        # threshold — the key had time to land.
        intent_stale=True)
    try:
        new_state, actions = transition(
            resolve_state(job), Event.E_RECONCILE, ev)
    except (GuardRejection, UnhandledTransition) as e:
        # A guard rejection (e.g. an id-less or zero-amount listed
        # capture) or an unresolvable state is not proof of
        # anything — fail closed, keep the intent.
        log.warning("sweeper: job %s: reconcile refused — leaving "
                    "intent in place: %s", job.job_id, e)
        return False
    try:
        # v8.2 (production-readiness item 2c): ONE executor. The sweeper
        # goes through the same _apply_dispatcher_actions the reconcile
        # bridge uses — one place where actions become state changes,
        # with identical settle/flag semantics. Read-only mode: no I/O,
        # no intent arming (round 28, C3). A GuardRejection (refused
        # action) fails closed — the intent stays for the next sweep.
        service._apply_dispatcher_actions(job, actions, new_state,
                                          read_only=True)
    except GuardRejection as e:
        log.warning("sweeper: job %s: action refused — leaving intent "
                    "in place: %s", job.job_id, e)
        service._record_reconcile_read(job, auth)
        return False
    # The executor synced job.status from new_state (§1.4: the
    # dispatcher decides WHAT; the caller executes HOW). Mark the
    # settlement method for swept reconciliations.
    if new_state in (State.S10a_CAPTURED_FULL,
                     State.S10b_CAPTURED_SPLIT,
                     State.S10c_CAPTURED_UNAUDITED):
        job.settlement_method = "reconciled"
    log.info("sweeper: job %s reconciled (%s)", job.job_id, new_state)
    # Round 5: a successful read is always persisted — the sweep's
    # evidence survives a restart. This also saves the job.
    service._record_reconcile_read(job, auth)
    return job.pending_capture_request_id is None


# v8.2 (production-readiness item 2c): _apply_actions deleted —
# the sweeper now calls EscrowService._apply_dispatcher_actions
# (read_only=True), the same executor the reconcile bridge
# uses. One place where actions become state changes.



def start_sweeper_thread(service, interval_seconds: int,
                         stale_threshold_seconds: int = DEFAULT_STALE_THRESHOLD_SECONDS):
    """v6.1: run sweep_stale_intents() every ``interval_seconds`` on a daemon
    thread. Returns the thread; ``thread.stop_event.set()`` stops it. A failed
    sweep is logged and the loop continues — one bad pass never kills it."""
    import threading
    if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int) or interval_seconds < 1:
        raise ValueError("interval_seconds must be an int >= 1")
    stop = threading.Event()

    def _loop():
        while not stop.is_set():
            try:
                sweep_stale_intents(service, stale_threshold_seconds)
            except Exception as e:  # noqa: BLE001 - the loop must survive
                log.warning("sweeper loop: pass failed: %s: %s", type(e).__name__, e)
            try:
                # Claude review (v8.4): the inspection-deadline auto-capture
                # was code-complete but never wired in — a silent payer
                # meant the agent was never paid. It rides the same loop.
                service.check_inspection_deadlines()
            except Exception as e:  # noqa: BLE001 - the loop must survive
                log.warning("sweeper loop: inspection deadlines failed: %s: %s",
                            type(e).__name__, e)
            stop.wait(interval_seconds)

    t = threading.Thread(target=_loop, name="lamplight-sweeper", daemon=True)
    t.stop_event = stop
    t.start()
    return t

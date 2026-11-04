
"""Durable job storage for Lamplight Checkout.

Grok's round-2 review (Oct 27, 2026) found the job store was process-local:
server.py kept one EscrowService in app.config, so every job vanished on a
restart and a previously AUTHORIZED job could never be captured or voided
again. The store is now pluggable:

* ``MemoryStore`` — plain dict, for tests (the default when no store is
  passed to EscrowService). Also selectable on the server via
  ``LAMPLIGHT_STORE=memory`` for ephemeral runs (nothing persisted).
* ``JsonFileStore`` — one JSON file, rewritten atomically on every save.
  Selectable via ``LAMPLIGHT_STORE=json`` or a ``.json`` jobs path.
  Single-process only. No longer the server default (v8.12).
* ``SqliteStore`` — SQLite in WAL mode (v8.12: the server default via
  ``jobs.db``). Multi-process safe; the production path. Selectable via
  ``LAMPLIGHT_STORE=sqlite`` or a ``.db`` / ``.sqlite`` / ``.sqlite3``
  jobs path.

Both stores also keep the ids of webhook events already processed
(``webhook_event_seen`` / ``mark_webhook_event``), so a replayed PayPal
delivery is a no-op even after a restart — the same file that holds the
jobs holds the event ids (top-level "webhook_events" key). Files written
before round 7 load with no event ids, i.e. an empty set.

Round 8 (Oct 28, 2026): the event-id set is CAPPED
(``WEBHOOK_EVENT_ID_MAX`` = 1000, oldest first evicted). A long-running
service that never capped it would grow the jobs file without bound
from repeated deliveries — one id per delivery, forever. The cap is
the simplest mechanism that fits a file store: no timestamps, no
clock, no sweep — the list is insertion-ordered and the front is
dropped when it overflows. The dedupe guarantee holds for the most
recent 1000 events; an older id that was evicted and is re-delivered
afterwards is decided again — which is safe, because re-deciding an
event for an already-settled job is a no-op ("ignored"), and
re-deciding one for an open job just re-runs the same reconcile.
At 100 webhooks a day the cap holds 10 days of dedupe; the file
stays bounded at roughly 1000 ids (~25 KB) however long the service
runs. A retention window was considered and rejected: event ids carry
no timestamps, so a window would need timestamp bookkeeping and a
sweep for the same guarantee the cap gives for free.

The store interface is deliberately small (get / save / delete / all,
plus the two webhook-event methods).

Three implementations ship:

* MemoryStore — in-memory; the default for tests.
* JsonFileStore — the default server store (jobs.json). Single-process
  ONLY: concurrent writes to the SAME job are last-writer-wins.
* SqliteStore — opt-in via ``LAMPLIGHT_STORE=sqlite`` or a ``.db`` /
  ``.sqlite`` / ``.sqlite3`` jobs path. WAL mode plus a ``version``
  column for optimistic concurrency: ``get()``/``all()`` stamp
  ``job.store_version`` and ``save()`` conditional-updates on it,
  raising on a version mismatch instead of silently overwriting.

Meta-review: JsonFileStore now uses cross-process file locking
(_StoreFileLock, fcntl-based, same pattern as sweeper.py's _FileLock)
on write operations. Each write re-reads the file from disk under the
exclusive lock before mutating, so two processes writing the same file
no longer clobber each other on the write itself (read-modify-write
under lock). HOWEVER: this is NOT sufficient for multi-process
deployment. The in-memory cache plus disk-stamp refresh (v7.1) fixes
stale reads, but concurrent writes to the SAME job are still
last-writer-wins — two processes can overwrite each other's changes.
Single-process is the ONLY supported deployment for JsonFileStore.
Multi-process or multi-host needs the SqliteStore (optimistic
concurrency) or Postgres. (Grok v7.1: the old comment claiming
multi-process safety was wrong.)
"""
from __future__ import annotations

import json
import os
import pathlib
import threading

from .escrow import Job
from .audit import AUDIT_EVENT_MAX


# Round 8 (Oct 28, 2026): cap on the durable webhook-event id set —
# see the module docstring. Oldest first evicted, insertion order
# kept, so the cap is deterministic and survives a reload.
WEBHOOK_EVENT_ID_MAX = 1000

# Phase 2 (Nov 3, 2026): HTTP-layer idempotency contracts (Stripe model).
# The Idempotency-Key header lets a client safely retry a mutating POST:
# same key + same params -> the saved response is replayed; same key +
# different params -> 422 (client misuse); the key is claimed atomically
# so concurrent double-POSTs execute exactly once.
#
# Record lifecycle: "in_progress" (claimed, handler running) ->
# "complete" (status + body saved) or "failed" (handler raised; a retry
# may re-claim and re-execute). A stale in_progress record (older than
# IDEMPOTENCY_IN_PROGRESS_TIMEOUT_SECONDS) is treated as orphaned — the
# original requester is gone — and may be re-claimed.
IDEMPOTENCY_KEY_TTL_SECONDS = 24 * 3600
IDEMPOTENCY_IN_PROGRESS_TIMEOUT_SECONDS = 60
# Cap on stored records (same reasoning as WEBHOOK_EVENT_ID_MAX):
# oldest first evicted. 24h TTL + janitor sweep is the primary expiry;
# the cap bounds a burst that outruns the sweep.
IDEMPOTENCY_RECORD_MAX = 2000


class _StoreLockBusy(Exception):
    """Raised when another process holds the store's file lock."""
    pass


class _StoreFileLock:
    """Cross-process mutex for JsonFileStore (meta-review).

    Same fcntl pattern as sweeper.py's _FileLock: an exclusive lock on
    a sidecar file, keyed on the store path. The lock file is NEVER
    unlinked on release (POSIX race — see sweeper.py).

    The sidecar lives NEXT TO the data file (not in /tmp): if the
    store moves, its lock moves with it, and there is no shared
    temp-dir cleanup to reason about.

    Single-host only: fcntl does not exist on Windows. Multi-host
    deployments need the SqliteStore or Postgres plus a distributed
    lock.
    """

    def __init__(self, store_path: pathlib.Path):
        # Sidecar next to the data file: <store>.lock. No sanitizing
        # or hashing needed — the lock name derives directly from the
        # store path it protects.
        self._path = str(store_path) + ".lock"
        self._fh = None

    def __enter__(self):
        import fcntl
        self._fh = open(self._path, "a+")
        try:
            # Blocking acquire — writes are quick, and silently skipping
            # a save would lose financial data (unlike the sweeper, which
            # can skip and retry next run).
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except OSError:
            self._fh.close()
            self._fh = None
            raise _StoreLockBusy()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._fh is not None:
            import fcntl
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None
                # DO NOT unlink self._path (POSIX race — see sweeper.py).
        return False


def _record_event_id(events: list[str], event_id: str) -> None:
    """Record one processed webhook event id in an insertion-ordered
    list, capped at WEBHOOK_EVENT_ID_MAX. Re-marking an id already
    present is a no-op (it keeps its original position — a replay
    must not extend an id's life). Over the cap, the oldest ids are
    dropped from the front until it fits. Shared by both stores so
    the cap behaves identically in memory and on disk."""
    if event_id in events:
        return
    events.append(event_id)
    while len(events) > WEBHOOK_EVENT_ID_MAX:
        events.pop(0)


class SqliteStore:
    """SQLite-backed job store with WAL mode and optimistic concurrency.

    v7.4 had JsonFileStore's read-modify-write as last-writer-wins on
    concurrent writes to the SAME job. This store uses real transactions
    plus a ``version`` column for optimistic concurrency control:

    - ``PRAGMA journal_mode=WAL``: readers don't block writers.
    - Jobs serialized as JSON in a TEXT column, keyed by job_id.
    - ``get()`` stamps ``job.store_version``; ``save()`` performs a
      conditional ``UPDATE ... WHERE version = ?`` and raises
      ``RuntimeError`` on a version mismatch, so concurrent writers
      detect the conflict instead of silently overwriting each other.
    - Webhook event ids in a separate table (insertion-ordered via
      rowid, capped at WEBHOOK_EVENT_ID_MAX).
    - Each method opens its own connection (sqlite3 connections are
      not thread-safe by default); WAL mode makes this cheap.

    Selection: set ``LAMPLIGHT_STORE=sqlite``, or point
    ``LAMPLIGHT_JOBS_FILE`` at a ``.db`` path.
    """

    def __init__(self, path: str | pathlib.Path):
        import sqlite3
        self.path = pathlib.Path(path)
        # Initialize schema on first use.
        conn = sqlite3.connect(str(self.path))
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS jobs ("
                " job_id TEXT PRIMARY KEY,"
                " version INTEGER NOT NULL DEFAULT 1,"
                " data TEXT NOT NULL"
                ")")
            # Migrate pre-versioning tables (v7.4 and earlier).
            cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
            if "version" not in cols:
                conn.execute("ALTER TABLE jobs ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS webhook_events ("
                " event_id TEXT PRIMARY KEY,"
                " seq INTEGER NOT NULL"
                ")")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_webhook_events_seq ON webhook_events(seq);")
            # v8.2 (production-readiness item 3): atomic webhook dedupe.
            # webhook_event_claims holds the claim lifecycle for each
            # event id: claimed -> processing -> completed/failed. A
            # single conditional INSERT wins the claim atomically
            # across processes; a crashed claim (stale claimed/
            # processing) can be re-claimed after the timeout; a
            # completed claim is never reprocessed.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS webhook_event_claims ("
                " event_id TEXT PRIMARY KEY,"
                " state TEXT NOT NULL,"
                " claimed_at INTEGER NOT NULL,"
                " updated_at INTEGER NOT NULL"
                ")")
            # v8.2 (production-readiness item 6): structured audit
            # events, stored durably alongside the jobs. paypal_reported
            # and recorded are JSON-encoded payload dicts (or NULL).
            conn.execute(
                "CREATE TABLE IF NOT EXISTS audit_events ("
                " event_id TEXT PRIMARY KEY,"
                " job_id TEXT NOT NULL,"
                " ts INTEGER NOT NULL,"
                " operation TEXT NOT NULL,"
                " idempotency_key TEXT,"
                " paypal_reported TEXT,"
                " recorded TEXT,"
                " outcome TEXT NOT NULL,"
                " reason TEXT"
                ")")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_events_job"
                " ON audit_events(job_id, ts);")
            # Phase 2 (Nov 3, 2026): HTTP idempotency records. One row
            # per Idempotency-Key: the claim lifecycle is in_progress ->
            # complete/failed, and the saved response is replayed on
            # retry. request_fingerprint detects same-key-different-
            # params (422). expires_at drives the janitor sweep.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS idempotency_records ("
                " key TEXT PRIMARY KEY,"
                " fingerprint TEXT NOT NULL,"
                " state TEXT NOT NULL,"
                " created_at INTEGER NOT NULL,"
                " updated_at INTEGER NOT NULL,"
                " expires_at INTEGER NOT NULL,"
                " status_code INTEGER,"
                " response_body TEXT"
                ")")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_idempotency_expires"
                " ON idempotency_records(expires_at);")
            # Phase 4 (property-testing game plan): nightly
            # reconciliation. reconciliation_runs records each scheduled
            # run; reconciliation_findings persists one row per (job_id,
            # issue_type) divergence. Alert dedupe keys on open findings
            # with the same (job_id, issue_type) — a repeat offender
            # does not page twice.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS reconciliation_runs ("
                " run_id TEXT PRIMARY KEY,"
                " started_at INTEGER NOT NULL,"
                " finished_at INTEGER,"
                " jobs_checked INTEGER NOT NULL DEFAULT 0,"
                " divergent_count INTEGER NOT NULL DEFAULT 0,"
                " status TEXT NOT NULL"
                ")")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS reconciliation_findings ("
                " finding_id TEXT PRIMARY KEY,"
                " run_id TEXT NOT NULL,"
                " job_id TEXT NOT NULL,"
                " issue_type TEXT NOT NULL,"
                " detail TEXT,"
                " first_seen INTEGER NOT NULL,"
                " status TEXT NOT NULL,"
                " resolved_at INTEGER,"
                " note TEXT"
                ")")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_recon_findings_job_type"
                " ON reconciliation_findings(job_id, issue_type, status);")
            conn.commit()
        finally:
            conn.close()

    def _connect(self):
        import sqlite3
        conn = sqlite3.connect(str(self.path), timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def get(self, job_id: str) -> Job | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT data, version FROM jobs WHERE job_id = ?",
                (job_id,)).fetchone()
            if row is None:
                return None
            job = Job.from_store_dict(json.loads(row[0]))
            job.store_version = row[1]
            return job
        finally:
            conn.close()

    def save(self, job: Job) -> None:
        """Save a job with optimistic concurrency on the version column.

        v8.2 (production-readiness item 4):
        (a) job.store_version is stamped ONLY after the transaction
            commits successfully — never inside the transaction block.
            A failed commit leaves the in-memory version untouched, so
            a retry uses the correct expected version instead of a
            phantom.
        (b) Bounded retries with backoff on sqlite3.OperationalError
            "database is locked" for the read-to-write upgrade path.
        """
        import sqlite3
        import time
        import random
        max_retries = 5
        base_delay = 0.01  # 10ms, exponential backoff with jitter
        last_exc = None
        for attempt in range(max_retries + 1):
            try:
                self._save_once(job)
                return
            except sqlite3.OperationalError as e:
                last_exc = e
                if "database is locked" not in str(e).lower():
                    raise
                if attempt == max_retries:
                    raise
                # Bounded exponential backoff with jitter.
                delay = base_delay * (2 ** attempt) * (0.5 + random.random())
                time.sleep(delay)
        raise last_exc  # unreachable, for type checkers

    def _save_once(self, job: Job) -> None:
        conn = self._connect()
        # v8.2 (item 4a): compute the new version inside the transaction,
        # but stamp job.store_version ONLY after the commit succeeds.
        new_version = None
        try:
            with conn:  # transaction: optimistic concurrency on version
                current_ver = getattr(job, "store_version", None)
                if current_ver is None:
                    # Unstamped object (legacy, or loaded before the
                    # all() stamp fix): read the row's current version
                    # first, then conditional-update — never a blind
                    # upsert. The old blind upsert bumped the DB to
                    # N+1 while stamping the object 1, so the next
                    # save raised a phantom conflict.
                    existing = conn.execute(
                        "SELECT version FROM jobs WHERE job_id = ?",
                        (job.job_id,)).fetchone()
                    if existing is None:
                        conn.execute(
                            "INSERT INTO jobs (job_id, version, data) "
                            "VALUES (?, 1, ?)",
                            (job.job_id,
                             json.dumps(job.to_store_dict())))
                        new_version = 1
                    else:
                        cur = conn.execute(
                            "UPDATE jobs SET data = ?, version = version + 1 "
                            "WHERE job_id = ? AND version = ?",
                            (json.dumps(job.to_store_dict()),
                             job.job_id, existing[0]))
                        if cur.rowcount == 0:
                            raise RuntimeError(
                                f"Concurrency conflict on job {job.job_id}: "
                                f"row changed during save")
                        new_version = existing[0] + 1
                else:
                    cur = conn.execute(
                        "UPDATE jobs SET data = ?, version = version + 1 WHERE job_id = ? AND version = ?",
                        (json.dumps(job.to_store_dict()), job.job_id, current_ver))
                    if cur.rowcount == 0:
                        # Version mismatch (concurrent writer) or row vanished.
                        existing = conn.execute(
                            "SELECT version FROM jobs WHERE job_id = ?",
                            (job.job_id,)).fetchone()
                        if existing:
                            raise RuntimeError(
                                f"Concurrency conflict on job {job.job_id}: "
                                f"stored version {existing[0]} != expected {current_ver}")
                        # Row was deleted between get and save; re-insert.
                        conn.execute(
                            "INSERT INTO jobs (job_id, version, data) VALUES (?, 1, ?)",
                            (job.job_id, json.dumps(job.to_store_dict())))
                        new_version = 1
                    else:
                        new_version = current_ver + 1
            # Commit succeeded — now stamp the in-memory version.
            job.store_version = new_version
        finally:
            conn.close()

    def delete(self, job_id: str) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "DELETE FROM jobs WHERE job_id = ?",
                    (job_id,))
        finally:
            conn.close()

    def all(self) -> list[Job]:
        conn = self._connect()
        try:
            # v8 money-safety fix: stamp store_version like get() does.
            # Without the stamp, every object from all() saved via the
            # None-version path — last-writer-wins, the exact bug the
            # version column was added to stop.
            rows = conn.execute(
                "SELECT data, version FROM jobs").fetchall()
            jobs = []
            for r in rows:
                job = Job.from_store_dict(json.loads(r[0]))
                job.store_version = r[1]
                jobs.append(job)
            return jobs
        finally:
            conn.close()

    def webhook_event_seen(self, event_id: str) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM webhook_events WHERE event_id = ?",
                (event_id,)).fetchone()
            return row is not None
        finally:
            conn.close()

    # v8.2 (production-readiness item 3): atomic webhook deduplication.
    # Claim states: claimed -> processing -> completed/failed.
    _WEBHOOK_CLAIM_TIMEOUT_SECONDS = 300

    def claim_webhook_event(self, event_id: str) -> str:
        """Atomically claim a webhook event for processing.

        Returns:
          "won"       — we won the claim; caller must process the event
                        and then call settle_webhook_event().
          "duplicate" — the event was already completed; do not process.
          "in_progress" — another worker holds a fresh claim; do not
                        process (it will complete or go stale).
          "retry"     — we re-claimed a stale (crashed) or failed claim;
                        caller must process and settle.

        The claim is a single conditional INSERT (INSERT OR IGNORE) —
        atomic across processes. A stale claimed/processing claim
        (older than the timeout) or a failed claim can be re-claimed
        with a single conditional UPDATE.
        """
        import time
        now = int(time.time())
        conn = self._connect()
        try:
            with conn:
                # Try to win the claim atomically.
                cur = conn.execute(
                    "INSERT OR IGNORE INTO webhook_event_claims"
                    " (event_id, state, claimed_at, updated_at)"
                    " VALUES (?, 'claimed', ?, ?)",
                    (event_id, now, now))
                if cur.rowcount == 1:
                    return "won"
                # We lost — inspect the existing claim.
                row = conn.execute(
                    "SELECT state, claimed_at FROM webhook_event_claims"
                    " WHERE event_id = ?", (event_id,)).fetchone()
                if row is None:
                    # Raced a delete (should not happen) — try again.
                    return "in_progress"
                state, claimed_at = row
                if state == "completed":
                    return "duplicate"
                if state == "failed":
                    # A failed claim can be retried — re-claim it.
                    conn.execute(
                        "UPDATE webhook_event_claims SET state='claimed',"
                        " claimed_at=?, updated_at=? WHERE event_id=?"
                        " AND state='failed'",
                        (now, now, event_id))
                    return "retry" if conn.total_changes > 0 else "in_progress"
                # claimed/processing: re-claim only if stale (crashed worker).
                if now - claimed_at >= self._WEBHOOK_CLAIM_TIMEOUT_SECONDS:
                    cur = conn.execute(
                        "UPDATE webhook_event_claims SET state='claimed',"
                        " claimed_at=?, updated_at=? WHERE event_id=?"
                        " AND state IN ('claimed', 'processing')"
                        " AND claimed_at=?",
                        (now, now, event_id, claimed_at))
                    if cur.rowcount == 1:
                        return "retry"
                return "in_progress"
        finally:
            conn.close()

    def settle_webhook_event(self, event_id: str, completed: bool) -> None:
        """Mark a claimed webhook event as completed or failed.

        Completed events are never reprocessed. Failed events can be
        retried by a later claim_webhook_event() call.
        """
        import time
        now = int(time.time())
        state = "completed" if completed else "failed"
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE webhook_event_claims SET state=?,"
                    " updated_at=? WHERE event_id=?",
                    (state, now, event_id))
        finally:
            conn.close()

    # v8.2 (production-readiness item 6): structured audit events.
    # event is an AuditEvent.to_dict(): paypal_reported/recorded are
    # JSON-encoded; everything else is a scalar column.

    def append_audit_event(self, event: dict) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO audit_events"
                    " (event_id, job_id, ts, operation, idempotency_key,"
                    "  paypal_reported, recorded, outcome, reason)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (event["event_id"], event["job_id"], event["ts"],
                     event["operation"], event.get("idempotency_key"),
                     json.dumps(event.get("paypal_reported"))
                     if event.get("paypal_reported") is not None else None,
                     json.dumps(event.get("recorded"))
                     if event.get("recorded") is not None else None,
                     event["outcome"], event.get("reason")))
        finally:
            conn.close()

    def audit_events(self, job_id: str | None = None,
                     limit: int | None = None) -> list[dict]:
        """Audit events oldest-first (by ts, then rowid for ties).
        job_id=None returns events across all jobs."""
        conn = self._connect()
        try:
            sql = ("SELECT event_id, job_id, ts, operation,"
                   " idempotency_key, paypal_reported, recorded,"
                   " outcome, reason FROM audit_events")
            args: list = []
            if job_id is not None:
                sql += " WHERE job_id = ?"
                args.append(job_id)
            sql += " ORDER BY ts ASC, rowid ASC"
            if limit is not None:
                sql += " LIMIT ?"
                args.append(limit)
            rows = conn.execute(sql, args).fetchall()
            events = []
            for r in rows:
                events.append({
                    "event_id": r[0], "job_id": r[1], "ts": r[2],
                    "operation": r[3], "idempotency_key": r[4],
                    "paypal_reported": (json.loads(r[5])
                                        if r[5] is not None else None),
                    "recorded": (json.loads(r[6])
                                 if r[6] is not None else None),
                    "outcome": r[7], "reason": r[8],
                })
            return events
        finally:
            conn.close()

    # Phase 4 (property-testing game plan): nightly reconciliation
    # persistence. Runs and findings are stored durably alongside the
    # jobs so a restart never loses the alert-dedupe state.

    _RECON_RUN_COLS = ("run_id, started_at, finished_at,"
                       " jobs_checked, divergent_count, status")
    _RECON_FINDING_COLS = ("finding_id, run_id, job_id, issue_type,"
                           " detail, first_seen, status, resolved_at, note")

    @staticmethod
    def _row_to_recon_run(r) -> dict:
        return {"run_id": r[0], "started_at": r[1], "finished_at": r[2],
                "jobs_checked": r[3], "divergent_count": r[4],
                "status": r[5]}

    @staticmethod
    def _row_to_recon_finding(r) -> dict:
        return {"finding_id": r[0], "run_id": r[1], "job_id": r[2],
                "issue_type": r[3], "detail": r[4], "first_seen": r[5],
                "status": r[6], "resolved_at": r[7], "note": r[8]}

    def record_reconciliation_run(self, run: dict) -> None:
        """Insert or replace a reconciliation run record."""
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO reconciliation_runs"
                    f" ({self._RECON_RUN_COLS})"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (run["run_id"], run["started_at"],
                     run.get("finished_at"), run.get("jobs_checked", 0),
                     run.get("divergent_count", 0), run["status"]))
        finally:
            conn.close()

    def get_reconciliation_runs(self, limit: int | None = None) -> list[dict]:
        """Reconciliation runs, newest first."""
        conn = self._connect()
        try:
            sql = ("SELECT " + self._RECON_RUN_COLS
                   + " FROM reconciliation_runs"
                   " ORDER BY started_at DESC, rowid DESC")
            args: list = []
            if limit is not None:
                sql += " LIMIT ?"
                args.append(limit)
            return [self._row_to_recon_run(r)
                    for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def record_reconciliation_finding(self, finding: dict) -> None:
        """Insert or replace a reconciliation finding."""
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO reconciliation_findings"
                    f" ({self._RECON_FINDING_COLS})"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (finding["finding_id"], finding["run_id"],
                     finding["job_id"], finding["issue_type"],
                     finding.get("detail"), finding["first_seen"],
                     finding["status"], finding.get("resolved_at"),
                     finding.get("note")))
        finally:
            conn.close()

    def find_open_finding(self, job_id: str,
                          issue_type: str) -> dict | None:
        """The open finding for (job_id, issue_type), if any.

        Alert dedupe keys on this: a repeat offender does not page
        twice while its finding is still open.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT " + self._RECON_FINDING_COLS
                + " FROM reconciliation_findings"
                " WHERE job_id = ? AND issue_type = ?"
                " AND status = 'open' ORDER BY first_seen DESC LIMIT 1",
                (job_id, issue_type)).fetchone()
            return self._row_to_recon_finding(row) if row else None
        finally:
            conn.close()

    def get_reconciliation_finding(self, finding_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT " + self._RECON_FINDING_COLS
                + " FROM reconciliation_findings WHERE finding_id = ?",
                (finding_id,)).fetchone()
            return self._row_to_recon_finding(row) if row else None
        finally:
            conn.close()

    def update_reconciliation_finding(self, finding_id: str, *,
                                      status: str, note: str | None = None,
                                      resolved_at: int | None = None) -> dict | None:
        """Ack or resolve a finding. Returns the updated finding, or
        None when the finding does not exist."""
        conn = self._connect()
        try:
            with conn:
                row = conn.execute(
                    "SELECT " + self._RECON_FINDING_COLS
                    + " FROM reconciliation_findings WHERE finding_id = ?",
                    (finding_id,)).fetchone()
                if row is None:
                    return None
                conn.execute(
                    "UPDATE reconciliation_findings"
                    " SET status = ?, note = ?, resolved_at = ?"
                    " WHERE finding_id = ?",
                    (status, note, resolved_at, finding_id))
                return self._row_to_recon_finding(
                    (row[0], row[1], row[2], row[3], row[4], row[5],
                     status, resolved_at, note))
        finally:
            conn.close()

    def list_reconciliation_findings(self,
                                     status: str | None = None) -> list[dict]:
        """Findings, newest first. status=None returns all."""
        conn = self._connect()
        try:
            sql = ("SELECT " + self._RECON_FINDING_COLS
                   + " FROM reconciliation_findings")
            args: list = []
            if status is not None:
                sql += " WHERE status = ?"
                args.append(status)
            sql += " ORDER BY first_seen DESC, rowid DESC"
            return [self._row_to_recon_finding(r)
                    for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def mark_webhook_event(self, event_id: str) -> None:
        conn = self._connect()
        try:
            with conn:
                # No-op if already present (keeps original position).
                row = conn.execute(
                    "SELECT 1 FROM webhook_events WHERE event_id = ?",
                    (event_id,)).fetchone()
                if row is not None:
                    return
                seq_row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM webhook_events"
                ).fetchone()
                conn.execute(
                    "INSERT INTO webhook_events (event_id, seq)"
                    " VALUES (?, ?)",
                    (event_id, seq_row[0]))
                # Cap: evict oldest first.
                conn.execute(
                    "DELETE FROM webhook_events WHERE event_id IN ("
                    " SELECT event_id FROM webhook_events"
                    " ORDER BY seq ASC"
                    " LIMIT max(0, (SELECT COUNT(*) FROM webhook_events)"
                    " - ?))",
                    (WEBHOOK_EVENT_ID_MAX,))
        finally:
            conn.close()

    # Phase 2 (Nov 3, 2026): HTTP idempotency records. The claim is the
    # concurrency control: exactly one requester wins the atomic INSERT
    # and executes; losers replay the winner's saved response.
    #
    # Returns one of:
    #   ("claimed",)                       — this caller executes
    #   ("replay", status_code, body)      — key seen, same fingerprint
    #   ("conflict",)                      — key seen, different fingerprint
    #   ("in_progress",)                   — another request is executing now
    def claim_idempotency_key(self, key: str, fingerprint: str,
                              ttl_seconds: int, now: int) -> tuple:
        conn = self._connect()
        try:
            with conn:
                row = conn.execute(
                    "SELECT fingerprint, state, updated_at, status_code,"
                    " response_body FROM idempotency_records WHERE key = ?",
                    (key,)).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO idempotency_records"
                        " (key, fingerprint, state, created_at, updated_at,"
                        "  expires_at, status_code, response_body)"
                        " VALUES (?, ?, 'in_progress', ?, ?, ?, NULL, NULL)",
                        (key, fingerprint, now, now,
                         now + ttl_seconds))
                    self._evict_idempotency_records(conn)
                    return ("claimed",)
                (fp, state, updated_at,
                 status_code, response_body) = row
                if state == "complete":
                    if fp == fingerprint:
                        return ("replay", status_code, response_body)
                    return ("conflict",)
                if state == "failed":
                    # A failed handler left the key behind; the retry
                    # re-claims and re-executes (never replays a 500).
                    conn.execute(
                        "UPDATE idempotency_records SET fingerprint = ?,"
                        " state = 'in_progress', updated_at = ?,"
                        " expires_at = ?, status_code = NULL,"
                        " response_body = NULL WHERE key = ?",
                        (fingerprint, now, now + ttl_seconds, key))
                    return ("claimed",)
                # state == "in_progress": fresh means someone is
                # executing now; stale means the requester died.
                if now - updated_at < IDEMPOTENCY_IN_PROGRESS_TIMEOUT_SECONDS:
                    return ("in_progress",)
                conn.execute(
                    "UPDATE idempotency_records SET fingerprint = ?,"
                    " state = 'in_progress', updated_at = ?,"
                    " expires_at = ? WHERE key = ?",
                    (fingerprint, now, now + ttl_seconds, key))
                return ("claimed",)
        finally:
            conn.close()

    def complete_idempotency_key(self, key: str, status_code: int,
                                 response_body: str, now: int) -> None:
        """Save the handler's response for replay. Only the claim
        winner calls this; it is a no-op if the key is gone."""
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE idempotency_records SET state = 'complete',"
                    " updated_at = ?, status_code = ?,"
                    " response_body = ? WHERE key = ?"
                    " AND state = 'in_progress'",
                    (now, status_code, response_body, key))
        finally:
            conn.close()

    def fail_idempotency_key(self, key: str, now: int) -> None:
        """Mark the claim failed (handler raised). A retry with the
        same key re-claims and re-executes; nothing is replayed."""
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE idempotency_records SET state = 'failed',"
                    " updated_at = ? WHERE key = ?"
                    " AND state = 'in_progress'",
                    (now, key))
        finally:
            conn.close()

    def delete_expired_idempotency_records(self, now: int) -> int:
        """Janitor sweep: delete records past expires_at. Returns the
        number removed."""
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "DELETE FROM idempotency_records WHERE expires_at <= ?",
                    (now,))
                return cur.rowcount or 0
        finally:
            conn.close()

    def _evict_idempotency_records(self, conn) -> None:
        """Cap the table: oldest first evicted (same pattern as the
        webhook event cap). Caller holds the transaction."""
        conn.execute(
            "DELETE FROM idempotency_records WHERE key IN ("
            " SELECT key FROM idempotency_records"
            " ORDER BY created_at ASC"
            " LIMIT max(0, (SELECT COUNT(*) FROM idempotency_records)"
            " - ?))",
            (IDEMPOTENCY_RECORD_MAX,))


def _is_sqlite_path(path: pathlib.Path) -> bool:
    return path.suffix.lower() in (".db", ".sqlite", ".sqlite3")


def make_store(path: str | pathlib.Path | None = None):
    """Build the configured job store.

    v8.12 selection order:
    1. ``LAMPLIGHT_STORE=memory`` → MemoryStore (ephemeral, nothing
       persisted; for tests and throwaway runs).
    2. ``LAMPLIGHT_STORE=sqlite`` or the jobs path ends in
       ``.db``/``.sqlite``/``.sqlite3`` → SqliteStore (the default;
       multi-process safe).
    3. ``LAMPLIGHT_STORE=json`` or any other path → JsonFileStore
       (legacy single-process store).
    Returns None when no path is configured — caller decides (tests
    use MemoryStore via EscrowService's default).
    """
    import os as _os
    store_opt = _os.environ.get("LAMPLIGHT_STORE", "").strip().lower()
    if store_opt == "memory":
        return MemoryStore()
    if path is None:
        override = _os.environ.get("LAMPLIGHT_JOBS_FILE")
        path = pathlib.Path(override) if override else None
    if path is None:
        # No path configured — caller decides (tests use MemoryStore).
        return None
    path = pathlib.Path(path)
    # Explicit opt-out beats the path suffix: LAMPLIGHT_STORE=json forces
    # the legacy store even on a .db path (and vice versa for sqlite).
    if store_opt == "json":
        return JsonFileStore(path)
    if store_opt == "sqlite" or _is_sqlite_path(path):
        return SqliteStore(path)
    return JsonFileStore(path)


class MemoryStore:
    """In-memory job store. The default; tests use this."""

    def __init__(self):
        self._jobs: dict[str, Job] = {}
        # Insertion-ordered (round 8): the cap evicts oldest first,
        # so the ids must keep their arrival order — a set cannot.
        self._webhook_events: list[str] = []
        # v8.2 (item 3): atomic webhook claims — event_id ->
        # (state, claimed_at). Guarded by the GIL (single process).
        self._webhook_claims: dict[str, tuple[str, int]] = {}
        # v8.2 (item 6): structured audit events, insertion-ordered.
        self._audit_events: list[dict] = []
        # Phase 4: reconciliation runs and findings, insertion-ordered.
        self._recon_runs: list[dict] = []
        self._recon_findings: dict[str, dict] = {}

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def save(self, job: Job) -> None:
        self._jobs[job.job_id] = job

    def delete(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)

    def all(self) -> list[Job]:
        return list(self._jobs.values())

    def webhook_event_seen(self, event_id: str) -> bool:
        """Has this PayPal webhook event id already been processed?
        A replayed delivery must be a no-op, never a second settlement."""
        return event_id in self._webhook_events

    def mark_webhook_event(self, event_id: str) -> None:
        """Record a webhook event id as processed (see webhook_event_seen).
        Capped at WEBHOOK_EVENT_ID_MAX, oldest first evicted (round 8)."""
        _record_event_id(self._webhook_events, event_id)

    _WEBHOOK_CLAIM_TIMEOUT_SECONDS = 300

    def claim_webhook_event(self, event_id: str) -> str:
        """Atomically claim a webhook event (see SqliteStore for the
        full contract). In-memory: the GIL makes the check-and-set
        atomic for threads; single-process only."""
        import time
        now = int(time.time())
        claim = self._webhook_claims.get(event_id)
        if claim is None:
            self._webhook_claims[event_id] = ("claimed", now)
            return "won"
        state, claimed_at = claim
        if state == "completed":
            return "duplicate"
        if state == "failed":
            self._webhook_claims[event_id] = ("claimed", now)
            return "retry"
        if now - claimed_at >= self._WEBHOOK_CLAIM_TIMEOUT_SECONDS:
            self._webhook_claims[event_id] = ("claimed", now)
            return "retry"
        return "in_progress"

    def settle_webhook_event(self, event_id: str, completed: bool) -> None:
        """Mark a claimed webhook event as completed or failed."""
        import time
        state = "completed" if completed else "failed"
        self._webhook_claims[event_id] = (state, int(time.time()))

    # Phase 2 (Nov 3, 2026): HTTP idempotency records (in-memory).
    # Same contract as SqliteStore.claim_idempotency_key — see there
    # for the return-value protocol. The GIL makes the check-and-set
    # atomic for threads; single-process only.

    def _idem_store(self) -> dict:
        if not hasattr(self, "_idempotency_records"):
            # key -> [fingerprint, state, created_at, updated_at,
            #          expires_at, status_code, response_body]
            self._idempotency_records = {}
        return self._idempotency_records

    def claim_idempotency_key(self, key: str, fingerprint: str,
                              ttl_seconds: int, now: int) -> tuple:
        import threading
        if not hasattr(self, "_idem_lock"):
            self._idem_lock = threading.Lock()
        with self._idem_lock:
            recs = self._idem_store()
            rec = recs.get(key)
            if rec is None:
                recs[key] = [fingerprint, "in_progress", now, now,
                             now + ttl_seconds, None, None]
                self._evict_idempotency_records()
                return ("claimed",)
            fp, state, _created, updated_at, _exp, status_code, body = rec
            if state == "complete":
                if fp == fingerprint:
                    return ("replay", status_code, body)
                return ("conflict",)
            if state == "failed":
                rec[0] = fingerprint
                rec[1] = "in_progress"
                rec[3] = now
                rec[4] = now + ttl_seconds
                rec[5] = None
                rec[6] = None
                return ("claimed",)
            if now - updated_at < IDEMPOTENCY_IN_PROGRESS_TIMEOUT_SECONDS:
                return ("in_progress",)
            rec[0] = fingerprint
            rec[1] = "in_progress"
            rec[3] = now
            rec[4] = now + ttl_seconds
            return ("claimed",)

    def complete_idempotency_key(self, key: str, status_code: int,
                                 response_body: str, now: int) -> None:
        recs = self._idem_store()
        rec = recs.get(key)
        if rec is not None and rec[1] == "in_progress":
            rec[1] = "complete"
            rec[3] = now
            rec[5] = status_code
            rec[6] = response_body

    def fail_idempotency_key(self, key: str, now: int) -> None:
        recs = self._idem_store()
        rec = recs.get(key)
        if rec is not None and rec[1] == "in_progress":
            rec[1] = "failed"
            rec[3] = now

    def delete_expired_idempotency_records(self, now: int) -> int:
        recs = self._idem_store()
        expired = [k for k, r in recs.items() if r[4] <= now]
        for k in expired:
            del recs[k]
        return len(expired)

    def _evict_idempotency_records(self) -> None:
        recs = self._idem_store()
        overflow = len(recs) - IDEMPOTENCY_RECORD_MAX
        if overflow > 0:
            # Oldest first (insertion order).
            for k in list(recs.keys())[:overflow]:
                del recs[k]

    # v8.2 (production-readiness item 6): structured audit events.

    def append_audit_event(self, event: dict) -> None:
        import copy
        self._audit_events.append(copy.deepcopy(event))

    def audit_events(self, job_id: str | None = None,
                     limit: int | None = None) -> list[dict]:
        """Audit events oldest-first (insertion order).
        job_id=None returns events across all jobs."""
        import copy
        events = [e for e in self._audit_events
                  if job_id is None or e["job_id"] == job_id]
        if limit is not None:
            events = events[:limit]
        return copy.deepcopy(events)

    # Phase 4 (property-testing game plan): nightly reconciliation
    # persistence. Same contract as SqliteStore; see it for the
    # alert-dedupe semantics on find_open_finding.

    def record_reconciliation_run(self, run: dict) -> None:
        import copy
        self._recon_runs = [r for r in self._recon_runs
                            if r["run_id"] != run["run_id"]]
        self._recon_runs.append(copy.deepcopy(run))

    def get_reconciliation_runs(self, limit: int | None = None) -> list[dict]:
        import copy
        runs = sorted(self._recon_runs,
                      key=lambda r: (r["started_at"], r["run_id"]),
                      reverse=True)
        if limit is not None:
            runs = runs[:limit]
        return copy.deepcopy(runs)

    def record_reconciliation_finding(self, finding: dict) -> None:
        import copy
        self._recon_findings[finding["finding_id"]] = copy.deepcopy(finding)

    def find_open_finding(self, job_id: str,
                          issue_type: str) -> dict | None:
        import copy
        for f in self._recon_findings.values():
            if (f["job_id"] == job_id and f["issue_type"] == issue_type
                    and f["status"] == "open"):
                return copy.deepcopy(f)
        return None

    def get_reconciliation_finding(self, finding_id: str) -> dict | None:
        import copy
        f = self._recon_findings.get(finding_id)
        return copy.deepcopy(f) if f is not None else None

    def update_reconciliation_finding(self, finding_id: str, *,
                                      status: str, note: str | None = None,
                                      resolved_at: int | None = None) -> dict | None:
        import copy
        f = self._recon_findings.get(finding_id)
        if f is None:
            return None
        f = copy.deepcopy(f)
        f["status"] = status
        f["note"] = note
        f["resolved_at"] = resolved_at
        self._recon_findings[finding_id] = f
        return copy.deepcopy(f)

    def list_reconciliation_findings(self,
                                     status: str | None = None) -> list[dict]:
        import copy
        findings = [f for f in self._recon_findings.values()
                    if status is None or f["status"] == status]
        findings.sort(key=lambda f: (f["first_seen"], f["finding_id"]),
                      reverse=True)
        return copy.deepcopy(findings)


class JsonFileStore:
    """One JSON file holding every job, keyed by job_id.

    The file is loaded once at construction and rewritten atomically
    (temp file + os.replace) on every save/delete, under a lock, so a
    crash mid-write never leaves a half-written file. A corrupt or
    unreadable file fails loudly at startup — silently dropping jobs
    would be worse than refusing to start.
    """

    def __init__(self, path: str | pathlib.Path):
        self.path = pathlib.Path(path)
        self._lock = threading.Lock()
        # Meta-review: cross-process file lock for write operations.
        # Each write re-reads from disk under this lock before
        # mutating, so two processes can't clobber each other.
        self._file_lock = _StoreFileLock(self.path)
        self._jobs: dict[str, Job] = {}
        # Processed webhook event ids (round 7). Files written before
        # round 7 have no "webhook_events" key — they load as empty.
        # Insertion-ordered list, not a set (round 8): the cap evicts
        # oldest first, so arrival order must survive a reload — the
        # file is written in insertion order, never sorted. A file
        # written before round 8 holds a sorted list; it loads as-is
        # (order among those old ids is arbitrary, the cap still holds).
        self._webhook_events: list[str] = []
        # v8.2 (item 3): webhook claims — event_id -> (state, claimed_at).
        # Loaded from disk in _read_from_disk; files written before v8.2
        # load as empty.
        self._webhook_claims: dict[str, tuple[str, int]] = {}
        # Phase 2 (Nov 3, 2026): HTTP idempotency records — key ->
        # [fingerprint, state, created_at, updated_at, expires_at,
        #  status_code, response_body]. Loaded from disk; files written
        # before Phase 2 load as empty.
        self._idempotency_records: dict[str, list] = {}
        # Phase 4: reconciliation runs and findings. Loaded from disk
        # in _read_from_disk; files written before Phase 4 load as
        # empty.
        self._recon_runs: list[dict] = []
        self._recon_findings: dict[str, dict] = {}
        # v8.2 (item 6): structured audit events, insertion-ordered.
        # Loaded from disk in _read_from_disk; files written before
        # v8.2 load as empty. Capped at AUDIT_EVENT_MAX, oldest first
        # evicted — same bounded-file rationale as the webhook event
        # id cap (round 8).
        self._audit_events: list[dict] = []
        self._stamp: tuple | None = None
        self._read_from_disk()

    def _disk_stamp(self) -> tuple | None:
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def _refresh_if_changed(self) -> None:
        """v6.1: re-read only when ANOTHER writer replaced the file.

        Writes are atomic (os.replace), so an unlocked stat + read sees the
        old or the new file, never a torn one. Our own writes record the new
        stamp, so one process keeps stable Job objects between its own
        saves. Without this, get()/all() served a copy that never saw
        another worker's writes."""
        if self._disk_stamp() != self._stamp:
            self._read_from_disk()

    def _read_from_disk(self) -> None:
        """Load jobs and webhook events from the file. Used at startup
        and before every write (under the file lock) so concurrent
        processes see each other's updates."""
        self._jobs = {}
        self._webhook_events = []
        self._audit_events = []
        self._idempotency_records = {}
        # Phase 4: reconciliation runs (insertion-ordered) and
        # findings (finding_id -> dict). Files written before Phase 4
        # load as empty.
        self._recon_runs = []
        self._recon_findings = {}
        self._stamp = self._disk_stamp()
        if self.path.exists():
            raw = self.path.read_text()
            if not raw.strip():
                # Round 11: an existing but empty file is corrupt,
                # not an empty store — the file had jobs in it before
                # a crash or a failed write truncated it, and starting
                # up with zero jobs would orphan live holds. Fail
                # loudly, like a corrupt file, instead of silently
                # coming up empty. (A file that never existed is
                # still a fresh, empty store.)
                raise ValueError(
                    f"job store file is empty, refusing to start: "
                    f"{self.path}")
            try:
                data = json.loads(raw)
            except ValueError as e:
                raise ValueError(
                    f"job store file is corrupt, refusing to start: "
                    f"{self.path}: {e}") from e
            for job_id, job_data in data.get("jobs", {}).items():
                self._jobs[job_id] = Job.from_store_dict(job_data)
            self._webhook_events = list(data.get("webhook_events", []))
            # v8.2 (item 6): structured audit events.
            self._audit_events = list(data.get("audit_events", []))
            # v8.2 (item 3): webhook claims — event_id -> [state, claimed_at].
            self._webhook_claims = {
                eid: (str(st), int(ts))
                for eid, (st, ts) in data.get("webhook_claims", {}).items()
            }
            # Phase 2 (Nov 3, 2026): HTTP idempotency records.
            self._idempotency_records = {
                k: [str(fp), str(st), int(ca), int(ua), int(ex),
                    sc, body]
                for k, (fp, st, ca, ua, ex, sc,
                        body) in data.get("idempotency_records", {}).items()
            }
            # Phase 4: reconciliation runs and findings.
            self._recon_runs = list(data.get("reconciliation_runs", []))
            self._recon_findings = {
                str(fid): dict(f)
                for fid, f in data.get("reconciliation_findings", {}).items()
            }

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            self._refresh_if_changed()
            return self._jobs.get(job_id)

    def save(self, job: Job) -> None:
        # Meta-review: read-modify-write under the cross-process file
        # lock. Re-reading from disk ensures we don't clobber another
        # process's concurrent save.
        with self._lock:
            with self._file_lock:
                self._read_from_disk()
                self._jobs[job.job_id] = job
                self._write_locked()

    def delete(self, job_id: str) -> None:
        with self._lock:
            with self._file_lock:
                self._read_from_disk()
                self._jobs.pop(job_id, None)
                self._write_locked()

    def all(self) -> list[Job]:
        with self._lock:
            self._refresh_if_changed()
            return list(self._jobs.values())

    def webhook_event_seen(self, event_id: str) -> bool:
        """Has this PayPal webhook event id already been processed?
        A replayed delivery must be a no-op, never a second settlement —
        and the record survives a restart, because it lives in the file."""
        with self._lock:
            self._refresh_if_changed()
            return event_id in self._webhook_events

    def mark_webhook_event(self, event_id: str) -> None:
        """Record a webhook event id as processed (see webhook_event_seen).
        Rewrites the file, like save/delete, so the record is durable.
        Capped at WEBHOOK_EVENT_ID_MAX, oldest first evicted (round 8)."""
        with self._lock:
            with self._file_lock:
                self._read_from_disk()
                _record_event_id(self._webhook_events, event_id)
                self._write_locked()

    _WEBHOOK_CLAIM_TIMEOUT_SECONDS = 300

    def claim_webhook_event(self, event_id: str) -> str:
        """Atomically claim a webhook event (see SqliteStore for the
        full contract). Read-modify-write under the cross-process file
        lock, like save/delete."""
        import time
        now = int(time.time())
        with self._lock:
            with self._file_lock:
                self._read_from_disk()
                claim = self._webhook_claims.get(event_id)
                if claim is None:
                    self._webhook_claims[event_id] = ("claimed", now)
                    self._write_locked()
                    return "won"
                state, claimed_at = claim
                if state == "completed":
                    return "duplicate"
                if state == "failed":
                    self._webhook_claims[event_id] = ("claimed", now)
                    self._write_locked()
                    return "retry"
                if now - claimed_at >= self._WEBHOOK_CLAIM_TIMEOUT_SECONDS:
                    self._webhook_claims[event_id] = ("claimed", now)
                    self._write_locked()
                    return "retry"
                return "in_progress"

    def settle_webhook_event(self, event_id: str, completed: bool) -> None:
        """Mark a claimed webhook event as completed or failed."""
        import time
        state = "completed" if completed else "failed"
        with self._lock:
            with self._file_lock:
                self._read_from_disk()
                self._webhook_claims[event_id] = (state, int(time.time()))
                self._write_locked()

    # Phase 2 (Nov 3, 2026): HTTP idempotency records. Same contract
    # as SqliteStore.claim_idempotency_key — see there for the
    # return-value protocol. Read-modify-write under the cross-process
    # file lock, like save/delete.

    def claim_idempotency_key(self, key: str, fingerprint: str,
                              ttl_seconds: int, now: int) -> tuple:
        with self._lock:
            with self._file_lock:
                self._read_from_disk()
                rec = self._idempotency_records.get(key)
                if rec is None:
                    self._idempotency_records[key] = [
                        fingerprint, "in_progress", now, now,
                        now + ttl_seconds, None, None]
                    self._evict_idempotency_records()
                    self._write_locked()
                    return ("claimed",)
                fp, state, _created, updated_at, _exp, status_code, body = rec
                if state == "complete":
                    if fp == fingerprint:
                        return ("replay", status_code, body)
                    return ("conflict",)
                if state == "failed":
                    rec[0] = fingerprint
                    rec[1] = "in_progress"
                    rec[3] = now
                    rec[4] = now + ttl_seconds
                    rec[5] = None
                    rec[6] = None
                    self._write_locked()
                    return ("claimed",)
                if now - updated_at < IDEMPOTENCY_IN_PROGRESS_TIMEOUT_SECONDS:
                    return ("in_progress",)
                rec[0] = fingerprint
                rec[1] = "in_progress"
                rec[3] = now
                rec[4] = now + ttl_seconds
                self._write_locked()
                return ("claimed",)

    def complete_idempotency_key(self, key: str, status_code: int,
                                 response_body: str, now: int) -> None:
        with self._lock:
            with self._file_lock:
                self._read_from_disk()
                rec = self._idempotency_records.get(key)
                if rec is not None and rec[1] == "in_progress":
                    rec[1] = "complete"
                    rec[3] = now
                    rec[5] = status_code
                    rec[6] = response_body
                    self._write_locked()

    def fail_idempotency_key(self, key: str, now: int) -> None:
        with self._lock:
            with self._file_lock:
                self._read_from_disk()
                rec = self._idempotency_records.get(key)
                if rec is not None and rec[1] == "in_progress":
                    rec[1] = "failed"
                    rec[3] = now
                    self._write_locked()

    def delete_expired_idempotency_records(self, now: int) -> int:
        with self._lock:
            with self._file_lock:
                self._read_from_disk()
                expired = [k for k, r in self._idempotency_records.items()
                           if r[4] <= now]
                for k in expired:
                    del self._idempotency_records[k]
                if expired:
                    self._write_locked()
                return len(expired)

    def _evict_idempotency_records(self) -> None:
        """Cap the records: oldest first evicted. Caller holds the
        lock and the file lock, and writes after."""
        recs = self._idempotency_records
        overflow = len(recs) - IDEMPOTENCY_RECORD_MAX
        if overflow > 0:
            for k in list(recs.keys())[:overflow]:
                del recs[k]

    # v8.2 (production-readiness item 6): structured audit events.
    # Durable in the JSON payload, capped oldest-first at
    # AUDIT_EVENT_MAX (see _write_locked).

    def append_audit_event(self, event: dict) -> None:
        import copy
        # v8.2 (item 6): SURGICAL file update — read the file, append
        # to its audit_events list, write it back — WITHOUT touching
        # self._jobs. A full _read_from_disk() here would swap the
        # cached Job objects for fresh ones, breaking the object
        # identity the service layer relies on across calls within
        # one operation (the in-memory job a method just mutated must
        # stay the same object). The cross-process file lock makes
        # the read-modify-write safe against concurrent writers, and
        # the stamp is refreshed so the next get() keeps serving the
        # in-memory jobs, exactly as after save().
        with self._lock:
            with self._file_lock:
                if self.path.exists():
                    raw = self.path.read_text()
                    data = json.loads(raw) if raw.strip() else {}
                else:
                    data = {}
                events = list(data.get("audit_events", []))
                events.append(copy.deepcopy(event))
                del events[:-AUDIT_EVENT_MAX]
                data["audit_events"] = events
                tmp = self.path.with_name(
                    f".{self.path.name}.tmp-{os.getpid()}")
                with open(tmp, "w") as f:
                    f.write(json.dumps(data, indent=2, sort_keys=True))
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
                self._stamp = self._disk_stamp()
                dir_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
                self._audit_events = events

    def audit_events(self, job_id: str | None = None,
                     limit: int | None = None) -> list[dict]:
        """Audit events oldest-first (insertion order).
        job_id=None returns events across all jobs."""
        import copy
        with self._lock:
            self._refresh_if_changed()
            events = [e for e in self._audit_events
                      if job_id is None or e["job_id"] == job_id]
            if limit is not None:
                events = events[:limit]
            return copy.deepcopy(events)

    # Phase 4 (property-testing game plan): nightly reconciliation
    # persistence. Full read-modify-write under both locks (the save()
    # pattern) — reconciliation writes are nightly, not hot-path, so
    # the object-identity concern that motivated append_audit_event's
    # surgical update does not apply.

    def record_reconciliation_run(self, run: dict) -> None:
        import copy
        with self._lock:
            with self._file_lock:
                self._read_from_disk()
                self._recon_runs = [
                    r for r in self._recon_runs
                    if r["run_id"] != run["run_id"]]
                self._recon_runs.append(copy.deepcopy(run))
                self._write_locked()

    def get_reconciliation_runs(self, limit: int | None = None) -> list[dict]:
        import copy
        with self._lock:
            self._refresh_if_changed()
            runs = sorted(self._recon_runs,
                          key=lambda r: (r["started_at"], r["run_id"]),
                          reverse=True)
            if limit is not None:
                runs = runs[:limit]
            return copy.deepcopy(runs)

    def record_reconciliation_finding(self, finding: dict) -> None:
        import copy
        with self._lock:
            with self._file_lock:
                self._read_from_disk()
                self._recon_findings[finding["finding_id"]] = \
                    copy.deepcopy(finding)
                self._write_locked()

    def find_open_finding(self, job_id: str,
                          issue_type: str) -> dict | None:
        import copy
        with self._lock:
            self._refresh_if_changed()
            for f in self._recon_findings.values():
                if (f["job_id"] == job_id
                        and f["issue_type"] == issue_type
                        and f["status"] == "open"):
                    return copy.deepcopy(f)
            return None

    def get_reconciliation_finding(self, finding_id: str) -> dict | None:
        import copy
        with self._lock:
            self._refresh_if_changed()
            f = self._recon_findings.get(finding_id)
            return copy.deepcopy(f) if f is not None else None

    def update_reconciliation_finding(self, finding_id: str, *,
                                      status: str, note: str | None = None,
                                      resolved_at: int | None = None) -> dict | None:
        import copy
        with self._lock:
            with self._file_lock:
                self._read_from_disk()
                f = self._recon_findings.get(finding_id)
                if f is None:
                    return None
                f = copy.deepcopy(f)
                f["status"] = status
                f["note"] = note
                f["resolved_at"] = resolved_at
                self._recon_findings[finding_id] = f
                self._write_locked()
                return copy.deepcopy(f)

    def list_reconciliation_findings(self,
                                     status: str | None = None) -> list[dict]:
        import copy
        with self._lock:
            self._refresh_if_changed()
            findings = [f for f in self._recon_findings.values()
                        if status is None or f["status"] == status]
            findings.sort(key=lambda f: (f["first_seen"], f["finding_id"]),
                          reverse=True)
            return copy.deepcopy(findings)

    def _write_locked(self) -> None:
        """Rewrite the whole file atomically. Caller must hold the lock.
        Round 11: the temp file is fsynced before the rename and the
        directory is fsynced after it — without that, a power loss
        could leave a zero-length file (or no file at all) at the path
        even though os.replace returned, and the next start would come
        up with zero jobs."""
        tmp = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}")
        payload = {"jobs": {jid: j.to_store_dict()
                            for jid, j in self._jobs.items()},
                   # Insertion order, not sorted (round 8): the cap
                   # evicts oldest first, so the file must keep the
                   # arrival order across a reload.
                   "webhook_events": list(self._webhook_events),
                   # v8.2 (item 6): structured audit events, capped
                   # oldest-first so the file stays bounded.
                   "audit_events": list(self._audit_events[-AUDIT_EVENT_MAX:]),
                   # v8.2 (item 3): webhook claims.
                   "webhook_claims": {eid: [st, ts] for eid, (st, ts)
                                      in self._webhook_claims.items()},
                   # Phase 2 (Nov 3, 2026): HTTP idempotency records.
                   "idempotency_records": {
                       k: [fp, st, ca, ua, ex, sc, body]
                       for k, (fp, st, ca, ua, ex, sc,
                               body) in self._idempotency_records.items()},
                   # Phase 4: reconciliation runs and findings.
                   "reconciliation_runs": list(self._recon_runs),
                   "reconciliation_findings": dict(self._recon_findings)}
        with open(tmp, "w") as f:
            f.write(json.dumps(payload, indent=2, sort_keys=True))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        self._stamp = self._disk_stamp()
        dir_fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

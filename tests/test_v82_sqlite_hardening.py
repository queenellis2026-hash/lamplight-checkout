"""Item 4 (production-readiness): harden SQLite transactions.

(a) job.store_version is stamped ONLY after commit — a failed commit
    leaves the in-memory version untouched (no phantom version).
(b) Bounded retries with backoff on "database is locked".
"""
import os
import sqlite3
import tempfile
import threading
import time

import pytest

from app.store import SqliteStore
from app.escrow import Job


def _job(job_id="j1"):
    return Job(job_id=job_id, quote={"price_cents": 1000},
               payer="p@example", agent="a@example")


def _store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return SqliteStore(path), path


def test_failed_commit_leaves_version_untouched():
    """A version-mismatch (concurrent writer) raises — and the
    in-memory job.store_version is NOT bumped (no phantom)."""
    store, path = _store()
    try:
        job = _job()
        store.save(job)
        assert job.store_version == 1

        # Simulate a concurrent writer bumping the DB version.
        conn = sqlite3.connect(path)
        conn.execute("UPDATE jobs SET version = 99 WHERE job_id = 'j1'")
        conn.commit()
        conn.close()

        # Our save must fail (version mismatch)...
        with pytest.raises(RuntimeError, match="Concurrency conflict"):
            store.save(job)
        # ...but the in-memory version must be untouched (still 1, not 2).
        assert job.store_version == 1
    finally:
        os.unlink(path)


def test_database_locked_retries_then_succeeds():
    """A transient 'database is locked' is retried with backoff and
    eventually succeeds. Simulated by making _save_once raise
    OperationalError('database is locked') twice, then succeed."""
    import sqlite3
    from unittest import mock
    store, path = _store()
    try:
        job = _job()
        store.save(job)
        assert job.store_version == 1

        calls = {"n": 0}
        orig_save_once = store._save_once

        def flaky_save_once(j):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise sqlite3.OperationalError("database is locked")
            return orig_save_once(j)

        job2 = _job("j1")
        job2.store_version = 1
        with mock.patch.object(store, "_save_once", flaky_save_once):
            store.save(job2)
        assert calls["n"] == 3
        assert job2.store_version == 2
    finally:
        os.unlink(path)


def test_database_locked_gives_up_after_max_retries():
    """A persistent 'database is locked' raises after bounded retries."""
    import sqlite3
    from unittest import mock
    store, path = _store()
    try:
        job = _job()
        def always_locked(j):
            raise sqlite3.OperationalError("database is locked")
        with mock.patch.object(store, "_save_once", always_locked):
            with pytest.raises(sqlite3.OperationalError,
                               match="database is locked"):
                store.save(job)
    finally:
        os.unlink(path)


def test_non_lock_operational_error_not_retried():
    """A non-lock OperationalError is raised immediately, not retried."""
    store, path = _store()
    try:
        job = _job()
        # Corrupt the DB to cause a non-lock error (e.g., no such table).
        conn = sqlite3.connect(path)
        conn.execute("DROP TABLE jobs")
        conn.commit()
        conn.close()
        with pytest.raises(sqlite3.OperationalError):
            store.save(job)
    finally:
        os.unlink(path)

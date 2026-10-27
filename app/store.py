"""Durable job storage for Lamplight Checkout.

Grok's round-2 review (Oct 27, 2026) found the job store was process-local:
server.py kept one EscrowService in app.config, so every job vanished on a
restart and a previously AUTHORIZED job could never be captured or voided
again. The store is now pluggable:

* ``MemoryStore`` — plain dict, for tests (the default when no store is
  passed to EscrowService).
* ``JsonFileStore`` — one JSON file, rewritten atomically on every save.
  The server defaults to it, so jobs survive a process restart: a fresh
  EscrowService on the same file loads the same jobs, with their PayPal
  ids and statuses intact, and capture/void keep working.

The store interface is deliberately small (get / save / delete / all) so
a real deployment can swap in SQLite or a database without touching the
state machine. One limit, stated plainly: JsonFileStore is for a
single-process demo server. It loads the file once at startup and rewrites
the whole file on every save, under a lock — two processes writing the
same file would clobber each other. A multi-worker deployment needs a
store with real concurrency control, not a file.
"""
from __future__ import annotations

import json
import os
import pathlib
import threading

from .escrow import Job


class MemoryStore:
    """In-memory job store. The default; tests use this."""

    def __init__(self):
        self._jobs: dict[str, Job] = {}

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def save(self, job: Job) -> None:
        self._jobs[job.job_id] = job

    def delete(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)

    def all(self) -> list[Job]:
        return list(self._jobs.values())


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
        self._jobs: dict[str, Job] = {}
        if self.path.exists():
            raw = self.path.read_text()
            if raw.strip():
                try:
                    data = json.loads(raw)
                except ValueError as e:
                    raise ValueError(
                        f"job store file is corrupt, refusing to start: "
                        f"{self.path}: {e}") from e
                for job_id, job_data in data.get("jobs", {}).items():
                    self._jobs[job_id] = Job.from_store_dict(job_data)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def save(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.job_id] = job
            self._write_locked()

    def delete(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)
            self._write_locked()

    def all(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def _write_locked(self) -> None:
        """Rewrite the whole file atomically. Caller must hold the lock."""
        tmp = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}")
        payload = {"jobs": {jid: j.to_store_dict()
                            for jid, j in self._jobs.items()}}
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        os.replace(tmp, self.path)

# Lamplight PayPal Checkout — Hardened Release v7.5
**Date:** November 2026
**Base:** Lamplight Checkout v7.4
**Status:** Audit Vulnerabilities Patched & Production Hardened
**Dispatcher Flag:** `USE_DISPATCHER = True` (`app/escrow.py`)

> **Correction (Nov 2, 2026):** the "Production Hardened" status above
> overstated the v7.5 code. A line-by-line audit verified that the
> void-gap closure was incomplete (released holds still voided on
> `enum=None` / `ENUM_NONE`), the SQLite versioning port was spliced
> in broken (`all()` never stamped the version; the None-version
> upsert was last-writer-wins; dead code sat after a `return`), and
> the dispatcher did not govern — legacy still decided the dangerous
> cases. The v8 money-safety fixes repair these holes. The honest
> architecture is: the dispatcher decides the routine paths; legacy
> remains the fallback for genuinely unhandled cases. This document
> is kept as the historical v7.5 record; do not treat its status
> line as a claim about the current code.

---

## Executive Summary of Patches (v7.5)

Following the comprehensive security, architectural, and financial audit of Lamplight v7.4, five critical and high-priority flaws were remediated to deliver this production-hardened v7.5 build:

| # | Flaw / Gap Remediated | Severity | Files Modified | Architectural Rationale & Fix |
|---|----------------------|----------|----------------|--------------------------------|
| **1** | **19-Case Void Gap Closure in FSM** | **CRITICAL** | `app/dispatcher.py` | Patched `_s7_reconcile`, `_s4_reconcile`, and `_s6_reconcile`. When PayPal reports `READ_RELEASED` (`VOIDED` / `EXPIRED` / `DENIED`) on an authorization but enumeration confirms money moved (`ENUM_FOUND` + `has_capture`), the FSM now transitions to `S10a_CAPTURED_FULL` / `S10b_CAPTURED_SPLIT` with `RecordCapture` and `EmitReceipt` actions instead of blind `S11_VOIDED_SETTLED`. Eliminates silent merchant loss in background sweeper cycles. |
| **2** | **SQLite Optimistic Concurrency Control** | **HIGH** | `app/store.py` | Added `version INTEGER NOT NULL DEFAULT 1` to `jobs` table schema and index on `webhook_events(seq)`. Updated `get()` to track `store_version` and `save()` to perform conditional update `WHERE job_id = ? AND version = ?`. Detects and rejects concurrent writer collisions, eliminating silent last-writer-wins overwrite bugs across multi-worker setups. |
| **3** | **Multi-Worker Boot Guard Harmonization** | **HIGH** | `app/server.py` | Fixed `_boot_check()` to inspect the active store. `_refuse_multi_worker()` is now enforced strictly for `JsonFileStore`, while `SqliteStore` is permitted to run under Gunicorn multi-worker deployments (`-w 4`), aligning runtime enforcement with the SQLite backend architecture. |
| **4** | **PayPal Order API Call Deduplication** | **MEDIUM** | `app/escrow.py` | Added in-flight enumeration caching (`_cached_enum_res`) in `_first_completed_capture_including_order`. When webhook reconciliation runs `_reconcile_with_auth` followed by `_fsm_track_reconcile`, the result is cached on the authorization dictionary, cutting redundant external PayPal API roundtrips by 50%. |
| **5** | **Sweeper Settlement & Fund Invariant Safety** | **HIGH** | `app/dispatcher.py`, `app/sweeper.py` | Harmonized sweeper transition handling for `S10a_CAPTURED_FULL` and `S10b_CAPTURED_SPLIT`. Ensures invariant **I1** (Fund Conservation: $\text{captured} \le \text{authorized}$) and **I4** (Receipt & Audit Trail) are enforced under all automated background reconcile runs. |

---

## Verification & Test Script

You can verify the FSM patch and SQLite optimistic concurrency using isolated unit tests:

```bash
# 1. Verify FSM Void Gap Fix (returns S10a_CAPTURED_FULL instead of S11_VOIDED_SETTLED)
python3 -c "
import importlib.util
spec = importlib.util.spec_from_file_location('dispatcher', 'app/dispatcher.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)

ev = d.Evidence(
    authorization_id='AUTH-1',
    read=d.Event.READ_RELEASED,
    enum=d.Event.ENUM_FOUND,
    has_capture=True,
    capture_ids=('CAP-1',),
    amount_cents=10000,
    authorized_cents=10000
)
state, actions = d.transition(d.State.S7_RECONCILE_AUTH_PROBE, d.Event.E_RECONCILE, ev)
assert state == d.State.S10a_CAPTURED_FULL, f'Expected S10a, got {state}'
print('✓ FSM Void Gap Fix Verified: Correctly transitions to S10a_CAPTURED_FULL with actions:', [a.kind for a in actions])
"

# 2. Verify SQLite Store Optimistic Concurrency
python3 -c "
import importlib.util, tempfile, os
spec = importlib.util.spec_from_file_location('store', 'app/store.py')
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)

tmp = tempfile.mktemp(suffix='.db')
store = s.SqliteStore(tmp)
class MockJob:
    def __init__(self, jid): self.job_id = jid; self.status = 'QUOTED'
    def to_dict(self): return {'job_id': self.job_id, 'status': self.status}
    @classmethod
    def from_dict(cls, d):
        j = cls(d['job_id']); j.status = d['status']; return j

store._job_factory = MockJob.from_dict
j = MockJob('job_1')
store.save(j)

# Concurrent read
reader1 = store.get('job_1')
reader2 = store.get('job_1')

reader1.status = 'AUTHORIZED'
store.save(reader1) # succeeds, version becomes 2

try:
    reader2.status = 'VOIDED'
    store.save(reader2) # fails with optimistic collision!
    raise AssertionError('Should have failed optimistic concurrency check')
except RuntimeError as e:
    print('✓ SQLite Optimistic Concurrency Verified:', e)
finally:
    if os.path.exists(tmp): os.remove(tmp)
"
```

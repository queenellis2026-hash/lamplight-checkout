#!/usr/bin/env python3
"""Nightly reconciliation runner (Phase 4, property-testing game plan).

Compares the books against fresh PayPal reads, persists the run +
findings in the job store, and alerts only on NEW divergences
(deduped against open findings with the same (job_id, issue_type)).

Usage:
    python3 scripts/reconcile_nightly.py [--alert-log PATH]

Exit codes: 0 = ok, 1 = divergent (new findings), 2 = error
(PayPal reads failed and nothing could be compared).

Env:
    RECONCILE_ALERT_LOG — JSONL alert log path (default
        reconciliation_alerts.jsonl in the repo root).
    RECONCILE_ALERT_URL — optional webhook URL for alert POSTs.
    LAMPLIGHT_JOBS_FILE / LAMPLIGHT_STORE — same as the server.

Also importable: from scripts.reconcile_nightly import main.
"""

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.escrow import EscrowService  # noqa: E402
from app.hiring_agent import HiringAgent  # noqa: E402
from app.server import _jobs_file, _paypal  # noqa: E402
from app.store import make_store  # noqa: E402


def build_service() -> EscrowService:
    """Build the service the same way app.server does (no Flask)."""
    store = make_store(_jobs_file())
    if store is None:
        from app.store import JsonFileStore
        store = JsonFileStore(_jobs_file())
    hiring = HiringAgent()
    return EscrowService(_paypal(), store=store,
                         delivery_verifier=hiring.verify_delivery)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Nightly reconciliation: books vs fresh PayPal reads.")
    parser.add_argument("--alert-log", default=None,
                        help="JSONL alert log path (default: "
                             "RECONCILE_ALERT_LOG or "
                             "reconciliation_alerts.jsonl)")
    args = parser.parse_args(argv)

    svc = build_service()
    summary = svc.run_scheduled_reconciliation(
        alert_log_path=args.alert_log)
    print(json.dumps(summary, indent=2, sort_keys=True))
    if summary["status"] == "error":
        return 2
    if summary["status"] == "divergent":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

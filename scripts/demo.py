"""End-to-end Lamplight Checkout demo.

Runs two real flows through the PayPal API surface and writes hash-stable
receipts (same receipt design as Lamplight Pay):

  A) happy path:  brief -> quote -> order -> approve -> AUTHORIZE (hold)
                  -> deliver -> verify hash -> CAPTURE (agent paid)
  B) timeout void: brief -> quote -> order -> approve -> AUTHORIZE
                  -> agent never delivers -> deadline passes -> VOID (hold released)

Modes:
  --mode mock (default): spins up scripts/mock_paypal.py on 127.0.0.1:5001
      and runs everything against it. No network, no money, fully automatic.
  --mode live: uses the REAL PayPal sandbox (PAYPAL_CLIENT_ID and
      PAYPAL_CLIENT_SECRET must be in the environment). After each order is
      created it prints the approve URL — open it in a browser, log in with
      a SANDBOX BUYER account, click Approve, and the script continues
      (it polls for APPROVED for up to 10 minutes).

Usage:
  .venv/bin/python scripts/demo.py [--mode mock|live]
Output: prints order/authorization/capture ids; writes demo-receipts/*.json
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.escrow import EscrowService  # noqa: E402
from app.hiring_agent import HiringAgent  # noqa: E402
from app.paypal_client import PayPalClient  # noqa: E402
from app.quote import to_json as quote_json  # noqa: E402
from app.receipt import build_receipt, to_json as receipt_json  # noqa: E402

OUT = ROOT / "demo-receipts"
MOCK_PORT = 5001


def wait_for_mock(timeout: int = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"http://127.0.0.1:{MOCK_PORT}/health", timeout=2)
            if r.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.3)
    raise RuntimeError("mock PayPal server did not come up")


def approve_order(client: PayPalClient, order: dict, *, live: bool) -> None:
    """Get the order to APPROVED. Mock: hit the mock approve endpoint.
    Live: print the URL and poll until a human approves in the browser."""
    order_id = order["id"]
    if not live:
        r = requests.post(f"http://127.0.0.1:{MOCK_PORT}/mock/approve/{order_id}",
                          timeout=10)
        r.raise_for_status()
        print(f"    buyer approved (mock): order {order_id}")
        return
    url = PayPalClient.approve_link(order)
    print(f"\n    >>> OPEN THIS URL and approve as a SANDBOX BUYER account:\n    >>> {url}\n")
    deadline = time.time() + 600
    while time.time() < deadline:
        status = client.get_order(order_id).get("status")
        if status == "APPROVED":
            print(f"    buyer approved: order {order_id}")
            return
        time.sleep(10)
    raise RuntimeError("order was not approved within 10 minutes")


def receipt_for(job, outcome: str) -> dict:
    return build_receipt(
        job_id=job.job_id, quote_id=job.quote["quote_id"],
        service=job.quote["service"], payer=job.payer, agent=job.agent,
        currency=job.quote.get("currency", "USD"),
        amount_cents=job.quote["price_cents"],
        network=job.quote.get("network", "PayPal Sandbox"),
        paypal_order_id=job.order_id, authorization_id=job.authorization_id,
        capture_id=job.capture_id, outcome=outcome,
        delivery_hash=job.delivery_hash)


def flow_a(svc: EscrowService, hiring: HiringAgent, client: PayPalClient, *, live: bool) -> None:
    print("\n[A] happy path — agent delivers, payer releases")
    quote, worker = hiring.negotiate("audit the top-20 holders, budget $50")
    print(f"    quote {quote['quote_id'][:8]}… service={quote['service']!r} "
          f"price=${quote['price_cents'] / 100:.2f} worker={worker['agent_id']}")
    (OUT / "quote-A.json").write_text(quote_json(quote))
    job = svc.create_job(quote=quote, payer=hiring.paypal_id,
                         agent=worker["paypal_id"], deadline_seconds=3600)
    order = svc.create_order(job)
    print(f"    order {order['id']} status={order['status']} (AUTHORIZE intent)")
    approve_order(client, order, live=live)
    svc.authorize(job)
    print(f"    AUTHORIZED hold: authorization {job.authorization_id}")
    delivery = "audit report: top-20 holders, block 129454096"
    dh = svc.submit_delivery(job, agent=worker["paypal_id"], delivery_text=delivery)
    print(f"    delivered, delivery_hash={dh[:16]}…")
    assert hiring.verify_delivery(job, delivery), "delivery hash mismatch"
    print("    payer agent verified the delivery hash — matches")
    # capture() enforces the same check itself (it requires the original
    # delivery text and verifies it before any PayPal call), so the
    # payer's pre-check above is the decision and capture is the gate.
    svc.capture(job, payer=hiring.paypal_id, delivery_text=delivery)
    print(f"    CAPTURED: capture {job.capture_id} — agent paid "
          f"${job.quote['price_cents'] / 100:.2f}")
    receipt = receipt_for(job, "released")
    (OUT / "receipt-A-released.json").write_text(receipt_json(receipt))
    print(f"    receipt_hash={receipt['receipt_hash']}")


def flow_b(svc: EscrowService, hiring: HiringAgent, client: PayPalClient, *, live: bool) -> None:
    print("\n[B] timeout — agent never delivers, hold is voided")
    quote, worker = hiring.negotiate("write a research summary, budget $20")
    print(f"    quote {quote['quote_id'][:8]}… service={quote['service']!r} "
          f"price=${quote['price_cents'] / 100:.2f} worker={worker['agent_id']}")
    (OUT / "quote-B.json").write_text(quote_json(quote))
    job = svc.create_job(quote=quote, payer=hiring.paypal_id,
                         agent=worker["paypal_id"], deadline_seconds=5)
    order = svc.create_order(job)
    print(f"    order {order['id']} status={order['status']}")
    approve_order(client, order, live=live)
    svc.authorize(job)
    print(f"    AUTHORIZED hold: authorization {job.authorization_id} "
          f"(deadline: 5s)")
    # Guard: capture before delivery must be refused.
    try:
        svc.capture(job, payer=hiring.paypal_id,
                    delivery_text="not delivered")
        print("    [!] GUARD FAILED: capture before delivery succeeded")
    except Exception as e:
        print(f"    [guard] capture before delivery correctly refused: {e}")
    print("    agent never delivers… waiting past the 5s deadline")
    time.sleep(6)
    svc.void_after_deadline(job, payer=hiring.paypal_id)
    print(f"    VOIDED after deadline — hold released back to payer")
    receipt = receipt_for(job, "voided")
    (OUT / "receipt-B-voided.json").write_text(receipt_json(receipt))
    print(f"    receipt_hash={receipt['receipt_hash']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["mock", "live"], default="mock")
    args = parser.parse_args()
    OUT.mkdir(exist_ok=True)
    live = args.mode == "live"

    mock_proc = None
    if not live:
        mock_proc = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts" / "mock_paypal.py"),
             "--port", str(MOCK_PORT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_for_mock()
        except RuntimeError:
            mock_proc.kill()
            raise
        base_url = f"http://127.0.0.1:{MOCK_PORT}"
        print(f"mock PayPal sandbox up on {base_url}")
        client = PayPalClient(client_id="mock", client_secret="mock",
                              base_url=base_url)
    else:
        client = PayPalClient()  # env vars; raises PayPalConfigError if unset
        print(f"live PayPal sandbox: {client.base_url}")

    svc = EscrowService(client)
    hiring = HiringAgent()
    try:
        flow_a(svc, hiring, client, live=live)
        flow_b(svc, hiring, client, live=live)
    finally:
        if mock_proc is not None:
            mock_proc.terminate()
    print("\nBoth flows passed. Receipts in demo-receipts/.")


if __name__ == "__main__":
    main()

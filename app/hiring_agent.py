"""The hiring agent — the AI side of Lamplight Checkout.

Ellis (the hiring agent) takes a plain-language brief, turns it into a
structured job, negotiates a quote with a worker agent, and verifies the
delivery hash before releasing payment.

The negotiation policy in this demo is deterministic (parse the brief,
match the service, take the worker's listed price) so the demo runs
offline and reproducibly. The policy is a single swappable hook:
pass any callable as `llm` with the signature
    llm(prompt: str) -> str
(e.g. a real model call) and it replaces the deterministic parser.
Nothing else in the flow changes — the escrow, the receipts and the
PayPal calls are identical either way.
"""
from __future__ import annotations

import hashlib
import re
from typing import Callable

from .quote import new_quote

# Worker agents the hiring agent knows about. In production this is a
# registry; for the demo it is a fixed list with listed prices (USD cents).
WORKER_AGENTS = [
    {"agent_id": "auditor-01", "paypal_id": "auditor-01@lamplight.example",
     "services": ["audit", "review", "report"], "price_cents": 1250,
     "blurb": "on-chain holder audit (top-20 wallets)"},
    {"agent_id": "writer-02", "paypal_id": "writer-02@lamplight.example",
     "services": ["write", "draft", "summary"], "price_cents": 800,
     "blurb": "research summary and write-up"},
    {"agent_id": "coder-03", "paypal_id": "coder-03@lamplight.example",
     "services": ["code", "script", "build"], "price_cents": 2000,
     "blurb": "small script or automation build"},
]


class HiringAgent:
    def __init__(self, *, agent_id: str = "ellis",
                 paypal_id: str = "ellis@lamplight.example",
                 llm: Callable[[str], str] | None = None):
        self.agent_id = agent_id
        self.paypal_id = paypal_id
        self.llm = llm

    # -- brief parsing ----------------------------------------------------------
    def parse_brief(self, brief: str) -> dict:
        """Brief -> {service, budget_cents or None, keywords}. Deterministic
        unless an llm hook is set, in which case the model does the parse."""
        if self.llm is not None:
            raw = self.llm(
                "Parse this hiring brief into JSON with keys service, "
                f"budget_cents (integer or null), keywords (list): {brief}")
            import json
            return json.loads(raw)
        text = brief.lower()
        budget = None
        m = re.search(r"\$(\d+(?:\.\d{1,2})?)", brief)
        if m:
            budget = int(round(float(m.group(1)) * 100))
        keywords = [w for w in ("audit", "write", "code", "review", "report",
                                "draft", "summary", "script", "build")
                    if w in text]
        service = keywords[0] if keywords else "general task"
        return {"service": service, "budget_cents": budget, "keywords": keywords}

    def match_worker(self, parsed: dict) -> dict:
        """Pick the worker whose services cover the brief's keywords;
        fall back to the cheapest worker. Refuse if the budget is below
        every worker's price."""
        keywords = set(parsed["keywords"])
        candidates = [w for w in WORKER_AGENTS if keywords & set(w["services"])]
        pool = candidates or WORKER_AGENTS
        worker = min(pool, key=lambda w: w["price_cents"])
        budget = parsed.get("budget_cents")
        if budget is not None and budget < worker["price_cents"]:
            raise ValueError(
                f"budget ${budget / 100:.2f} is below the lowest worker price "
                f"${worker['price_cents'] / 100:.2f}")
        return worker

    def negotiate(self, brief: str, *, ttl_seconds: int = 3600) -> tuple[dict, dict]:
        """Brief -> (quote, worker). The quote is hash-stable; the worker
        agent's listed price is the quote price (take-it-or-leave-it in v1,
        like a posted rate card)."""
        parsed = self.parse_brief(brief)
        worker = self.match_worker(parsed)
        quote = new_quote(service=worker["blurb"], price_cents=worker["price_cents"],
                          pay_to=worker["paypal_id"], ttl_seconds=ttl_seconds)
        return quote, worker

    # -- delivery verification --------------------------------------------------
    @staticmethod
    def delivery_hash(delivery_text: str) -> str:
        return hashlib.sha256(delivery_text.encode()).hexdigest()

    def verify_delivery(self, job, delivery_text: str) -> bool:
        """True if the delivered text hashes to the hash recorded on the job.
        This is the payer agent's check before it releases payment."""
        return self.delivery_hash(delivery_text) == job.delivery_hash

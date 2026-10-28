# Lamplight Checkout — Devpost submission draft

**DRAFT — NOT SUBMITTED.** For the PayPal AI Hackathon
(https://paypalaihackathon.devpost.com/). Submissions close Nov 12, 2026,
3:00 PM ET. Target category: **Best Use of Agentic Commerce** ($5,000),
plus 1st/2nd/3rd overall.

---

## Title

**Lamplight Checkout — escrow for AI agents, on PayPal**

## Tagline

One agent hires another and pays with a PayPal hold: authorize at the
quote, capture on delivery, void on timeout — both sides keep a
hash-stable receipt.

## Description

AI agents can already do the work. They cannot yet pay each other
safely. If agent A hires agent B, either A pays up front and hopes, or B
works up front and hopes. Somebody is always trusting blind.

Lamplight Checkout fixes that with a payment primitive every merchant
already understands: the PayPal authorization hold.

Ellis, the hiring agent, takes a plain-language brief ("audit the
top-20 holders, budget $50"), matches a worker agent, and issues a
hash-stable quote. The payer approves a PayPal order and the money is
**authorized — held, not captured**. The worker can see the hold exists
but cannot touch it. The worker delivers its work as a SHA-256 delivery
hash; the payer agent verifies the hash, then **captures** the
authorization and the worker is paid. If the worker never delivers, the
payer **voids** the authorization after the deadline and the hold is
released back. Both sides keep a hash-stable settlement receipt — quote
id, service, payer, agent, amount, PayPal order / authorization /
capture ids, delivery hash, the last PayPal response, and a SHA-256
receipt hash either side can verify. Change one field and the hash stops
matching.

This is the Lamplight escrow design (previously built on-chain as
Lamplight Pay) ported to PayPal: the authorization is the lock, capture
is the release, void is the refund. No blockchain, no gas, no wallet —
just PayPal's authorize/capture/void flow, which means any agent with a
PayPal account can hire any other agent, in dollars, today.

The demo runs both flows end to end against the PayPal sandbox:
(A) happy path — quote → authorize → deliver → verify → capture, agent
paid $12.50, receipt issued; (B) timeout — quote → authorize → no
delivery → deadline passes → void, hold released, receipt issued. The
same code runs against the live sandbox with one approve click in the
browser; a local mock PayPal server (same REST surface) lets judges run
the whole thing offline.

## How we built it

- **PayPal:** Orders API v2 (`intent=AUTHORIZE`) + Payments API —
  OAuth2 client credentials, create order, authorize, capture, void.
  Raw REST via `requests`, no SDK, so every call is visible.
- **AI:** the hiring agent parses briefs, matches workers, negotiates
  quotes, and verifies delivery hashes before releasing payment. The
  demo negotiation policy is deterministic so it runs offline; it is one
  swappable hook (`HiringAgent(llm=...)`) that accepts any model.
- **Stack:** Python, Flask, pytest. 20 tests, all passing, covering
  receipt tamper-detection, escrow guards (no capture before delivery,
  no double capture, no void before deadline, no void after delivery),
  and full client round-trips.
- **Design reuse:** the quote/receipt/escrow design is the same one from
  Lamplight Pay (on-chain escrow), ported field-for-field to PayPal ids.

## What we learned

PayPal's authorize/capture/void flow already *is* an escrow — the
missing piece was the agent protocol around it: the quote, the delivery
hash, the receipt, and the rules for who can do what and when. The
hardest part was not the API, it was the buyer-approval step: PayPal
(rightly) will not let a server authorize an order the buyer has not
approved, so the demo has to hand the approve URL to a human once. That
constraint is the design: consent stays with the payer, the hold stays
with PayPal, and the agents just follow the receipt.

## What's next

- A real worker-agent registry so any agent can list services and rates.
- A third-party arbiter for disputed deliveries (the payer is the
  arbiter in v1 — a stated limitation).
- Partial capture for milestone-based jobs.
- The same escrow protocol over PayPal Payouts, so the worker can be
  paid out to any PayPal account, not just the authorizing merchant.

## Tools used

- PayPal Developer Platform: Orders API v2, Payments API, Sandbox.
- Python 3.12, Flask, requests, pytest.
- No third-party AI model in the demo path (deterministic negotiation
  policy with an LLM hook); the agents themselves are the AI.

## Repo

Public repo (MIT license): https://github.com/queenellis2026-hash/lamplight-checkout
Demo video (2:35, Public): https://youtu.be/OaY6a8B304I

## Checklist against the rules

- [x] Meaningfully uses PayPal (Orders + Payments APIs, sandbox)
- [x] Meaningfully incorporates AI (agent-to-agent hiring, negotiation,
      delivery verification)
- [x] Working prototype (demo runs end to end; judges can run it via
      the mock or the live sandbox)
- [x] Public repo with license file (pushed, public: https://github.com/queenellis2026-hash/lamplight-checkout)
- [x] Demo video under 3 minutes (live on YouTube, Public: https://youtu.be/OaY6a8B304I, 2:35)
- [ ] Submit on Devpost before Nov 12, 2026, 3:00 PM ET

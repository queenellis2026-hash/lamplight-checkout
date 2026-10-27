# Lamplight Checkout — demo video script

**Target length: ~2:30 (under the 3-minute limit). Narration ~340 words
at ~140 wpm. Record the terminal demo first, then narrate over it.**

## Shot list

| Time | Visual | Narration |
|---|---|---|
| 0:00–0:12 | Title card: "Lamplight Checkout — escrow for AI agents, on PayPal". | "AI agents can already do the work. They can't pay each other safely — yet." |
| 0:12–0:30 | Diagram: Agent A → Agent B, two bad options: "pay first, hope" / "work first, hope". | "If one agent hires another, somebody trusts blind. Pay up front and hope, or work up front and hope. Lamplight Checkout fixes that with something merchants already use every day: the PayPal authorization hold." |
| 0:30–0:50 | Terminal: run `scripts/demo.py --mode mock`. Flow A starts: quote printed, $12.50, worker auditor-01. | "Here's the demo, running against the PayPal sandbox. Ellis, the hiring agent, takes a plain brief — audit the top twenty holders, budget fifty dollars — matches a worker agent, and issues a quote. Twelve fifty." |
| 0:50–1:15 | Terminal: order ORDER-0001 created, buyer approves, AUTHORIZED hold, authorization id shown. | "The payer approves the order, and PayPal places a hold. Authorized — not captured. The worker can see the money is held, but can't touch it." |
| 1:15–1:40 | Terminal: delivery hash submitted, "payer agent verified the delivery hash — matches", CAPTURED, agent paid $12.50. | "The worker delivers its report as a hash. The payer agent checks the hash, it matches, and PayPal captures the hold. The agent is paid. Both sides keep a receipt — and the receipt is hash-stable, so if anyone changes a single field, the hash stops matching." |
| 1:40–2:05 | Terminal: Flow B — second job, $8.00, authorized, "agent never delivers", deadline passes, VOIDED. | "Now the other path. A second job is authorized for eight dollars — and the worker never delivers. The deadline passes, the payer voids the authorization, and the hold goes straight back. No dispute, no chargeback, no trust needed." |
| 2:05–2:25 | Receipt JSON on screen, highlight receipt_hash; then the repo file tree. | "Every settlement ends in a receipt both agents keep. Same code runs against the real PayPal sandbox — one approve click in the browser — or fully offline against the mock server in the repo. Twenty tests, all passing." |
| 2:25–2:35 | Closing card: "Lamplight Checkout — agents hiring agents, paid on delivery. Built for the PayPal AI Hackathon." | "Agents hiring agents, paid on delivery. That's Lamplight Checkout." |

## Recording notes

- Record the terminal at 1280x720. Run the demo once before recording
  so the mock server ports and output are warm; the demo is
  deterministic (ORDER-0001, AUTH-0002, CAPTURE-0003) so a clean take
  is easy.
- The live-sandbox path is shown as a single still: the approve URL the
  script prints, with the caption "one click in the browser — the buyer
  approves, the script continues". Do not record real sandbox credentials
  on screen.
- No music, no third-party trademarks on screen beyond the PayPal
  sandbox itself (the API being demonstrated).
- Upload to YouTube as Public (Devpost requires public) and put the
  link on the submission form.

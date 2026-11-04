# Arbiter Selection and Conflict-of-Interest Policy

**Lamplight Checkout v8.12 — November 3, 2026**

This document defines who can serve as an arbiter in disputed Lamplight
Checkout jobs, how arbiters are selected, and what disqualifies them.
It covers the *policy* layer; the *mechanism* (primary/backup arbiter,
staking bonds, 14-day timeout, 2-day challenge window, permissionless
batch finalization) is implemented in the escrow contracts and described
in the HSK/Arc build documentation.

## Why this document exists

The dispute mechanism was designed with the roles but not the rules:
we specified *what* an arbiter does (confirm delivery, resolve disputes)
but never *who* gets to be one. A mechanism without a selection policy
is a door with no lock — anyone can walk through. This document is the
lock.

## Arbiter eligibility

An arbiter MUST meet all of the following:

1. **Independent of both parties.** The arbiter is not the payer, not
   the agent/worker, not employed by either, and not a financial
   beneficiary of either side of the disputed job.
2. **Staked.** The arbiter has posted the required staking bond before
   accepting any arbitration assignment. The bond is slashable for
   provably wrong or absent rulings (see "Accountability" below).
3. **Reachable.** The arbiter has a published contact method and a
   track record of responding within the dispute window. An arbiter
   who cannot be reached is functionally absent.
4. **No prior relationship to the job.** The arbiter did not quote on,
   bid for, or previously work on the disputed job or any directly
   related job between the same parties.

## Selection process

1. **Nomination.** When a dispute is filed, the payer nominates a
   primary arbiter and a backup arbiter from the pool of eligible
   arbiters.
2. **Worker confirmation.** The worker MUST explicitly accept the
   nominated arbiters before arbitration begins. This prevents the
   payer from unilaterally appointing a friendly arbiter. If the
   worker rejects a nominee, the payer nominates an alternative.
   (This confirmation step is enforced on-chain in the v2 contracts.)
3. **Backup activation.** If the primary arbiter does not respond
   within the response window, the backup arbiter is automatically
   activated. If neither responds, the dispute escalates to the
   permissionless resolution path (anyone may submit the timeout
   evidence after the 14-day window).

## Conflict-of-interest disqualifiers

An arbiter is IMMEDIATELY disqualified (and their bond is at risk) if
any of the following is discovered, before or during arbitration:

1. **Financial interest.** The arbiter holds a financial position —
   equity, revenue share, token allocation, or outstanding invoice —
   with either party.
2. **Prior undisclosed relationship.** The arbiter has a personal or
   professional relationship with either party that was not disclosed
   at nomination time.
3. **Repeat-player bias.** The arbiter has ruled in favor of the same
   party in more than 70% of their last 10 arbitrations. (This is a
   tripwire for investigation, not an automatic slash — patterns
   matter more than single rulings.)
4. **Self-dealing.** The arbiter attempts to route the disputed funds
   to any address they control, directly or indirectly.
5. **Collusion.** Any evidence of coordination with either party
   outside the arbitration channel.

## Accountability

- **Slashing.** A provably wrong ruling (contradicted by on-chain
  evidence the arbiter had access to) or a failure to rule within
  the window results in a partial or full slash of the staking bond,
  at the discretion of the backup arbiter or the permissionless
  resolution path.
- **Appeal.** Either party may appeal an arbiter's ruling within the
  2-day challenge window by posting a counter-bond. A successful
  appeal returns the appellant's bond and slashes the original
  arbiter's bond.
- **Record.** All rulings, with their reasoning, are recorded
  on-chain. Arbiters build a public track record; parties can review
  an arbiter's history before confirming a nomination.

## What this policy does NOT cover (yet)

- The exact bond amounts (chain-specific, set at deployment).
- The response-window duration (currently aligned with the 14-day
  dispute timeout; may be shortened for low-value jobs).
- A decentralized arbiter registry or reputation UI. For now, the
  eligible-arbiter pool is maintained off-chain by the Lamplight
  team; a permissionless registry is a post-hackathon item.
- Cross-chain arbitration (a dispute on HSK resolved by an arbiter
  staking on Arc, or vice versa). Out of scope for v8.

## Status

**Draft policy, not yet enforced in code.** The on-chain mechanism
(worker confirmation of arbiter nomination) is implemented. The
eligibility checks, disqualifiers, and slashing conditions described
here are enforced socially (by the Lamplight team curating the
arbiter pool), not yet by smart contract. Full on-chain enforcement
is a production item.

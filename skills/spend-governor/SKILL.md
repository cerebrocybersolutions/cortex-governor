---
name: spend-governor
description: "Route every spend through the Cortex governor before money moves: ALLOW proceeds, ESCALATE waits for a human, DENY stops."
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [payments, stripe, governance, spend-control, safety]
    related_skills: []
---

# Spend Governor: the gate between you and money

## Overview

You are an agent that can spend money. This skill makes every spend pass a
deterministic policy gate FIRST. The gate is not a model and cannot be argued
with: it validates the request, applies hard-deny rules before soft-escalate
rules, records the decision to a hash-chained tamper-evident ledger, and
returns exactly one verdict. You only touch a wallet on ALLOW.

## Prerequisites

- The cortex-governor repo on the local filesystem (`python3` 3.9+, stdlib only).
- `GOVERNOR_DIR` — path to the repo root (contains `governor.py`).
- Optional env overrides: `CEREBRO_GOVERNOR_POLICY`, `CEREBRO_GOVERNOR_LEDGER`.

## The one rule

**Never execute a payment, purchase, subscription, or top-up by any means
(Stripe skill, browser checkout, API call) without an ALLOW from the gate for
that exact spend.** If you changed the amount, merchant, or purpose after an
ALLOW, that is a NEW spend: gate it again.

## Workflow

### 1. Build the request

One JSON object per spend:

```json
{
  "agent": "hermes",
  "task": "<what business task this spend serves>",
  "merchant": "<vendor, lowercase>",
  "amount": 12.50,
  "currency": "usd",
  "category": "<api-credits|compute|saas-subscription|infra|supplies>",
  "purpose": "<why — must match an allowed purpose>"
}
```

### 2. Call the gate

```bash
echo '<request-json>' | python3 "$GOVERNOR_DIR/governor.py"
```

Output is one line, `VERDICT rule reason`, and the exit code is the contract:

| exit | verdict  | what you do |
|------|----------|-------------|
| 0    | ALLOW    | proceed with the spend, exactly as gated |
| 2    | ESCALATE | STOP. Tell the human what you wanted and why, with the rule that fired. Do NOT retry, split the amount, or reword the purpose to slip past the rule. |
| 1    | DENY     | STOP. Report the rule. A deny is final for this request — do not resubmit variants. |

### 3. On ESCALATE — wait for the human

The pending escalation lives on the ledger. A human resolves it out-of-band
(`escalation.py` records an approve/deny bound to the entry hash, and an
approval can never override a hard-deny re-check). Only after a recorded
approval do you proceed, and only for the exact gated request.

### 4. After the spend

Report the request id from the gate line alongside the payment receipt id, so
the ledger entry, the receipt, and your own narrative reconcile on one id.

## Anti-patterns (these get the whole capability revoked)

- Splitting one $600 spend into seven $90 spends. The rate window counts them.
- Re-gating the same denied request with a nicer purpose string.
- Spending first and gating retroactively. The ledger orders decisions before
  receipts; an out-of-order receipt is evidence of a bypass, not compliance.

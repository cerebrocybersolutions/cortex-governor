"""H5 demo spine — automated-company vehicle, run OFFLINE through the governor.

The submission narrative: an agentic "company" where NVIDIA agent skills earn and
spend, and EVERY spend passes the Cerebro governor before it can reach the wallet.
This script drives that story with synthetic spend-requests — no Stripe, no fleet,
no OpenRouter — so the demo logic is provable and repeatable today. At H5 the
desktop-app recording follows this exact sequence; Stripe TEST mode slots in as the
wallet that executes only on ALLOW (see build-spec §5 / D4).

What it shows, end to end:
  - one request per governor rule class: ALLOW / ESCALATE / DENY across amount cap,
    vendor deny, vendor not-allowlisted, category deny, purpose-binding
  - the rate-window finale: a run of clean ALLOWs that trips the $/hour cap
  - the hash-chained ledger recording ALL decisions (not just spends)
  - chain verify() = intact, then a tamper demo proving any edit is caught

stdlib only. ponytail: a list of requests + a walk. Run: `python3 demo_scenario.py`.
"""
import json
import sys
from pathlib import Path

from governor import gate
from policy_engine import ALLOW, DENY, ESCALATE
from ledger import Ledger

_HERE = Path(__file__).resolve().parent
DEMO_LEDGER = _HERE / "demo_ledger.jsonl"      # gitignored; fresh each run
POLICY = _HERE / "policy.json"

# Glyphs for the decision column (kept ASCII-safe for terminal + screen-record).
MARK = {ALLOW: "[ALLOW ]", ESCALATE: "[ESCAL.]", DENY: "[DENY  ]"}

# The narrative. Order matters: the rate-window reads prior ALLOWs, so the clean
# spends accumulate before the finale trips the $300/hour cap. Each line names the
# rule we expect to fire — the self-check at the bottom asserts it.
SCENARIO = [
    # (label, request, expected_verdict, expected_rule_prefix)
    ("Buy OpenAI API credits",
     dict(agent="nvidia-skill", task="earn-loop", merchant="openai", amount=12,
          currency="usd", category="api-credits", purpose="api-credits"),
     ALLOW, "ok"),
    ("Rent AWS compute",
     dict(agent="nvidia-skill", task="earn-loop", merchant="aws", amount=45,
          currency="usd", category="compute", purpose="compute"),
     ALLOW, "ok"),
    ("GitHub seat",
     dict(agent="nvidia-skill", task="tooling", merchant="github", amount=20,
          currency="usd", category="saas-subscription", purpose="saas-subscription"),
     ALLOW, "ok"),
    ("Vercel infra $150 — over per-txn cap, needs a human",
     dict(agent="nvidia-skill", task="deploy", merchant="vercel", amount=150,
          currency="usd", category="compute", purpose="infra"),
     ESCALATE, "amount.per_txn_cap"),
    ("AWS $600 — over hard ceiling, blocked outright",
     dict(agent="nvidia-skill", task="scale-up", merchant="aws", amount=600,
          currency="usd", category="compute", purpose="compute"),
     DENY, "amount.hard_max"),
    ("Sanctioned vendor — blocked",
     dict(agent="nvidia-skill", task="supplies", merchant="sanctioned-vendor", amount=50,
          currency="usd", category="supplies", purpose="supplies"),
     DENY, "vendor.deny"),
    ("Crypto category on an allowed merchant — blocked",
     dict(agent="nvidia-skill", task="treasury", merchant="stripe", amount=40,
          currency="usd", category="crypto", purpose="infra"),
     DENY, "category.deny"),
    ("Unknown merchant — escalate for review",
     dict(agent="nvidia-skill", task="experiment", merchant="shady-llc", amount=25,
          currency="usd", category="compute", purpose="compute"),
     ESCALATE, "vendor.not_allowlisted"),
    ("Off-purpose spend on a good merchant — escalate",
     dict(agent="nvidia-skill", task="growth", merchant="openai", amount=10,
          currency="usd", category="api-credits", purpose="marketing"),
     ESCALATE, "purpose.unbound"),
    # Rate-window finale: window ALLOWs so far = 12+45+20 = 77. Two more clean
    # spends push it near the $300/hour cap; the third trips it.
    ("Compute spend $90",
     dict(agent="nvidia-skill", task="earn-loop", merchant="aws", amount=90,
          currency="usd", category="compute", purpose="compute"),
     ALLOW, "ok"),
    ("Compute spend $90",
     dict(agent="nvidia-skill", task="earn-loop", merchant="aws", amount=90,
          currency="usd", category="compute", purpose="compute"),
     ALLOW, "ok"),
    ("Compute spend $90 — window total would hit $347, over the $300/hour cap",
     dict(agent="nvidia-skill", task="earn-loop", merchant="aws", amount=90,
          currency="usd", category="compute", purpose="compute"),
     DENY, "rate_window.max_amount"),
]


def run():
    # Fresh ledger each run so the demo is repeatable (drop the checkpoint too,
    # or gate()'s fail-closed guard would reject the fresh ledger).
    DEMO_LEDGER.unlink(missing_ok=True)
    DEMO_LEDGER.with_name(DEMO_LEDGER.name + ".checkpoint").unlink(missing_ok=True)

    print("=" * 74)
    print("  CEREBRO GOVERNOR — agentic-spend demo (offline, no real money)")
    print("  Stripe lets an agent hold a card. Cerebro lets it run unattended.")
    print("=" * 74)
    print("  %-52s %s  %s" % ("scenario", "verdict", "rule fired"))
    print("  " + "-" * 70)

    tally = {ALLOW: 0, DENY: 0, ESCALATE: 0}
    fails = 0
    for label, req, exp_verdict, exp_rule in SCENARIO:
        dec = gate(req, policy_path=POLICY, ledger_path=DEMO_LEDGER)
        tally[dec.verdict] += 1
        ok = dec.verdict == exp_verdict and dec.rule.startswith(exp_rule)
        fails += 0 if ok else 1
        flag = "" if ok else "  <-- UNEXPECTED (wanted %s/%s)" % (exp_verdict, exp_rule)
        print("  %-52s %s  %s%s" % (label[:52], MARK[dec.verdict], dec.rule, flag))

    print("  " + "-" * 70)
    print("  totals: %d ALLOW  %d ESCALATE  %d DENY   (only ALLOW reaches the wallet)"
          % (tally[ALLOW], tally[ESCALATE], tally[DENY]))

    # The audit trail: every decision is on the hash chain, not just the spends.
    led = Ledger(DEMO_LEDGER)
    ok, bad = led.verify()
    print("\n  ledger: %d entries (all 3 verdict classes recorded)" % len(led.entries()))
    print("  chain verify: %s" % ("INTACT" if ok else "BROKEN at index %s" % bad))

    # Tamper demo — flip one recorded amount on a COPY, prove verify() catches it.
    tampered = _HERE / "demo_ledger.tampered.jsonl"
    lines = DEMO_LEDGER.read_text().splitlines()
    victim = json.loads(lines[2])
    victim["amount"] = 999999          # someone edits a spend after the fact
    lines[2] = json.dumps(victim)
    tampered.write_text("\n".join(lines) + "\n")
    t_ok, t_bad = Ledger(tampered).verify()
    print("  tamper test: edited entry #2 amount -> verify says %s"
          % ("INTACT (BUG!)" if t_ok else "BROKEN at index %s (caught)" % t_bad))
    tampered.unlink(missing_ok=True)

    if fails or ok is False or t_ok is True:
        print("\n  RESULT: FAIL (%d unexpected verdicts)" % fails)
        return 1
    print("\n  RESULT: PASS — policy gate + tamper-evident ledger both hold")
    return 0


if __name__ == "__main__":
    sys.exit(run())

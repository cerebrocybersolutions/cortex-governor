"""H5 autonomous spine — the filmable 60 seconds. Run: python3 autonomous_demo.py

demo_scenario.py proves the RULES offline (one request per rule class). This
proves the STORY: an agentic 'company' running unattended where the governor is
the only thing between the agent and the wallet. Watch it:

  1. agent earns + spends on autopilot — clean spends ALLOW and the wallet charges,
     no human in the loop
  2. a runaway $600 spend is DENIED live — agent keeps running, nobody woken up
  3. a $150 spend ESCALATES — agent PAUSES that item and flags a human
  4. human approves once -> agent RESUMES -> wallet charges the $150
  5. every decision + the human override are on the hash chain; verify() = intact

The wallet here is a mock (prints, never moves money). At H5 the real Stripe TEST
wallet swaps in at exactly one line — `Wallet.charge` -> `link-cli spend-request
create` (sandbox keys + virtual card). Nemotron via OpenRouter replaces the
scripted intent stream as the brain. Nothing else changes.

stdlib only. Deterministic (no random, no sleep) so it is repeatable on camera and
doubles as a self-check. Pass --pace N to add N-second beats for the recording.
"""
import sys
import time
from pathlib import Path

from governor import gate
from policy_engine import ALLOW, DENY, ESCALATE
from ledger import Ledger
from escalation import pending, resolve

_HERE = Path(__file__).resolve().parent
DEMO_LEDGER = _HERE / "autonomous_ledger.jsonl"   # gitignored; fresh each run
POLICY = _HERE / "policy.json"

MARK = {ALLOW: "ALLOW ", ESCALATE: "ESCAL.", DENY: "DENY  "}


class Wallet:
    """Mock wallet. At H5 this is Stripe TEST: charge() -> link-cli spend-request."""
    def __init__(self):
        self.charged = []

    def charge(self, req):
        self.charged.append(req)
        return "tok_test_%04d" % len(self.charged)  # fake Stripe TEST token


# The agent's intent stream — what an unattended earn/spend loop would emit.
# Deterministic so the recording is identical every take. (agent brain = Nemotron
# at H5; this scripted list stands in for its buy/skip reasoning.)
INTENTS = [
    ("buy API credits to keep the earn-loop running",
     dict(merchant="openai", amount=12, category="api-credits", purpose="api-credits")),
    ("rent compute for the current job",
     dict(merchant="aws", amount=45, category="compute", purpose="compute")),
    ("renew the GitHub seat the pipeline needs",
     dict(merchant="github", amount=20, category="saas-subscription", purpose="saas-subscription")),
    ("scale up hard — $600 burst of compute",
     dict(merchant="aws", amount=600, category="compute", purpose="compute")),   # DENY
    ("provision Vercel infra, $150",
     dict(merchant="vercel", amount=150, category="compute", purpose="infra")),  # ESCALATE
    ("top up API credits again",
     dict(merchant="openai", amount=18, category="api-credits", purpose="api-credits")),
]


def _beat(pace):
    if pace:
        time.sleep(pace)


def run(pace=0.0):
    DEMO_LEDGER.unlink(missing_ok=True)
    DEMO_LEDGER.with_name(DEMO_LEDGER.name + ".checkpoint").unlink(missing_ok=True)
    wallet = Wallet()
    parked = []   # (intent, escalated_entry_hash) awaiting a human

    bar = "=" * 76
    print(bar)
    print("  CEREBRO GOVERNOR — autonomous agentic company (mock wallet, no real money)")
    print("  The agent runs unattended. The governor is the only gate to the wallet.")
    print(bar)

    for intent, spend in INTENTS:
        req = dict(agent="nvidia-skill", task="earn-loop", currency="usd", **spend)
        dec = gate(req, policy_path=POLICY, ledger_path=DEMO_LEDGER)
        line = "  agent: %-44s [%s]" % (intent[:44], MARK[dec.verdict])
        if dec.verdict == ALLOW:
            tok = wallet.charge(req)
            print(line + "  -> wallet charged %s" % tok)
        elif dec.verdict == DENY:
            print(line + "  -> BLOCKED (%s), agent keeps running" % dec.rule)
        else:  # ESCALATE
            led = Ledger(DEMO_LEDGER)
            esc = pending(led)[-1]["entry_hash"]
            parked.append((intent, esc))
            print(line + "  -> PAUSED, flagged for human (%s)" % dec.rule)
        _beat(pace)

    # The human shows up once and clears the queue (here: approve the parked spend).
    print("  " + "-" * 72)
    print("  [human] reviewing %d escalation(s)..." % len(parked))
    _beat(pace)
    resumed = 0
    for intent, esc in parked:
        led = Ledger(DEMO_LEDGER)
        res = resolve(led, esc, approve=True, approver="operator", policy_path=POLICY)
        if res and res["verdict"] == ALLOW:
            tok = wallet.charge(res)   # approval authorizes the parked spend
            resumed += 1
            print("  [human] approved: %-36s -> agent RESUMES, wallet charged %s"
                  % (intent[:36], tok))
        _beat(pace)

    # Audit trail: every decision + every override is on the chain.
    led = Ledger(DEMO_LEDGER)
    ok, bad = led.verify()
    print("  " + "-" * 72)
    print("  wallet: %d spends executed   |   ledger: %d decisions on the audit chain"
          % (len(wallet.charged), len(led.entries())))
    print("  chain verify: %s" % ("INTACT" if ok else "BROKEN at index %s" % bad))

    # Self-check: 4 clean + 1 approved-escalation = 5 charges; 1 DENY blocked;
    # chain intact. Lets this double as a test.
    expect_charges, expect_denies = 5, 1
    denies = sum(1 for e in led.entries() if e["verdict"] == DENY)
    bad = (len(wallet.charged) != expect_charges or denies != expect_denies
           or resumed != 1 or not ok)
    print("\n  RESULT: %s" % ("FAIL" if bad else
          "PASS — unattended ALLOW, live DENY, escalate->approve->resume, all audited"))
    return 1 if bad else 0


if __name__ == "__main__":
    pace = 0.0
    if "--pace" in sys.argv:
        pace = float(sys.argv[sys.argv.index("--pace") + 1])
    sys.exit(run(pace))

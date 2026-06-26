"""On-camera demo: the governor releasing / stopping a real Stripe TEST wallet.

Three beats, the hackathon's whole thesis in one run:
  [1] ALLOW    -> the agent charges autonomously (a REAL Stripe test charge, ch_…)
  [2] ESCALATE -> over the per-txn cap: wallet NOT called, held for a human tap
  [3] DENY     -> over the hard max: wallet NOT called, blocked at the gate

The "stopped-spend moment" (beats 2+3) is the point: an agent holding a live
card still cannot move money the policy forbids.

Runs two ways:
  - bare CLI (testing):       python3 stripe_demo.py            # dry-run, no charge
                              python3 stripe_demo.py --live     # real TEST charges
  - Hermes desktop app:       invoked from the cerebro-spend-governor skill. The
                              desktop GUI does NOT inherit shell env, so the key
                              is loaded from a .env the app can see (see _ensure_key).

SAFETY: dry-run by DEFAULT. --live places real Stripe TEST charges (livemode:false,
zero real money). The wallet itself enforces the test-key guard; this script only
arranges the demo + guarantees the key is in os.environ first.
stdlib only.
"""
import os
import sys
import time
from pathlib import Path

from stripe_test_wallet import settle_stripe
from policy_engine import ALLOW, DENY, ESCALATE, load_policy

_HERE = Path(__file__).resolve().parent

# .env files the Hermes desktop app can see, most-specific first. The Dock GUI
# can't read ~/.zshrc, so a desktop run needs the key in one of these.
_ENV_CANDIDATES = [
    Path.home() / ".hermes" / "profiles" / "hackathon" / ".env",
    Path.home() / ".hermes" / ".env",
    _HERE / ".env",
]


def _ensure_key():
    """Guarantee STRIPE_TEST_KEY is in os.environ before the wallet is called.

    Shell env first (a terminal run inherits ~/.zshrc); else parse the candidate
    .env files (a desktop-app run can't see shell env). Never prints the key —
    only which source supplied it."""
    if os.environ.get("STRIPE_TEST_KEY"):
        return "shell env"
    for env_path in _ENV_CANDIDATES:
        if not env_path.is_file():
            continue
        try:
            text = env_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue   # unreadable / binary .env -> skip, never fatal (fail closed)
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("STRIPE_TEST_KEY=") and "=" in line:
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                # a NUL in the value would raise on os.environ assignment — skip it
                if val and "\x00" not in val:
                    os.environ["STRIPE_TEST_KEY"] = val
                    return str(env_path)
    return None


def _beats(policy):
    """Derive the three demo amounts FROM the live policy so each beat always
    triggers its intended verdict under whatever policy is loaded.

    This is the fix for the drift bug that let the demo print 'stopped TWO… safe'
    while an enterprise-scaled policy actually ALLOWed every spend: the amounts
    are no longer hardcoded constants that can fall out of sync with the caps.
    Each beat carries its EXPECTED verdict; run() asserts the governor returns it
    and refuses to declare success on a mismatch (Principle #10 — proven, not
    narrated)."""
    cap = float(policy.get("per_txn_cap", 100.0))
    hard = float(policy.get("hard_max", 500.0))
    allow_amt = min(12.0, round(cap * 0.4, 2)) or 1.0          # routine, under the cap
    esc_amt = round(cap + (hard - cap) * 0.2, 2) if hard > cap else round(cap + 1, 2)  # over cap, under hard_max
    deny_amt = round(hard * 1.2, 2)                            # over hard_max
    return [
        ("ALLOW — agent spends autonomously",
         dict(agent="hermes", task="earn-loop", merchant="openai", amount=allow_amt,
              currency="usd", category="api-credits", purpose="api-credits"), ALLOW),
        ("ESCALATE — over per-txn cap, held for human",
         dict(agent="hermes", task="deploy", merchant="vercel", amount=esc_amt,
              currency="usd", category="compute", purpose="infra"), ESCALATE),
        ("DENY — over hard max, blocked at the gate",
         dict(agent="hermes", task="scale", merchant="aws", amount=deny_amt,
              currency="usd", category="compute", purpose="compute"), DENY),
    ]

_MARK = {ALLOW: "✅ charged", ESCALATE: "🛑 NOT CALLED — held for human",
         DENY: "🛑 NOT CALLED — blocked"}


def _line(s=""):
    print(s, flush=True)   # flush so a screen recording sees each beat live


def run(live=False, pause=1.2):
    src = _ensure_key()
    if not src:
        _line("✗ STRIPE_TEST_KEY not found.")
        _line("  Terminal:    export STRIPE_TEST_KEY=rk_test_... in ~/.zshrc")
        _line("  Hermes app:  add STRIPE_TEST_KEY=rk_test_... to "
              "~/.hermes/profiles/hackathon/.env")
        return 2

    mode = "LIVE Stripe TEST charges (livemode:false)" if live else "DRY-RUN (no charge)"
    _line("=" * 64)
    _line("  CEREBRO SPEND GOVERNOR  ·  Stripe raw-API wallet demo")
    _line("  mode: %s" % mode)
    _line("  key source: %s" % src)
    _line("=" * 64)

    policy = load_policy(_HERE / "policy.json")
    beats = _beats(policy)

    led = _HERE / "demo_stripe_ledger.jsonl"
    led.unlink(missing_ok=True)
    Path(str(led) + ".checkpoint").unlink(missing_ok=True)

    verdicts = []
    mismatches = []
    for i, (label, req, expected) in enumerate(beats, 1):
        _line("")
        _line("[%d/%d] %s" % (i, len(beats), label))
        _line("       %s wants $%s from %s (%s)"
              % (req["task"], req["amount"], req["merchant"], req["purpose"]))
        time.sleep(pause)
        res = settle_stripe(req, ledger_path=led, dry_run=not live)
        verdict = res.get("verdict")
        verdicts.append(verdict)
        ok = verdict == expected
        if not ok:
            mismatches.append((label, expected, verdict))
        mark = _MARK.get(verdict, res.get("error", "?"))
        # the verdict is CHECKED against the policy contract, not narrated
        _line("       governor → %s   [%s]"
              % (verdict, "✓ matches policy" if ok else "✗ MISMATCH (expected %s)" % expected))
        if verdict == ALLOW and live:
            _line("       wallet   → %s  %s  livemode=%s"
                  % (mark, res.get("charge_id"), res.get("livemode")))
        elif verdict == ALLOW:
            plan = res.get("plan", {})
            _line("       wallet   → would charge %s¢ %s (dry-run, no call)"
                  % (plan.get("amount_cents"), plan.get("currency")))
        else:
            _line("       wallet   → %s" % mark)
        time.sleep(pause)

    led.unlink(missing_ok=True)
    Path(str(led) + ".checkpoint").unlink(missing_ok=True)
    _line("")
    _line("=" * 64)
    # The summary is COMPUTED from actual verdicts and gated on the contract —
    # the demo cannot print "safe" if a verdict disagreed with the policy.
    if mismatches:
        _line("  ✗ DEMO FAILED — governor verdicts did not match the policy contract:")
        for label, exp, got in mismatches:
            _line("      %s — expected %s, got %s" % (label, exp, got))
        _line("=" * 64)
        return 3
    allowed = verdicts.count(ALLOW)
    stopped = sum(1 for v in verdicts if v in (ESCALATE, DENY))
    _line("  The governor released %d spend and stopped %d — every verdict matched the"
          % (allowed, stopped))
    _line("  policy contract, checked on the spot. Verified, not narrated. Unattended, safe.")
    _line("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(run(live="--live" in sys.argv[1:]))

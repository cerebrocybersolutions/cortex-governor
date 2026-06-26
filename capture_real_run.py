"""Capture a REAL governor->Stripe test run into a fresh ledger — the authentic
event log the governance dashboard replays. Real api.stripe.com test-mode charges
(sk_test_ + tok_visa): real ch_ receipts, real hash-chain, ZERO real money.

The Stripe key is read from the macOS Keychain at run time (hex-decoded — security
returns the blob as hex) and held ONLY in process memory: never an arg on a command
line, never an env dump, never a log line. The wallet redacts secrets from every
returned field.

Usage:  python3 capture_real_run.py [--ledger capture_ledger.jsonl]
"""
import argparse
import subprocess
import sys
from pathlib import Path

from stripe_test_wallet import settle_stripe
from ledger import Ledger

KEYCHAIN_SERVICE = "cerebro-stripe-test-key"

# The 3-beat scenario from the demo spec. sandbox_ref carries the real NemoClaw
# event reference when this runs in-sandbox (Phase 3); for the local capture it
# marks the surface so the dashboard can show provenance honestly.
SCENARIO = [
    dict(agent="nvidia-skill", task="earn-loop", merchant="openai", amount=12.0,
         currency="usd", category="api-credits", purpose="api-credits",
         sandbox_ref="local-capture"),
    dict(agent="nvidia-skill", task="deploy", merchant="vercel", amount=150.0,
         currency="usd", category="compute", purpose="infra",
         sandbox_ref="local-capture"),
    dict(agent="nvidia-skill", task="scale", merchant="aws", amount=600.0,
         currency="usd", category="compute", purpose="compute",
         sandbox_ref="local-capture"),
]


def read_key():
    """Read the test key from Keychain. security -w returns hex for blobs it
    doesn't treat as clean strings — decode when the output is all hex. Refuse
    anything that isn't a test-mode key, so this tool can never spend real money."""
    out = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit("no Stripe key in Keychain under %r — add it first" % KEYCHAIN_SERVICE)
    raw = out.stdout.strip()
    if raw and len(raw) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in raw):
        try:
            raw = bytes.fromhex(raw).decode()
        except (ValueError, UnicodeDecodeError):
            pass
    if not raw.startswith(("sk_test_", "rk_test_")):
        sys.exit("Keychain key is not a test-mode key (sk_test_/rk_test_) — refusing")
    return raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default="capture_ledger.jsonl")
    args = ap.parse_args()
    led_path = Path(__file__).resolve().parent / args.ledger
    # Fresh capture — a clean chain is what the dashboard replays.
    led_path.unlink(missing_ok=True)
    led_path.with_name(led_path.name + ".checkpoint").unlink(missing_ok=True)

    key = read_key()
    print("captured run -> %s\n" % led_path.name)
    for req in SCENARIO:
        res = settle_stripe(req, ledger_path=str(led_path), api_key=key, dry_run=False)
        v = res.get("verdict")
        if v == "ALLOW" and res.get("spent"):
            print("  ALLOW    %-7s $%-7.2f -> charged %s  req=%s"
                  % (req["merchant"], req["amount"], res["charge_id"], res["request_id"]))
        elif v == "ESCALATE":
            print("  ESCALATE %-7s $%-7.2f -> held for human (balance unchanged)"
                  % (req["merchant"], req["amount"]))
        elif v == "DENY":
            print("  DENY     %-7s $%-7.2f -> blocked at gate (never reached Stripe)"
                  % (req["merchant"], req["amount"]))
        else:
            print("  %-8s %-7s -> %s" % (v, req["merchant"],
                                         res.get("error") or res.get("reason")))

    # Reconciliation proof: every SETTLED entry ties a real ch_ receipt to the
    # exact gate decision it resolves — same request_id across surfaces.
    led = Ledger(str(led_path))
    ok, bad = led.verify_against_checkpoint()
    print("\n  ledger: %d entries, chain %s"
          % (len(led.entries()), "INTACT" if ok else "BROKEN@%s" % bad))
    for e in led.entries():
        if e.get("verdict") == "SETTLED":
            print("  reconciled: req=%s  receipt=%s  resolves=%s..."
                  % (e["request_id"], e["stripe_receipt"], e["resolves"][:12]))


if __name__ == "__main__":
    main()

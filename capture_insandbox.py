"""Capture a REAL governor->Stripe run from INSIDE the NemoClaw sandbox.

This is the run with all four sponsor pillars real, not labeled:
  - NemoClaw   : the charge executes inside the Landlock-confined sandbox,
                 reaching api.stripe.com only through the stripe egress preset.
  - Nemotron   : the sandbox brain (model id captured from the onboard env).
  - NVIDIA GPU : captured live from nvidia-smi at run time.
  - Stripe     : real test-mode ch_ receipts via the egress.

The Stripe key arrives on STDIN (piped through ssh -> nemoclaw exec), so it never
touches a command line, env dump, or file. Writes the ledger + a run_meta.json
sidecar (the provenance the dashboard reads). Run from the installed skill dir so
the governor modules import.

Usage (from the host):
  security find-generic-password -s cerebro-stripe-test-key -w | xxd -r -p \
    | ssh <gpu-host> '... nemoclaw cerebro-spend exec -- \
        bash -lc "cd <skilldir> && python3 capture_insandbox.py"'
"""
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from stripe_test_wallet import settle_stripe
from ledger import Ledger
from model_roles import reason_beat, ROLES

_HERE = Path(__file__).resolve().parent

# Full-policy-surface run — every beat hits a DISTINCT governor rule, deterministic
# so each recording take is identical. 3 ALLOW (charged) · 3 ESCALATE (3 rules) ·
# 4 DENY (4 rules). The escalate->human-approve resolution is intentionally NOT
# auto-faked here — it becomes the real human-in-the-loop moment.
def _b(merchant, amount, category, purpose, currency="usd", task="earn-loop"):
    return dict(agent="hermes-operator", task=task, merchant=merchant, amount=amount,
                currency=currency, category=category, purpose=purpose)

def build_scenario():
    """Footage-scale run (10 governed decisions) under the $100/$500 per-txn caps — DETERMINISTIC
    so every recording take is identical, and it reproduces the captured `sandbox_ledger.jsonl`
    (3 ALLOW charged · 3 ESCALATE / 3 rules · 4 DENY / 4 rules), the $600 hard-max DENY = the peak.
    Each verdict is machine-verified against policy_engine.evaluate (see __main__ --verify)."""
    return [
        # 3 ALLOW — under both per-txn caps, allow-listed vendor + bound purpose -> charged
        _b("openai",            12.0, "api-credits", "api-credits", task="buy-inference"),
        _b("anthropic",         80.0, "api-credits", "api-credits", task="buy-inference"),
        _b("github",             8.0, "supplies",    "supplies",    task="buy-tooling"),
        # 3 ESCALATE — each a distinct escalation rule (human-in-the-loop moment)
        _b("vercel",           150.0, "compute",     "infra",       task="deploy"),       # amount > per_txn_cap ($100)
        _b("newsaas",           40.0, "api-credits", "api-credits", task="trial"),        # vendor not on allowlist
        _b("openai",            25.0, "api-credits", "marketing",   task="campaign"),     # purpose not in allowed_purposes
        # 4 DENY — each a distinct hard-deny rule; the $600 hard-max DENY is the peak
        _b("aws",              600.0, "compute",     "compute",     task="scale"),        # amount > hard_max ($500)
        _b("sanctioned-vendor", 50.0, "supplies",    "supplies",    task="procure"),      # vendor on deny list
        _b("coinbase",          30.0, "crypto",      "supplies",    task="speculate"),    # category denied
        _b("openai",            20.0, "api-credits", "api-credits", currency="eur",
           task="buy-eu"),                                                                # currency not allowed
    ]


SCENARIO = build_scenario()

EXPECTED_CAPTURE_CONTRACT = [
    # exact captured-footage request fields + policy verdict/rule for every beat.
    # This is stronger than a 3/3/4 spread: a scenario with the right counts but wrong
    # amounts, merchants, purposes, or rules must not pass the recording pre-check.
    dict(agent="hermes-operator", task="buy-inference", merchant="openai",
         amount=12.0, currency="usd", category="api-credits", purpose="api-credits",
         verdict="ALLOW", rule="ok"),
    dict(agent="hermes-operator", task="buy-inference", merchant="anthropic",
         amount=80.0, currency="usd", category="api-credits", purpose="api-credits",
         verdict="ALLOW", rule="ok"),
    dict(agent="hermes-operator", task="buy-tooling", merchant="github",
         amount=8.0, currency="usd", category="supplies", purpose="supplies",
         verdict="ALLOW", rule="ok"),
    dict(agent="hermes-operator", task="deploy", merchant="vercel",
         amount=150.0, currency="usd", category="compute", purpose="infra",
         verdict="ESCALATE", rule="amount.per_txn_cap"),
    dict(agent="hermes-operator", task="trial", merchant="newsaas",
         amount=40.0, currency="usd", category="api-credits", purpose="api-credits",
         verdict="ESCALATE", rule="vendor.not_allowlisted"),
    dict(agent="hermes-operator", task="campaign", merchant="openai",
         amount=25.0, currency="usd", category="api-credits", purpose="marketing",
         verdict="ESCALATE", rule="purpose.unbound"),
    dict(agent="hermes-operator", task="scale", merchant="aws",
         amount=600.0, currency="usd", category="compute", purpose="compute",
         verdict="DENY", rule="amount.hard_max"),
    dict(agent="hermes-operator", task="procure", merchant="sanctioned-vendor",
         amount=50.0, currency="usd", category="supplies", purpose="supplies",
         verdict="DENY", rule="vendor.deny"),
    dict(agent="hermes-operator", task="speculate", merchant="coinbase",
         amount=30.0, currency="usd", category="crypto", purpose="supplies",
         verdict="DENY", rule="category.deny"),
    dict(agent="hermes-operator", task="buy-eu", merchant="openai",
         amount=20.0, currency="eur", category="api-credits", purpose="api-credits",
         verdict="DENY", rule="invalid_request.currency"),
]


def read_key_from_stdin():
    key = (sys.stdin.readline() or "").strip()
    if not key.startswith(("rk_test_", "sk_test_")):
        sys.exit("stdin key is not a test-mode key (rk_test_/sk_test_) — refusing")
    return key


def gpu_name():
    """Live GPU identity — the real 'runs on NVIDIA hardware' proof."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=10)
        line = out.stdout.strip().splitlines()
        if out.returncode == 0 and line:
            return line[0].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "GPU not visible from sandbox"


def verify_scenario():
    """Offline self-check: evaluate every beat through the policy engine and assert the
    exact captured-footage contract: 10 beats, the captured small-dollar amounts, and
    the expected policy verdict+rule for each beat. No Stripe, no stdin, no charge —
    proves the captured scenario still matches policy.json before any real run."""
    from policy_engine import evaluate, load_policy
    pol = load_policy(str(_HERE / "policy.json"))
    want = {"ALLOW": 3, "ESCALATE": 3, "DENY": 4}
    got = {}
    if len(SCENARIO) != len(EXPECTED_CAPTURE_CONTRACT):
        raise ValueError("scenario length %s != expected %s"
                         % (len(SCENARIO), len(EXPECTED_CAPTURE_CONTRACT)))
    request_fields = ("agent", "task", "merchant", "amount", "currency", "category", "purpose")
    for i, (req, expected) in enumerate(zip(SCENARIO, EXPECTED_CAPTURE_CONTRACT), start=1):
        mismatched = [k for k in request_fields if req.get(k) != expected[k]]
        if mismatched:
            raise ValueError("beat %d request drift: %s" % (
                i, ", ".join("%s=%r expected %r" % (k, req.get(k), expected[k])
                             for k in mismatched)
            ))
        decision = evaluate(req, pol)
        if decision.verdict != expected["verdict"] or decision.rule != expected["rule"]:
            raise ValueError("beat %d policy drift: got %s/%s expected %s/%s"
                             % (i, decision.verdict, decision.rule,
                                expected["verdict"], expected["rule"]))
        got[decision.verdict] = got.get(decision.verdict, 0) + 1
        print("  %-9s %-24s $%-7.2f %s" %
              (decision.verdict, decision.rule, req["amount"], req["merchant"]))
    print("  spread:", got)
    if got != want:
        raise ValueError("verdict spread %s != expected %s" % (got, want))
    print("  ✅ scenario matches captured contract + policy.json (3 ALLOW / 3 ESCALATE / 4 DENY)")


def main():
    if "--verify" in sys.argv:
        try:
            verify_scenario()
        except ValueError as e:
            print("VERIFY FAILED — %s" % e, file=sys.stderr)
            return 1
        return 0
    # Gate the real recording on the scenario still matching policy.json — a drifted
    # scenario must NOT record (Principle #10: the capture can't proceed while its own
    # pre-check is red). Fail closed: refuse rather than record an inconsistent run.
    try:
        verify_scenario()
    except ValueError as e:
        print("REFUSING to record — scenario does not match policy.json: %s" % e, file=sys.stderr)
        return 1
    key = read_key_from_stdin()
    led_path = _HERE / "sandbox_ledger.jsonl"
    led_path.unlink(missing_ok=True)
    led_path.with_name(led_path.name + ".checkpoint").unlink(missing_ok=True)

    run_id = uuid.uuid4().hex[:12]
    sandbox = os.environ.get("NEMOCLAW_SANDBOX") or "cerebro-spend"
    model = os.environ.get("NEMOCLAW_MODEL") or "nvidia/nemotron-3-ultra-550b-a55b:free"
    # Provenance is split correctly: the SANDBOX (NemoClaw) runs on the local 4090;
    # the model (Nemotron 3 Ultra) is reasoned via OpenRouter (compatible-endpoint),
    # NOT on the GPU. Conflating them was wrong — keep the two surfaces distinct.
    endpoint = os.environ.get("NEMOCLAW_ENDPOINT_URL", "")
    inference = "OpenRouter" if "openrouter" in endpoint.lower() else (endpoint or "OpenRouter (compatible-endpoint)")
    meta = {
        "origin": "in-sandbox",
        "sandbox": sandbox,
        "runtime": "NemoClaw — Landlock-confined sandbox",
        "sandbox_gpu": gpu_name(),          # the 4090 the SANDBOX runs on
        "model": model,
        "inference": inference,             # where Nemotron actually reasons (cloud)
        "operator": "Hermes agent",
        "run_id": run_id,
        "host": os.uname().nodename,
    }
    # Provenance only (does NOT touch the gate/charge path): snapshot the governing
    # policy values + a sha256 of the exact policy.json bytes into the captured meta,
    # so every on-screen number (budget / per-txn cap / hard max) traces to this
    # captured file and stays checkable against policy.json on disk.
    pol_raw = (_HERE / "policy.json").read_bytes()
    pol = json.loads(pol_raw)
    meta["policy"] = {
        "budget": pol.get("rate_window", {}).get("max_amount"),
        "per_txn_cap": pol.get("per_txn_cap"),
        "hard_max": pol.get("hard_max"),
        "sha256": hashlib.sha256(pol_raw).hexdigest(),
    }
    # right-sized model family — the routing table the dashboard shows (which tier did what).
    # No model approves money; this is provenance, the gate stays deterministic (Cortex).
    meta["model_roles"] = {r: {"tier": s["tier"], "model": s["model"], "desc": s["desc"],
                               "where": "local-gpu (RTX 4090)" if s["local"] else "cloud (OpenRouter)"}
                           for r, s in ROLES.items()}
    print("in-sandbox capture run_id=%s  sandbox_gpu=%s  model=%s via %s"
          % (run_id, meta["sandbox_gpu"], model, inference))

    reasoning = []
    for i, base in enumerate(SCENARIO):
        # right-sized model reasoning BEFORE the gate (Nano triages, 30B extracts, Ultra plans
        # the judgment calls). Provenance only — Cortex still decides. Gated so a quick capture
        # runs without the model fleet; set CORTEX_REASONING=1 for the full real run.
        if os.environ.get("CORTEX_REASONING"):
            evs = reason_beat(base)
            reasoning.append({"beat": i + 1, "merchant": base["merchant"], "events": evs})
            for e in evs:
                print("    %-7s %-5s %-26s reached=%s %dms"
                      % (e["role"], e["tier"], e["model"][:26], e["reached"], e["ms"]))
        # sandbox_ref ties this beat to a real NemoClaw event id for cross-ref.
        req = dict(base, sandbox_ref="nemoclaw:%s:%s#%d" % (sandbox, run_id, i + 1))
        res = settle_stripe(req, ledger_path=str(led_path), dry_run=False, api_key=key)
        v = res.get("verdict")
        rcpt = res.get("charge_id") or res.get("reason") or res.get("error")
        print("  %-8s %-7s $%-7.2f -> %s" % (v, req["merchant"], req["amount"], rcpt))

    led = Ledger(str(led_path))
    ok, bad = led.verify_against_checkpoint()
    charged = [e for e in led.entries() if e.get("verdict") == "SETTLED"]
    meta["n_charged"] = len(charged)
    meta["total_charged"] = sum(e.get("amount") or 0 for e in charged)
    meta["reasoning"] = reasoning          # per-beat role events (empty unless CORTEX_REASONING=1)
    (_HERE / "sandbox_run_meta.json").write_text(json.dumps(meta, indent=2))
    print("\n  ledger: %d entries, chain %s" % (len(led.entries()),
                                                "INTACT" if ok else "BROKEN@%s" % bad))
    for e in charged:
        print("  reconciled: req=%s receipt=%s sandbox_ref=%s"
              % (e["request_id"], e["stripe_receipt"], e.get("sandbox_ref")))


if __name__ == "__main__":
    raise SystemExit(main())

"""Self-check for the ESCALATE resolution loop. Run: python3 test_escalation.py

Covers the happy path PLUS the Codex review exploit classes: forged/double/empty
resolutions don't close an escalation, an approval can't override a hard-deny
policy, and a tampered override breaks the hash chain.
"""
import json
import sys
import tempfile
import time
from pathlib import Path

from governor import gate
from policy_engine import ALLOW, DENY, ESCALATE, evaluate, load_policy, _recent_allow_amounts
from ledger import Ledger
from escalation import pending, resolve, invalid_resolutions

_HERE = Path(__file__).resolve().parent
POLICY = _HERE / "policy.json"


def _fresh_ledger():
    f = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, dir=_HERE)
    p = Path(f.name); f.close()
    p.unlink(missing_ok=True)
    return p


def _cleanup(p):
    p.unlink(missing_ok=True)
    p.with_name(p.name + ".checkpoint").unlink(missing_ok=True)


def test_happy_path():
    lp = _fresh_ledger()
    try:
        req = dict(agent="nvidia-skill", task="deploy", merchant="vercel",
                   amount=150, currency="usd", category="compute", purpose="infra")
        dec = gate(req, policy_path=POLICY, ledger_path=lp)
        assert dec.verdict == ESCALATE and dec.rule == "amount.per_txn_cap", dec

        led = Ledger(lp)
        pend = pending(led)
        assert len(pend) == 1, len(pend)
        target = pend[0]["entry_hash"]

        assert resolve(led, "deadbeef" * 8, approve=True, approver="operator") is None
        assert resolve(led, target, approve=True, approver="   ") is None      # empty approver

        res = resolve(led, target, approve=True, approver="operator", policy_path=POLICY)
        assert res and res["verdict"] == ALLOW and res["rule_fired"] == "escalation.approved"
        assert res["resolves"] == target and res["approver"] == "operator"
        assert pending(Ledger(lp)) == [], "escalation should be resolved"

        # double-resolve is a no-op (target no longer pending)
        assert resolve(Ledger(lp), target, approve=True, approver="operator") is None

        # approved spend counts toward the rate-window (verdict ALLOW)
        assert 150 in _recent_allow_amounts(Ledger(lp), 3600, time.time())

        ok, bad = Ledger(lp).verify()
        assert ok, bad
        print("  happy path (escalate->approve->resume, double-resolve no-op): OK")
    finally:
        _cleanup(lp)


def test_forged_resolution_ignored():
    lp = _fresh_ledger()
    try:
        req = dict(agent="a", task="t", merchant="shady-llc", amount=25, currency="usd",
                   category="compute", purpose="compute")
        gate(req, policy_path=POLICY, ledger_path=lp)        # ESCALATE not_allowlisted
        led = Ledger(lp)
        target = pending(led)[0]["entry_hash"]

        # Forge a hash-valid resolution pointing at a NON-existent escalation.
        led.append(agent="x", task="t", merchant="shady-llc", amount=25, currency="usd",
                   category="compute", purpose="compute",
                   rule_fired="escalation.approved", verdict=ALLOW,
                   resolves="f" * 64, approver="attacker")
        # Forge one with a verdict that disagrees with its rule.
        led.append(agent="x", task="t", merchant="shady-llc", amount=25, currency="usd",
                   category="compute", purpose="compute",
                   rule_fired="escalation.approved", verdict=DENY,
                   resolves=target, approver="attacker")

        led2 = Ledger(lp)
        assert [e["entry_hash"] for e in pending(led2)] == [target], \
            "forged resolutions must NOT close the real escalation"
        assert len(invalid_resolutions(led2)) == 2, "both forgeries flagged invalid"
        assert Ledger(lp).verify()[0], "forgeries are hash-valid (semantic, not tamper)"
        print("  forged/mismatched resolutions ignored: OK")
    finally:
        _cleanup(lp)


def test_approval_cannot_override_hard_deny():
    lp = _fresh_ledger()
    try:
        # Escalate a $150 spend under the live policy (over the $100 per_txn cap).
        req = dict(agent="a", task="t", merchant="vercel", amount=150, currency="usd",
                   category="compute", purpose="infra")
        dec = gate(req, policy_path=POLICY, ledger_path=lp)
        assert dec.verdict == ESCALATE, dec
        led = Ledger(lp)
        target = pending(led)[0]["entry_hash"]

        # Policy tightens before approval: vercel is now a denied vendor.
        tightened = json.loads(POLICY.read_text())
        tightened.setdefault("vendors", {}).setdefault("deny", []).append("vercel")
        tp = _HERE / "tightened_policy.tmp.json"
        tp.write_text(json.dumps(tightened))
        try:
            res = resolve(led, target, approve=True, approver="operator", policy_path=tp)
            assert res["verdict"] == DENY and res["rule_fired"] == "escalation.denied", res
            assert res.get("recheck_denied_by") == "vendor.deny", res
        finally:
            tp.unlink(missing_ok=True)
        print("  approval cannot override hard-deny (re-check): OK")
    finally:
        _cleanup(lp)


def test_tampered_override_breaks_chain():
    lp = _fresh_ledger()
    try:
        req = dict(agent="a", task="t", merchant="vercel", amount=150, currency="usd",
                   category="compute", purpose="infra")
        gate(req, policy_path=POLICY, ledger_path=lp)
        led = Ledger(lp)
        target = pending(led)[0]["entry_hash"]
        resolve(led, target, approve=True, approver="operator", policy_path=POLICY)

        # Flip approver on the recorded resolution WITHOUT recomputing the hash.
        lines = lp.read_text().splitlines()
        idx = next(i for i, ln in enumerate(lines)
                   if json.loads(ln).get("rule_fired") == "escalation.approved")
        ent = json.loads(lines[idx]); ent["approver"] = "attacker"; lines[idx] = json.dumps(ent)
        tampered = _HERE / "esc_tampered.jsonl"
        tampered.write_text("\n".join(lines) + "\n")
        ok, bad = Ledger(tampered).verify()
        tampered.unlink(missing_ok=True)
        assert (not ok) and bad == idx, ("forged approver must break the chain", ok, bad)
        print("  tampered override caught by chain verify: OK")
    finally:
        _cleanup(lp)


if __name__ == "__main__":
    test_happy_path()
    test_forged_resolution_ignored()
    test_approval_cannot_override_hard_deny()
    test_tampered_override_breaks_chain()
    print("RESULT: PASS — escalate/approve/resume + forged/double/empty rejected + "
          "approval can't override hard-deny + tamper caught")

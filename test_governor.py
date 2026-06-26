"""Self-check for the governor (H3 policy engine + H4 ledger).

Run from this dir:  python3 test_governor.py
Covers the original happy-path PLUS every exploit class from the 2026-06-17 Codex
adversarial review. ponytail: assert-based, no framework.
"""
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import policy_engine as pe
from ledger import Ledger, _entry_hash, _hashed_fields, GENESIS

_HERE = Path(__file__).resolve().parent

POLICY = {
    "allowed_currencies": ["usd"],
    "hard_max": 500.0,
    "per_txn_cap": 100.0,
    "vendors": {"allow": ["openai", "aws"], "deny": ["badco"]},
    "categories": {"deny": ["gambling"]},
    "rate_window": {"seconds": 3600, "max_count": 3, "max_amount": 150.0},
    "allowed_purposes": ["api-credits", "compute"],
}


def _req(**kw):
    base = dict(agent="hermes", task="t_demo", merchant="openai", amount=10.0,
                currency="usd", category="api-credits", purpose="api-credits",
                ts=time.time())
    base.update(kw)
    return base


def test_policy():
    assert pe.evaluate(_req(), POLICY).verdict == pe.ALLOW
    assert pe.evaluate(_req(amount=120.0), POLICY).verdict == pe.ESCALATE      # over per_txn, under window
    assert pe.evaluate(_req(amount=600.0), POLICY).verdict == pe.DENY          # over hard_max
    assert pe.evaluate(_req(merchant="badco"), POLICY).verdict == pe.DENY      # denied vendor
    assert pe.evaluate(_req(merchant="randomco"), POLICY).verdict == pe.ESCALATE  # not allowlisted
    assert pe.evaluate(_req(category="gambling"), POLICY).verdict == pe.DENY   # denied category
    assert pe.evaluate(_req(purpose="vacation"), POLICY).verdict == pe.ESCALATE   # unbound purpose
    print("  policy verdicts: OK")


def test_rate_window_and_ledger():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "ledger.jsonl"
        led = Ledger(p)
        now = time.time()
        for _ in range(3):
            req = _req(amount=40.0, ts=now)
            dec = pe.evaluate(req, POLICY, ledger=led, now=now)
            assert dec.verdict == pe.ALLOW, dec
            led.record(req, dec.rule, dec.verdict, now=now)
        dec = pe.evaluate(_req(amount=40.0, ts=now), POLICY, ledger=led, now=now)
        assert dec.verdict == pe.DENY and "max_count" in dec.rule, dec
        print("  rate-window: OK")

        ok, bad = led.verify()
        assert ok and bad is None, (ok, bad)
        print("  ledger chain verify: OK")

        # Naive tamper (edit, no recompute) — internal walk catches at index 0.
        lines = p.read_text().splitlines()
        e0 = json.loads(lines[0]); e0["amount"] = 9999.0; lines[0] = json.dumps(e0)
        p.write_text("\n".join(lines) + "\n")
        ok2, bad2 = Ledger(p).verify()
        assert (not ok2) and bad2 == 0, (ok2, bad2)
        print("  tamper detection: OK")


# --- Codex review exploit classes ---

def test_invalid_amounts_deny():
    # missing / None / zero / negative / string / bool / NaN / infinity all DENY.
    cases = [
        ("missing", _req()), ("none", _req(amount=None)), ("zero", _req(amount=0)),
        ("negative", _req(amount=-50.0)), ("string", _req(amount="100")),
        ("bool", _req(amount=True)), ("nan", _req(amount=float("nan"))),
        ("inf", _req(amount=float("inf"))),
    ]
    cases[0][1].pop("amount")  # truly missing
    for name, req in cases:
        dec = pe.evaluate(req, POLICY)
        assert dec.verdict == pe.DENY and dec.rule == "invalid_request.amount", (name, dec)
    print("  invalid amounts -> DENY: OK")


def test_caller_ts_cannot_evade_window():
    # Attacker stamps ts=0 on every spend hoping prior ALLOWs fall outside the
    # window. The governor-owned decision ts (=now) is what the window reads.
    pol = dict(POLICY, rate_window={"seconds": 3600, "max_count": 99, "max_amount": 300.0})
    with tempfile.TemporaryDirectory() as d:
        led = Ledger(Path(d) / "l.jsonl")
        now = time.time()
        for _ in range(3):                       # 3 x 90 = 270, under 300
            req = _req(amount=90.0, ts=0)
            dec = pe.evaluate(req, pol, ledger=led, now=now)
            assert dec.verdict == pe.ALLOW, dec
            led.record(req, dec.rule, dec.verdict, now=now)
        # 4th would push window to 360 > 300 — must DENY despite caller ts=0.
        dec = pe.evaluate(_req(amount=90.0, ts=0), pol, ledger=led, now=now)
        assert dec.verdict == pe.DENY and "max_amount" in dec.rule, dec
        # confirm the malicious ts didn't become the enforced ts
        assert all(e["ts"] != 0 for e in led.entries()), "decision ts must be governor-owned"
    print("  caller ts=0 cannot evade window: OK")


def test_hard_deny_beats_soft_escalate():
    # Denied category + amount over per_txn_cap: DENY must win, not ESCALATE.
    dec = pe.evaluate(_req(category="gambling", amount=150.0), POLICY)
    assert dec.verdict == pe.DENY and dec.rule == "category.deny", dec
    # Denied vendor + over cap: DENY wins.
    dec = pe.evaluate(_req(merchant="badco", amount=150.0), POLICY)
    assert dec.verdict == pe.DENY and dec.rule == "vendor.deny", dec
    print("  hard-DENY beats soft-ESCALATE: OK")


def test_normalization_blocks_variants():
    assert pe.evaluate(_req(category="Gambling"), POLICY).verdict == pe.DENY     # case
    assert pe.evaluate(_req(category="gambling "), POLICY).verdict == pe.DENY    # trailing space
    assert pe.evaluate(_req(merchant="BADCO"), POLICY).verdict == pe.DENY        # denied vendor case
    assert pe.evaluate(_req(merchant="OpenAI"), POLICY).verdict == pe.ALLOW      # allowed vendor case
    print("  normalization blocks case/space variants: OK")


def test_currency_enforced():
    assert pe.evaluate(_req(currency="kwd"), POLICY).verdict == pe.DENY
    assert pe.evaluate(_req(currency="kwd"), POLICY).rule == "invalid_request.currency"
    assert pe.evaluate(_req(currency="USD"), POLICY).verdict == pe.ALLOW         # normalized
    assert pe.evaluate(_req(currency="usd "), POLICY).verdict == pe.ALLOW
    print("  currency enforced: OK")


def _recompute(entries):
    """Mimic an attacker who edits an entry and recomputes the whole chain."""
    prev = GENESIS
    out = []
    for e in entries:
        fields = _hashed_fields(e)
        e2 = dict(fields)
        e2["prev_hash"] = prev
        e2["entry_hash"] = _entry_hash(fields, prev)
        prev = e2["entry_hash"]
        out.append(e2)
    return out


def test_checkpoint_catches_recompute_and_tail_delete():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "l.jsonl"
        led = Ledger(p)
        now = time.time()
        for i in range(4):
            req = _req(amount=10.0, ts=now)
            dec = pe.evaluate(req, POLICY, ledger=led, now=now)
            led.record(req, dec.rule, dec.verdict, now=now)
        good_head, good_count = led.head_hash, len(led.entries())

        # internal walk alone PASSES a recompute — the checkpoint must catch it.
        entries = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
        entries[1]["amount"] = 999999.0
        rebuilt = _recompute(entries)
        p.write_text("\n".join(json.dumps(e) for e in rebuilt) + "\n")
        walk_ok, _ = Ledger(p).verify()                      # no checkpoint args
        assert walk_ok, "recompute should pass a naive internal walk"
        anchored_ok, bad = Ledger(p).verify(expected_head_hash=good_head,
                                            expected_count=good_count)
        assert (not anchored_ok) and bad == -1, ("recompute must fail vs checkpoint", bad)

        # tail deletion: drop the last entry, count mismatch caught.
        rebuilt2 = rebuilt[:-1]
        p.write_text("\n".join(json.dumps(e) for e in rebuilt2) + "\n")
        del_ok, _ = Ledger(p).verify(expected_head_hash=good_head, expected_count=good_count)
        assert not del_ok, "tail deletion must fail vs checkpoint"
    print("  checkpoint catches recompute + tail-delete: OK")


def test_cli_stable_deny_no_traceback():
    with tempfile.TemporaryDirectory() as d:
        env = {**os.environ, "CEREBRO_GOVERNOR_LEDGER": str(Path(d) / "cli.jsonl")}

        def run(stdin):
            return subprocess.run([sys.executable, "governor.py"], input=stdin,
                                  capture_output=True, text=True, env=env, cwd=str(_HERE))

        r = run("{ not valid json")
        assert r.returncode == 1 and r.stdout.startswith("DENY invalid_request"), r.stdout
        assert "Traceback" not in (r.stdout + r.stderr), "no traceback on bad JSON"

        r = run(json.dumps(_req(amount="100")))      # string amount: stable DENY, no crash
        assert r.returncode == 1 and r.stdout.startswith("DENY invalid_request.amount"), r.stdout

        r = run(json.dumps(_req()))                   # valid ALLOW -> exit 0
        assert r.returncode == 0 and r.stdout.startswith("ALLOW"), r.stdout

        # CLI subprocess uses the live policy.json (footage: $100 cap / $500 hard_max).
        r = run(json.dumps(_req(amount=600.0)))       # over hard_max -> DENY -> exit 1
        assert r.returncode == 1 and r.stdout.startswith("DENY"), r.stdout

        r = run(json.dumps(_req(amount=150.0)))       # over per_txn cap -> ESCALATE -> exit 2
        assert r.returncode == 2 and r.stdout.startswith("ESCALATE"), r.stdout
    print("  CLI stable DENY + exit codes, no traceback: OK")


if __name__ == "__main__":
    test_policy()
    test_rate_window_and_ledger()
    test_invalid_amounts_deny()
    test_caller_ts_cannot_evade_window()
    test_hard_deny_beats_soft_escalate()
    test_normalization_blocks_variants()
    test_currency_enforced()
    test_checkpoint_catches_recompute_and_tail_delete()
    test_cli_stable_deny_no_traceback()
    print("ALL GOVERNOR TESTS PASS")

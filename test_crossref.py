"""Cross-reference anchor tests — the anti-fake foundation.

ONE request_id must reconcile the ledger gate entry <-> the Stripe settlement
entry (receipt) <-> the sandbox event. These asserts fail if that chain breaks,
which is exactly the "is the dashboard faked?" risk the demo spec calls out.
"""
import tempfile
import unittest
from pathlib import Path

from policy_engine import mint_request_id, ALLOW, DENY, ESCALATE
from ledger import Ledger
from governor import gate
from stripe_test_wallet import settle_stripe


def _tmp_ledger():
    p = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    Path(p).unlink(missing_ok=True)
    return p


def _cleanup(p):
    Path(p).unlink(missing_ok=True)
    Path(p + ".checkpoint").unlink(missing_ok=True)


ALLOW_REQ = dict(agent="hermes", task="earn-loop", merchant="openai", amount=12.0,
                 currency="usd", category="api-credits", purpose="api-credits")


def _fake_charge(api_key, params, idem):
    """Mimic a Stripe test-mode success without touching the network."""
    return {"id": "ch_TESTcrossref01", "status": "succeeded", "paid": True,
            "amount": params["amount"], "currency": params["currency"], "livemode": False}


class CrossRef(unittest.TestCase):
    def test_request_id_deterministic_and_case_stable(self):
        a = mint_request_id(dict(ALLOW_REQ, merchant="OpenAI", currency="USD "))
        b = mint_request_id(dict(ALLOW_REQ, merchant="openai", currency="usd"))
        self.assertEqual(a, b)               # casing/whitespace collapse to one id
        self.assertTrue(a.startswith("req_"))

    def test_caller_request_id_wins(self):
        # the NemoClaw sandbox injects its own id at the call site — it must survive
        self.assertEqual(
            mint_request_id(dict(ALLOW_REQ, request_id="rid-from-sandbox")),
            "rid-from-sandbox")

    def test_gate_entry_carries_request_id_and_sandbox_ref(self):
        led_path = _tmp_ledger()
        try:
            req = dict(ALLOW_REQ, sandbox_ref="nemoclaw-evt-77")
            # gate stamps a COPY (never mutates the caller's dict), so the id the
            # entry carries is the deterministic mint of the original request.
            expected_id = mint_request_id(req)
            gate(req, ledger_path=led_path)
            self.assertNotIn("request_id", req)   # caller's dict untouched
            entry = Ledger(led_path).entries()[-1]
            self.assertEqual(entry["verdict"], ALLOW)
            self.assertEqual(entry["request_id"], expected_id)
            self.assertEqual(entry["sandbox_ref"], "nemoclaw-evt-77")
        finally:
            _cleanup(led_path)

    def test_settlement_reconciles_three_surfaces(self):
        led_path = _tmp_ledger()
        try:
            req = dict(ALLOW_REQ, sandbox_ref="nemoclaw-evt-88")
            res = settle_stripe(req, ledger_path=led_path, api_key="sk_test_" + "x",
                                dry_run=False, charge_fn=_fake_charge)
            self.assertEqual(res["verdict"], ALLOW)
            self.assertTrue(res["spent"])
            self.assertEqual(res["charge_id"], "ch_TESTcrossref01")

            entries = Ledger(led_path).entries()
            gate_entry, settle_entry = entries[-2], entries[-1]
            # the returned result is the dashboard's source of truth for the id
            rid = res["request_id"]
            self.assertEqual(rid, mint_request_id(req))   # deterministic from the request
            # same request_id on the gate decision, the returned result, the settlement
            self.assertEqual(gate_entry["request_id"], rid)
            self.assertEqual(settle_entry["request_id"], rid)
            # the settlement resolves the exact gate entry + carries the Stripe receipt
            self.assertEqual(settle_entry["resolves"], gate_entry["entry_hash"])
            self.assertEqual(settle_entry["stripe_receipt"], "ch_TESTcrossref01")
            self.assertEqual(settle_entry["sandbox_ref"], "nemoclaw-evt-88")
            self.assertEqual(settle_entry["verdict"], "SETTLED")
            # the result echoes the settlement linkage for the dashboard
            self.assertEqual(res["resolves"], gate_entry["entry_hash"])
            # chain still intact after the second append
            self.assertEqual(Ledger(led_path).verify_against_checkpoint(), (True, None))
        finally:
            _cleanup(led_path)

    def test_escalate_and_deny_write_no_settlement(self):
        for req, want in [
            (dict(ALLOW_REQ, merchant="vercel", amount=150.0,     # over $100 per_txn cap
                  category="compute", purpose="infra"), ESCALATE),
            (dict(ALLOW_REQ, merchant="aws", amount=600.0,        # over $500 hard_max
                  category="compute", purpose="compute"), DENY),
        ]:
            led_path = _tmp_ledger()
            try:
                res = settle_stripe(req, ledger_path=led_path, api_key="sk_test_" + "x",
                                    dry_run=False, charge_fn=_fake_charge)
                self.assertEqual(res["verdict"], want)
                self.assertFalse(res["spent"])
                # no SETTLED entry — the receipt surface never gets a row
                self.assertFalse(any(e.get("verdict") == "SETTLED"
                                     for e in Ledger(led_path).entries()))
            finally:
                _cleanup(led_path)


if __name__ == "__main__":
    unittest.main()

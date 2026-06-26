"""Self-check for the governor->Stripe raw-API bridge. Run: python3 test_stripe_test_wallet.py

Same threat model as test_link_wallet.py, retargeted at the Stripe path:
DENY/ESCALATE never charge, the verdict is re-gated (no caller verdict), a live
key is refused without explicit opt-in, sub-cent amounts are rejected, the raw
Stripe object never leaks (only allowlisted fields), and ALLOW charges with the
exact governor-approved cents. No network + no real key: the Stripe POST is
injected via charge_fn.
"""
import base64
import io
import json
import tempfile
import urllib.error
from pathlib import Path

import stripe_test_wallet as sw
from policy_engine import ALLOW, DENY, ESCALATE

_HERE = Path(__file__).resolve().parent
POLICY = _HERE / "policy.json"
TEST_KEY = "rk_test_" + "FAKE_FOR_TESTS_ONLY"   # synthetic fixture, not a real key

ALLOW_REQ = dict(task="t", merchant="openai", amount=12.0, currency="usd",
                 category="api-credits", purpose="api-credits")
DENY_REQ = dict(task="t", merchant="aws", amount=600.0, currency="usd",
                category="compute", purpose="compute")          # over $500 hard_max
ESC_REQ = dict(task="t", merchant="vercel", amount=150.0, currency="usd",
               category="compute", purpose="infra")             # over $100 per_txn


def _led():
    p = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, dir=_HERE).name
    Path(p).unlink(missing_ok=True)
    return p


def _clean(p):
    Path(p).unlink(missing_ok=True)
    Path(p + ".checkpoint").unlink(missing_ok=True)


def _fake_charge(captured):
    """A charge_fn that records what it was asked to charge and returns a Stripe-
    shaped object carrying secrets that MUST NOT leak into settle's output."""
    def fn(api_key, params, idempotency_key):
        captured["api_key"] = api_key
        captured["params"] = params
        captured["idem"] = idempotency_key
        return {
            "id": "ch_TESTONLY123",
            "object": "charge",
            "status": "succeeded",
            "paid": True,
            "amount": params["amount"],
            "currency": params["currency"],
            "livemode": False,
            "source": {"id": "card_LEAKME", "last4": "4242", "number": "4242424242424242"},
        }
    return fn


def test_deny_never_charges():
    p = _led()
    try:
        cap = {}
        res = sw.settle_stripe(DENY_REQ, policy_path=POLICY, ledger_path=p,
                               api_key=TEST_KEY, dry_run=False, charge_fn=_fake_charge(cap))
        assert res["verdict"] == DENY and res["spent"] is False, res
        assert cap == {}, ("charge_fn must not be called on DENY", cap)
    finally:
        _clean(p)


def test_escalate_holds_no_charge():
    p = _led()
    try:
        cap = {}
        res = sw.settle_stripe(ESC_REQ, policy_path=POLICY, ledger_path=p,
                               api_key=TEST_KEY, dry_run=False, charge_fn=_fake_charge(cap))
        assert res["verdict"] == ESCALATE and res["spent"] is False, res
        assert "human" in res.get("reason", ""), res
        assert cap == {}, ("ESCALATE is the stopped-spend moment", cap)
    finally:
        _clean(p)


def test_allow_dryrun_plans_exact_cents_no_call():
    p = _led()
    try:
        cap = {}
        res = sw.settle_stripe(ALLOW_REQ, policy_path=POLICY, ledger_path=p,
                               api_key=TEST_KEY, dry_run=True, charge_fn=_fake_charge(cap))
        assert res["verdict"] == ALLOW and res["spent"] is False and res["dry_run"] is True, res
        assert res["plan"]["amount_cents"] == 1200, res     # $12.00 -> 1200 cents
        assert cap == {}, ("dry_run must not call Stripe", cap)
    finally:
        _clean(p)


def test_allow_charges_exact_cents():
    p = _led()
    try:
        cap = {}
        res = sw.settle_stripe(ALLOW_REQ, policy_path=POLICY, ledger_path=p,
                               api_key=TEST_KEY, dry_run=False, charge_fn=_fake_charge(cap))
        assert res["verdict"] == ALLOW and res["spent"] is True, res
        assert res["charge_id"] == "ch_TESTONLY123", res
        assert cap["params"]["amount"] == 1200 and cap["params"]["source"] == "tok_visa", cap
    finally:
        _clean(p)


def test_no_raw_stripe_object_leaks():
    p = _led()
    try:
        res = sw.settle_stripe(ALLOW_REQ, policy_path=POLICY, ledger_path=p,
                               api_key=TEST_KEY, dry_run=False, charge_fn=_fake_charge({}))
        blob = json.dumps(res)
        assert "card_LEAKME" not in blob and "4242424242424242" not in blob, ("LEAK", res)
        assert "source" not in res and "object" not in res, res
        assert res.get("livemode") is False, res
    finally:
        _clean(p)


def test_live_key_refused_without_optin():
    p = _led()
    try:
        cap = {}
        res = sw.settle_stripe(ALLOW_REQ, policy_path=POLICY, ledger_path=p,
                               api_key="rk_live_" + "REAL_MONEY", dry_run=False,  # live-prefix triggers the real-money guard
                               charge_fn=_fake_charge(cap))
        assert res["spent"] is False and "REAL-MONEY GUARD" in res.get("error", ""), res
        assert cap == {}, ("live key must not reach the charge fn", cap)
    finally:
        _clean(p)


def test_missing_key_fails_closed():
    p = _led()
    try:
        res = sw.settle_stripe(ALLOW_REQ, policy_path=POLICY, ledger_path=p,
                               api_key=None, dry_run=True)
        assert res["spent"] is False and "no Stripe key" in res.get("error", ""), res
    finally:
        _clean(p)


def test_subcent_amount_rejected():
    p = _led()
    try:
        cap = {}
        res = sw.settle_stripe(dict(ALLOW_REQ, amount=0.004), policy_path=POLICY, ledger_path=p,
                               api_key=TEST_KEY, dry_run=False, charge_fn=_fake_charge(cap))
        assert res["spent"] is False and cap == {}, res
    finally:
        _clean(p)


def test_key_never_returned():
    p = _led()
    try:
        res = sw.settle_stripe(ALLOW_REQ, policy_path=POLICY, ledger_path=p,
                               api_key=TEST_KEY, dry_run=False, charge_fn=_fake_charge({}))
        assert TEST_KEY not in json.dumps(res), ("key leaked into output", res)
    finally:
        _clean(p)


def test_http_error_does_not_leak_api_key():
    # Codex HIGH: Stripe's auth-error body echoes the key. Prove settle scrubs it.
    def raiser(api_key, params, idem):
        body = json.dumps({"error": {"message":
               "Invalid API Key provided: %s" % api_key}}).encode()
        raise urllib.error.HTTPError(sw._STRIPE_CHARGES_URL, 401, "Unauthorized",
                                     {}, io.BytesIO(body))
    p = _led()
    try:
        res = sw.settle_stripe(ALLOW_REQ, policy_path=POLICY, ledger_path=p,
                               api_key=TEST_KEY, dry_run=False, charge_fn=raiser)
        blob = json.dumps(res)
        assert TEST_KEY not in blob and "rk_test_" not in blob, ("KEY LEAK", res)
        assert res["spent"] is False and "[REDACTED_KEY]" in res["error"], res
    finally:
        _clean(p)


def test_url_error_does_not_leak_api_key():
    def raiser(api_key, params, idem):
        raise urllib.error.URLError("connect failed using %s" % api_key)
    p = _led()
    try:
        res = sw.settle_stripe(ALLOW_REQ, policy_path=POLICY, ledger_path=p,
                               api_key=TEST_KEY, dry_run=False, charge_fn=raiser)
        assert TEST_KEY not in json.dumps(res) and res["spent"] is False, res
    finally:
        _clean(p)


def test_duplicate_request_same_idempotency_key():
    # Codex HIGH: same approved spend must reuse ONE idempotency key (no double-charge).
    p = _led()
    try:
        keys = []
        def cap_idem(api_key, params, idem):
            keys.append(idem)
            return {"id": "ch_X", "status": "succeeded", "paid": True,
                    "amount": params["amount"], "currency": params["currency"], "livemode": False}
        sw.settle_stripe(ALLOW_REQ, policy_path=POLICY, ledger_path=p,
                         api_key=TEST_KEY, dry_run=False, charge_fn=cap_idem)
        sw.settle_stripe(ALLOW_REQ, policy_path=POLICY, ledger_path=p,
                         api_key=TEST_KEY, dry_run=False, charge_fn=cap_idem)
        assert keys[0] == keys[1], ("idempotency key must be deterministic", keys)
        # a different amount -> a different key (distinct logical spend)
        keys.clear()
        sw.settle_stripe(ALLOW_REQ, policy_path=POLICY, ledger_path=p,
                         api_key=TEST_KEY, dry_run=False, charge_fn=cap_idem)
        sw.settle_stripe(dict(ALLOW_REQ, amount=13.0), policy_path=POLICY, ledger_path=p,
                         api_key=TEST_KEY, dry_run=False, charge_fn=cap_idem)
        assert keys[0] != keys[1], ("distinct spends must differ", keys)
    finally:
        _clean(p)


def test_explicit_and_request_id_idempotency_win():
    p = _led()
    try:
        keys = []
        def cap_idem(api_key, params, idem):
            keys.append(idem)
            return {"id": "ch_X", "status": "succeeded", "paid": True,
                    "amount": params["amount"], "currency": params["currency"], "livemode": False}
        sw.settle_stripe(ALLOW_REQ, policy_path=POLICY, ledger_path=p, api_key=TEST_KEY,
                         dry_run=False, charge_fn=cap_idem, idempotency_key="explicit-123")
        assert keys[-1] == "explicit-123", keys
        # caller-supplied request_id makes two same-field spends genuinely distinct
        sw.settle_stripe(dict(ALLOW_REQ, request_id="r1"), policy_path=POLICY, ledger_path=p,
                         api_key=TEST_KEY, dry_run=False, charge_fn=cap_idem)
        sw.settle_stripe(dict(ALLOW_REQ, request_id="r2"), policy_path=POLICY, ledger_path=p,
                         api_key=TEST_KEY, dry_run=False, charge_fn=cap_idem)
        assert keys[-2] != keys[-1] and keys[-1] == "cerebro-gov-r2", keys
    finally:
        _clean(p)


def test_currency_normalized_before_charge():
    # Codex LOW: gate normalizes currency; the CHARGED currency must match.
    p = _led()
    try:
        cap = {}
        res = sw.settle_stripe(dict(ALLOW_REQ, currency="USD "), policy_path=POLICY, ledger_path=p,
                               api_key=TEST_KEY, dry_run=False, charge_fn=_fake_charge(cap))
        assert res["verdict"] == ALLOW and cap["params"]["currency"] == "usd", cap
    finally:
        _clean(p)


def test_basic_auth_encoded_key_scrubbed():
    # Codex R2 HIGH: a base64(api_key + ":") echoed in an error decodes back to
    # the key. Prove settle scrubs the ENCODED form, not just plaintext.
    b64 = base64.b64encode((TEST_KEY + ":").encode()).decode()
    def raiser(api_key, params, idem):
        body = json.dumps({"error": {"message":
               "Authorization Basic %s failed" % b64}}).encode()
        raise urllib.error.HTTPError(sw._STRIPE_CHARGES_URL, 401, "Unauthorized",
                                     {}, io.BytesIO(body))
    p = _led()
    try:
        res = sw.settle_stripe(ALLOW_REQ, policy_path=POLICY, ledger_path=p,
                               api_key=TEST_KEY, dry_run=False, charge_fn=raiser)
        blob = json.dumps(res)
        assert b64 not in blob and TEST_KEY not in blob, ("ENCODED KEY LEAK", res)
        # and the decode-back path is dead: no base64 in output decodes to the key
        assert "[REDACTED_KEY]" in res["error"], res
    finally:
        _clean(p)


def test_url_error_basic_auth_key_scrubbed():
    b64 = base64.b64encode((TEST_KEY + ":").encode()).decode()
    def raiser(api_key, params, idem):
        raise urllib.error.URLError("sent Authorization Basic %s" % b64)
    p = _led()
    try:
        res = sw.settle_stripe(ALLOW_REQ, policy_path=POLICY, ledger_path=p,
                               api_key=TEST_KEY, dry_run=False, charge_fn=raiser)
        assert b64 not in json.dumps(res) and res["spent"] is False, res
    finally:
        _clean(p)


def test_normalized_equivalent_duplicates_share_one_key():
    # Codex R2 HIGH: gate normalizes merchant/currency/category/purpose, so
    # case/space variants are the SAME logical spend and must share a key.
    p = _led()
    try:
        keys = []
        def cap_idem(api_key, params, idem):
            keys.append(idem)
            return {"id": "ch_X", "status": "succeeded", "paid": True,
                    "amount": params["amount"], "currency": params["currency"], "livemode": False}

        def key_for(req):
            keys.clear()
            sw.settle_stripe(req, policy_path=POLICY, ledger_path=p, api_key=TEST_KEY,
                             dry_run=False, charge_fn=cap_idem)
            return keys[-1]

        base = key_for(ALLOW_REQ)
        assert key_for(dict(ALLOW_REQ, currency="USD ")) == base, "currency case/space must share key"
        assert key_for(dict(ALLOW_REQ, merchant=" OpenAI ")) == base, "merchant case/space must share key"
        assert key_for(dict(ALLOW_REQ, purpose="API-Credits")) == base, "purpose case must share key"
        # genuinely distinct spend (different amount) still differs
        assert key_for(dict(ALLOW_REQ, amount=13.0)) != base, "distinct amount must differ"
    finally:
        _clean(p)


if __name__ == "__main__":
    test_deny_never_charges();                  print("  DENY never charges: OK")
    test_escalate_holds_no_charge();            print("  ESCALATE holds (stopped-spend): OK")
    test_allow_dryrun_plans_exact_cents_no_call(); print("  ALLOW dry_run plans exact cents, no call: OK")
    test_allow_charges_exact_cents();           print("  ALLOW charges exact cents via tok_visa: OK")
    test_no_raw_stripe_object_leaks();          print("  no raw Stripe object leaks (card/PAN): OK")
    test_live_key_refused_without_optin();      print("  live key refused without opt-in: OK")
    test_missing_key_fails_closed();            print("  missing key fails closed: OK")
    test_subcent_amount_rejected();             print("  sub-cent amount rejected: OK")
    test_key_never_returned();                  print("  API key never returned in output: OK")
    test_http_error_does_not_leak_api_key();    print("  HTTPError body scrubs API key: OK")
    test_url_error_does_not_leak_api_key();     print("  URLError string scrubs API key: OK")
    test_duplicate_request_same_idempotency_key(); print("  duplicate spend -> one idempotency key: OK")
    test_explicit_and_request_id_idempotency_win(); print("  explicit / request_id idempotency wins: OK")
    test_currency_normalized_before_charge();   print("  currency normalized before charge: OK")
    test_basic_auth_encoded_key_scrubbed();     print("  base64 basic-auth key scrubbed (HTTPError): OK")
    test_url_error_basic_auth_key_scrubbed();   print("  base64 basic-auth key scrubbed (URLError): OK")
    test_normalized_equivalent_duplicates_share_one_key(); print("  normalized-equiv duplicates share one key: OK")
    print("ALL STRIPE-WALLET TESTS PASS")

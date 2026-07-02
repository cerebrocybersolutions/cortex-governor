"""Offline tests for the Stripe Issuing bridge. Synthetic payloads, injected
transport, throwaway ledgers — no network, no Stripe account."""
import hashlib
import hmac
import json
import time

import pytest

from issuing_gate import (authorization_to_request, decide, respond,
                          verify_signature, _require_test_key,
                          SIGNATURE_TOLERANCE_SECONDS)
from policy_engine import ALLOW, DENY, ESCALATE

POLICY = "policy.json"


def _auth(amount_cents=1200, merchant="openai", mcc="digital_goods_applications",
          purpose="api-credits", currency="usd"):
    return {
        "id": "iauth_test_001",
        "metadata": {"agent": "hermes", "task": "earn-loop", "purpose": purpose},
        "merchant_data": {"name": merchant, "network_id": "1234567890", "category": mcc},
        "pending_request": {"amount": amount_cents, "currency": currency},
    }


def _sign(payload: bytes, secret: str, ts=None) -> str:
    ts = int(ts if ts is not None else time.time())
    mac = hmac.new(secret.encode(), ("%d." % ts).encode() + payload, hashlib.sha256)
    return "t=%d,v1=%s" % (ts, mac.hexdigest())


# --- mapping ---------------------------------------------------------------

def test_mapping_cents_merchant_category():
    req = authorization_to_request(_auth(amount_cents=1250, merchant="  OpenAI "))
    assert req["amount"] == 12.50
    assert req["merchant"] == "openai"
    assert req["category"] == "api-credits"
    assert req["currency"] == "usd"


def test_unmapped_mcc_passes_through_raw():
    req = authorization_to_request(_auth(mcc="gambling_establishments"))
    assert req["category"] == "gambling_establishments"  # unknown != allowed


def test_malformed_payload_raises():
    with pytest.raises((KeyError, TypeError)):
        authorization_to_request({"id": "iauth_x"})


# --- decisions (throwaway ledger per test) ----------------------------------

def test_clean_spend_approves(tmp_path):
    approve, dec = decide(_auth(), POLICY, tmp_path / "led.jsonl")
    assert approve is True and dec.verdict == ALLOW


def test_over_cap_escalate_maps_to_decline(tmp_path):
    approve, dec = decide(_auth(amount_cents=15000), POLICY, tmp_path / "led.jsonl")
    assert approve is False and dec.verdict == ESCALATE  # fail closed, no human window


def test_denied_vendor_declines(tmp_path):
    approve, dec = decide(_auth(merchant="sanctioned-vendor", purpose="supplies",
                                mcc="stationery_office_supplies_printing_and_writing_paper"),
                          POLICY, tmp_path / "led.jsonl")
    assert approve is False and dec.verdict == DENY


def test_malformed_auth_fails_closed(tmp_path):
    approve, dec = decide({"id": "iauth_bad"}, POLICY, tmp_path / "led.jsonl")
    assert approve is False and dec is None


def test_decision_lands_on_ledger(tmp_path):
    led = tmp_path / "led.jsonl"
    decide(_auth(), POLICY, led)
    entry = json.loads(led.read_text().splitlines()[-1])
    assert entry["verdict"] == ALLOW and entry["merchant"] == "openai"


# --- webhook signature -------------------------------------------------------

def test_signature_roundtrip():
    payload, secret = b'{"ok":1}', "whsec_testsecret"
    assert verify_signature(payload, _sign(payload, secret), secret) is True


def test_signature_wrong_secret_and_garbage():
    payload = b'{"ok":1}'
    assert verify_signature(payload, _sign(payload, "whsec_a"), "whsec_b") is False
    assert verify_signature(payload, "not-a-header", "whsec_a") is False
    assert verify_signature(payload, "", "whsec_a") is False


def test_signature_replay_window():
    payload, secret = b'{"ok":1}', "whsec_testsecret"
    stale = _sign(payload, secret, ts=time.time() - SIGNATURE_TOLERANCE_SECONDS - 10)
    assert verify_signature(payload, stale, secret) is False


# --- transport guard ----------------------------------------------------------

def test_live_key_refused_no_override():
    with pytest.raises(RuntimeError):
        _require_test_key("sk_live_abc123")
    with pytest.raises(RuntimeError):
        respond("iauth_x", True, "rk_live_abc123")


def test_respond_hits_approve_and_decline_urls():
    calls = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"id":"iauth_test_001","status":"closed"}'

    def fake_urlopen(req, timeout=10):
        calls.append(req.full_url)
        assert req.headers["Authorization"].startswith("Bearer sk_test_")
        return _Resp()

    respond("iauth_test_001", True, "sk_test_k", _urlopen=fake_urlopen)
    respond("iauth_test_001", False, "sk_test_k", _urlopen=fake_urlopen)
    assert calls[0].endswith("/iauth_test_001/approve")
    assert calls[1].endswith("/iauth_test_001/decline")

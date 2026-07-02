"""Stripe Issuing bridge — the governor as the real-time authorization decision.

Stripe Issuing gives each agent a (test-mode) virtual card. When the card is
swiped, Stripe emits an `issuing_authorization.request` event and waits for an
approve/decline. This module makes the Cortex gate that decision endpoint:

    card swipe -> Stripe webhook -> verify signature -> map to a gate request
              -> gate() (policy + tamper-checked ledger) -> approve / decline

Semantics are stricter than the CLI path: Issuing gives no window for a human,
so ESCALATE maps to DECLINE (fail closed). The declined attempt is still on the
ledger; a human can pre-approve via escalation.py and the retry then ALLOWs.

Scope (honest): built and tested OFFLINE against synthetic authorization
payloads — no Issuing account is required to run the tests, and the transport
refuses any key that is not test-mode (`sk_test_`/`rk_test_`). To wire it live:
enable Issuing in a Stripe TEST account, point an `issuing_authorization.request`
webhook at `python3 issuing_gate.py serve`, set STRIPE_WEBHOOK_SECRET +
STRIPE_TEST_KEY. stdlib only, like everything else in this repo.
"""
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from governor import gate, DEFAULT_LEDGER, DEFAULT_POLICY
from policy_engine import ALLOW

_HERE = Path(__file__).resolve().parent
SIGNATURE_TOLERANCE_SECONDS = 300  # Stripe's own recommended replay window
API_BASE = "https://api.stripe.com/v1/issuing/authorizations"

# Issuing category codes -> governor categories. Anything unmapped stays as the
# raw MCC string, which is not in any allow-list and therefore escalates/denies
# downstream — unknown-by-default, never allow-by-default.
MCC_TO_CATEGORY = {
    "computer_software_stores": "saas-subscription",
    "computer_network_services": "compute",
    "telecommunication_services": "infra",
    "digital_goods_applications": "api-credits",
    "office_and_commercial_furniture": "supplies",
    "stationery_office_supplies_printing_and_writing_paper": "supplies",
}


def authorization_to_request(auth):
    """Map a Stripe Issuing authorization object to a governor request.
    Raises KeyError/TypeError on malformed input — callers treat that as DENY."""
    pending = auth["pending_request"]
    merchant = auth["merchant_data"]
    mcc = merchant.get("category", "")
    return {
        "agent": auth.get("metadata", {}).get("agent", "issuing-card"),
        "task": auth.get("metadata", {}).get("task", "card-authorization"),
        "merchant": (merchant.get("name") or merchant.get("network_id") or "").strip().lower(),
        "amount": pending["amount"] / 100.0,  # Issuing amounts are integer cents
        "currency": pending["currency"],
        "category": MCC_TO_CATEGORY.get(mcc, mcc),
        "purpose": auth.get("metadata", {}).get("purpose", ""),
    }


def decide(auth, policy_path=DEFAULT_POLICY, ledger_path=DEFAULT_LEDGER):
    """Gate one authorization. Returns (approve: bool, decision|None).
    Fail closed: malformed payloads and ESCALATE both mean approve=False."""
    try:
        request = authorization_to_request(auth)
    except (KeyError, TypeError, ValueError):
        return False, None
    dec = gate(request, policy_path=policy_path, ledger_path=ledger_path)
    return dec.verdict == ALLOW, dec


def verify_signature(payload, sig_header, secret, now=None):
    """Stripe webhook signature (v1 scheme): sig_header is
    `t=<unix>,v1=<hex hmac-sha256 of "<t>.<payload>">`. Constant-time compare,
    replay-window enforced. Returns bool; any parse failure is False."""
    try:
        parts = dict(kv.split("=", 1) for kv in sig_header.split(","))
        ts = int(parts["t"])
        given = parts["v1"]
    except (AttributeError, KeyError, ValueError):
        return False
    if abs((now if now is not None else time.time()) - ts) > SIGNATURE_TOLERANCE_SECONDS:
        return False
    signed = ("%d." % ts).encode() + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, given)


def _require_test_key(api_key):
    """Same guard class as stripe_test_wallet: this module never touches a live
    key. No override here — Issuing auth is fully automated, so the bar is higher."""
    if not (api_key.startswith("sk_test_") or api_key.startswith("rk_test_")):
        raise RuntimeError("issuing_gate refuses non-test Stripe keys (sk_test_/rk_test_ only)")


def respond(auth_id, approve, api_key, _urlopen=urllib.request.urlopen):
    """POST the approve/decline back to Stripe. Transport is injectable so tests
    never touch the network. Returns the parsed response dict."""
    _require_test_key(api_key)
    url = "%s/%s/%s" % (API_BASE, auth_id, "approve" if approve else "decline")
    req = urllib.request.Request(url, data=b"", method="POST")
    req.add_header("Authorization", "Bearer %s" % api_key)
    with _urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


class IssuingWebhookHandler(BaseHTTPRequestHandler):
    """Minimal decision endpoint. Config via class attrs (set by serve())."""
    webhook_secret = None
    api_key = None
    policy_path = DEFAULT_POLICY
    ledger_path = DEFAULT_LEDGER
    respond_fn = staticmethod(respond)

    def do_POST(self):
        payload = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        sig = self.headers.get("Stripe-Signature", "")
        if not verify_signature(payload, sig, self.webhook_secret):
            self.send_response(400)
            self.end_headers()
            return
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return
        if event.get("type") != "issuing_authorization.request":
            self.send_response(200)  # ack unrelated events, do nothing
            self.end_headers()
            return
        auth = event["data"]["object"]
        approve, dec = decide(auth, self.policy_path, self.ledger_path)
        self.respond_fn(auth["id"], approve, self.api_key)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        body = {"approved": approve, "rule": dec.rule if dec else "invalid_request"}
        self.wfile.write(json.dumps(body).encode("utf-8"))

    def log_message(self, fmt, *args):  # one line per decision, no noise
        sys.stderr.write("[issuing-gate] %s\n" % (fmt % args))


def serve(port=8787):
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    api_key = os.environ.get("STRIPE_TEST_KEY", "")
    if not secret or not api_key:
        print("issuing_gate: set STRIPE_WEBHOOK_SECRET and STRIPE_TEST_KEY", file=sys.stderr)
        return 1
    _require_test_key(api_key)
    IssuingWebhookHandler.webhook_secret = secret
    IssuingWebhookHandler.api_key = api_key
    print("issuing-gate listening on 127.0.0.1:%d (test mode)" % port, file=sys.stderr)
    HTTPServer(("127.0.0.1", port), IssuingWebhookHandler).serve_forever()


if __name__ == "__main__":
    sys.exit(serve() if "serve" in sys.argv[1:] else
             print(__doc__) or 0)

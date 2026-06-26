"""Bridge: governor decision -> raw Stripe API charge (TEST MODE).

The Stripe Link CLI (`link_wallet.py`) needs a real card even with --test. This
bridge instead drives the raw Stripe API with a test-mode key + Stripe's built-in
`tok_visa` test token, so the governor can place a LIVE Stripe test charge with
zero real card and zero real money — the cleanest end-to-end proof that the
policy gate, not a human in-app tap, is what releases the spend.

settle_stripe(request) OWNS the policy decision — same contract as settle() in
link_wallet.py:
  - it RE-GATES the request internally; it never accepts a caller verdict (a
    forged or malformed verdict was the round-2 review's top finding).
  - ALLOW charges autonomously. ESCALATE and DENY never touch the wallet —
    ESCALATE is the demo's "stopped-spend" moment (held for a human), DENY is
    blocked at the gate.
  - it returns ONLY safe, allowlisted fields (charge id `ch_...`, status,
    amount, currency, livemode). The raw Stripe response (which carries source
    / card / billing detail) is NEVER returned.

SAFETY (money path — NOT lazy here):
  - TEST-KEY GUARD: a live key (`sk_live_` / `rk_live_`, or any non-test prefix)
    is refused unless i_understand_real_money=True. test-mode keys spend no real
    money, so the default path cannot.
  - dry_run=True by DEFAULT: builds + returns the planned charge without calling
    Stripe.
  - the API key is read from the STRIPE_TEST_KEY env var, never an argument
    default that could land in a traceback / log; it is never returned or logged.
  - an Idempotency-Key is sent so a retry cannot double-charge.

Network egress: POST https://api.stripe.com/v1/charges (test mode). This is the
one bridge that intentionally reaches an external API — that is the point of the
raw-API path. stdlib only (urllib); no Stripe SDK dependency.
"""
import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from policy_engine import (evaluate, load_policy, normalize_text, ensure_request_id,
                           ALLOW, DENY, ESCALATE)
from ledger import Ledger
from link_wallet import to_cents  # whole-cent Decimal conversion, already hardened

_HERE = Path(__file__).resolve().parent
DEFAULT_POLICY = _HERE / "policy.json"
DEFAULT_LEDGER = _HERE / "ledger.jsonl"

_STRIPE_CHARGES_URL = "https://api.stripe.com/v1/charges"
# Only a Stripe charge id is safe to surface from the API response.
_CHARGE_ID_RE = re.compile(r"\bch_[A-Za-z0-9]+\b")
# Any Stripe-key-shaped token — scrubbed from error text before it's returned.
_KEY_RE = re.compile(r"[sr]k_(?:test|live)_[A-Za-z0-9]+")
# Defense-in-depth: an echoed `Authorization: Basic <base64>` decodes back to the
# key, so never surface a basic-auth blob in returned error text.
_BASIC_AUTH_RE = re.compile(r"Basic\s+[A-Za-z0-9+/=]{16,}")
# Test-mode key prefixes. Standard secret (sk_test_) or restricted (rk_test_).
_TEST_KEY_PREFIXES = ("sk_test_", "rk_test_")


def _is_test_key(api_key):
    return bool(api_key) and api_key.startswith(_TEST_KEY_PREFIXES)


def _scrub_secrets(text, api_key):
    """Strip the API key — plaintext AND its derived basic-auth encodings — plus
    any Stripe-key-shaped token from text before it is returned to a caller.
    Stripe's auth-error body echoes the key verbatim ("Invalid API Key provided:
    sk_test_..."), and an echoed `Authorization: Basic <base64(key:)>` decodes
    straight back to the key, so both forms must go."""
    s = str(text)
    if api_key:
        s = s.replace(api_key, "[REDACTED_KEY]")
        # the exact derived encodings the request uses (key:) and bare key
        for enc in (base64.b64encode((api_key + ":").encode()).decode(),
                    base64.b64encode(api_key.encode()).decode()):
            s = s.replace(enc, "[REDACTED_KEY]")
    s = _KEY_RE.sub("[REDACTED_KEY]", s)
    return _BASIC_AUTH_RE.sub("Basic [REDACTED_KEY]", s)


def _idempotency_key(request, explicit):
    """A DETERMINISTIC key per logical spend, so a retry of the SAME approved
    request dedupes at Stripe instead of charging twice. Explicit key wins; then
    a caller-supplied request_id; else a hash of the CANONICAL gate-equivalent
    spend fields.

    The canonical values matter: the gate normalizes merchant/category/purpose/
    currency and the charge sends canonical values to Stripe, so two requests
    that differ only in casing/whitespace ("OpenAI" vs "openai", "USD " vs "usd")
    are the SAME logical spend and MUST share one key — else a retry with slightly
    different LLM formatting double-charges. Amount is the validated cents, not a
    raw float repr.

    ponytail: case/space variants intentionally collapse to one key — pass
    request_id (or idempotency_key) when a genuinely separate repeat charge of
    the same logical spend is wanted."""
    if explicit:
        return explicit
    rid = request.get("request_id")
    if rid:
        return "cerebro-gov-" + str(rid)
    cents, _ = to_cents(request.get("amount"))
    stable = {
        "agent": normalize_text(request.get("agent")),
        "task": normalize_text(request.get("task")),
        "merchant": normalize_text(request.get("merchant")),
        "cents": cents,
        "currency": normalize_text(request.get("currency")),
        "category": normalize_text(request.get("category")),
        "purpose": normalize_text(request.get("purpose")),
    }
    blob = json.dumps(stable, sort_keys=True, default=str)
    return "cerebro-gov-" + hashlib.sha256(blob.encode()).hexdigest()[:32]


def _stripe_charge(api_key, params, idempotency_key, timeout=30):
    """POST a charge to Stripe. Returns the parsed JSON dict (raw — caller
    redacts). Raises urllib.error.HTTPError on a 4xx/5xx (Stripe puts the error
    body in the response, which the caller reads without leaking the key)."""
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(_STRIPE_CHARGES_URL, data=body, method="POST")
    # Key as HTTP basic-auth username (Stripe's scheme); never in the URL/body.
    req.add_header("Authorization",
                   "Basic " + base64.b64encode((api_key + ":").encode()).decode())
    req.add_header("Idempotency-Key", idempotency_key)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _redact(charge):
    """Allowlist exactly the safe fields off a Stripe charge object. The full
    object carries source/card/billing detail and must never be returned."""
    cid = charge.get("id")
    if not (isinstance(cid, str) and _CHARGE_ID_RE.fullmatch(cid)):
        cid = None
    return {
        "charge_id": cid,
        "status": charge.get("status"),
        "paid": charge.get("paid"),
        "amount_cents": charge.get("amount"),
        "currency": charge.get("currency"),
        "livemode": charge.get("livemode"),
    }


def settle_stripe(request, *, policy_path=DEFAULT_POLICY, ledger_path=DEFAULT_LEDGER,
                  api_key=None, source="tok_visa", dry_run=True,
                  i_understand_real_money=False, idempotency_key=None, charge_fn=None):
    """Re-gate the request and carry the AUTHORITATIVE verdict to Stripe.

    Returns a redacted result dict. Never accepts a caller verdict; never returns
    the raw Stripe object; charges ONLY on ALLOW; fails closed on DENY /
    ESCALATE / non-actionable verdict / sub-cent amount / live key without
    explicit opt-in / tampered ledger.
    """
    api_key = api_key if api_key is not None else os.environ.get("STRIPE_TEST_KEY")

    # TEST-KEY GUARD: only a test-mode key may spend on the default path.
    if not api_key:
        return {"verdict": None, "spent": False,
                "error": "no Stripe key (set STRIPE_TEST_KEY=rk_test_... or sk_test_...)"}
    if not _is_test_key(api_key) and not i_understand_real_money:
        return {"verdict": None, "spent": False,
                "error": "REAL-MONEY GUARD: key is not a test-mode key (rk_test_/sk_test_); "
                         "refusing to charge. Use a test key, or pass "
                         "i_understand_real_money=True to override."}

    # Stamp the cross-surface anchor up front so the SAME request_id flows to the
    # gate entry, the Stripe idempotency key, and the settlement entry — the id
    # the dashboard reconciles across ledger / Stripe / sandbox. Work on a copy so
    # a reused request template isn't poisoned with this spend's id.
    request = dict(request)
    ensure_request_id(request)

    # OWN the verdict — re-gate; a caller cannot forge or malform it. On the real
    # path go through gate() so the ledger-tamper checkpoint guards the spend.
    if dry_run:
        dec = evaluate(request, load_policy(policy_path), ledger=Ledger(ledger_path))
    else:
        from governor import gate
        dec = gate(request, policy_path=policy_path, ledger_path=ledger_path)

    if dec.verdict == DENY:
        return {"verdict": DENY, "spent": False, "rule": dec.rule,
                "reason": "blocked by governor; wallet not called"}
    if dec.verdict == ESCALATE:
        # The stopped-spend moment: an autonomous agent does NOT get to charge.
        return {"verdict": ESCALATE, "spent": False, "rule": dec.rule,
                "reason": "needs human approval; wallet not called autonomously"}
    if dec.verdict != ALLOW:
        return {"verdict": dec.verdict, "spent": False,
                "error": "non-actionable verdict; failing closed"}

    # Whole-cent money only — the charged amount equals what the governor approved.
    cents, cerr = to_cents(request.get("amount"))
    if cerr:
        return {"verdict": ALLOW, "spent": False, "error": cerr}

    # Send the gate-normalized currency, not the raw request value: the gate
    # ALLOWed the normalized form, so "USD"/"usd " must charge as "usd" — the
    # approved value equals the charged value.
    currency = normalize_text(request.get("currency")) or "usd"
    params = {
        "amount": cents,
        "currency": currency,
        "source": source,
        "description": "Cerebro spend-governor test charge — task %r, merchant %r"
                       % (request.get("task"), request.get("merchant")),
    }

    if dry_run:
        return {"verdict": ALLOW, "spent": False, "dry_run": True, "test": _is_test_key(api_key),
                "request_id": request.get("request_id"),
                "plan": {"amount_cents": cents, "currency": currency, "source": source}}

    poster = charge_fn or _stripe_charge
    idem = _idempotency_key(request, idempotency_key)
    try:
        charge = poster(api_key, params, idem)
    except urllib.error.HTTPError as e:
        # Stripe's error body can echo the key — scrub before returning, then cap.
        try:
            msg = json.loads(e.read().decode()).get("error", {}).get("message", "")
        except Exception:
            msg = ""
        err = _scrub_secrets("stripe %s: %s" % (e.code, msg), api_key)[:200]
        return {"verdict": ALLOW, "spent": False, "error": err}
    except (urllib.error.URLError, OSError) as e:
        return {"verdict": ALLOW, "spent": False,
                "error": _scrub_secrets("stripe request failed: %s" % e, api_key)[:200]}

    out = _redact(charge)
    out["verdict"] = ALLOW
    out["spent"] = out.get("status") in ("succeeded", "paid") or out.get("paid") is True
    out["request_id"] = request.get("request_id")

    # Anti-fake anchor: on a real charge, write the SETTLED entry that ties this
    # Stripe receipt back to the ALLOW decision gate() just recorded. gate()
    # appended that decision as the ledger head, so head_hash IS the entry this
    # settlement resolves.
    # ponytail: head==gate-entry holds because this is the single-process demo
    # flow (gate -> charge -> settle, nothing else writing). A concurrent writer
    # would need gate() to return the entry hash instead — upgrade there if the
    # wallet ever runs multi-process.
    if out["spent"] and out.get("charge_id"):
        led = Ledger(ledger_path)
        settlement = led.record_settlement(
            request_id=request.get("request_id"),
            gate_entry_hash=led.head_hash,
            receipt=out["charge_id"],
            sandbox_ref=request.get("sandbox_ref"),
            agent=request.get("agent"), task=request.get("task"),
            merchant=request.get("merchant"), amount=request.get("amount"),
            currency=currency,
        )
        out["settlement_hash"] = settlement["entry_hash"]
        out["resolves"] = settlement["resolves"]
    return out


if __name__ == "__main__":
    # Self-demo (dry-run, throwaway ledger, no record, no network): settle_stripe
    # re-gates each request and prints the gated verdict + planned charge.
    import tempfile
    # synthetic fixture — not a real key; the prefix is split so no key-shaped literal sits in source.
    key = os.environ.get("STRIPE_TEST_KEY", "rk_test_" + "DEMO_PLACEHOLDER")
    for label, req in [
        ("ALLOW -> charge", dict(task="earn-loop", merchant="openai", amount=12.0,
                                 currency="usd", category="api-credits", purpose="api-credits")),
        ("ESCALATE -> hold", dict(task="deploy", merchant="vercel", amount=150.0,
                                  currency="usd", category="compute", purpose="infra")),
        ("DENY -> blocked", dict(task="scale", merchant="aws", amount=600.0,
                                 currency="usd", category="compute", purpose="compute")),
    ]:
        tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
        Path(tmp).unlink(missing_ok=True)
        res = settle_stripe(req, ledger_path=tmp, api_key=key, dry_run=True)
        summary = res.get("plan") or res.get("reason") or res.get("error")
        print("%-18s -> %s  %s" % (label, res.get("verdict"), summary))
        Path(tmp).unlink(missing_ok=True)
        Path(tmp + ".checkpoint").unlink(missing_ok=True)

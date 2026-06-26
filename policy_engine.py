"""H3 — Cerebro policy engine: the governor between an agent's hands and the wallet.

evaluate() takes a spend-request + a policy (+ optional ledger for rate-window
history) and returns ALLOW / DENY / ESCALATE plus the rule that fired. This is
the layer Stripe Skills for Hermes does NOT ship — every Stripe spend is gated
by a human in-app tap with no programmable policy.

Hardened 2026-06-17 after Codex adversarial review (handoff
`2026-06-17-spend-governor-adversarial-review`):
  - `amount` is VALIDATED before any check: must exist, be a real finite number
    (not bool, not NaN/inf), strictly positive -> else DENY invalid_request.amount.
    Closes "missing/None/negative/NaN amount returns ALLOW".
  - request strings are NORMALIZED (strip + Unicode NFKC + casefold) and policy
    lists normalized the same way -> case/space/unicode variants ("Crypto",
    "crypto ") can't dodge vendor/category/purpose rules.
  - `currency` is ENFORCED against an allowlist.
  - HARD-DENY rules are evaluated BEFORE soft-ESCALATE rules, so a denied
    vendor/category/over-ceiling spend can never be downgraded into an
    approvable escalation (first-failing-rule used to let per_txn ESCALATE mask
    a category DENY).
  - the rate-window reads the GOVERNOR-owned decision timestamp from the ledger,
    never a caller-supplied request ts.

stdlib only. ponytail: validate, normalize, deny-pass, escalate-pass.
"""
import hashlib
import json
import math
import time
import unicodedata
from collections import namedtuple
from decimal import Decimal, InvalidOperation
from pathlib import Path

ALLOW, DENY, ESCALATE = "ALLOW", "DENY", "ESCALATE"

Decision = namedtuple("Decision", ["verdict", "rule", "reason"])


def load_policy(path):
    return json.loads(Path(path).read_text())


def normalize_text(value):
    """Strip + Unicode-NFKC + casefold. Returns None for non-str or empty."""
    if not isinstance(value, str):
        return None
    v = unicodedata.normalize("NFKC", value).strip().casefold()
    return v or None


def mint_request_id(request):
    """Deterministic id for one logical spend — the anchor that lets the dashboard
    reconcile the SAME request across three surfaces (ledger entry <-> Stripe
    receipt <-> sandbox event). A caller-supplied request_id wins (the NemoClaw
    sandbox injects one at the call site); else derive it from the canonical,
    normalized spend fields so casing/whitespace variants ("OpenAI" / "openai ")
    collapse to one id — same basis the Stripe idempotency key uses."""
    rid = request.get("request_id")
    if rid:
        return str(rid)
    stable = {
        "agent": normalize_text(request.get("agent")),
        "task": normalize_text(request.get("task")),
        "merchant": normalize_text(request.get("merchant")),
        "amount": str(request.get("amount")),
        "currency": normalize_text(request.get("currency")),
        "category": normalize_text(request.get("category")),
        "purpose": normalize_text(request.get("purpose")),
    }
    blob = json.dumps(stable, sort_keys=True, default=str)
    return "req_" + hashlib.sha256(blob.encode()).hexdigest()[:16]


def ensure_request_id(request):
    """Set request['request_id'] in place if absent; return the id. Idempotent —
    a request that already carries an id keeps it."""
    rid = mint_request_id(request)
    request["request_id"] = rid
    return rid


def _norm_set(values):
    out = set()
    for v in values or []:
        n = normalize_text(v)
        if n is not None:
            out.add(n)
    return out


def validate_amount(amount):
    """(value, None) if a finite positive real; else (None, reason).

    Rejects: missing/None, bool (True/False are ints in Python), non-numeric,
    NaN, +/-inf, zero, negative. This is the gate that stops a missing/None/
    negative/NaN amount slipping through as ALLOW.
    """
    if amount is None:
        return None, "amount is missing"
    if isinstance(amount, bool):
        return None, "amount must not be a boolean"
    if not isinstance(amount, (int, float)):
        return None, "amount must be a number, got %s" % type(amount).__name__
    if not math.isfinite(amount):
        return None, "amount must be finite (got %r)" % amount
    if amount <= 0:
        return None, "amount must be positive (got %r)" % amount
    # Money is whole cents. Reject sub-cent precision so the amount the governor
    # approves equals the amount the wallet charges (no float-rounding mismatch).
    try:
        d = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        return None, "amount not a valid decimal (got %r)" % amount
    if d != d.quantize(Decimal("0.01")):
        return None, "amount has sub-cent precision (got %r)" % amount
    return float(amount), None


def _recent_allow_amounts(ledger, window_seconds, now):
    """Sum prior ALLOW amounts inside the window, keyed on the GOVERNOR-owned
    decision timestamp (ledger entry 'ts'), never the caller's request ts."""
    if ledger is None:
        return []
    out = []
    for e in ledger.entries():
        if e.get("verdict") == ALLOW:
            ts = e.get("ts", 0) or 0
            if (now - ts) <= window_seconds:
                out.append(e.get("amount", 0) or 0)
    return out


def evaluate(request, policy, ledger=None, now=None):
    """Validate -> HARD-DENY rules -> soft-ESCALATE rules -> ALLOW.

    Verdict precedence is deliberate: every DENY condition is checked before any
    ESCALATE condition, so a denied vendor/category/over-hard-ceiling spend can
    never be downgraded into an approvable escalation.
    """
    now = now if now is not None else time.time()

    # --- 0. validate + normalize the request ---
    amount, err = validate_amount(request.get("amount"))
    if err:
        return Decision(DENY, "invalid_request.amount", err)

    allowed_cur = policy.get("allowed_currencies")
    if allowed_cur is not None:
        currency = normalize_text(request.get("currency"))
        if currency is None or currency not in _norm_set(allowed_cur):
            return Decision(DENY, "invalid_request.currency",
                            "currency %r not in allowed_currencies" % request.get("currency"))

    merchant = normalize_text(request.get("merchant"))
    category = normalize_text(request.get("category"))
    purpose = normalize_text(request.get("purpose"))

    vendors = policy.get("vendors", {})
    deny_vendors = _norm_set(vendors.get("deny"))
    allow_vendors = vendors.get("allow")
    deny_categories = _norm_set(policy.get("categories", {}).get("deny"))

    # --- 1. HARD DENY (dominates; all deny conditions checked first) ---
    hard_max = policy.get("hard_max")
    if hard_max is not None and amount > hard_max:
        return Decision(DENY, "amount.hard_max",
                        "amount %s > hard_max %s" % (amount, hard_max))

    if merchant is not None and merchant in deny_vendors:
        return Decision(DENY, "vendor.deny", "merchant %r on deny list" % request.get("merchant"))

    if category is not None and category in deny_categories:
        return Decision(DENY, "category.deny", "category %r denied" % request.get("category"))

    rw = policy.get("rate_window")
    if rw:
        window = rw.get("seconds", 3600)
        recent = _recent_allow_amounts(ledger, window, now)
        max_count = rw.get("max_count")
        if max_count is not None and len(recent) + 1 > max_count:
            return Decision(DENY, "rate_window.max_count",
                            "%s spends > max_count %s in %ss"
                            % (len(recent) + 1, max_count, window))
        max_amount = rw.get("max_amount")
        if max_amount is not None and sum(recent) + amount > max_amount:
            return Decision(DENY, "rate_window.max_amount",
                            "window total %s > max_amount %s"
                            % (sum(recent) + amount, max_amount))

    # --- 2. SOFT ESCALATE (only reached when no hard-deny fired) ---
    per_txn = policy.get("per_txn_cap")
    if per_txn is not None and amount > per_txn:
        return Decision(ESCALATE, "amount.per_txn_cap",
                        "amount %s > per_txn_cap %s" % (amount, per_txn))

    if allow_vendors is not None:
        if merchant is None or merchant not in _norm_set(allow_vendors):
            return Decision(ESCALATE, "vendor.not_allowlisted",
                            "merchant %r not on allowlist" % request.get("merchant"))

    allowed_purposes = policy.get("allowed_purposes")
    if allowed_purposes is not None:
        if purpose is None or purpose not in _norm_set(allowed_purposes):
            return Decision(ESCALATE, "purpose.unbound",
                            "purpose %r not in allowed_purposes" % request.get("purpose"))

    return Decision(ALLOW, "ok", "all checks passed")

"""Close the ESCALATE loop — unattended UNTIL it needs you, then auditable.

The policy engine returns ESCALATE when a spend is plausible but needs a human.
This module is the resume path: a human approves (or denies) the escalated spend
once, and the resolution is itself appended as a hash-chained entry, so
who-approved-what is as tamper-evident as the spends.

Hardened 2026-06-17 after Codex adversarial review:
  - SEMANTIC validation of resolutions. A resolution only closes an escalation if
    it points to exactly one earlier ESCALATE entry, isn't self-referential, is
    the FIRST resolution for that target, has rule_fired/verdict that agree
    (approved->ALLOW, denied->DENY), and carries a non-empty approver. pending()
    ignores forged/duplicate/self resolutions, so a fabricated (but hash-valid)
    ALLOW resolution cannot silently mark an escalation approved.
  - approve=True RE-CHECKS hard-deny invariants. With a policy_path, the target
    is re-evaluated; if it would now hard-DENY (denied vendor/category, over the
    hard ceiling, over the rate window), the approval is refused and recorded as
    a denial — an approval can never override a hard-deny rule.

stdlib only (rides ledger.py + policy_engine.py).
"""
from policy_engine import ALLOW, DENY, ESCALATE, evaluate, load_policy

# rule_fired -> the verdict that rule must carry
_RES_RULES = {"escalation.approved": ALLOW, "escalation.denied": DENY}

_REQUEST_FIELDS = ("agent", "task", "merchant", "amount", "currency", "category", "purpose")


def _request_from_entry(entry):
    return {k: entry.get(k) for k in _REQUEST_FIELDS}


def _valid_resolution(entry, escalations_seen, already_resolved):
    """True if `entry` is a well-formed resolution of a real pending escalation.

    escalations_seen: {entry_hash: escalate_entry} seen earlier in the chain.
    already_resolved: set of target hashes a valid resolution already closed.
    """
    tgt = entry.get("resolves")
    if not tgt:
        return False
    if tgt == entry.get("entry_hash"):          # no self-resolution
        return False
    if tgt not in escalations_seen:             # must point at a real earlier ESCALATE
        return False
    if tgt in already_resolved:                 # first resolution wins; no double-resolve
        return False
    rule = entry.get("rule_fired")
    if rule not in _RES_RULES:                  # must be an escalation resolution rule
        return False
    if entry.get("verdict") != _RES_RULES[rule]:  # verdict must match the rule
        return False
    approver = entry.get("approver")
    if not isinstance(approver, str) or not approver.strip():  # real, non-empty approver
        return False
    return True


def _walk(ledger):
    """One pass: ESCALATE entries by hash + the set of validly-resolved targets."""
    escalations = {}
    resolved = set()
    for e in ledger.entries():
        if e.get("verdict") == ESCALATE and e.get("entry_hash"):
            escalations[e["entry_hash"]] = e
        elif e.get("resolves"):
            if _valid_resolution(e, escalations, resolved):
                resolved.add(e["resolves"])
    return escalations, resolved


def pending(ledger):
    """ESCALATE entries with no VALID later resolution, oldest first (chain order)."""
    escalations, resolved = _walk(ledger)
    return [e for h, e in escalations.items() if h not in resolved]


def invalid_resolutions(ledger):
    """Resolution-shaped entries (carry `resolves`) that FAIL semantic validation.
    Exposed for audits/tests — these are entries that look like resolutions but
    do not legitimately close any escalation."""
    escalations = {}
    resolved = set()
    bad = []
    for e in ledger.entries():
        if e.get("verdict") == ESCALATE and e.get("entry_hash"):
            escalations[e["entry_hash"]] = e
        elif e.get("resolves"):
            if _valid_resolution(e, escalations, resolved):
                resolved.add(e["resolves"])
            else:
                bad.append(e)
    return bad


def resolve(ledger, target_hash, approve, approver, policy_path=None):
    """Record a human decision on one escalated spend as a new chain entry.

    approve=True  -> ALLOW (the spend may proceed) — UNLESS a hard-deny re-check
                     (when policy_path is given) says the spend is now denied, in
                     which case it is recorded as a denial instead.
    approve=False -> DENY  (permanently refused)

    Returns the resolution entry, or None if target_hash is not a currently
    pending escalation or `approver` is empty.
    """
    if not isinstance(approver, str) or not approver.strip():
        return None
    tgt = next((e for e in pending(ledger) if e.get("entry_hash") == target_hash), None)
    if tgt is None:
        return None

    approved = bool(approve)
    deny_reason = None
    if approved and policy_path is not None:
        # An approval must never override a hard-deny rule (defense in depth).
        recheck = evaluate(_request_from_entry(tgt), load_policy(policy_path), ledger=ledger)
        if recheck.verdict == DENY:
            approved = False
            deny_reason = recheck.rule

    extra = {}
    if deny_reason:
        extra["recheck_denied_by"] = deny_reason
    return ledger.append(
        agent=tgt.get("agent"), task=tgt.get("task"), merchant=tgt.get("merchant"),
        amount=tgt.get("amount"), currency=tgt.get("currency"),
        category=tgt.get("category"), purpose=tgt.get("purpose"),
        rule_fired="escalation.approved" if approved else "escalation.denied",
        verdict=ALLOW if approved else DENY,
        resolves=target_hash, approver=approver, **extra,
    )

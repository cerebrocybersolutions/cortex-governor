"""H4 — Hash-chained tamper-evident spend ledger + attribution.

Append-only JSONL. Each entry records ONE governor decision (ALLOW / DENY /
ESCALATE), sha256-linked to its predecessor.

Hardened 2026-06-17 after Codex adversarial review:
  - The decision timestamp ('ts') is GOVERNOR-OWNED — set at append from a
    trusted clock, never the caller's claimed request ts. The caller's claimed
    ts is recorded separately as 'request_ts' (audit only) and is never used for
    rate-window enforcement. Closes the "caller ts=0 evades the window" attack.
  - verify() accepts an external checkpoint (expected_head_hash + expected_count).
    A pure internal walk passes a tamper+recompute (edit an entry, recompute all
    downstream hashes) and a tail deletion. Anchoring head+count outside the
    editable ledger catches both. A sidecar '<ledger>.checkpoint' persists the
    last good (head, count) so gate() can fail closed across processes.

    Honest scope: the sidecar is itself a local file, so this is DEMO-grade
    tamper-evidence (an attacker with write access to BOTH files can still forge).
    Production anchors the head in a keyed MAC / external service — out of scope
    for a Stripe TEST demo. The claim we make on camera is exactly this: any edit
    to the ledger alone is caught.

stdlib only. ponytail: a JSONL file IS the ledger; a tiny sidecar IS the anchor.
"""
import hashlib
import json
import os
import time
from pathlib import Path

GENESIS = "0" * 64

# prev_hash + entry_hash are derived. EVERYTHING else in an entry is hashed —
# so any field added later (request_ts, escalation overrides) is tamper-evident
# automatically.
_DERIVED = ("prev_hash", "entry_hash")


def _hashed_fields(entry):
    return {k: v for k, v in entry.items() if k not in _DERIVED}


def _canonical(fields):
    # sort_keys + tight separators => stable bytes for the same logical entry.
    return json.dumps(fields, sort_keys=True, separators=(",", ":"))


def _entry_hash(fields, prev_hash):
    return hashlib.sha256((_canonical(fields) + prev_hash).encode("utf-8")).hexdigest()


class Ledger:
    def __init__(self, path):
        self.path = Path(path)
        self._entries = []
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                line = line.strip()
                if line:
                    self._entries.append(json.loads(line))

    @property
    def head_hash(self):
        return self._entries[-1]["entry_hash"] if self._entries else GENESIS

    @property
    def checkpoint_path(self):
        return self.path.with_name(self.path.name + ".checkpoint")

    def append(self, *, agent, task, merchant, amount, currency, category,
               purpose, rule_fired, verdict, now=None, request_ts=None, **extra):
        # 'ts' is the GOVERNOR-owned decision time. A caller's claimed ts never
        # lands here — it is routed to request_ts (audit only). Defensive: if a
        # caller smuggles 'ts' via extra, drop it into request_ts.
        if "ts" in extra:
            request_ts = extra.pop("ts") if request_ts is None else request_ts
        decision_ts = now if now is not None else time.time()
        fields = {
            "ts": decision_ts,
            "agent": agent, "task": task, "merchant": merchant,
            "amount": amount, "currency": currency, "category": category,
            "purpose": purpose, "rule_fired": rule_fired, "verdict": verdict,
        }
        if request_ts is not None:
            fields["request_ts"] = request_ts
        fields.update(extra)   # e.g. resolves=<hash>, approver="operator" — also hashed
        prev = self.head_hash
        entry = dict(fields)
        entry["prev_hash"] = prev
        entry["entry_hash"] = _entry_hash(fields, prev)
        self._entries.append(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        self._write_checkpoint()
        return entry

    def record(self, request, rule_fired, verdict, now=None):
        """Append from a spend-request dict + the governor verdict. The request's
        claimed 'ts' is stored as request_ts (audit); the enforced decision ts is
        governor-owned.

        request_id + sandbox_ref (when present) are carried onto the entry as
        hashed audit fields — they are the cross-surface anchors the dashboard
        reconciles against. Both flow through as **extra, so they are tamper-
        evident like every other field."""
        extra = {}
        if request.get("request_id") is not None:
            extra["request_id"] = request.get("request_id")
        if request.get("sandbox_ref") is not None:
            extra["sandbox_ref"] = request.get("sandbox_ref")
        return self.append(
            agent=request.get("agent"), task=request.get("task"),
            merchant=request.get("merchant"), amount=request.get("amount"),
            currency=request.get("currency"), category=request.get("category"),
            purpose=request.get("purpose"), rule_fired=rule_fired,
            verdict=verdict, now=now, request_ts=request.get("ts"), **extra,
        )

    def record_settlement(self, *, request_id, gate_entry_hash, receipt,
                          sandbox_ref=None, agent=None, task=None, merchant=None,
                          amount=None, currency=None, now=None):
        """Append the post-charge SETTLED entry that ties a Stripe receipt back to
        the ALLOW decision it settles. This is the anti-fake anchor: the dashboard
        reconciles a row by request_id + resolves=<gate entry_hash>, and the
        receipt (Stripe surface) + sandbox_ref (sandbox surface) carry the SAME
        request_id — same id, three surfaces. The receipt only exists after the
        charge, so it cannot live on the gate entry; it lands here."""
        extra = {"request_id": request_id, "resolves": gate_entry_hash,
                 "stripe_receipt": receipt}
        if sandbox_ref is not None:
            extra["sandbox_ref"] = sandbox_ref
        return self.append(
            agent=agent, task=task, merchant=merchant, amount=amount,
            currency=currency, category=None, purpose=None,
            rule_fired="stripe.charged", verdict="SETTLED", now=now, **extra,
        )

    def entries(self):
        return list(self._entries)

    # --- external checkpoint (head + count anchored outside the chain walk) ---

    def _write_checkpoint(self):
        cp = {"head_hash": self.head_hash, "count": len(self._entries)}
        p = self.checkpoint_path
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(cp))

    def read_checkpoint(self):
        p = self.checkpoint_path
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except (ValueError, OSError):
            return None

    def verify(self, expected_head_hash=None, expected_count=None):
        """Walk the chain, recompute every hash + linkage.

        With expected_head_hash / expected_count (an external checkpoint), also
        catches edit+recompute (head changes) and tail deletion (count + head
        change) — attacks a pure internal walk passes.

        Returns (True, None) if intact; (False, i) for a broken link/hash at
        index i; (False, -1) for a checkpoint mismatch the walk can't localize.
        """
        prev = GENESIS
        for i, e in enumerate(self._entries):
            fields = _hashed_fields(e)
            if e.get("prev_hash") != prev:
                return (False, i)
            if e.get("entry_hash") != _entry_hash(fields, prev):
                return (False, i)
            prev = e["entry_hash"]
        if expected_count is not None and len(self._entries) != expected_count:
            return (False, -1)
        if expected_head_hash is not None and self.head_hash != expected_head_hash:
            return (False, -1)
        return (True, None)

    def verify_against_checkpoint(self):
        """Verify the on-disk chain against the persisted sidecar checkpoint.
        Returns (True, None) if no checkpoint exists yet (nothing to contradict)."""
        cp = self.read_checkpoint()
        if cp is None:
            return self.verify()
        return self.verify(expected_head_hash=cp.get("head_hash"),
                           expected_count=cp.get("count"))

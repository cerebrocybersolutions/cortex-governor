"""Runtime entry — the gate a Hermes skill / link-cli wrapper calls per spend.

gate(request) does the whole job: verify the existing ledger hasn't been
tampered (fail closed if it has), evaluate the policy, record the decision to
the tamper-evident ledger (ALLOW/DENY/ESCALATE all logged), return the verdict.
Only an ALLOW should proceed to `link-cli spend-request create`.

Hardened 2026-06-17 (Codex review):
  - gate() FAILS CLOSED: if an existing ledger no longer matches its checkpoint
    (tamper+recompute, tail deletion), it returns DENY ledger.tamper instead of
    operating on a compromised audit trail.
  - the CLI never throws a traceback: malformed JSON, non-numeric amounts, and
    missing fields all resolve to a stable `DENY invalid_request ...` line with
    exit code 1.

CLI:
    echo '{"agent":"hermes","task":"t1","merchant":"openai","amount":12,
           "currency":"usd","category":"api-credits","purpose":"api-credits"}' \
      | python3 governor.py
    -> prints  ALLOW ok  (exit 0 ALLOW, 1 DENY, 2 ESCALATE)
"""
import json
import os
import sys
from pathlib import Path

from policy_engine import (evaluate, load_policy, Decision, ensure_request_id,
                           ALLOW, DENY, ESCALATE)
from ledger import Ledger

_HERE = Path(__file__).resolve().parent
# Env overrides keep the CLI scriptable + let tests use a throwaway ledger
# instead of polluting the real one.
DEFAULT_POLICY = Path(os.environ.get("CEREBRO_GOVERNOR_POLICY", _HERE / "policy.json"))
DEFAULT_LEDGER = Path(os.environ.get("CEREBRO_GOVERNOR_LEDGER", _HERE / "ledger.jsonl"))

_EXIT = {ALLOW: 0, DENY: 1, ESCALATE: 2}


def gate(request, policy_path=DEFAULT_POLICY, ledger_path=DEFAULT_LEDGER):
    policy = load_policy(policy_path)
    led = Ledger(ledger_path)
    # Fail closed: never operate on a ledger that no longer matches its anchor.
    # Do NOT append or rewrite the checkpoint here — that would bless the tamper.
    ok, bad = led.verify_against_checkpoint()
    if not ok:
        return Decision(DENY, "ledger.tamper",
                        "ledger failed checkpoint verify at %s — refusing to spend" % bad)
    # Stamp the cross-surface anchor before recording, so the gate entry carries
    # the request_id the Stripe settlement + sandbox event reconcile against.
    # Work on a copy: never mutate the caller's dict — a reused request template
    # would otherwise inherit the first spend's id and collapse every spend to one.
    request = dict(request)
    ensure_request_id(request)
    dec = evaluate(request, policy, ledger=led)
    led.record(request, dec.rule, dec.verdict)
    return dec


def main(argv):
    try:
        raw = " ".join(argv[1:]) if len(argv) > 1 else sys.stdin.read()
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")
        dec = gate(request)
    except json.JSONDecodeError as e:
        print("DENY invalid_request.json  malformed JSON: %s" % e)
        return _EXIT[DENY]
    except Exception as e:  # last-resort: a stable DENY line, never a traceback
        print("DENY invalid_request  %s" % e)
        return _EXIT[DENY]
    print("%s %s  %s" % (dec.verdict, dec.rule, dec.reason))
    return _EXIT.get(dec.verdict, _EXIT[ESCALATE])


if __name__ == "__main__":
    sys.exit(main(sys.argv))

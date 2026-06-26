"""Bridge: governor decision -> Stripe Link CLI spend.

settle(request) OWNS the policy decision — it re-gates the request through the
governor and acts only on that authoritative verdict. It never accepts a verdict
string from a caller (a forged or malformed verdict was the round-2 review's top
finding). ALLOW spends autonomously, ESCALATE flips to a human Link-app tap, and
DENY / anything-not-actionable never touches the wallet.

Hardened 2026-06-17 after Codex final review (handoff
`2026-06-17-spend-path-final-link-wallet-remediation`):
  - settle() RE-GATES internally; no caller verdict (forged-ALLOW + malformed
    verdict both fail closed). On execution it goes through `gate()`, so the
    ledger-tamper checkpoint also guards the spend.
  - returns ONLY safe structured fields; raw link-cli stdout/stderr is NEVER
    returned (no token/PAN leak), only an allowlisted spend-request id.
  - the executable is pinned; an arbitrary LINK_CLI override is refused for real
    execution unless explicitly opted in (a wrapper could strip --test).
  - money is whole cents via Decimal; sub-cent precision is rejected so the
    approved amount equals the charged amount.

SAFETY (money path — NOT lazy here):
  - test=True + dry_run=True by DEFAULT. Real money needs test=False AND
    i_understand_real_money=True, or settle() raises.
  - card credential -> 0600 temp file (--outputFile), deleted after; never stdout.

stdlib only.
"""
import os
import re
import shlex
import subprocess
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

from policy_engine import evaluate, load_policy, ALLOW, DENY, ESCALATE
from ledger import Ledger

_HERE = Path(__file__).resolve().parent
DEFAULT_POLICY = _HERE / "policy.json"
DEFAULT_LEDGER = _HERE / "ledger.jsonl"

# The wallet executable is pinned. An arbitrary LINK_CLI override could wrap or
# strip --test and spend real money, so overrides are refused for real execution
# unless the caller explicitly opts in.
_EXPECTED_LINK_CLI = str(Path.home() / ".hermes" / "node" / "bin" / "link-cli")

# Only a Link spend-request id is safe to surface from CLI output.
_SPEND_ID_RE = re.compile(r"\blsrq_[A-Za-z0-9]+\b")


def to_cents(amount):
    """(cents:int, None) for a positive whole-cent amount, else (None, reason).

    Decimal-based: rejects sub-cent precision and anything below one cent so the
    charged amount exactly equals what the governor approved (no float rounding)."""
    try:
        d = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError):
        return None, "amount not a valid decimal (got %r)" % amount
    if d <= 0:
        return None, "amount must be positive (got %r)" % amount
    if d != d.quantize(Decimal("0.01")):
        return None, "amount has sub-cent precision (got %r)" % amount
    cents = int((d * 100).to_integral_value())
    if cents < 1:
        return None, "amount below one cent (got %r)" % amount
    return cents, None


def _resolve_link_cli(allow_override):
    """Path to execute, or None to refuse. Refuses an arbitrary LINK_CLI env
    override unless allow_override=True, and requires the binary to exist —
    never run a missing or attacker-supplied executable."""
    env = os.environ.get("LINK_CLI")
    if env and env != _EXPECTED_LINK_CLI and not allow_override:
        return None
    path = env if (env and (allow_override or env == _EXPECTED_LINK_CLI)) else _EXPECTED_LINK_CLI
    return path if Path(path).exists() else None


def _context_for(request):
    ctx = request.get("context")
    if ctx and len(ctx) >= 100:
        return ctx
    return ("Autonomous agent purchase gated by the Cerebro spend governor. "
            "Task %r buying from %r for purpose %r. Verdict was policy-checked "
            "(amount caps, vendor/category rules, rate window, purpose binding) and "
            "recorded to the tamper-evident ledger before this request was created."
            % (request.get("task"), request.get("merchant"), request.get("purpose")))


def build_command(request, verdict, *, payment_method_id, out_file, cents, link_cli, test=True):
    """The exact link-cli argv. ALLOW = no approval poll; ESCALATE = poll (human tap)."""
    cmd = [
        link_cli, "spend-request", "create",
        "--paymentMethodId", payment_method_id,
        "--credentialType", "card",
        "--amount", str(cents),
        "--currency", str(request.get("currency", "usd")),
        "--merchantName", str(request.get("merchant", "")),
        "--merchantUrl", str(request.get("merchant_url") or "https://demo.example.com"),
        "--context", _context_for(request),
        "--lineItem", "name:%s,unit_amount:%d,quantity:1" % (request.get("task", "item"), cents),
        "--total", "type:total,display_text:Total,amount:%d" % cents,
        "--requestApproval", "false" if verdict == ALLOW else "true",
        "--outputFile", out_file, "--force",
    ]
    if test:
        cmd.append("--test")
    return cmd


def settle(request, *, policy_path=DEFAULT_POLICY, ledger_path=DEFAULT_LEDGER,
           payment_method_id=None, test=True, dry_run=True,
           i_understand_real_money=False, allow_link_cli_override=False):
    """Re-gate the request and carry the AUTHORITATIVE verdict to the wallet.

    Returns a redacted result dict. Never accepts a caller verdict; never returns
    raw CLI output; fails closed on DENY / non-actionable verdict / missing pm /
    sub-cent amount / suspicious executable / tampered ledger.
    """
    if not test and not i_understand_real_money:
        raise RuntimeError(
            "REAL-MONEY GUARD: settle(test=False) needs i_understand_real_money=True. "
            "Refusing to spend real funds. Use test=True for the demo.")

    # 1+2. OWN the verdict — re-gate; a caller cannot forge or malform it. On the
    # real path go through gate() so the ledger-tamper checkpoint guards the spend
    # (a tampered ledger returns DENY ledger.tamper -> no spend below).
    if dry_run:
        dec = evaluate(request, load_policy(policy_path), ledger=Ledger(ledger_path))
    else:
        from governor import gate
        dec = gate(request, policy_path=policy_path, ledger_path=ledger_path)

    if dec.verdict == DENY:
        return {"verdict": DENY, "spent": False, "rule": dec.rule,
                "reason": "blocked by governor; wallet not called"}
    if dec.verdict not in (ALLOW, ESCALATE):
        return {"verdict": dec.verdict, "spent": False,
                "error": "non-actionable verdict; failing closed"}
    if not payment_method_id:
        return {"verdict": dec.verdict, "spent": False, "error": "payment_method_id required"}

    # 5. whole-cent money only.
    cents, cerr = to_cents(request.get("amount"))
    if cerr:
        return {"verdict": dec.verdict, "spent": False, "error": cerr}

    link_cli = _resolve_link_cli(allow_link_cli_override)
    out_file = tempfile.NamedTemporaryFile(suffix=".link-card.json", delete=False).name
    cmd = build_command(request, dec.verdict, payment_method_id=payment_method_id,
                        out_file=out_file, cents=cents,
                        link_cli=link_cli or _EXPECTED_LINK_CLI, test=test)

    if dry_run:
        try:
            os.unlink(out_file)
        except OSError:
            pass
        return {"verdict": dec.verdict, "spent": False, "dry_run": True, "test": test,
                "command": " ".join(shlex.quote(c) for c in cmd)}

    # 4. real execution: refuse a missing/suspicious executable, fail closed.
    if link_cli is None:
        try:
            os.unlink(out_file)
        except OSError:
            pass
        return {"verdict": dec.verdict, "spent": False,
                "error": "link-cli not at expected path (or override refused); refusing to spend"}

    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
        # 3. return ONLY safe fields — never raw stdout/stderr; one allowlisted id.
        m = _SPEND_ID_RE.search(r.stdout or "")
        return {"verdict": dec.verdict, "spent": r.returncode == 0, "exit_code": r.returncode,
                "test": test, "spend_request_id": m.group(0) if m else None}
    except FileNotFoundError:
        return {"verdict": dec.verdict, "spent": False, "error": "link-cli not executable"}
    finally:
        try:
            os.unlink(out_file)   # card credential never lingers on disk
        except OSError:
            pass


if __name__ == "__main__":
    # Self-demo (dry-run, throwaway ledger, no record, no execution): settle
    # re-gates each request and prints the exact command for the gated verdict.
    tmp_led = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    Path(tmp_led).unlink(missing_ok=True)
    for label, req in [
        ("gates ALLOW", dict(task="earn-loop", merchant="openai", amount=12.0, currency="usd",
                             category="api-credits", purpose="api-credits")),
        ("gates ESCALATE", dict(task="deploy", merchant="vercel", amount=150.0, currency="usd",
                                category="compute", purpose="infra")),
        ("gates DENY", dict(task="scale", merchant="aws", amount=600.0, currency="usd",
                            category="compute", purpose="compute")),
    ]:
        res = settle(req, ledger_path=tmp_led, payment_method_id="<PAYMENT_METHOD_ID>",
                     test=True, dry_run=True)
        print("%-14s -> %s" % (label, res.get("command") or res.get("reason") or res.get("error")))
    Path(tmp_led).unlink(missing_ok=True)
    Path(tmp_led + ".checkpoint").unlink(missing_ok=True)

"""Self-check for the governor->Stripe Link bridge. Run: python3 test_link_wallet.py

One test per Codex round-2 finding: forged/DENY requests never spend, malformed
verdicts are impossible (settle re-gates), no raw CLI output leaks, arbitrary
LINK_CLI override is refused for execution, sub-cent amounts are rejected, the
real-money guard holds, and test commands carry --test.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

import link_wallet as lw
from policy_engine import ALLOW, DENY, ESCALATE

_HERE = Path(__file__).resolve().parent
POLICY = _HERE / "policy.json"

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


def test_deny_request_never_builds_a_command():
    p = _led()
    try:
        res = lw.settle(DENY_REQ, policy_path=POLICY, ledger_path=p,
                        payment_method_id="pm_x", test=True, dry_run=True)
        assert res["verdict"] == DENY and res["spent"] is False and "command" not in res, res
    finally:
        _clean(p)


def test_forged_allow_is_regated_and_blocked():
    # No caller verdict exists to forge anymore — a DENY-worthy request can never
    # produce a spend, even asking to execute.
    p = _led()
    try:
        res = lw.settle(DENY_REQ, policy_path=POLICY, ledger_path=p,
                        payment_method_id="pm_x", test=True, dry_run=False)
        assert res["spent"] is False and res["verdict"] == DENY, res
    finally:
        _clean(p)


def test_allow_dryrun_has_test_and_no_poll():
    p = _led()
    try:
        res = lw.settle(ALLOW_REQ, policy_path=POLICY, ledger_path=p,
                        payment_method_id="pm_x", test=True, dry_run=True)
        assert res["verdict"] == ALLOW, res
        assert "--test" in res["command"] and "--requestApproval false" in res["command"], res
    finally:
        _clean(p)


def test_escalate_polls_for_human():
    p = _led()
    try:
        res = lw.settle(ESC_REQ, policy_path=POLICY, ledger_path=p,
                        payment_method_id="pm_x", test=True, dry_run=True)
        assert res["verdict"] == ESCALATE and "--requestApproval true" in res["command"], res
    finally:
        _clean(p)


def test_missing_payment_method_fails_closed():
    p = _led()
    try:
        res = lw.settle(ALLOW_REQ, policy_path=POLICY, ledger_path=p,
                        payment_method_id=None, test=True, dry_run=True)
        assert res["spent"] is False and "payment_method_id" in res.get("error", ""), res
    finally:
        _clean(p)


def test_subcent_amount_rejected():
    p = _led()
    try:
        res = lw.settle(dict(ALLOW_REQ, amount=0.004), policy_path=POLICY, ledger_path=p,
                        payment_method_id="pm_x", test=True, dry_run=True)
        assert res["spent"] is False and "command" not in res, res
    finally:
        _clean(p)


def test_real_money_guard_raises():
    p = _led()
    try:
        raised = False
        try:
            lw.settle(ALLOW_REQ, policy_path=POLICY, ledger_path=p,
                      payment_method_id="pm_x", test=False, dry_run=True)
        except RuntimeError:
            raised = True
        assert raised, "test=False without i_understand_real_money must raise"
    finally:
        _clean(p)


def test_link_cli_override_refused_on_execute():
    p = _led()
    try:
        os.environ["LINK_CLI"] = "/tmp/definitely-not-link-cli-bogus-xyz"
        res = lw.settle(ALLOW_REQ, policy_path=POLICY, ledger_path=p, payment_method_id="pm_x",
                        test=True, dry_run=False, allow_link_cli_override=False)
        assert res["spent"] is False and "refusing" in res.get("error", ""), res
    finally:
        os.environ.pop("LINK_CLI", None)
        _clean(p)


def test_no_raw_cli_output_leaks():
    # A fake CLI prints a token + PAN; prove settle returns NONE of it — only the
    # allowlisted spend-request id and structured fields.
    p = _led()
    fake = Path(tempfile.NamedTemporaryFile(suffix="_fakecli.sh", delete=False).name)
    fake.write_text('#!/bin/bash\n'
                    'echo "secret_token=liwltoken_LEAKME pan=4242424242424242"\n'
                    'echo "spend_request_id=lsrq_TESTONLY123"\n'
                    'exit 0\n')
    fake.chmod(0o755)
    try:
        os.environ["LINK_CLI"] = str(fake)
        res = lw.settle(ALLOW_REQ, policy_path=POLICY, ledger_path=p, payment_method_id="pm_x",
                        test=True, dry_run=False, allow_link_cli_override=True)
        blob = json.dumps(res)
        assert "liwltoken_LEAKME" not in blob and "4242424242424242" not in blob, ("LEAK", res)
        assert "stdout" not in res and "stderr" not in res, res
        assert res.get("spend_request_id") == "lsrq_TESTONLY123", res     # allowlisted id ok
        assert res["spent"] is True, res
    finally:
        os.environ.pop("LINK_CLI", None)
        fake.unlink(missing_ok=True)
        _clean(p)


if __name__ == "__main__":
    test_deny_request_never_builds_a_command(); print("  DENY request never builds a command: OK")
    test_forged_allow_is_regated_and_blocked(); print("  forged ALLOW re-gated -> blocked: OK")
    test_allow_dryrun_has_test_and_no_poll(); print("  ALLOW: --test + requestApproval false: OK")
    test_escalate_polls_for_human(); print("  ESCALATE: requestApproval true: OK")
    test_missing_payment_method_fails_closed(); print("  missing pm -> fail closed: OK")
    test_subcent_amount_rejected(); print("  sub-cent amount rejected: OK")
    test_real_money_guard_raises(); print("  real-money guard raises: OK")
    test_link_cli_override_refused_on_execute(); print("  arbitrary LINK_CLI override refused: OK")
    test_no_raw_cli_output_leaks(); print("  no raw CLI output leaks (token/PAN): OK")
    print("ALL LINK-WALLET TESTS PASS")

"""Self-check for the demo's env-loading + gating orchestration.
Run: python3 test_stripe_demo.py

The demo is glue over the already-tested wallet; the one piece of real logic is
_ensure_key (the .env fallback that lets a Hermes desktop run find the key when
it can't see shell env). Prove it finds a key in a .env, returns the source,
never leaks the value, and fails closed when absent.
"""
import os
import tempfile
from pathlib import Path

import stripe_demo as sd

KEY = "rk_test_" + "FAKE_FOR_DEMO_TESTS"   # synthetic fixture, not a real key


def _without_env_key(fn):
    saved = os.environ.pop("STRIPE_TEST_KEY", None)
    try:
        return fn()
    finally:
        os.environ.pop("STRIPE_TEST_KEY", None)
        if saved is not None:
            os.environ["STRIPE_TEST_KEY"] = saved


def test_ensure_key_from_env_file():
    def body():
        d = Path(tempfile.mkdtemp())
        env = d / ".env"
        env.write_text('# comment\nFOO=bar\nSTRIPE_TEST_KEY="%s"\n' % KEY)
        saved = sd._ENV_CANDIDATES
        sd._ENV_CANDIDATES = [env]
        try:
            src = sd._ensure_key()
            assert src == str(env), src
            assert os.environ.get("STRIPE_TEST_KEY") == KEY, "key not loaded into env"
        finally:
            sd._ENV_CANDIDATES = saved
    _without_env_key(body)


def test_ensure_key_shell_env_wins():
    os.environ["STRIPE_TEST_KEY"] = KEY
    try:
        saved = sd._ENV_CANDIDATES
        sd._ENV_CANDIDATES = [Path("/nonexistent/.env")]
        try:
            assert sd._ensure_key() == "shell env"
        finally:
            sd._ENV_CANDIDATES = saved
    finally:
        os.environ.pop("STRIPE_TEST_KEY", None)


def test_ensure_key_missing_returns_none():
    def body():
        saved = sd._ENV_CANDIDATES
        sd._ENV_CANDIDATES = [Path("/nonexistent/a.env"), Path("/nonexistent/b.env")]
        try:
            assert sd._ensure_key() is None
        finally:
            sd._ENV_CANDIDATES = saved
    _without_env_key(body)


def test_ensure_key_skips_invalid_utf8_candidate():
    # Codex LOW: a binary/invalid-UTF-8 .env must be skipped, not crash. A valid
    # candidate after it still resolves.
    def body():
        d = Path(tempfile.mkdtemp())
        bad = d / "bad.env"
        bad.write_bytes(b"\xff\xfe\x00STRIPE_TEST_KEY=junk\xff")
        good = d / "good.env"
        good.write_text("STRIPE_TEST_KEY=%s\n" % KEY)
        saved = sd._ENV_CANDIDATES
        sd._ENV_CANDIDATES = [bad, good]
        try:
            assert sd._ensure_key() == str(good), "bad candidate must be skipped, good one used"
            assert os.environ.get("STRIPE_TEST_KEY") == KEY
        finally:
            sd._ENV_CANDIDATES = saved
    _without_env_key(body)


def test_ensure_key_skips_nul_value():
    # A NUL in the value would raise on os.environ assignment — skip, fail closed.
    def body():
        d = Path(tempfile.mkdtemp())
        env = d / ".env"
        env.write_text("STRIPE_TEST_KEY=ab\x00cd\n")
        saved = sd._ENV_CANDIDATES
        sd._ENV_CANDIDATES = [env]
        try:
            assert sd._ensure_key() is None          # no crash, no key set
            assert os.environ.get("STRIPE_TEST_KEY") is None
        finally:
            sd._ENV_CANDIDATES = saved
    _without_env_key(body)


def test_run_fails_closed_without_key():
    def body():
        saved = sd._ENV_CANDIDATES
        sd._ENV_CANDIDATES = [Path("/nonexistent/.env")]
        try:
            assert sd.run(live=False, pause=0) == 2     # no key -> exit 2, no crash
        finally:
            sd._ENV_CANDIDATES = saved
    _without_env_key(body)


def test_run_dryrun_returns_zero_with_key():
    os.environ["STRIPE_TEST_KEY"] = KEY
    try:
        assert sd.run(live=False, pause=0) == 0         # dry-run: gates 3 beats, no charge
    finally:
        os.environ.pop("STRIPE_TEST_KEY", None)


def test_beats_match_policy_contract():
    # The demo's derived amounts must ACTUALLY produce their expected verdict under
    # the live policy — the guarantee that the demo can't narrate a verdict it
    # didn't get (Principle #10). This is the regression test for the drift bug.
    from policy_engine import evaluate, load_policy
    policy = load_policy(Path(sd.__file__).resolve().parent / "policy.json")
    for label, req, expected in sd._beats(policy):
        got = evaluate(dict(req), policy).verdict
        assert got == expected, (label, "expected", expected, "got", got)


def test_run_fails_on_verdict_mismatch():
    # If a beat's actual verdict disagrees with its claimed expected, run() must
    # FAIL (non-zero) and cannot print "safe". Force a mismatch: claim an ALLOW
    # request is "expected DENY".
    from policy_engine import DENY
    os.environ["STRIPE_TEST_KEY"] = KEY
    saved = sd._beats
    try:
        sd._beats = lambda policy: [
            ("forced-mismatch",
             dict(agent="h", task="t", merchant="openai", amount=12.0, currency="usd",
                  category="api-credits", purpose="api-credits"), DENY),
        ]
        assert sd.run(live=False, pause=0) != 0, "mismatch must fail closed"
    finally:
        sd._beats = saved
        os.environ.pop("STRIPE_TEST_KEY", None)


def test_capture_verify_exact_contract_passes():
    import capture_insandbox as ci
    ci.verify_scenario()


def test_capture_verify_rejects_same_spread_wrong_amount():
    # A 3/3/4 spread alone is not enough: the capture gate must bind the exact
    # captured-footage amounts/rules. $13 openai is still ALLOW, so only the exact
    # contract check catches this drift.
    import capture_insandbox as ci
    saved = ci.SCENARIO
    bad = [dict(req) for req in saved]
    bad[0]["amount"] = 13.0
    ci.SCENARIO = bad
    try:
        try:
            ci.verify_scenario()
            assert False, "verify_scenario must reject request drift even if verdict spread stays 3/3/4"
        except ValueError as e:
            assert "beat 1 request drift" in str(e)
    finally:
        ci.SCENARIO = saved


if __name__ == "__main__":
    test_ensure_key_from_env_file();       print("  _ensure_key reads .env + loads key: OK")
    test_ensure_key_shell_env_wins();      print("  shell env wins over .env: OK")
    test_ensure_key_missing_returns_none(); print("  missing key -> None: OK")
    test_ensure_key_skips_invalid_utf8_candidate(); print("  invalid-utf8 .env skipped, not fatal: OK")
    test_ensure_key_skips_nul_value();     print("  NUL value skipped, fail closed: OK")
    test_run_fails_closed_without_key();   print("  run() fails closed (exit 2) without key: OK")
    test_run_dryrun_returns_zero_with_key(); print("  run() dry-run returns 0: OK")
    test_beats_match_policy_contract();    print("  demo beats match policy contract: OK")
    test_run_fails_on_verdict_mismatch();  print("  run() fails closed on verdict mismatch: OK")
    test_capture_verify_exact_contract_passes(); print("  capture --verify exact contract passes: OK")
    test_capture_verify_rejects_same_spread_wrong_amount(); print("  capture --verify rejects same-spread drift: OK")
    print("ALL STRIPE-DEMO TESTS PASS")

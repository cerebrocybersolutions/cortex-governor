"""Render the production demo SITE (the hero) from a captured run + real brand marks.

Design: Restrained color strategy (impeccable) — tinted-graphite near-black base,
off-white type, and color used ONLY where it carries meaning (the verdicts, the
flow branches, small sponsor marks). Calistoga serif display + Inter + JetBrains
Mono. Deliberately not a green-drenched dev theme.

Centerpiece = the real topology: a circular reasoning loop where the Hermes agent
reasons with Nemotron (on an OpenRouter cloud) and proposes a spend to Cortex (the
policy gate); Cortex then forks the spend three ways (ALLOW -> Stripe,
ESCALATE -> Human, DENY -> blocked); every decision drops to the hash-chain ledger.
The Hermes agent uses the operator's real avatar (assets/hermes-avatar.png) when
present, embedded as base64; otherwise a neutral fallback.

Provenance split correctly: NemoClaw (sandbox) runs on the local RTX 4090; Nemotron
3 Ultra reasons via OpenRouter (cloud), not on the GPU.

Self-contained + deterministic replay. Pure render over captured payload.

Two builds from one generator, same captured payload:
  - LIVE (default): countdown-approve-or-queue async pipeline + fire-and-forget
    Telegram alert per queued escalation -> index-live.html.
  - LOCKED (--locked): the recorded hold-for-human cinematic -> the protected
    index.html fallback (writing index.html needs --allow-locked-overwrite).

Usage:  python3 build_site.py                 # live async build -> index-live.html
        python3 build_site.py --no-notify     # live, no Telegram
        python3 build_site.py --locked --out index.html --allow-locked-overwrite
"""
import argparse
import base64
import json
from pathlib import Path

from ledger import Ledger

_HERE = Path(__file__).resolve().parent
_ASSETS = _HERE / "assets"
VERDICT_OK = {"ALLOW": True, "ESCALATE": False, "DENY": False}


def svg(name):
    p = _ASSETS / name
    return p.read_text() if p.exists() else ""


def hermes_icon():
    """Operator's real avatar embedded as base64 if present; else a neutral mark."""
    for ext in ("png", "jpg", "jpeg", "webp"):
        p = _ASSETS / ("hermes-avatar." + ext)
        if p.exists():
            mime = "jpeg" if ext in ("jpg", "jpeg") else ext
            b64 = base64.b64encode(p.read_bytes()).decode()
            return '<img class="ava" alt="Hermes" src="data:image/%s;base64,%s">' % (mime, b64)
    return '<span class="ava ava-fb">H</span>'


def build_beats(entries):
    settlements = {e["resolves"]: e for e in entries
                   if e.get("verdict") == "SETTLED" and e.get("resolves")}
    # Fold ONLY resolutions that pass escalation.py's canonical semantics (mirror of
    # _valid_resolution / pending): close a real EARLIER ESCALATE, first-resolution-wins,
    # rule matches verdict, non-empty approver, no self-reference. A forged/duplicate/self
    # resolution must not silently fold into a beat.
    _RES_VERDICT = {"escalation.approved": "ALLOW", "escalation.denied": "DENY"}
    escalations_seen, resolutions = {}, {}
    for e in entries:
        if e.get("verdict") == "ESCALATE" and e.get("entry_hash"):
            escalations_seen[e["entry_hash"]] = e
            continue
        tgt, rule = e.get("resolves"), e.get("rule_fired")
        if (not tgt or rule not in _RES_VERDICT or tgt == e.get("entry_hash")
                or tgt not in escalations_seen or tgt in resolutions
                or e.get("verdict") != _RES_VERDICT[rule]):
            continue
        approver = e.get("approver")
        if not isinstance(approver, str) or not approver.strip():
            continue
        resolutions[tgt] = e
    out = []
    for e in entries:
        # primary gate decisions only — settlements + resolutions carry `resolves`
        # and fold into the beat they close, never a beat of their own
        if e.get("verdict") not in VERDICT_OK or e.get("resolves"):
            continue
        s = settlements.get(e.get("entry_hash"))
        beat = {
            "verdict": e["verdict"], "agent": e.get("agent"), "task": e.get("task"),
            "merchant": e.get("merchant"), "amount": e.get("amount"),
            "request_id": e.get("request_id") or "(unset)", "rule_fired": e.get("rule_fired"),
            "entry_hash": e.get("entry_hash"), "sandbox_ref": e.get("sandbox_ref") or "(none)",
            "stripe_receipt": (s or {}).get("stripe_receipt"),
        }
        r = resolutions.get(e.get("entry_hash"))
        if r:
            beat["resolution"] = {
                "approver": r.get("approver"), "verdict": r.get("verdict"),
                "rule_fired": r.get("rule_fired"), "entry_hash": r.get("entry_hash"),
            }
        out.append(beat)
    return out


def verify_chain(led_path, entries, beats):
    """Actually verify the ledger before the site is allowed to claim it.

    1. hash-chain walk + recompute — reuses the canonical Ledger.verify (the SAME
       code the gate trusts), anchored to the sidecar checkpoint when present.
    2. every SETTLED entry resolves a real ALLOW gate entry and carries its request_id.
    3. every ALLOW beat that shows a receipt is backed by a settlement entry.

    Returns (ok, reason). The site only prints "chain verified" when ok is True.
    """
    ok, bad = Ledger(str(led_path)).verify_against_checkpoint()
    if not ok:
        return False, "hash chain broken at index %s" % bad
    by_hash = {e.get("entry_hash"): e for e in entries}
    settled = set()
    for e in entries:
        if e.get("verdict") == "SETTLED":
            tgt = by_hash.get(e.get("resolves"))
            if tgt is None:
                return False, "settlement resolves a missing gate entry"
            if tgt.get("verdict") != "ALLOW":
                return False, "settlement resolves a non-ALLOW gate entry"
            if tgt.get("request_id") != e.get("request_id"):
                return False, "settlement request_id mismatch"
            settled.add(e.get("resolves"))
    for b in beats:
        if b["verdict"] == "ALLOW" and b["stripe_receipt"] and b["entry_hash"] not in settled:
            return False, "ALLOW shows a receipt with no settlement entry"
    return True, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default="sandbox_ledger.jsonl")
    ap.add_argument("--meta", default="sandbox_run_meta.json")
    # Default points at the LIVE file, never the locked cinematic fallback — a bare
    # `python3 build_site.py` can no longer clobber index.html (the recorded run).
    ap.add_argument("--out", default="index-live.html")
    ap.add_argument("--async-queue", dest="async_queue", action=argparse.BooleanOptionalAction,
                    default=True, help="countdown-approve-or-queue async pipeline (default on); "
                    "--no-async-queue restores the locked hold-for-human behavior")
    ap.add_argument("--notify", action=argparse.BooleanOptionalAction, default=True,
                    help="fire-and-forget Telegram alert once per queued escalation request_id (default on)")
    ap.add_argument("--grace-ms", dest="grace_ms", type=int, default=2500,
                    help="manual-approve grace window per escalation, in ms (default 2500)")
    ap.add_argument("--locked", action="store_true",
                    help="reproduce the locked cinematic behavior (no async queue, no notify)")
    ap.add_argument("--allow-locked-overwrite", action="store_true",
                    help="permit writing to index.html, the locked fallback (guard; off by default)")
    args = ap.parse_args()

    if args.locked:
        args.async_queue = False
        args.notify = False

    # Safety rail: this TEMPLATE is STALE. It predates the 2026-06-18 record-ready polish
    # (commit 68f5e82e: cloud removed + permanent auto-stage) that was hand-applied to
    # index.html and index-live.html directly. Both demo pages are currently HAND-MAINTAINED
    # from the record-ready index.html — a regen from this template REVERTS that look (brings
    # the cloud back, drops auto-stage). So refuse to write either unless explicitly forced.
    # Re-sync this TEMPLATE to the record-ready index.html before trusting the generator again
    # (needed before the 25-beat capture regen).
    _PROTECTED = ("index.html", "index-live.html")
    _out = _HERE / args.out

    def _hits_protected(op):
        # case-insensitive basename: this FS is case-insensitive, so Index.html / INDEX.HTML
        # target the same inode as index.html without an exact-name match.
        if op.name.casefold() in {n.casefold() for n in _PROTECTED}:
            return True
        # a symlink / alias whose REAL target is a protected page (different basename).
        for prot in _PROTECTED:
            pp = _HERE / prot
            try:
                if pp.exists() and op.exists() and op.samefile(pp):
                    return True
                if pp.exists() and op.resolve() == pp.resolve():
                    return True
            except OSError:
                pass
        return False

    if _hits_protected(_out) and not args.allow_locked_overwrite:
        raise SystemExit(
            "refusing to write %s — it resolves onto a protected demo page (index.html / "
            "index-live.html), which are HAND-MAINTAINED from the record-ready index.html. THIS "
            "generator's template is stale (it would revert cloud-removal + auto-stage). Re-sync "
            "the template first, or pass --allow-locked-overwrite to override (WILL revert the "
            "record-ready look). For a scratch render use --out <other-name>.html." % args.out)

    led = _HERE / args.ledger
    if not led.exists():
        led = _HERE / "capture_ledger.jsonl"
    entries = [json.loads(l) for l in led.read_text().splitlines() if l.strip()]
    mp = _HERE / args.meta
    prov = json.loads(mp.read_text()) if mp.exists() else {}

    # Every on-screen number must trace to the CAPTURED meta, not a mutable side file.
    # The capture writes a policy snapshot into run_meta (values + a sha256 of the exact
    # policy.json bytes). If it's missing we FAIL CLOSED rather than reading the live
    # policy.json — a number we can't anchor to captured provenance must not reach the site.
    pol = prov.get("policy")
    if not pol:
        raise SystemExit(
            "refusing to build: %s carries no policy snapshot — every on-screen number "
            "must trace to captured provenance, not the mutable live policy.json. "
            "Re-capture with capture_insandbox.py to embed the snapshot." % args.meta)

    beats = build_beats(entries)
    charged = sum(b["amount"] for b in beats if b["verdict"] == "ALLOW" and b["stripe_receipt"])
    chain_ok, chain_reason = verify_chain(led, entries, beats)
    meta = {
        "budget": pol.get("budget"), "per_txn_cap": pol.get("per_txn_cap"),
        "hard_max": pol.get("hard_max"), "policy_sha256": pol.get("sha256"),
        "charged_total": charged,
        "n_esc": sum(1 for b in beats if b["verdict"] == "ESCALATE"),
        "n_deny": sum(1 for b in beats if b["verdict"] == "DENY"),
        "sandbox_gpu": prov.get("sandbox_gpu", prov.get("gpu", "RTX 4090")),
        "inference": prov.get("inference", "OpenRouter"),
        "chain_verified": chain_ok,
        "real": prov.get("origin") == "in-sandbox"
                and all(b["stripe_receipt"] for b in beats if b["verdict"] == "ALLOW"),
    }

    # Live async-queue features are injected ONLY when enabled, so the locked build's
    # template body is unchanged (placeholders collapse to ""). The config object is
    # present in both modes; the JS branches on CFG.asyncQueue.
    live_cfg = {"asyncQueue": bool(args.async_queue), "notify": bool(args.notify),
                "graceMs": int(args.grace_ms)}

    html = (TEMPLATE
            .replace("__BEATS__", json.dumps(beats))
            .replace("__META__", json.dumps(meta))
            .replace("__LIVE_CFG__", json.dumps(live_cfg))
            .replace("__LIVE_CSS__", LIVE_CSS if args.async_queue else "")
            .replace("__LIVE_QUEUE_DOM__", LIVE_QUEUE_DOM if args.async_queue else "")
            .replace("__LIVE_JS__", LIVE_JS if args.async_queue else "")
            .replace("__HERMES_ICON__", hermes_icon())
            .replace("__SVG_CEREBRO__", svg("cerebro-mark.svg"))
            .replace("__SVG_STRIPE__", svg("stripe.svg"))
            .replace("__SVG_NVIDIA__", svg("nvidia.svg"))
            .replace("__SVG_NEMOCLAW__", svg("nemoclaw.svg")))
    (_HERE / args.out).write_text(html)
    print("site -> %s  (%d beats, $%.2f charged, chain=%s, async=%s, notify=%s, avatar=%s)"
          % (args.out, len(beats), charged,
             "verified" if chain_ok else "UNVERIFIED(%s)" % chain_reason,
             "on" if args.async_queue else "off", "on" if args.notify else "off",
             "real" if "img" in hermes_icon() else "fallback"))


TEMPLATE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cortex — the control plane for agentic spend</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Calistoga&family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  /* Restrained: tinted-graphite neutrals, cool hue. Color only where meaningful. */
  --bg:#0a0b0e;--bg2:#0d0f13;--surface:#14161c;--surface2:#1b1e26;--border:#262a33;--line:#313742;
  --fg:#eceef3;--muted:#959ba8;--faint:#767d8d;
  --ink:#0a0b0e;
  --allow:#5fae5a;--escalate:#e0a93a;--deny:#e8554d;        /* semantic verdicts */
  --nv:#76b900;--stripe:#635bff;--purple:#8b85f0;           /* sponsor + cloud */
  --serif:'Calistoga',Georgia,'Times New Roman',serif;
  --sans:'Inter',system-ui,-apple-system,'Segoe UI',sans-serif;
  --mono:'JetBrains Mono','SF Mono',Menlo,Consolas,monospace;
  --eo:cubic-bezier(.16,1,.3,1);
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--fg);font-family:var(--sans);-webkit-font-smoothing:antialiased;line-height:1.5;overflow-x:hidden;
  background-image:radial-gradient(820px 480px at 80% -8%,rgba(139,133,240,.06),transparent 60%)}
.mono{font-family:var(--mono)}
.wrap{max-width:1180px;margin:0 auto;padding:0 30px}
a{color:inherit;text-decoration:none}
section{padding:92px 0;position:relative}
.eyebrow{font-family:var(--mono);font-size:11.5px;font-weight:600;letter-spacing:.24em;text-transform:uppercase;color:var(--muted)}
h2{font-family:var(--serif);font-size:clamp(2rem,4.4vw,3.2rem);line-height:1.04;letter-spacing:-.01em;margin:14px 0 12px;font-weight:400}
.lead{color:var(--muted);font-size:17px;max-width:600px;line-height:1.6}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
.bridge{font-family:var(--mono);font-size:12px;color:var(--muted);margin-top:13px;line-height:1.5;letter-spacing:.01em;border-left:2px solid var(--line);padding-left:11px}
.bridge b{color:var(--fg);font-weight:600}
.xstamp{float:right;font-family:var(--mono);font-size:9px;font-weight:600;color:var(--allow);border:1px solid rgba(95,174,90,.4);background:rgba(95,174,90,.1);border-radius:20px;padding:1px 8px;opacity:0;transform:translateY(-2px);transition:.3s var(--eo)}
.xstamp.show{opacity:1;transform:none}
.xcard{transition:border-color .4s var(--eo),box-shadow .4s var(--eo)}
.xcard.reconciled{border-color:rgba(95,174,90,.5);box-shadow:0 0 0 1px rgba(95,174,90,.4),0 18px 48px -22px rgba(95,174,90,.45)}
@keyframes xpulse{0%{box-shadow:0 0 0 0 rgba(95,174,90,.5)}70%{box-shadow:0 0 0 14px rgba(95,174,90,0)}100%{box-shadow:0 0 0 0 transparent}}
.xcard.pulse{animation:xpulse .9s var(--eo)}
.chain-bad{color:var(--deny)!important}

.logo{display:inline-flex;align-items:center;justify-content:center}
.logo svg{display:block;width:100%;height:100%}
.logo-stripe svg path{fill:var(--stripe)}
.logo-nv svg path{fill:var(--nv)}
.logo-nemoclaw svg path,.logo-nemoclaw svg polygon{fill:var(--nv)!important}
.ava{width:30px;height:30px;border-radius:50%;object-fit:cover;flex:none;border:1px solid var(--line)}
.ava-fb{display:grid;place-items:center;background:var(--surface2);color:var(--fg);font-family:var(--serif);font-size:14px}
.gbadge{border-radius:8px;display:grid;place-items:center;flex:none;background:var(--surface2);border:1px solid var(--line);color:var(--fg)}
.gbadge svg{width:62%;height:62%;display:block}

nav{position:sticky;top:0;z-index:60;background:rgba(10,11,14,.72);backdrop-filter:blur(16px);border-bottom:1px solid var(--border)}
.nav-in{display:flex;align-items:center;justify-content:space-between;height:86px}
.brandmark{display:flex;align-items:center;gap:13px;font-weight:700;font-size:27px;letter-spacing:-.01em}
.brandmark .bm{width:42px;height:42px;font-size:22px;font-family:var(--serif)}.brandmark b{font-family:var(--serif);font-weight:400;color:var(--muted)}
.builton{display:flex;align-items:center;gap:22px}
.builton .lbl{font-family:var(--mono);font-size:11.5px;color:var(--faint);letter-spacing:.16em;text-transform:uppercase}
.builton .logo{height:27px;width:auto;opacity:.85;transition:opacity .25s var(--eo)}.builton .logo:hover{opacity:1}
.builton .logo-stripe{height:19px}.builton .ava{width:28px;height:28px;vertical-align:middle}
.cta{font-family:var(--sans);font-size:13px;font-weight:650;padding:9px 17px;border-radius:9px;background:var(--fg);color:var(--ink);cursor:pointer;border:none;transition:.2s var(--eo)}
.cta:hover{transform:translateY(-1px);box-shadow:0 8px 22px -10px rgba(236,238,243,.4)}
@media(max-width:860px){.builton .lbl,.builton .logo-nemoclaw{display:none}}

.hero{padding:42px 0 34px;text-align:center}
.kicker{display:inline-flex;align-items:center;gap:8px;font-family:var(--mono);font-size:12px;font-weight:500;padding:6px 15px;border-radius:30px;border:1px solid var(--border);background:var(--surface);color:var(--muted);margin-bottom:30px}
.kdot{width:7px;height:7px;border-radius:50%;background:var(--allow);animation:pulse 2.4s var(--eo) infinite}
.kicker.pending .kdot{background:var(--escalate)}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(95,174,90,.5)}70%{box-shadow:0 0 0 8px rgba(95,174,90,0)}100%{box-shadow:0 0 0 0 transparent}}
.hero h1{font-family:var(--serif);font-weight:400;font-size:clamp(2rem,4.2vw,3.3rem);line-height:1.06;letter-spacing:-.02em;margin-bottom:18px;color:var(--fg)}
.hero h1 em{font-style:italic;color:var(--muted)}
.hero p{color:var(--muted);font-size:clamp(16px,2vw,19px);max-width:660px;margin:0 auto 24px;line-height:1.55}
.hero-cta{display:flex;gap:13px;justify-content:center;align-items:center;flex-wrap:wrap}
.btn{font-family:var(--sans);font-size:14.5px;font-weight:650;padding:13px 24px;border-radius:11px;cursor:pointer;border:1px solid var(--line);background:transparent;color:var(--fg);transition:.2s var(--eo)}
.btn:hover{background:var(--surface);border-color:#3d4350}
.btn.primary{background:var(--fg);color:var(--ink);border-color:var(--fg)}.btn.primary:hover{transform:translateY(-1px);box-shadow:0 10px 26px -12px rgba(236,238,243,.45)}
.statline{display:flex;justify-content:center;margin-top:26px;border:1px solid var(--border);border-radius:16px;overflow:hidden;background:var(--surface)}
.stat{flex:1;text-align:center;padding:16px 12px;border-right:1px solid var(--border)}.stat:last-child{border-right:0}
.stat b{font-family:var(--serif);display:block;font-size:30px;font-weight:400;line-height:1}
.stat span{font-family:var(--mono);font-size:11px;color:var(--faint);letter-spacing:.1em;display:block;margin-top:9px}
@media(max-width:680px){.statline{flex-wrap:wrap}.stat{flex:1 1 50%;border-bottom:1px solid var(--border)}}

/* ==== circular reasoning loop + fork ==== */
.demo{background:linear-gradient(180deg,var(--bg2),var(--bg));border-top:1px solid var(--border);border-bottom:1px solid var(--border)}
.demo-head{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;margin-bottom:30px;flex-wrap:wrap}
.controls{display:flex;gap:10px;align-items:center}.status{font-family:var(--mono);color:var(--faint);font-size:12px}
.flow{position:relative;width:100%;aspect-ratio:1180/540;min-height:380px;margin-bottom:24px}
.flow svg.wires{position:absolute;inset:0;width:100%;height:100%;overflow:visible}
.orbit{fill:none;stroke:var(--line);stroke-width:1.4;stroke-dasharray:2 8;opacity:.6;transform-origin:300px 300px;animation:spin 26s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.arc{fill:none;stroke:var(--line);stroke-width:1.8;opacity:.5;transition:.3s var(--eo)}
.arc.on{opacity:1;stroke:var(--muted);stroke-dasharray:8 9;animation:dash .8s linear infinite}
.fpath{fill:none;stroke:var(--line);stroke-width:2;opacity:.45;transition:.3s var(--eo)}
.fpath.on{opacity:1;stroke-width:2.4;stroke-dasharray:9 10;animation:dash .8s linear infinite}
.fpath.on.allow{stroke:var(--allow)}.fpath.on.escalate{stroke:var(--escalate)}.fpath.on.deny{stroke:var(--deny)}.fpath.on.ledger{stroke:var(--muted)}
@keyframes dash{to{stroke-dashoffset:-38}}
.sbx{fill:rgba(255,255,255,.012);stroke:var(--border);stroke-width:1.4;stroke-dasharray:7 7;rx:18}
.cloud{fill:rgba(139,133,240,.05);stroke:rgba(139,133,240,.32);stroke-width:1.4}
.lbl-m{font-family:var(--mono);font-size:12px;fill:var(--faint);letter-spacing:.04em}
.node{position:absolute;transform:translate(-50%,-50%);background:var(--surface);border:1px solid var(--border);border-radius:13px;padding:11px 13px;width:166px;z-index:2;
  transition:border-color .3s var(--eo),box-shadow .3s var(--eo),transform .3s var(--eo);opacity:0;animation:rise .6s var(--eo) forwards}
.node.sm{width:150px;padding:10px 12px}
.node .nh{display:flex;align-items:center;gap:9px;margin-bottom:6px}
.node .nh .logo{height:20px;flex:none}.node .nh .logo-stripe{width:46px}.node .nh .logo-nv{width:20px;height:20px}
.node .nh .gbadge{width:28px;height:28px;font-size:15px;border-radius:7px}
.node .nname{font-weight:700;font-size:13.5px;letter-spacing:-.01em}
.node .nsub{font-family:var(--mono);font-size:11px;color:var(--muted);margin-top:3px;line-height:1.35}
.node .ndata{font-family:var(--mono);font-size:11px;color:var(--fg);margin-top:7px;word-break:break-all;min-height:11px;opacity:.92}
@keyframes rise{from{opacity:0;transform:translate(-50%,calc(-50% + 12px))}to{opacity:1;transform:translate(-50%,-50%)}}
.node.lit{border-color:var(--line);box-shadow:0 14px 40px -18px rgba(0,0,0,.7),0 0 0 1px var(--line)}
.node.gov{width:184px}
.node.gov.lit-allow{border-color:var(--allow);box-shadow:0 0 0 1px var(--allow),0 16px 42px -18px rgba(95,174,90,.5)}
.node.gov.lit-escalate{border-color:var(--escalate);box-shadow:0 0 0 1px var(--escalate),0 16px 42px -18px rgba(224,169,58,.5)}
.node.gov.lit-deny{border-color:var(--deny);box-shadow:0 0 0 1px var(--deny),0 16px 42px -18px rgba(232,85,77,.5)}
#govStamp{font-family:var(--serif);font-size:23px;font-weight:400;opacity:.22;transition:.25s var(--eo);line-height:1.1;margin-top:3px}
#govStamp.allow{color:var(--allow);opacity:1}#govStamp.escalate{color:var(--escalate);opacity:1}#govStamp.deny{color:var(--deny);opacity:1}
.node.out{opacity:.34}.node.out.lit{opacity:1}
.node.out.stripe.lit{border-color:var(--stripe);box-shadow:0 0 0 1px var(--stripe)}
.node.out.human.lit{border-color:var(--escalate);box-shadow:0 0 0 1px var(--escalate)}
.node.out.deny.lit{border-color:var(--deny);box-shadow:0 0 0 1px var(--deny)}
.chip{font-family:var(--mono);font-size:8px;font-weight:600;border-radius:20px;padding:1px 7px;letter-spacing:.05em;margin-left:auto;color:var(--purple);border:1px solid rgba(139,133,240,.3);background:rgba(139,133,240,.08)}
.glyph{width:28px;height:28px;border-radius:7px;display:grid;place-items:center;font-size:14px;flex:none;background:var(--surface2);border:1px solid var(--line)}
.glyph.human{color:var(--escalate)}.glyph.deny{color:var(--deny)}
@media(max-width:820px){.flow{aspect-ratio:auto;min-height:0;display:flex;flex-direction:column;gap:10px}.flow svg.wires{display:none}
  .node{position:static;transform:none;width:100%!important;animation:none;opacity:1}.node.lit{transform:none}
  /* stack in real flow order: Hermes -> Nemotron -> Cortex -> branches */
  #n_hermes{order:1}#n_nemo{order:2}#n_gov{order:3}#n_stripe{order:4}#n_human{order:5}#n_deny{order:6}}

.grid2{display:grid;grid-template-columns:1fr 1.28fr;gap:16px;margin-top:4px}
@media(max-width:820px){.grid2{grid-template-columns:1fr}}
.card{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:20px 22px}
.label{font-family:var(--mono);font-size:11.5px;text-transform:uppercase;letter-spacing:.12em;color:var(--faint);margin-bottom:14px}
.bignum{font-family:var(--serif);font-size:40px;font-weight:400}
.budget-sub{color:var(--muted);font-size:13px;margin-top:3px}
.bar{height:8px;background:var(--surface2);border-radius:6px;overflow:hidden;margin-top:14px}
.bar>span{display:block;height:100%;width:100%;background:var(--allow);transition:width .7s var(--eo)}
.capline{display:flex;justify-content:space-between;font-family:var(--mono);font-size:12.5px;color:var(--muted);margin-top:14px}.capline b{color:var(--fg)}
.xrow{display:grid;grid-template-columns:104px 1fr;gap:10px;padding:8px 0;border-top:1px solid var(--border);font-size:13px}
.xrow:first-child{border-top:0}.xrow .k{color:var(--faint);font-family:var(--mono);font-size:12.5px}
.xrow .v{font-family:var(--mono);font-size:13px;word-break:break-all}
.xrow .v.match{color:var(--allow)}.xrow .v.stripe{color:var(--purple)}.xrow .v.pending{color:var(--escalate)}.xrow .v.mute{color:var(--faint)}
.xnote{font-size:12.5px;color:var(--muted);margin-top:13px;line-height:1.55}.xnote b{color:var(--fg)}

table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-family:var(--mono);color:var(--faint);font-weight:500;font-size:11.5px;text-transform:uppercase;letter-spacing:.08em;padding:10px;border-bottom:1px solid var(--border)}
td{padding:10px;border-bottom:1px solid var(--border);font-family:var(--mono);font-size:13px}
tbody tr{opacity:0;transform:translateY(6px);transition:.4s var(--eo)}tbody tr.in{opacity:1;transform:none}
.pill{font-family:var(--mono);font-size:10px;font-weight:600;padding:2px 9px;border-radius:20px;border:1px solid}
.pill.allow{color:var(--allow);border-color:rgba(95,174,90,.34);background:rgba(95,174,90,.1)}
.pill.escalate{color:var(--escalate);border-color:rgba(224,169,58,.34);background:rgba(224,169,58,.1)}
.pill.deny{color:var(--deny);border-color:rgba(232,85,77,.34);background:rgba(232,85,77,.1)}
.recpt{color:var(--purple)}.recpt.pending{color:var(--escalate)}
.chain-ok{color:var(--allow)}.foot{font-family:var(--mono);color:var(--muted);font-size:12.5px;margin-top:16px;line-height:1.6}

.stack{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-top:34px}
@media(max-width:820px){.stack{grid-template-columns:1fr 1fr}}
.scard{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:22px;transition:.3s var(--eo)}
.scard:hover{border-color:var(--line);transform:translateY(-3px)}
.scard .sh{height:30px;display:flex;align-items:center;margin-bottom:15px}
.scard .sh .logo-stripe{width:64px}.scard .sh .logo-nv,.scard .sh .logo-nemoclaw{width:30px}.scard .sh .gbadge{width:32px;height:32px;font-size:16px}.scard .sh .ava{width:32px;height:32px}
.scard h4{font-size:15px;font-weight:700;margin-bottom:5px}.scard p{font-size:12.5px;color:var(--muted);line-height:1.5}
.scard .real{font-family:var(--mono);font-size:10.5px;margin-top:11px;color:var(--muted)}

.pitch{text-align:center;background:linear-gradient(180deg,var(--bg),var(--bg2));border-top:1px solid var(--border)}
.pitch h2{max-width:880px;margin:14px auto 16px}.pitch .lead{margin:0 auto 8px}
.three{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-top:44px;text-align:left}
@media(max-width:820px){.three{grid-template-columns:1fr}}
.tcard{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:24px}
.tcard .tn{font-family:var(--serif);font-size:28px;color:var(--muted);line-height:1;margin-bottom:14px}
.tcard h4{font-size:16px;margin-bottom:7px}.tcard p{font-size:13px;color:var(--muted);line-height:1.6}
footer{padding:40px 0;border-top:1px solid var(--border);color:var(--faint);font-family:var(--mono);font-size:11.5px;text-align:center}
footer .builton{justify-content:center;margin-top:16px}

/* ---- human-in-the-loop approval (lives on the Human branch of the fork) ---- */
.approval{max-height:0;overflow:hidden;opacity:0;transition:max-height .5s var(--eo),opacity .4s var(--eo),margin .4s var(--eo);margin:0}
.approval.open{max-height:200px;opacity:1;margin:0 0 16px}
.ap-inner{display:flex;align-items:center;gap:18px;background:var(--surface);border:1px solid var(--escalate);border-radius:16px;padding:17px 22px;
  box-shadow:0 0 0 1px rgba(224,169,58,.22),0 18px 50px -26px rgba(224,169,58,.5)}
.approval.resolved .ap-inner{border-color:var(--allow);box-shadow:0 0 0 1px rgba(95,174,90,.3),0 18px 50px -26px rgba(95,174,90,.5)}
.approval.resolved.denied .ap-inner{border-color:var(--deny);box-shadow:0 0 0 1px rgba(232,85,77,.3),0 18px 50px -26px rgba(232,85,77,.5)}
.approval.resolved.denied .ap-title{color:var(--deny)}
.ap-icon{font-size:24px;flex:none}
.ap-body{flex:1;min-width:0}
.ap-title{font-family:var(--serif);font-size:18px;font-weight:400;color:var(--fg)}
.approval.resolved .ap-title{color:var(--allow)}
.ap-detail{font-family:var(--mono);font-size:12.5px;color:var(--muted);margin-top:5px;word-break:break-all;line-height:1.5}
.ap-note{font-family:var(--mono);font-size:11.5px;color:var(--faint);margin-top:7px;line-height:1.5}
.ap-actions{display:flex;gap:10px;flex:none}
.ap-actions .btn{font-size:13px;padding:11px 18px}
.btn.ap-approve{background:var(--allow);color:var(--ink);border-color:var(--allow)}
.btn.ap-approve:hover{transform:translateY(-1px);box-shadow:0 10px 24px -12px rgba(95,174,90,.6)}
.btn.ap-deny{color:var(--deny);border-color:rgba(232,85,77,.5)}.btn.ap-deny:hover{background:rgba(232,85,77,.1)}
@media(max-width:820px){.ap-inner{flex-direction:column;align-items:stretch}.ap-actions{justify-content:flex-end}}

/* ===== STAGE / camera-safe recording view (opt-in: index.html#stage or the ⛶ button) ===== */
.stagebtn{font-family:var(--mono);font-size:11px;font-weight:600;color:var(--faint);border:1px solid var(--border);background:transparent;border-radius:8px;padding:9px 13px;cursor:pointer;transition:.2s var(--eo)}
.stagebtn:hover{color:var(--fg);border-color:var(--line)}
body.stage .hero,body.stage #stack,body.stage .pitch,body.stage footer{display:none}
body.stage nav{position:static}
body.stage .demo{border-top:0;border-bottom:0;padding:22px 0 40px;background:var(--bg)}
body.stage section{padding:0}
body.stage .wrap{max-width:1340px}
body.stage .demo-head{margin-bottom:22px}
body.stage .demo-head .bridge{display:none}
body.stage .demo-head h2{font-size:clamp(2.1rem,3.8vw,3rem)}
/* stage = the circular topology (operator prefers this diagram) + camera-safe, bigger type */
body.stage .flow{min-height:452px;margin-bottom:30px}
body.stage .node{width:184px;padding:9px 11px}body.stage .node.sm{width:172px}body.stage .node.gov{width:204px}
body.stage .node .nname{font-size:16.5px}body.stage .node .nsub{font-size:11.5px}body.stage .node .ndata{font-size:11.5px}
body.stage #govStamp{font-size:31px}
body.stage .lbl-m{font-size:13px;font-weight:600}
/* Codex de-cram: stage-scoped node positions for the half-width pane (full-width circle keeps its inline positions) */
body.stage #n_hermes{left:15.8%!important}body.stage #n_gov{left:40.8%!important}body.stage #n_nemo{left:24.2%!important}
body.stage #n_stripe,body.stage #n_human,body.stage #n_deny{left:75.5%!important}
body.stage .fpath{opacity:.16}body.stage .node.out:not(.lit){opacity:.2}body.stage .arc{opacity:.28}
body.stage .card{padding:18px 20px}body.stage .bignum{font-size:38px}
body.stage table th:first-child,body.stage table td:first-child{display:none}   /* drop the # column for camera clarity */
body.stage #ledger tr:last-child td{font-size:14px;padding:13px 10px;background:rgba(139,133,240,.06)}

/* ---- $600 DENY cinematic peak ---- */
.node.out.deny.peak{opacity:1!important;border-color:var(--deny);box-shadow:0 0 0 2px var(--deny),0 0 64px -6px var(--deny);animation:peakshake .55s var(--eo)}
@keyframes peakshake{0%,100%{transform:translate(-50%,-50%)}22%{transform:translate(calc(-50% - 7px),-50%)}66%{transform:translate(calc(-50% + 6px),-50%)}}
.peakbanner{position:absolute;left:50%;top:4%;transform:translateX(-50%);z-index:6;font-family:var(--mono);font-size:13px;font-weight:600;
  color:var(--deny);background:rgba(232,85,77,.13);border:1px solid var(--deny);border-radius:30px;padding:9px 20px;white-space:nowrap;
  opacity:0;transition:opacity .4s var(--eo),top .4s var(--eo);pointer-events:none}
.peakbanner.show{opacity:1;top:1.5%}
body.stage .peakbanner{font-size:16px;padding:12px 26px}

/* ---- scoreboard end frame ---- */
.scoreboard{max-height:0;overflow:hidden;opacity:0;transition:max-height .6s var(--eo),opacity .5s var(--eo),margin .5s var(--eo);margin:0}
.scoreboard.show{max-height:420px;opacity:1;margin:18px 0 0}
.sb-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:12px}
@media(max-width:820px){.sb-grid{grid-template-columns:repeat(3,1fr)}}
.sb-tile{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:18px 14px;text-align:center}
.sb-tile b{font-family:var(--serif);display:block;font-size:31px;font-weight:400;line-height:1}
.sb-tile span{font-family:var(--mono);font-size:10.5px;color:var(--faint);letter-spacing:.07em;text-transform:uppercase;display:block;margin-top:9px;line-height:1.3}
.sb-tile.good b{color:var(--allow)}.sb-tile.block b{color:var(--deny)}.sb-tile.zero b{color:var(--allow)}
.sb-tag{text-align:center;font-family:var(--serif);font-size:clamp(1.3rem,2.5vw,1.85rem);color:var(--fg);margin-top:22px;line-height:1.3}
.sb-tag em{color:var(--muted);font-style:italic}

/* ---- authenticity spine: same request id glows across all surfaces ---- */
.xrow.spine{background:rgba(95,174,90,.09);border-radius:7px}
.xrow.spine .v{color:var(--allow)}
.xrow.spine .k{color:var(--allow)}

/* ---- persistent 'real run' badge during replay ---- */
.runbadge{display:inline-flex;align-items:center;gap:7px;font-family:var(--mono);font-size:10.5px;color:var(--allow);
  border:1px solid rgba(95,174,90,.35);background:rgba(95,174,90,.08);border-radius:20px;padding:4px 12px;opacity:0;transition:.3s var(--eo)}
.runbadge.show{opacity:1}.runbadge .rb{width:6px;height:6px;border-radius:50%;background:var(--allow);animation:pulse 2.4s var(--eo) infinite}
__LIVE_CSS__
</style></head><body>

<nav><div class="wrap nav-in">
  <div class="brandmark"><span class="gbadge bm">__SVG_CEREBRO__</span> Cortex</div>
  <div class="builton">
    <span class="lbl">built on</span>
    <span title="Hermes / Nous Research">__HERMES_ICON__</span>
    <span class="logo logo-nv" title="NVIDIA — NemoClaw + Nemotron">__SVG_NVIDIA__</span>
    <span class="logo logo-stripe" title="Stripe">__SVG_STRIPE__</span>
  </div>
</div></nav>

<header class="hero"><div class="wrap">
  <div class="kicker" id="kicker"><span class="kdot"></span><span id="kickerText">captured from a real in-sandbox run</span></div>
  <h1>Agents can earn and spend.<br>We make them <em>safe enough</em><br>for business.</h1>
  <p>A policy gate, a tamper-evident ledger, and human-in-the-loop approval &mdash; sitting between any autonomous agent and the money it can spend. The control plane for agentic spend.</p>
  <div class="hero-cta">
    <button class="btn primary" onclick="document.getElementById('demo').scrollIntoView();setTimeout(play,520)">&#9654; Watch it govern a live run</button>
    <a class="btn" href="#stack">See the stack</a>
  </div>
  <div class="statline">
    <div class="stat"><b style="color:var(--allow)" id="s_allow">0</b><span>ALLOWED &amp; CHARGED</span></div>
    <div class="stat"><b style="color:var(--escalate)" id="s_esc">0</b><span>HELD FOR A HUMAN</span></div>
    <div class="stat"><b style="color:var(--deny)" id="s_deny">0</b><span>BLOCKED AT THE GATE</span></div>
    <div class="stat"><b style="font-size:18px" id="s_gpu">RTX 4090</b><span>NEMOCLAW SANDBOX HOST</span></div>
  </div>
</div></header>

<section class="demo" id="demo"><div class="wrap">
  <div class="demo-head">
    <div><div class="eyebrow">Live governed run <span class="runbadge" id="runbadge"><span class="rb"></span>captured from a real run</span></div><h2>How a spend actually flows</h2>
      <div class="lead">Hermes proposes a spend. Cortex gates it three ways &mdash; ALLOW to Stripe, ESCALATE to a human, DENY before any money moves. Captured inside the NemoClaw sandbox.</div>
      <div class="bridge">Control-plane view of the Hermes Desktop run &mdash; <b>left:</b> the agent proposes a spend &middot; <b>right:</b> Cortex gates it, records it, reconciles it.</div></div>
    <div class="controls"><button class="btn primary" id="toggle">&#9654; Play</button><button class="stagebtn" id="stagebtn" title="Camera / recording view">&#9974; Stage</button><span class="status" id="status">ready</span></div>
  </div>

  <div class="flow" id="flow">
    <svg class="wires" viewBox="0 0 1180 540" preserveAspectRatio="none">
      <!-- cloud behind Nemotron (OpenRouter) -->
      <path class="cloud" d="M232 120 a40 40 0 0 1 12 -78 a52 52 0 0 1 96 -14 a44 44 0 0 1 50 40 a36 36 0 0 1 -10 70 z"></path>
      <text class="lbl-m" x="250" y="44" fill="#8b85f0">OpenRouter cloud</text>
      <!-- sandbox around hermes + governor -->
      <rect class="sbx" x="96" y="246" width="470" height="250" rx="18"></rect>
      <text class="lbl-m" x="116" y="234">&#9678; NemoClaw &middot; Landlock sandbox &middot; RTX 4090</text>
      <!-- reasoning orbit (continuous loop) -->
      <ellipse class="orbit" cx="300" cy="300" rx="168" ry="150"></ellipse>
      <!-- cycle arcs: hermes -> nemotron -> governor -> hermes -->
      <path id="aRe" class="arc" d="M214 360 C 150 280, 200 150, 290 132" marker-end="url(#am)"></path>
      <text class="lbl-m" x="150" y="250">reasons</text>
      <path id="aPlan" class="arc" d="M340 140 C 440 165, 470 240, 452 300" marker-end="url(#am)"></path>
      <text class="lbl-m" x="430" y="195">plans</text>
      <path id="aSpend" class="arc" d="M250 372 C 320 410, 380 400, 430 320" marker-end="url(#am)"></path>
      <text class="lbl-m" x="270" y="424">proposes spend</text>
      <!-- governor forks the spend out -->
      <path id="pAllow" class="fpath" d="M512 300 C 660 300, 720 168, 858 168" marker-end="url(#aG)"></path>
      <text class="lbl-m" x="640" y="206" fill="#5fae5a">ALLOW</text>
      <path id="pEscalate" class="fpath" d="M512 312 C 680 312, 730 312, 858 312" marker-end="url(#aE)"></path>
      <text class="lbl-m" x="648" y="303" fill="#e0a93a">ESCALATE</text>
      <path id="pDeny" class="fpath" d="M512 326 C 660 326, 720 452, 858 452" marker-end="url(#aD)"></path>
      <text class="lbl-m" x="648" y="424" fill="#e8554d">DENY</text>
      <path id="pLedger" class="fpath" d="M452 360 L 452 502" marker-end="url(#am)"></path>
      <text class="lbl-m" x="464" y="462">every decision &rarr; ledger</text>
      <defs>
        <marker id="am" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto"><path d="M0 0 L7 4 L0 8 z" fill="#5b6270"/></marker>
        <marker id="aG" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto"><path d="M0 0 L7 4 L0 8 z" fill="#5fae5a"/></marker>
        <marker id="aE" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto"><path d="M0 0 L7 4 L0 8 z" fill="#e0a93a"/></marker>
        <marker id="aD" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto"><path d="M0 0 L7 4 L0 8 z" fill="#e8554d"/></marker>
      </defs>
    </svg>

    <div class="peakbanner" id="peakbanner">&#9940; $600 to AWS &mdash; BLOCKED at the gate &middot; never reached Stripe &middot; balance unchanged</div>
    <div class="node sm out" id="n_nemo" style="left:25.4%;top:14.8%;animation-delay:.05s">
      <div class="nh"><span class="logo logo-nv">__SVG_NVIDIA__</span><span style="font-weight:700;font-size:12.5px">Nemotron 3 Ultra</span></div>
      <div class="nsub" id="nemoSub">NVIDIA brain &middot; via OpenRouter cloud</div>
    </div>
    <div class="node" id="n_hermes" style="left:17.8%;top:66.7%;animation-delay:.12s">
      <div class="nh">__HERMES_ICON__<span class="nname">Hermes agent</span></div>
      <div class="nsub">operator &middot; Nous</div><div class="ndata" id="hermesData">idle</div>
    </div>
    <div class="node gov" id="n_gov" style="left:38.3%;top:55.6%;animation-delay:.19s">
      <div class="nh"><span class="gbadge" style="width:28px;height:28px;border-radius:7px">__SVG_CEREBRO__</span><span class="nname">Cortex</span></div>
      <div id="govStamp">&mdash;</div><div class="ndata" id="govData">the policy gate</div>
    </div>
    <div class="node sm out stripe" id="n_stripe" style="left:78.5%;top:31.1%;animation-delay:.26s">
      <div class="nh"><span class="logo logo-stripe">__SVG_STRIPE__</span></div>
      <div class="nsub">charge on ALLOW</div><div class="ndata" id="stripeData">&mdash;</div>
    </div>
    <div class="node sm out human" id="n_human" style="left:78.5%;top:57.8%;animation-delay:.3s">
      <div class="nh"><span class="glyph human">&#128100;</span><span class="nname">Human</span></div>
      <div class="nsub">approve / deny</div><div class="ndata" id="humanData">&mdash;</div>
    </div>
    <div class="node sm out deny" id="n_deny" style="left:78.5%;top:83.7%;animation-delay:.34s">
      <div class="nh"><span class="glyph deny">&#10005;</span><span class="nname">Blocked</span></div>
      <div class="nsub">never reaches Stripe</div><div class="ndata" id="denyData">&mdash;</div>
    </div>
  </div>

  <!-- human-in-the-loop: Cortex holds the spend; a human resolves it on-screen -->
  <div class="approval" id="approval">
    <div class="ap-inner">
      <span class="ap-icon">&#128100;</span>
      <div class="ap-body">
        <div class="ap-title" id="apTitle">Human approval required</div>
        <div class="ap-detail" id="apDetail">&mdash;</div>
        <div class="ap-note" id="apNote">The agent cannot self-approve. The decision is appended to the tamper-evident ledger.</div>
        <div class="ap-countdown" id="apCountdown" style="display:none">
          <div class="ap-cd-bar"><span id="apCountdownBar"></span></div>
          <div class="ap-cd-num" id="apCountdownNum">&mdash;</div>
        </div>
      </div>
      <div class="ap-actions" id="apActions">
        <button class="btn ap-approve" id="apApprove">&#10003; Approve as Operator</button>
        <button class="btn ap-deny" id="apDeny">&#10005; Deny</button>
      </div>
    </div>
  </div>
__LIVE_QUEUE_DOM__
  <div class="grid2">
    <div class="card">
      <div class="label">Spend budget &middot; 1h window</div>
      <div class="bignum" id="remain">$0</div>
      <div class="budget-sub">remaining of <span id="budget">$0</span>. moves only on an approved charge</div>
      <div class="bar"><span id="barFill"></span></div>
      <div class="capline"><span>per-txn cap <b id="cap">$0</b></span><span>hard max <b id="hardmax">$0</b></span></div>
      <div class="capline" style="margin-top:10px;border-top:1px solid var(--border);padding-top:11px"><span>policy fingerprint</span><span class="mono" id="polfp">&mdash;</span></div>
    </div>
    <div class="card xcard" id="xcard">
      <div class="label">Cross-reference &middot; one request id, three surfaces <span class="xstamp" id="xstamp">&#10003; reconciled</span></div>
      <div class="xrow"><span class="k">request id</span><span class="v" id="x_req">&mdash;</span></div>
      <div class="xrow"><span class="k">policy rule</span><span class="v" id="x_rule">&mdash;</span></div>
      <div class="xrow"><span class="k">ledger hash</span><span class="v" id="x_hash">&mdash;</span></div>
      <div class="xrow"><span class="k">stripe receipt</span><span class="v" id="x_recpt">&mdash;</span></div>
      <div class="xrow"><span class="k">sandbox event</span><span class="v" id="x_sbx">&mdash;</span></div>
      <div class="xnote"><b>Why this isn&rsquo;t a mockup:</b> the same request id reconciles across the tamper-evident ledger hash and the NemoClaw sandbox event. For an ALLOW it also carries a Stripe receipt; for ESCALATE/DENY the absence of a receipt is the proof no money moved.</div>
    </div>
  </div>

  <div class="card" style="margin-top:16px">
    <div class="label">Hash-chained spend ledger &middot; <span class="chain-ok" id="chain">append-only, tamper-evident</span></div>
    <table><thead><tr><th>#</th><th>decision</th><th>merchant</th><th>amount</th><th>rule fired</th><th>ledger hash</th><th>stripe receipt</th></tr></thead>
    <tbody id="ledger"></tbody></table>
    <div class="foot" id="foot"></div>
  </div>

  <!-- scoreboard end frame -->
  <div class="scoreboard" id="scoreboard">
    <div class="sb-grid">
      <div class="sb-tile good"><b id="sb_charged">$0</b><span>charged via Stripe</span></div>
      <div class="sb-tile block"><b id="sb_blocked">$0</b><span>blocked at the gate</span></div>
      <div class="sb-tile"><b id="sb_esc">0</b><span>escalated to a human</span></div>
      <div class="sb-tile"><b id="sb_appr">0</b><span>approved to proceed</span></div>
      <div class="sb-tile good"><b id="sb_chain">&mdash;</b><span>hash chain</span></div>
      <div class="sb-tile zero"><b>0</b><span>unauthorized charges</span></div>
    </div>
    <div class="sb-tag">Autonomous agents can spend.<br><em>Cortex makes that safe enough for business.</em></div>
  </div>
</div></section>

<section id="stack"><div class="wrap">
  <div class="eyebrow">Built on the platform</div><h2>Four pillars, one governed run</h2>
  <div class="lead">Every layer is real and on camera, with provenance captured live from the sandbox.</div>
  <div class="stack">
    <div class="scard"><span class="sh">__HERMES_ICON__</span><h4>Hermes &middot; Nous Research</h4>
      <p>The autonomous operator running the revenue workflow that requests each spend.</p><div class="real">agent: hermes-operator</div></div>
    <div class="scard"><span class="logo sh logo-nv">__SVG_NVIDIA__</span><h4>NemoClaw</h4>
      <p>NVIDIA OpenShell sandbox. The run is Landlock-confined on the local 4090; payment egress reaches Stripe only through an explicit policy.</p><div class="real" id="stackGpu">sandbox host: RTX 4090</div></div>
    <div class="scard"><span class="logo sh logo-nv">__SVG_NVIDIA__</span><h4>Nemotron 3 Ultra</h4>
      <p>The agent&rsquo;s brain &mdash; an NVIDIA open model, reasoning via OpenRouter cloud inference.</p><div class="real" id="stackInf">via OpenRouter</div></div>
    <div class="scard"><span class="logo sh logo-stripe">__SVG_STRIPE__</span><h4>Stripe</h4>
      <p>The payment rail. A real test-mode charge fires only when Cortex returns ALLOW.</p><div class="real" id="stackChg">$0 charged</div></div>
  </div>
</div></section>

<section class="pitch"><div class="wrap">
  <div class="eyebrow">Why governance wins</div>
  <h2>Everyone is racing to make agents more autonomous. We make autonomy safe enough to trust with money.</h2>
  <p class="lead">An autonomous agent with a payment method is a liability the moment it is unsupervised. Cortex is the enforcement layer between any spending agent and the money.</p>
  <div class="three">
    <div class="tcard"><div class="tn">01</div><h4>Policy gate</h4><p>Per-transaction caps, hard limits, vendor and category allow or deny, rate windows, purpose binding. Every spend is gated before a cent moves.</p></div>
    <div class="tcard"><div class="tn">02</div><h4>Tamper-evident audit</h4><p>A hash-chained, append-only ledger. Every decision cross-references its Stripe receipt and sandbox event by one request id. Audit-grade by construction.</p></div>
    <div class="tcard"><div class="tn">03</div><h4>Human in the loop</h4><p>Unusual spend escalates and holds. The agent cannot self-approve. A human decides, then the agent resumes. Safe enough for business.</p></div>
  </div>
</div></section>

<footer><div class="wrap">
  Cortex &middot; agentic spend governance by Cerebro &middot; a reusable open-source governor plus hosted compliance product.
  <div class="builton" style="justify-content:center">
    <span class="lbl">built on</span>__HERMES_ICON__
    <span class="logo logo-nv" style="height:14px;width:21px">__SVG_NVIDIA__</span>
    <span class="logo logo-stripe" style="height:14px;width:40px">__SVG_STRIPE__</span>
  </div>
</div></footer>

<script>
const BEATS=__BEATS__,META=__META__,CFG=__LIVE_CFG__,$=id=>document.getElementById(id);
const money=n=>"$"+Number(n).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const short=h=>h?h.slice(0,12)+"…":"—";
$("nemoSub").textContent="NVIDIA brain · via "+(META.inference||"OpenRouter").split(" (")[0];
$("s_gpu").textContent=(META.sandbox_gpu||"RTX 4090").replace("NVIDIA GeForce ","");
$("stackGpu").textContent="sandbox host: "+(META.sandbox_gpu||"RTX 4090").replace("NVIDIA GeForce ","");
$("stackInf").textContent="via "+(META.inference||"OpenRouter").split(" (")[0];
$("budget").textContent=money(META.budget);$("cap").textContent=money(META.per_txn_cap);$("hardmax").textContent=money(META.hard_max);
$("polfp").textContent=META.policy_sha256?"sha256:"+META.policy_sha256.slice(0,12)+"…":"—";
if(!META.real){$("kickerText").textContent="capture pending live key";$("kicker").classList.add("pending");}

const ARCS=["aRe","aPlan","aSpend"],FORK=["pAllow","pEscalate","pDeny","pLedger"];
const NODES=["n_nemo","n_hermes","n_gov","n_stripe","n_human","n_deny"];
const BRANCH={ALLOW:{wire:"pAllow",node:"n_stripe",cls:"allow"},ESCALATE:{wire:"pEscalate",node:"n_human",cls:"escalate"},DENY:{wire:"pDeny",node:"n_deny",cls:"deny"}};
let remaining=META.budget,i=0,timer=null,nA=0,nE=0,nD=0,paused=false,started=false,runToken=0,holding=false;
function setB(){$("remain").textContent=money(remaining);$("barFill").style.width=(100*remaining/META.budget)+"%";}
function setWire(id,on,cls){const e=$(id);e.classList.toggle("on",on);["allow","escalate","deny","ledger"].forEach(c=>e.classList.remove(c));if(on&&cls)e.classList.add(cls);}
function clearFlow(){ARCS.forEach(a=>$(a).classList.remove("on"));FORK.forEach(f=>setWire(f,false));
  NODES.forEach(n=>$(n).classList.remove("lit"));
  $("n_gov").className="node gov";$("n_nemo").className="node sm out";$("n_stripe").className="node sm out stripe";
  $("n_human").className="node sm out human";$("n_deny").className="node sm out deny";
  // wipe stale branch data so a prior receipt cannot bleed into the next verdict
  ["stripeData","humanData","denyData"].forEach(k=>$(k).textContent="—");
  $("x_recpt").textContent="—";$("x_recpt").className="v";$("peakbanner").classList.remove("show");}
function reset(){
  clearTimeout(timer);runToken++;i=0;remaining=META.budget;nA=nE=nD=0;setB();clearFlow();
  $("ledger").innerHTML="";$("foot").textContent="";
  $("hermesData").textContent="idle";$("govStamp").className="";$("govStamp").textContent="—";$("govData").textContent="the policy gate";
  ["stripeData","humanData","denyData"].forEach(k=>$(k).textContent="—");
  ["x_req","x_rule","x_hash","x_recpt","x_sbx"].forEach(k=>{$(k).textContent="—";$(k).className="v";});
  ["s_allow","s_esc","s_deny"].forEach(k=>$(k).textContent="0");$("stackChg").textContent="$0 charged";
  $("xcard").classList.remove("reconciled","pulse");$("xstamp").classList.remove("show");
  $("chain").textContent="append-only, tamper-evident";$("chain").classList.remove("chain-bad");
  $("approval").classList.remove("open","resolved","denied");$("apActions").style.display="";holding=false;
  $("scoreboard").classList.remove("show");$("runbadge").classList.remove("show");$("peakbanner").classList.remove("show");
  if(CFG.asyncQueue)qReset();
  $("status").textContent="ready";started=false;paused=false;$("toggle").innerHTML="&#9654; Play";
}
function cell(text,cls){const td=document.createElement("td");if(cls)td.className=cls;td.textContent=text;return td;}
function rowEl(b,n){
  // textContent for every captured field — no innerHTML interpolation of run data
  const tr=document.createElement("tr"),vtd=document.createElement("td"),rtd=document.createElement("td");
  const pill=document.createElement("span");pill.className="pill "+b.verdict.toLowerCase();pill.textContent=b.verdict;vtd.appendChild(pill);
  if(b.stripe_receipt){const s=document.createElement("span");s.className="recpt";s.textContent=b.stripe_receipt;rtd.appendChild(s);}
  else if(b.verdict==="ALLOW"){const s=document.createElement("span");s.className="recpt pending";s.textContent="pending";rtd.appendChild(s);}
  else{rtd.textContent="—";}
  tr.append(cell(n),vtd,cell(b.merchant),cell(money(b.amount)),cell(b.rule_fired||""),cell(short(b.entry_hash)),rtd);
  $("ledger").appendChild(tr);requestAnimationFrame(()=>tr.classList.add("in"));
}
const wait=ms=>new Promise(r=>setTimeout(r,ms));
async function beat(){
  if(i>=BEATS.length)return done();
  const myToken=++runToken;   // a pause/reset/replay bumps runToken and cancels this beat
  const b=BEATS[i],br=BRANCH[b.verdict];
  const isPeak=b.verdict==="DENY"&&b.rule_fired==="amount.hard_max";   // the $600 hard-ceiling block = the cinematic peak
  clearFlow();
  $("x_req").textContent=b.request_id;$("x_req").className="v match";
  $("x_rule").textContent=b.rule_fired||"—";$("x_hash").textContent=short(b.entry_hash);
  $("x_sbx").textContent=b.sandbox_ref;$("x_sbx").className="v match";
  // reasoning loop: agent <-> brain
  $("n_hermes").classList.add("lit");$("n_nemo").classList.add("lit");$("aRe").classList.add("on");$("aPlan").classList.add("on");
  $("hermesData").textContent=b.task+" → "+b.merchant+" "+money(b.amount);
  await wait(460);if(myToken!==runToken)return;
  // proposes spend to the gate
  $("aRe").classList.remove("on");$("aPlan").classList.remove("on");$("aSpend").classList.add("on");$("n_gov").classList.add("lit");
  await wait(420);if(myToken!==runToken)return;
  // governor decides + records
  $("govStamp").className=b.verdict.toLowerCase();$("govStamp").textContent=b.verdict;$("govData").textContent="rule: "+(b.rule_fired||"");
  $("n_gov").classList.add("lit-"+b.verdict.toLowerCase());setWire("pLedger",true,"ledger");
  await wait(360);if(myToken!==runToken)return;
  // the branch the verdict took
  setWire(br.wire,true,br.cls);$(br.node).classList.add("lit");
  if(b.verdict==="ALLOW"){
    $("stripeData").textContent=b.stripe_receipt||"charged";
    $("x_recpt").textContent=b.stripe_receipt||"charged";$("x_recpt").className="v stripe";
    if(b.stripe_receipt){remaining-=b.amount;setB();$("stackChg").textContent=money(META.budget-remaining)+" charged";
      // signature moment: one request id reconciled across ledger hash + Stripe receipt + sandbox event
      $("xcard").classList.add("reconciled");$("xstamp").classList.add("show");
      $("xcard").classList.remove("pulse");void $("xcard").offsetWidth;$("xcard").classList.add("pulse");spine();}
    nA++;$("s_allow").textContent=nA;
  }else if(b.verdict==="ESCALATE"){
    $("humanData").textContent="held · "+b.merchant+" "+money(b.amount);
    $("x_recpt").textContent="held (balance unchanged)";$("x_recpt").className="v pending";
    nE++;$("s_esc").textContent=nE;
  }else{
    $("denyData").textContent=b.rule_fired;
    $("x_recpt").textContent="— never reached Stripe";$("x_recpt").className="v mute";
    nD++;$("s_deny").textContent=nD;
    if(isPeak){$("n_deny").classList.add("peak");$("peakbanner").classList.add("show");
      $("status").textContent="⛔ "+money(b.amount)+" to "+b.merchant+" — BLOCKED at the gate · never reached Stripe · balance unchanged";}
  }
  rowEl(b,i+1);
  if(!isPeak)$("status").textContent="beat "+(i+1)+" / "+BEATS.length+" — "+b.verdict;
  // LIVE async-queue: every ESCALATE gets a bounded grace window -> resolve | queue (never halts).
  if(CFG.asyncQueue&&b.verdict==="ESCALATE"){return openEscalation(b);}
  if(b.resolution){return holdForHuman(b);}   // locked path: Cortex holds, a human resolves on-screen
  i++;if(paused)return;timer=setTimeout(beat,isPeak?3000:1500);   // dwell on the $600 peak
}
// ---- human-in-the-loop: Cortex held a spend; surface the human decision ----
function holdForHuman(b){
  holding=true;
  const a=$("approval");a.classList.remove("resolved","denied");$("apActions").style.display="";
  $("apTitle").textContent="Human approval required";
  $("apNote").textContent="The agent cannot self-approve. This replay writes only the captured ledger decision, appended to the tamper-evident ledger.";
  $("apDetail").textContent=b.agent+" requests "+money(b.amount)+" at "+b.merchant+" for "+b.task
    +"  ·  rule "+b.rule_fired+"  ·  req "+b.request_id+"  ·  held "+short(b.entry_hash);
  $("humanData").textContent="decision needed · "+b.merchant;
  a.classList.add("open");
  // The run HOLDS here and does NOT advance until the operator clicks a decision.
  // Never auto-approve: the human is the point — a self-approving timer undercuts both
  // the demo and the thesis. Every other beat auto-flows; this one waits for the human.
  $("status").textContent="held — waiting for your decision · click Approve to continue";
  $("apApprove").onclick=()=>decide(b,true);
  $("apDeny").onclick=()=>decide(b,false);
}
function decide(b,approved){
  if(BEATS[i]!==b)return;                 // a reset moved us off this beat — ignore stale fire
  clearTimeout(timer);
  const a=$("approval"),r=b.resolution;
  // only the CAPTURED decision writes real ledger data; a contradicting live click
  // is shown as a what-if, never faked into the chain
  if(approved!==(r.verdict==="ALLOW")){
    $("apNote").textContent="This captured run resolved as "+(r.verdict==="ALLOW"?"approved":"denied")
      +" by "+r.approver+". A contradicting decision is not written — the ledger only replays real captured entries.";
    return;
  }
  $("apActions").style.display="none";a.classList.add("resolved");holding=false;
  if(approved){
    $("apTitle").textContent="Approved by "+r.approver;
    $("n_human").classList.add("lit");$("humanData").textContent="✓ approved by "+r.approver;
    // HONEST: approval CLEARS the spend to proceed — it is NOT a Stripe charge. The
    // captured run has no post-approval settlement, so we never move the balance or
    // claim a receipt. The proof is the chained resolution entry, not money movement.
    $("x_recpt").textContent="approved to proceed — not charged in this run";$("x_recpt").className="v pending";
    $("apDetail").textContent="✓ "+r.approver+" cleared "+money(b.amount)+" at "+b.merchant
      +" to proceed — chained to the ledger, no charge captured in this run"
      +"  ·  held "+short(b.entry_hash)+"  ·  resolution "+short(r.entry_hash);
  }else{
    $("apTitle").textContent="Denied by "+r.approver;a.classList.add("denied");
    $("humanData").textContent="✕ denied by "+r.approver;
    $("x_recpt").textContent="denied by human — no charge";$("x_recpt").className="v mute";
    $("apDetail").textContent="✕ "+r.approver+" denied "+money(b.amount)+" at "+b.merchant
      +" — permanently refused, chained to the ledger"
      +"  ·  held "+short(b.entry_hash)+"  ·  resolution "+short(r.entry_hash);
  }
  $("status").textContent=(approved?"✓ approved by ":"✕ denied by ")+r.approver+" — decision chained to the ledger";
  resolutionRow(b,r,approved);
  i++;if(paused)return;timer=setTimeout(()=>{$("approval").classList.remove("open");beat();},1900);
}
function resolutionRow(b,r,approved){
  const tr=document.createElement("tr"),vtd=document.createElement("td"),rtd=document.createElement("td"),v=approved?"ALLOW":"DENY";
  const pill=document.createElement("span");pill.className="pill "+v.toLowerCase();pill.textContent=v;vtd.appendChild(pill);
  // approval clears the spend to proceed — NOT a charge. No receipt styling, no implied money movement.
  rtd.textContent=approved?"approved · not charged":"—";
  tr.append(cell("↳"),vtd,cell(b.merchant),cell(money(b.amount)),cell(r.rule_fired+" ("+r.approver+")"),cell(short(r.entry_hash)),rtd);
  $("ledger").appendChild(tr);requestAnimationFrame(()=>tr.classList.add("in"));
}
function spine(){
  // the SAME request id glows in sequence across all three surfaces — the authenticity spine
  const ids=["x_req","x_hash","x_recpt","x_sbx"];
  ids.forEach((id,k)=>setTimeout(()=>{const r=$(id).closest(".xrow");if(r)r.classList.add("spine");},120+k*170));
  setTimeout(()=>{ids.forEach(id=>{const r=$(id).closest(".xrow");if(r)r.classList.remove("spine");});},120+ids.length*170+1200);
}
function done(){
  clearFlow();
  // only claim verification the site generator actually proved (hash chain + settlement links)
  const v=META.chain_verified;
  $("status").textContent="run complete"+(v?" — chain verified":" — chain UNVERIFIED");
  $("chain").textContent=v?"append-only, tamper-evident, chain verified":"append-only ledger — chain verification FAILED";
  $("chain").classList.toggle("chain-bad",!v);
  const appr=BEATS.filter(b=>b.resolution&&b.resolution.verdict==="ALLOW").length;
  $("foot").innerHTML=money(META.charged_total)+" policy-allowed &amp; charged via Stripe &middot; "+META.n_esc+" escalated to a human ("+appr+" human-approved to proceed, "+(META.n_esc-appr)+" still held) &middot; "+META.n_deny+" blocked before any money moved.";
  // scoreboard end frame
  const blocked=BEATS.filter(b=>b.verdict==="DENY").reduce((s,b)=>s+b.amount,0);
  $("sb_charged").textContent=money(META.charged_total);$("sb_blocked").textContent=money(blocked);
  $("sb_esc").textContent=META.n_esc;$("sb_appr").textContent=appr;
  $("sb_chain").textContent=v?"✓":"✗";$("sb_chain").style.color=v?"var(--allow)":"var(--deny)";
  $("scoreboard").classList.add("show");
  started=true;paused=false;$("toggle").innerHTML="&#10227; Replay";
}
function play(){reset();started=true;paused=false;$("runbadge").classList.add("show");$("toggle").innerHTML="&#9208; Pause";$("status").textContent="replaying…";timer=setTimeout(beat,360);}
function pause(){paused=true;clearTimeout(timer);runToken++;$("toggle").innerHTML="&#9654; Resume";$("status").textContent="paused — narrate, then resume";}
function resume(){paused=false;$("toggle").innerHTML="&#9208; Pause";$("status").textContent="replaying…";timer=setTimeout(beat,220);}
function onToggle(){if(holding)return;   // while waiting on the human, Play/Pause is inert — only Approve/Deny advances
  if(!started||i>=BEATS.length){play();return;}if(paused){resume();return;}pause();}
// LIVE async-queue model injected here — BEFORE the load-time reset() below, so its
// state (QUEUE/escState) initializes before reset() -> qReset() can touch it. Empty in --locked.
__LIVE_JS__
$("toggle").onclick=onToggle;reset();
// stage / camera-safe recording view — demo-first, enlarged, marketing chrome hidden
function setStage(on){document.body.classList.toggle("stage",on);$("stagebtn").innerHTML=on?"&#9974; Exit stage":"&#9974; Stage";if(on)$("demo").scrollIntoView({behavior:"smooth"});}
$("stagebtn").onclick=()=>setStage(!document.body.classList.contains("stage"));
if(location.hash==="#stage")setStage(true);
// Default PAUSED — no autoplay. For the side-by-side recording the operator presses Play
// (or the hero button) exactly when Hermes Desktop proposes the first spend, so the two
// panes stay in sync. reset() left the toggle on "▶ Play" and status "ready".
</script>
</body></html>"""


# ===== LIVE async-queue injections (empty in --locked builds) =====

LIVE_CSS = r""".ap-countdown{margin-top:9px}
.ap-cd-bar{height:4px;background:var(--surface2);border-radius:4px;overflow:hidden}
.ap-cd-bar>span{display:block;height:100%;width:100%;background:var(--escalate);transition:width .1s linear}
.ap-cd-num{font-family:var(--mono);font-size:11px;color:var(--escalate);margin-top:5px}
.queuewrap{max-height:0;overflow:hidden;opacity:0;transition:max-height .5s var(--eo),opacity .4s var(--eo),margin .4s var(--eo);margin:0}
.queuewrap.show{max-height:640px;opacity:1;margin:0 0 16px}
.q-head{display:flex;align-items:center;gap:10px;margin-bottom:11px}
.q-dot{width:8px;height:8px;border-radius:50%;background:var(--escalate);animation:pulse 2.4s var(--eo) infinite;flex:none}
.q-title{font-family:var(--serif);font-size:18px;font-weight:400}
.q-count{font-family:var(--mono);font-size:12px;color:var(--muted);margin-left:auto}.q-count b{color:var(--escalate);font-size:15px}
.q-sub{color:var(--faint)}
.q-list{display:flex;flex-direction:column;gap:8px}
.q-item{display:flex;align-items:center;gap:14px;background:var(--surface);border:1px solid var(--border);border-radius:11px;padding:11px 16px;opacity:0;transform:translateX(-8px);transition:.4s var(--eo)}
.q-item.in{opacity:1;transform:none}
.q-m{font-family:var(--mono);font-size:13.5px;font-weight:600;color:var(--fg);min-width:150px}
.q-meta{font-family:var(--mono);font-size:11.5px;color:var(--muted);flex:1;word-break:break-all}
.q-tag{font-family:var(--mono);font-size:10.5px;font-weight:600;color:var(--escalate);border:1px solid rgba(224,169,58,.34);background:rgba(224,169,58,.1);border-radius:20px;padding:3px 11px;white-space:nowrap;flex:none}
body.stage .q-title{font-size:20px}body.stage .q-m{font-size:15px}body.stage .q-meta{font-size:12.5px}
@media(max-width:820px){.q-item{flex-wrap:wrap;gap:7px}.q-m{min-width:0}}"""

LIVE_QUEUE_DOM = r"""  <!-- LIVE: escalations not approved inside the grace window auto-queue here (read-only) -->
  <div class="queuewrap" id="queuePanel">
    <div class="q-head">
      <span class="q-dot"></span>
      <span class="q-title">Pending approval queue</span>
      <span class="q-count"><b id="queueCount">0</b> awaiting a human &middot; <span class="q-sub">alert sent, no money moved</span></span>
    </div>
    <div class="q-list" id="queueList"></div>
  </div>"""

LIVE_JS = r"""
/* ===== LIVE async-queue model — state machine per escalation: open -> resolved | queued.
   First-event-wins: a click cancels the timeout (resolve); the timeout disables the button
   (queue). A late click after timeout is a no-op. Pure replay — only a click that MATCHES the
   captured resolution writes a row; pending / contradicting clicks are what-ifs, never written. */
const QUEUE=[],NOTIFIED=new Set();let escState="idle",escTimer=null,escTick=null,nWritten=0,nWhatIf=0,RUN_ID="";
function qReset(){QUEUE.length=0;NOTIFIED.clear();escState="idle";nWritten=0;nWhatIf=0;RUN_ID=Date.now().toString(36)+"-"+Math.floor(Math.random()*1e5);clearTimeout(escTimer);clearInterval(escTick);
  $("queuePanel").classList.remove("show");$("queueList").innerHTML="";$("queueCount").textContent="0";
  $("apApprove").disabled=false;$("apDeny").disabled=false;$("apCountdown").style.display="none";}
function escEnd(){clearTimeout(escTimer);clearInterval(escTick);$("apCountdown").style.display="none";}
function escAdvance(dwell){holding=false;i++;if(paused)return;const tok=runToken;timer=setTimeout(()=>{if(tok!==runToken)return;$("approval").classList.remove("open");beat();},dwell);}
function openEscalation(b){
  escState="open";holding=true;   // bounded grace window; Play/Pause inert until it resolves or queues
  const a=$("approval");a.classList.remove("resolved","denied");$("apActions").style.display="";
  $("apApprove").disabled=false;$("apDeny").disabled=false;
  $("apTitle").textContent="Human approval required";
  $("apDetail").textContent=b.agent+" requests "+money(b.amount)+" at "+b.merchant+" for "+b.task
    +"  ·  rule "+b.rule_fired+"  ·  req "+b.request_id+"  ·  held "+short(b.entry_hash);
  $("apNote").textContent="The agent cannot self-approve. Approve in the window to resolve on-screen — otherwise it auto-queues for approval and alerts a human.";
  $("humanData").textContent="decision needed · "+b.merchant;
  a.classList.add("open");
  const total=CFG.graceMs;let left=total;const bar=$("apCountdownBar");
  $("apCountdown").style.display="";bar.style.width="100%";$("apCountdownNum").textContent=(left/1000).toFixed(1)+"s to auto-queue";
  $("status").textContent="held — approve now or it queues ("+(left/1000).toFixed(1)+"s)";
  clearInterval(escTick);escTick=setInterval(()=>{left-=100;if(left<0)left=0;
    bar.style.width=(100*left/total)+"%";$("apCountdownNum").textContent=(left/1000).toFixed(1)+"s to auto-queue";},100);
  $("apApprove").onclick=()=>escResolve(b,true);
  $("apDeny").onclick=()=>escResolve(b,false);
  clearTimeout(escTimer);escTimer=setTimeout(()=>escTimeout(b),total);
}
function escResolve(b,approved){
  if(escState!=="open")return;            // first-event-wins: a late click after the timeout is a no-op
  escState="resolved";escEnd();$("apApprove").disabled=true;$("apDeny").disabled=true;
  const a=$("approval"),r=b.resolution;
  // captured-disposition rule: only a click MATCHING the captured resolution writes a row.
  // pending (no captured resolution) OR a contradicting click -> what-if only, nothing written.
  if(!r||approved!==(r.verdict==="ALLOW")){
    const why=!r?"is still pending in the captured run — no human decision was recorded"
              :"resolved as "+(r.verdict==="ALLOW"?"approved":"denied")+" by "+r.approver+" in the captured run";
    $("apNote").textContent="This spend "+why+". A contradicting on-screen decision is not written — the ledger only replays real captured entries.";
    $("status").textContent=(!r?"pending":"what-if")+" — nothing written to the ledger";
    $("humanData").textContent=(!r?"pending · ":"what-if · ")+b.merchant;
    nWhatIf++;escAdvance(1700);return;
  }
  a.classList.add("resolved");$("apActions").style.display="none";nWritten++;
  if(approved){
    $("apTitle").textContent="Approved by "+r.approver;$("n_human").classList.add("lit");
    $("humanData").textContent="✓ approved by "+r.approver;
    $("x_recpt").textContent="approved to proceed — not charged in this run";$("x_recpt").className="v pending";
    $("apDetail").textContent="✓ "+r.approver+" cleared "+money(b.amount)+" at "+b.merchant
      +" to proceed — chained to the ledger, no charge captured in this run  ·  held "+short(b.entry_hash)+"  ·  resolution "+short(r.entry_hash);
  }else{
    $("apTitle").textContent="Denied by "+r.approver;a.classList.add("denied");
    $("humanData").textContent="✕ denied by "+r.approver;
    $("x_recpt").textContent="denied by human — no charge";$("x_recpt").className="v mute";
    $("apDetail").textContent="✕ "+r.approver+" denied "+money(b.amount)+" at "+b.merchant
      +" — permanently refused, chained to the ledger  ·  held "+short(b.entry_hash)+"  ·  resolution "+short(r.entry_hash);
  }
  $("status").textContent=(approved?"✓ approved by ":"✕ denied by ")+r.approver+" — decision chained to the ledger";
  resolutionRow(b,r,approved);
  escAdvance(1900);
}
function escTimeout(b){
  if(escState!=="open")return;            // first-event-wins: a click already resolved this beat
  escState="queued";escEnd();$("apApprove").disabled=true;$("apDeny").disabled=true;
  QUEUE.push(b);renderQueue();
  $("humanData").textContent="queued · pending approval";
  $("apNote").textContent="No decision in the window — queued for approval and an alert was sent. The pipeline keeps streaming; the balance does not move.";
  $("x_recpt").textContent="queued — pending approval (balance unchanged)";$("x_recpt").className="v pending";
  $("status").textContent="queued for approval ("+QUEUE.length+") · alert sent — pipeline streaming";
  if(CFG.notify&&!NOTIFIED.has(b.request_id)){NOTIFIED.add(b.request_id);
    // fire-and-forget Telegram alert, once per request_id — never blocks or breaks playback
    try{fetch('/notify',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({request_id:RUN_ID+":"+b.request_id,text:'Cortex — pending approval\n'+b.agent+' requests '+money(b.amount)+' at '+b.merchant+' for '+b.task+'\nrule: '+b.rule_fired+'\nreq '+b.request_id})}).catch(function(){});}catch(e){}
  }
  escAdvance(1300);
}
function renderQueue(){
  $("queuePanel").classList.add("show");$("queueCount").textContent=QUEUE.length;
  const b=QUEUE[QUEUE.length-1];
  // textContent only — never innerHTML-interpolate run data
  const row=document.createElement("div");row.className="q-item";
  const m=document.createElement("span");m.className="q-m";m.textContent=b.merchant+"  "+money(b.amount);
  const meta=document.createElement("span");meta.className="q-meta";meta.textContent=(b.rule_fired||"")+" · req "+b.request_id;
  const tag=document.createElement("span");tag.className="q-tag";tag.textContent="pending approval · alert sent";
  row.append(m,meta,tag);$("queueList").appendChild(row);requestAnimationFrame(()=>row.classList.add("in"));
}
// done(): in async mode, restate the foot from ON-SCREEN reality (resolved vs queued),
// not the captured 'approved/held' framing — resolved + queued always == n_esc.
const _origDone=done;
done=function(){_origDone();
  if(CFG.asyncQueue){const q=QUEUE.length;
    $("foot").innerHTML=money(META.charged_total)+" policy-allowed &amp; charged via Stripe &middot; "
      +META.n_esc+" escalated to a human ("+nWritten+" human-resolved on-screen"+(nWhatIf?", "+nWhatIf+" reviewed (no decision recorded)":"")+", "
      +"<b style='color:var(--escalate)'>"+q+" queued for approval</b> — alert sent, no money moved) &middot; "
      +META.n_deny+" blocked before any money moved.";
  }
};"""


if __name__ == "__main__":
    main()

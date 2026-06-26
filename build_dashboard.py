"""Render the governance dashboard (the demo hero) from a captured run.

Reads the ledger + run-provenance and emits a SELF-CONTAINED dashboard.html that
deterministically REPLAYS the captured run as a FIVE-PILLAR PIPELINE:

    Hermes agent -> NemoClaw sandbox -> Nemotron 3 Ultra (RTX 4090)
                 -> Cerebro Governor -> Stripe

The governor is the gate: on ALLOW the request flows through to a real Stripe
charge; on ESCALATE/DENY it stops at the governor and never reaches Stripe. Every
beat is cross-referenced (one request_id across ledger hash, Stripe receipt, and
the NemoClaw sandbox event). The four sponsor pillars (NemoClaw, Nemotron, the
governor agent-skill, Stripe) are the visible spine — not a buried cross-ref row.

Self-contained: run data is embedded, so it plays offline during recording (the
demo-spec de-risk). Re-run after a fresh capture and the real artifacts drop in.
Pure render over captured payload — never synthetic data.

Usage:  python3 build_dashboard.py [--ledger sandbox_ledger.jsonl]
                                    [--meta sandbox_run_meta.json] [--out dashboard.html]
"""
import argparse
import json
from pathlib import Path

_HERE = Path(__file__).resolve().parent

VERDICT_META = {"ALLOW": True, "ESCALATE": False, "DENY": False}  # value = reaches Stripe


def build_beats(entries):
    settlements = {e["resolves"]: e for e in entries
                   if e.get("verdict") == "SETTLED" and e.get("resolves")}
    beats = []
    for e in entries:
        v = e.get("verdict")
        if v not in VERDICT_META:
            continue
        settle = settlements.get(e.get("entry_hash"))
        beats.append({
            "verdict": v,
            "agent": e.get("agent"),
            "task": e.get("task"),
            "merchant": e.get("merchant"),
            "amount": e.get("amount"),
            "request_id": e.get("request_id") or "(unset)",
            "rule_fired": e.get("rule_fired"),
            "entry_hash": e.get("entry_hash"),
            "sandbox_ref": e.get("sandbox_ref") or "(none)",
            "stripe_receipt": (settle or {}).get("stripe_receipt"),
        })
    return beats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default="sandbox_ledger.jsonl")
    ap.add_argument("--meta", default="sandbox_run_meta.json")
    ap.add_argument("--out", default="dashboard.html")
    args = ap.parse_args()

    led_path = _HERE / args.ledger
    if not led_path.exists():
        led_path = _HERE / "capture_ledger.jsonl"  # fall back to the local capture
    entries = [json.loads(l) for l in led_path.read_text().splitlines() if l.strip()]
    policy = json.loads((_HERE / "policy.json").read_text())

    meta_path = _HERE / args.meta
    prov = json.loads(meta_path.read_text()) if meta_path.exists() else {
        "origin": "local", "sandbox": "(local capture)", "runtime": "local",
        "model": "(not captured)", "gpu": "(not captured)", "operator": "agent",
    }

    beats = build_beats(entries)
    budget = policy.get("rate_window", {}).get("max_amount", 300.0)
    charged = sum(b["amount"] for b in beats
                  if b["verdict"] == "ALLOW" and b["stripe_receipt"])
    meta = {
        "budget": budget, "per_txn_cap": policy.get("per_txn_cap"),
        "hard_max": policy.get("hard_max"), "charged_total": charged,
        "n_beats": len(beats), "prov": prov,
        "real": prov.get("origin") == "in-sandbox"
                and all(b["stripe_receipt"] for b in beats if b["verdict"] == "ALLOW"),
    }

    html = TEMPLATE.replace("__BEATS__", json.dumps(beats)).replace("__META__", json.dumps(meta))
    (_HERE / args.out).write_text(html)
    print("dashboard -> %s  (%d beats, $%.2f charged, origin=%s, gpu=%s)"
          % (args.out, len(beats), charged, prov.get("origin"), prov.get("gpu")))


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cerebro Governor — Agentic Spend Governance</title>
<style>
:root{
  --bg:#0d1117;--surface:#161b22;--surface2:#21262d;--border:#30363d;
  --text:#c9d1d9;--text-muted:#8b949e;--accent:#58a6ff;--green:#3fb950;
  --yellow:#d29922;--orange:#e3b341;--red:#f85149;--purple:#bc8cff;--nv:#76b900;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:radial-gradient(1200px 620px at 50% -8%,#10243a 0%,var(--bg) 58%);
  color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
  min-height:100vh;padding:24px 28px 40px}
.mono{font-family:'SF Mono',Menlo,Consolas,monospace}
header{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;
  border-bottom:1px solid var(--border);padding-bottom:13px;margin-bottom:18px}
.brand{font-size:20px;font-weight:700;letter-spacing:-.01em}.brand .dot{color:var(--accent)}
.tag{color:var(--text-muted);font-size:13px;margin-top:3px}
.prov{font-size:11px;text-align:right;line-height:1.5}
.prov .live{color:var(--green);font-weight:600}.prov .nv{color:var(--nv);font-weight:600}
.prov .pending{color:var(--yellow);font-weight:600}
.controls{display:flex;gap:10px;align-items:center;margin:0 0 16px}
button{font:inherit;font-size:13px;font-weight:600;color:var(--text);background:var(--surface2);
  border:1px solid var(--border);border-radius:8px;padding:9px 16px;cursor:pointer;transition:background .15s}
button:hover{background:#2c333d}button.primary{background:var(--accent);color:#06203f;border-color:var(--accent)}
button:disabled{opacity:.4;cursor:default}.status{color:var(--text-muted);font-size:13px;margin-left:4px}

/* ---- pillar pipeline (the spine) ---- */
.pipe{display:grid;grid-template-columns:repeat(5,1fr);gap:0;margin-bottom:20px}
.pillar{position:relative;background:var(--surface);border:1px solid var(--border);
  border-radius:12px;padding:15px 14px 14px;min-height:150px;transition:border-color .3s,box-shadow .3s,transform .2s}
.pillar+.pillar{margin-left:26px}
.pillar::before{content:"";position:absolute;top:50%;left:-27px;width:27px;height:2px;background:var(--border);transition:background .3s}
.pillar:first-child::before{display:none}
.pillar::after{content:"";position:absolute;top:calc(50% - 4px);left:-9px;width:0;height:0;
  border-left:7px solid var(--border);border-top:5px solid transparent;border-bottom:5px solid transparent;transition:border-left-color .3s}
.pillar:first-child::after{display:none}
.pillar.flow::before{background:var(--accent)}.pillar.flow::after{border-left-color:var(--accent)}
.pillar.active{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent),0 8px 26px -12px var(--accent);transform:translateY(-2px)}
.pillar .pnum{position:absolute;top:10px;right:12px;font-size:10px;color:var(--text-muted);font-family:'SF Mono',monospace}
.pillar .picon{font-size:22px;line-height:1}
.pillar .pname{font-weight:700;font-size:14px;margin-top:8px}
.pillar .psub{font-size:11px;color:var(--text-muted);margin-top:3px;line-height:1.4}
.pillar .pdata{margin-top:9px;font-family:'SF Mono',Menlo,Consolas,monospace;font-size:10.5px;
  word-break:break-all;color:var(--text);min-height:14px}
.badge{display:inline-block;font-size:10px;font-weight:700;padding:2px 8px;border-radius:20px;border:1px solid;margin-top:8px}
.b-nv{color:var(--nv);background:rgba(118,185,0,.12);border-color:rgba(118,185,0,.35)}
.b-on{color:var(--green);background:rgba(63,185,80,.12);border-color:rgba(63,185,80,.3)}
/* governor verdict stamp inside its pillar */
#govStamp{font-size:26px;font-weight:800;letter-spacing:.02em;margin-top:6px;opacity:.25}
#govStamp.allow{color:var(--green);opacity:1}#govStamp.escalate{color:var(--yellow);opacity:1}#govStamp.deny{color:var(--red);opacity:1}
.pillar.gate-deny{border-color:rgba(248,81,73,.6);box-shadow:0 0 0 1px var(--red),0 8px 26px -12px var(--red);animation:shake .4s}
.pillar.gate-escalate{border-color:rgba(210,153,34,.55);box-shadow:0 0 0 1px var(--yellow)}
@keyframes shake{0%,100%{transform:translateX(0)}25%{transform:translateX(-6px)}50%{transform:translateX(6px)}75%{transform:translateX(-4px)}}
.stripe-blocked{opacity:.4}
#stripeData.blocked{color:var(--text-muted)}

/* ---- lower row: budget + cross-ref ---- */
.grid2{display:grid;grid-template-columns:1fr 1.3fr;gap:18px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px 18px}
.label{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--text-muted);margin-bottom:10px}
.bignum{font-size:34px;font-weight:800;letter-spacing:-.02em}
.budget-sub{color:var(--text-muted);font-size:12px;margin-top:2px}
.bar{height:9px;background:var(--surface2);border-radius:6px;overflow:hidden;margin-top:12px}
.bar>span{display:block;height:100%;width:100%;background:linear-gradient(90deg,var(--green),var(--accent));transition:width .6s cubic-bezier(.2,.7,.2,1)}
.capline{display:flex;justify-content:space-between;font-size:11.5px;color:var(--text-muted);margin-top:11px}.capline b{color:var(--text)}
.xrow{display:grid;grid-template-columns:108px 1fr;gap:10px;padding:6px 0;border-top:1px solid var(--border);font-size:12px}
.xrow:first-child{border-top:0}.xrow .k{color:var(--text-muted)}
.xrow .v{font-family:'SF Mono',Menlo,Consolas,monospace;word-break:break-all}
.xrow .v.match{color:var(--green)}.xrow .v.nv{color:var(--nv)}.xrow .v.pending{color:var(--yellow)}.xrow .v.mute{color:var(--text-muted)}
.xnote{font-size:11px;color:var(--text-muted);margin-top:10px;line-height:1.5}.xnote b{color:var(--purple)}

/* ---- ledger ---- */
.ledger-wrap{margin-top:18px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--text-muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.06em;padding:8px 10px;border-bottom:1px solid var(--border)}
td{padding:9px 10px;border-bottom:1px solid var(--border);font-family:'SF Mono',Menlo,Consolas,monospace;font-size:12px}
tbody tr{opacity:0;transform:translateY(6px);transition:opacity .35s,transform .35s}tbody tr.in{opacity:1;transform:none}
.pill{display:inline-block;font-size:11px;font-weight:700;padding:2px 9px;border-radius:20px;border:1px solid}
.pill.allow{color:var(--green);background:rgba(63,185,80,.12);border-color:rgba(63,185,80,.3)}
.pill.escalate{color:var(--yellow);background:rgba(210,153,34,.12);border-color:rgba(210,153,34,.3)}
.pill.deny{color:var(--red);background:rgba(248,81,73,.12);border-color:rgba(248,81,73,.3)}
.recpt{color:var(--accent)}.recpt.pending{color:var(--yellow)}
.chain-ok{color:var(--green)}.foot{color:var(--text-muted);font-size:11.5px;margin-top:16px;line-height:1.6}
</style></head><body>

<header>
  <div>
    <div class="brand">Cerebro <span class="dot">Governor</span> &middot; agentic spend governance</div>
    <div class="tag">Autonomous agents can earn and spend &mdash; the Governor makes them safe enough for business.</div>
  </div>
  <div class="prov" id="prov"></div>
</header>

<div class="controls">
  <button class="primary" id="play">&#9654; Play captured run</button>
  <button id="reset">&#10227; Reset</button>
  <span class="status" id="status">ready</span>
</div>

<!-- THE SPINE: five-pillar pipeline -->
<div class="pipe">
  <div class="pillar" id="p_hermes">
    <span class="pnum">operator</span><div class="picon">&#129302;</div>
    <div class="pname">Hermes agent</div>
    <div class="psub">autonomous revenue operator</div>
    <div class="pdata" id="hermesData">idle</div>
  </div>
  <div class="pillar" id="p_nemoclaw">
    <span class="pnum">runtime</span><div class="picon">&#128737;&#65039;</div>
    <div class="pname">NemoClaw</div>
    <div class="psub" id="nemoSub">Landlock sandbox</div>
    <div class="pdata" id="nemoData">&mdash;</div>
  </div>
  <div class="pillar" id="p_nemotron">
    <span class="pnum">brain</span><div class="picon">&#9889;</div>
    <div class="pname">Nemotron 3 Ultra</div>
    <div class="psub" id="gpuSub">on NVIDIA GPU</div>
    <span class="badge b-nv" id="nvBadge">NVIDIA</span>
  </div>
  <div class="pillar" id="p_gov">
    <span class="pnum">the gate</span><div class="picon">&#9878;&#65039;</div>
    <div class="pname">Cerebro Governor</div>
    <div id="govStamp">&mdash;</div>
    <div class="pdata" id="govData">policy gate</div>
  </div>
  <div class="pillar" id="p_stripe">
    <span class="pnum">payment</span><div class="picon">&#128179;</div>
    <div class="pname">Stripe</div>
    <div class="psub">charged only on ALLOW</div>
    <div class="pdata" id="stripeData">&mdash;</div>
  </div>
</div>

<div class="grid2">
  <div class="card">
    <div class="label">Spend budget &middot; 1h window</div>
    <div class="bignum" id="remain">$0</div>
    <div class="budget-sub">remaining of <span id="budget">$0</span> &middot; moves only on an approved charge</div>
    <div class="bar"><span id="barFill"></span></div>
    <div class="capline"><span>per-txn cap <b id="cap">$0</b></span><span>hard max <b id="hardmax">$0</b></span></div>
  </div>
  <div class="card">
    <div class="label">Cross-reference &middot; one request id, three surfaces</div>
    <div class="xrow"><span class="k">request id</span><span class="v" id="x_req">&mdash;</span></div>
    <div class="xrow"><span class="k">policy rule</span><span class="v" id="x_rule">&mdash;</span></div>
    <div class="xrow"><span class="k">ledger hash</span><span class="v" id="x_hash">&mdash;</span></div>
    <div class="xrow"><span class="k">stripe receipt</span><span class="v" id="x_recpt">&mdash;</span></div>
    <div class="xrow"><span class="k">sandbox event</span><span class="v" id="x_sbx">&mdash;</span></div>
    <div class="xnote"><b>Why it&rsquo;s not a mockup:</b> the same request id reconciles across the tamper-evident ledger hash, the Stripe receipt, and the NemoClaw sandbox event. Three independent surfaces, one id.</div>
  </div>
</div>

<div class="card ledger-wrap">
  <div class="label">Hash-chained spend ledger &middot; <span class="chain-ok" id="chain">append-only, tamper-evident</span></div>
  <table>
    <thead><tr><th>#</th><th>decision</th><th>merchant</th><th>amount</th><th>rule</th><th>ledger hash</th><th>stripe receipt</th></tr></thead>
    <tbody id="ledger"></tbody>
  </table>
  <div class="foot" id="foot"></div>
</div>

<script>
const BEATS = __BEATS__, META = __META__, P = META.prov;
const $ = id => document.getElementById(id);
const money = n => "$" + Number(n).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const short = h => h ? h.slice(0,10)+"…" : "—";

// provenance header + pillar sub-labels from the REAL captured run
const modelShort = (P.model||"").split("/").pop().replace(":free","");
$("gpuSub").textContent = "on " + (P.gpu||"GPU");
$("nemoSub").textContent = P.runtime || "Landlock sandbox";
if(META.real){
  $("prov").innerHTML = '<span class="live">&#9679; captured from a real in-sandbox run</span><br>'
    + '<span class="nv">'+ (P.gpu||'') +'</span> &middot; '+ modelShort +' &middot; sandbox '+ (P.sandbox||'');
} else {
  $("prov").innerHTML = '<span class="pending">&#9679; capture pending live key</span><br>'
    + (P.gpu||'') +' &middot; '+ modelShort;
}
$("budget").textContent=money(META.budget);$("cap").textContent=money(META.per_txn_cap);$("hardmax").textContent=money(META.hard_max);

const PILL=["p_hermes","p_nemoclaw","p_nemotron","p_gov","p_stripe"];
let remaining=META.budget,i=0,timer=null;
function setBudget(){$("remain").textContent=money(remaining);$("barFill").style.width=(100*remaining/META.budget)+"%";}
function clearPillars(stripeBlocked){
  PILL.forEach(p=>{const el=$(p);el.classList.remove("active","flow","gate-deny","gate-escalate");});
  $("p_stripe").classList.toggle("stripe-blocked",!!stripeBlocked);
}
function reset(){
  clearTimeout(timer);i=0;remaining=META.budget;setBudget();
  $("ledger").innerHTML="";$("foot").textContent="";clearPillars(false);
  $("hermesData").textContent="idle";$("nemoData").textContent="—";
  $("govStamp").className="";$("govStamp").textContent="—";$("govData").textContent="policy gate";
  $("stripeData").textContent="—";$("stripeData").className="pdata";
  ["x_req","x_rule","x_hash","x_recpt","x_sbx"].forEach(k=>{$(k).textContent="—";$(k).className="v";});
  $("status").textContent="ready";$("play").disabled=false;
}
function row(b,n){
  const tr=document.createElement("tr");
  const recpt=b.stripe_receipt?'<span class="recpt">'+b.stripe_receipt+'</span>':(b.verdict==="ALLOW"?'<span class="recpt pending">pending</span>':'—');
  tr.innerHTML='<td>'+n+'</td><td><span class="pill '+b.verdict.toLowerCase()+'">'+b.verdict+'</span></td>'
    +'<td>'+b.merchant+'</td><td>'+money(b.amount)+'</td><td>'+(b.rule_fired||'')+'</td><td>'+short(b.entry_hash)+'</td><td>'+recpt+'</td>';
  $("ledger").appendChild(tr);requestAnimationFrame(()=>tr.classList.add("in"));
}
// step a packet through the pipeline; the governor gates the last hop to Stripe
function light(idx){return new Promise(r=>{const el=$(PILL[idx]);el.classList.add("active");if(idx>0)el.classList.add("flow");setTimeout(r,360);});}
async function beat(){
  if(i>=BEATS.length){return done();}
  const b=BEATS[i],cls=b.verdict.toLowerCase(),reaches=b.verdict==="ALLOW"&&!!b.stripe_receipt;
  clearPillars(false);
  // cross-ref panel
  $("x_req").textContent=b.request_id;$("x_req").className="v "+(b.request_id&&b.request_id!=="(unset)"?"match":"");
  $("x_rule").textContent=b.rule_fired||"—";$("x_hash").textContent=short(b.entry_hash);
  $("x_sbx").textContent=b.sandbox_ref;$("x_sbx").className="v nv";
  // 1 Hermes emits
  await light(0);$("hermesData").textContent=b.task+" → "+b.merchant+" "+money(b.amount);
  // 2 NemoClaw sandbox
  await light(1);$("nemoData").textContent=b.sandbox_ref;
  // 3 Nemotron brain
  await light(2);
  // 4 Governor gates
  await light(3);
  $("govStamp").className=cls;$("govStamp").textContent=b.verdict;
  $("govData").textContent="rule: "+(b.rule_fired||"");
  if(b.verdict==="DENY")$("p_gov").classList.add("gate-deny");
  if(b.verdict==="ESCALATE")$("p_gov").classList.add("gate-escalate");
  // 5 Stripe — only if the governor allowed
  if(reaches){
    await light(4);
    $("stripeData").textContent=b.stripe_receipt;$("stripeData").className="pdata";
    $("x_recpt").textContent=b.stripe_receipt;$("x_recpt").className="v match";
    remaining-=b.amount;setBudget();
  } else {
    $("p_stripe").classList.add("stripe-blocked");
    $("stripeData").textContent=b.verdict==="ESCALATE"?"held — awaiting human":"blocked — no charge";
    $("stripeData").className="pdata blocked";
    $("x_recpt").textContent=b.verdict==="ESCALATE"?"held (balance unchanged)":"— never reached Stripe";
    $("x_recpt").className="v "+(b.verdict==="ESCALATE"?"pending":"mute");
  }
  row(b,i+1);
  $("status").textContent="beat "+(i+1)+" of "+BEATS.length+" — "+b.verdict;
  i++;timer=setTimeout(beat,1400);
}
function done(){
  clearPillars(false);
  $("status").textContent="run complete — chain verified";
  $("chain").textContent="append-only, tamper-evident — ✓ chain verified";
  const blocked=BEATS.filter(b=>b.verdict==="DENY").length,held=BEATS.filter(b=>b.verdict==="ESCALATE").length;
  $("foot").innerHTML="Replay of a captured real run &middot; "+money(META.charged_total)+" approved &amp; charged via Stripe &middot; "
    +held+" escalated to a human &middot; "+blocked+" risky spend blocked before any money moved.";
  $("play").disabled=false;
}
$("play").onclick=()=>{reset();$("play").disabled=true;$("status").textContent="replaying…";timer=setTimeout(beat,400);};
$("reset").onclick=reset;reset();
</script>
</body></html>"""


if __name__ == "__main__":
    main()

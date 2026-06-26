#!/usr/bin/env python3
"""model_roles.py — right-sized Nemotron-family harness for the Cortex demo.

Match the model to the job (NVIDIA's own Nano/Super/Ultra premise) — not sponsor bingo.
All three tiers route through ONE gateway (a LiteLLM proxy); LiteLLM does local-vs-cloud:

    Hermes agent ─► triage  ─ nemotron-3-nano:4b   (local · RTX 4090 / Ollama via LiteLLM)
       (operator)   extract ─ nemotron-30b-a3b     (local · RTX 4090 / Ollama via LiteLLM)
                    plan    ─ nemotron-ultra 550B   (cloud · OpenRouter via LiteLLM)
                      │
                      ▼
                    Cortex  (deterministic policy gate — NO model approves money)

Nemotron-family harness (learned from the gemma4 master harness + the autoresearch Nemotron
design, `decisions/2026-06-09-autoresearch-nemotron-harness-design.md`):
  * /chat completions, NOT raw generate (4B swallows output into reasoning on generate)
  * thinking OFF for deterministic work — enforced via a Micro deterministic system prompt
    + temperature 0 (NVIDIA rec; the master-harness "Micro" profile for narrow extraction)
  * structured-output enforcement on the classification step (Ollama/BFCL malformed rate is real)
  * tuned per-tier timeouts (4B fast ~228 tok/s; 30B ~228 tok/s; cloud slower)

Honest provenance: each call records tier, model, where it runs (local GPU vs cloud), latency,
and whether it was actually reached. If a tier is unreachable, the event records the INTENDED
routing with reached=False — the architecture is shown truthfully, never faked.

Scope (Least-Privilege): talks only to the LiteLLM gateway; reads LITELLM_MASTER_KEY by NAME
from env, never logs its value; decides nothing about money (that is Cortex / policy_engine.py).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

# one gateway. A LiteLLM proxy (Colima/Docker) -> a local GPU (Ollama) or OpenRouter (cloud).
_BASE = os.environ.get("LITELLM_BASE", "http://localhost:4000")   # set LITELLM_BASE to your proxy
_KEYNAME = "LITELLM_MASTER_KEY"                                      # read by name only; never logged

# Micro deterministic system prompt = "thinking off" behaviorally + the master-harness Micro
# profile (narrow extraction). Keeps Nemotron-3 from emitting reasoning prose into content.
_SYS = ("You are a spend-governance worker for an autonomous business agent. Answer ONLY the "
        "question asked, in ONE line. No preamble, no step-by-step reasoning, no markdown. "
        "Be deterministic and terse. A separate deterministic policy gate (Cortex) makes the "
        "actual allow/deny call — you only advise.")

# role -> tier. model values are LiteLLM model_names (the LiteLLM config maps them to the real
# ollama tag / openrouter model). local tiers run on the 4090; ultra is cloud via OpenRouter.
ROLES = {
    "triage":  {"tier": "nano",  "local": True,  "structured": True,  "timeout": 20.0,
                "model": os.environ.get("NEMOTRON_NANO_MODEL", "nemotron-3-nano"),
                "desc": "in/out-of-scope classification"},
    "extract": {"tier": "30b",   "local": True,  "structured": False, "timeout": 90.0,
                "model": os.environ.get("NEMOTRON_30B_MODEL", "nemotron-30b-a3b"),
                "desc": "requirement / justification extraction"},
    "plan":    {"tier": "ultra", "local": False, "structured": False, "timeout": 60.0,
                "model": os.environ.get("NEMOTRON_ULTRA_MODEL", "nemotron-ultra"),
                "desc": "supervisor judgment (spend planner)"},
}


def _http_json(url, payload, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _chat(model, user, timeout, structured):
    """OpenAI-compatible chat against the LiteLLM gateway. temp 0 = deterministic."""
    body = {"model": model, "temperature": 0,
            "messages": [{"role": "system", "content": _SYS}, {"role": "user", "content": user}]}
    if structured:
        body["response_format"] = {"type": "json_object"}
    headers = {}
    key = os.environ.get(_KEYNAME)
    if key:
        headers["Authorization"] = "Bearer " + key
    out = _http_json(_BASE.rstrip("/") + "/v1/chat/completions", body, headers, timeout)
    return out["choices"][0]["message"]["content"] if out.get("choices") else ""


def call_role(role: str, prompt: str, timeout: float = None) -> dict:
    """Route one reasoning step to its tier's model via LiteLLM. Returns an honest provenance
    event; reached=False (with the intended routing) if unreachable — never faked."""
    spec = ROLES.get(role)
    if not spec:
        raise ValueError("unknown role: %s" % role)
    ev = {"role": role, "tier": spec["tier"], "model": spec["model"],
          "where": "RTX 4090 (Ollama via LiteLLM)" if spec["local"] else "OpenRouter (via LiteLLM)",
          "desc": spec["desc"], "reached": False, "ms": 0, "output": "", "error": None}
    t0 = time.time()
    try:
        text = _chat(spec["model"], prompt, timeout or spec["timeout"], spec["structured"])
        ev["reached"] = True
        ev["output"] = (text or "").strip()[:600]
    except (urllib.error.URLError, OSError, RuntimeError, KeyError, ValueError, json.JSONDecodeError) as e:
        ev["error"] = "%s: %s" % (type(e).__name__, str(e)[:120])
    ev["ms"] = int((time.time() - t0) * 1000)
    return ev


def route_for(beat: dict) -> list:
    """The constrained DAG: which roles reason about a spend beat. Triage + extract always; the
    supervisor (Ultra) also weighs in on the judgment-call spends. Deterministic."""
    roles = ["triage", "extract"]
    if (beat.get("amount") or 0) >= 100 or beat.get("purpose") in ("marketing", "infra", "trial"):
        roles.append("plan")
    return roles


def reason_beat(beat: dict, timeout: float = None) -> list:
    """Run the role DAG for one spend beat; return the provenance events. Provenance only —
    does NOT decide the spend (Cortex's deterministic gate does)."""
    ctx = ("A business agent wants to spend $%(amount)s at %(merchant)s for '%(task)s' "
           "(category %(category)s, purpose %(purpose)s). " % beat)
    asks = {
        "triage":  ctx + 'Return JSON {"in_scope": true|false, "reason": "<=12 words"}: '
                         "is this in scope for a GovCon bid-pursuit budget?",
        "extract": ctx + "One line: what requirement or opportunity-value justifies it?",
        "plan":    ctx + "As supervisor, one line: auto-approve, escalate to a human, or deny — and why?",
    }
    return [call_role(r, asks[r], timeout) for r in route_for(beat)]


if __name__ == "__main__":
    # Offline-safe self-check: proves routing + event shape even when the gateway is unreachable
    # (reached=False with intended routing). When LiteLLM + the model entries are live, prints real output.
    sample = {"merchant": "datavendor", "amount": 150.0, "category": "data",
              "purpose": "infra", "task": "pull-solicitation-data"}
    evs = reason_beat(sample, timeout=6.0)
    for e in evs:
        assert {"role", "tier", "model", "where", "reached", "ms"} <= set(e), "event shape"
        tail = "" if e["reached"] else "  (%s)" % (e["error"] or "")[:46]
        print("  %-7s -> %-5s %-18s %-30s reached=%s %dms%s"
              % (e["role"], e["tier"], e["model"], e["where"], e["reached"], e["ms"], tail))
    print("gateway:", _BASE, "| route_for(150/infra) =", route_for(sample))
    print("self-check ok")

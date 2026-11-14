"""Plain English rules.

Staff type a rule in plain words. It becomes a draft policy document that
reads back exactly what it will do. Only the agency owner (admin) can switch
it on, and every version of every policy is kept in `policy_versions`, so the
time machine can show which rules were in force at any moment.

The parser covers the common agency rules below. With an LLM key, the same
draft and approve flow would accept any wording; the safety comes from the
human approving the readback, not from the parser.

  never spend more than $200 a day on dental clients
  never go over $150 a day on TikTok for Harbour Coffee
  always ask before pausing anything on Reddit
  ask a human before any budget change for Northside
  never change targeting for Bright Smile
  let the agent move budgets up to 15% on its own for Harbour Coffee on Meta
"""

from __future__ import annotations

import re
import uuid

import gate
from platforms import utcnow

INDUSTRY_WORDS = {"Medical": ["dental", "dentist", "medical", "health", "clinic"],
                  "Real estate": ["real estate", "realty", "realtor", "housing", "property"],
                  "Ecommerce": ["ecommerce", "e-commerce", "shop", "store", "coffee"]}
PLATFORM_WORDS = {"tiktok": ["tiktok", "tik tok"], "reddit": ["reddit"],
                  "meta": ["meta", "facebook", "instagram", "fb", "ig"],
                  "google": ["google", "youtube", "search ads"]}
ACTION_WORDS = [("budget", ["set_budget"]), ("spend", ["set_budget"]), ("pause", ["pause"]),
                ("targeting", ["set_targeting"]), ("audience", ["set_targeting"]), ("paus", ["pause"]),
                ("keyword", ["pause_keyword", "add_negative"]), ("review", ["reply_review"]),
                ("anything", ["set_budget", "pause", "set_targeting", "pause_keyword", "add_negative", "reply_review"]),
                ("any change", ["set_budget", "pause", "set_targeting"])]


def _scope(text: str, clients: list[dict]) -> tuple[dict, list[str]]:
    when, words = {}, []
    low = text.lower()
    for c in clients:
        names = {c["name"].lower(), c["_id"].lower(), c["name"].lower().split()[0]}
        if any(n and re.search(rf"\b{re.escape(n)}\b", low) for n in names):
            when["client_id"] = c["_id"]
            words.append(c["name"])
            break
    if "client_id" not in when:
        for ind, ws in INDUSTRY_WORDS.items():
            if any(w in low for w in ws):
                when["industry"] = ind
                words.append(f"{ind.lower()} clients")
                break
    for plat, ws in PLATFORM_WORDS.items():
        if any(re.search(rf"\b{re.escape(w)}\b", low) for w in ws):
            when["platform"] = plat
            words.append({"meta": "Facebook + Instagram", "google": "Google Ads",
                          "tiktok": "TikTok", "reddit": "Reddit"}[plat])
            break
    return when, words


def _actions(text: str) -> list[str]:
    low = text.lower()
    for w, acts in ACTION_WORDS:
        if w in low:
            return acts
    return ["set_budget", "pause", "set_targeting"]


def parse(text: str, clients: list[dict]) -> dict:
    raw = text.strip()
    low = raw.lower()
    when, scope_words = _scope(raw, clients)
    where = " for " + " on ".join(scope_words) if scope_words else " for every client"

    m = re.search(r"never (?:spend|go|let .*? go) (?:more than|over|above) \$?\s?([\d,]+)\s*(?:a|per|/)\s*day", low)
    if m:
        n = float(m.group(1).replace(",", ""))
        return {"effect": "deny", "roles": ["*"], "actions": ["set_budget"], "priority": 15,
                "when": {**when, "new_budget_gt": n},
                "readback": f"Block any daily budget above ${n:,.0f}{where}. Applies to everyone, agency owner included."}

    m = re.search(r"(?:let|allow) the agent (?:to )?(?:move|change) (?:budgets? )?(?:by )?(?:up to )?(\d+)\s?%", low)
    if m:
        n = float(m.group(1))
        return {"effect": "allow", "roles": ["agent"], "actions": ["set_budget"], "priority": 95,
                "when": {**when, "change_pct_lte": n},
                "readback": f"Let the agent move budgets by up to {n:.0f}% on its own{where}. "
                            f"Stricter rules (protected campaigns, budget ceilings, memory warnings, Reddit) still win."}

    if re.search(r"\b(always )?ask\b|\bneeds? (a )?(human|approval)\b|\bcheck with me\b", low):
        acts = _actions(low)
        return {"effect": "require_approval", "roles": ["agent"], "actions": acts, "priority": 80,
                "when": when,
                "readback": f"The agent must ask a person before: {', '.join(a.replace('_', ' ') for a in acts)}{where}."}

    if re.search(r"\bnever\b|\bdon'?t\b|\bdo not\b|\bblock\b", low):
        acts = _actions(low)
        return {"effect": "deny", "roles": ["*"], "actions": acts, "priority": 16, "when": when,
                "readback": f"Block {', '.join(a.replace('_', ' ') for a in acts)}{where}, for everyone."}

    return {"error": "I couldn't turn that into a rule. Try: \"never spend more than $200 a day on dental clients\", "
                     "\"always ask before pausing anything on Reddit\", or \"let the agent move budgets up to 15% for Harbour Coffee\"."}


def _version(db, policy: dict, change: str, actor_id: str) -> None:
    n = db.policy_versions.count_documents({"policy_id": policy["_id"]}) + 1
    db.policy_versions.insert_one({"policy_id": policy["_id"], "version": n, "doc": dict(policy),
                                   "active": bool(policy.get("active")), "change": change,
                                   "by": actor_id, "at": utcnow()})


def version_all(db, actor_id: str = "system:seed") -> None:
    for p in db.policies.find():
        _version(db, p, "seeded", actor_id)


def draft(db, text: str, actor_id: str) -> dict:
    actor = gate.get_actor(db, actor_id)
    if not actor or actor.get("kind") != "human" or "client" in str(actor.get("grants")):
        return {"ok": False, "error": "Only agency staff can draft rules."}
    parsed = parse(text, list(db.clients.find()))
    if "error" in parsed:
        return {"ok": False, "error": parsed["error"]}
    pid = "custom-" + uuid.uuid4().hex[:6]
    policy = {"_id": pid, "priority": parsed["priority"], "roles": parsed["roles"], "actions": parsed["actions"],
              "when": parsed["when"], "effect": parsed["effect"], "why": f"Agency rule: \"{text.strip()}\"",
              "active": False, "status": "draft", "source_text": text.strip(), "readback": parsed["readback"],
              "drafted_by": actor_id, "drafted_at": utcnow()}
    db.policies.insert_one(policy)
    _version(db, policy, "drafted", actor_id)
    gate.audit(db, "policy_drafted", actor_id=actor_id, policy_id=pid, text=text.strip(), readback=parsed["readback"])
    return {"ok": True, "policy": policy}


def decide(db, policy_id: str, actor_id: str, activate: bool) -> dict:
    p = db.policies.find_one({"_id": policy_id})
    if not p:
        return {"ok": False, "error": "Rule not found."}
    effect, pid, why = gate.check(db, actor_id, {"action_type": "approve_policy" if activate else "retire_policy"})
    if effect != "allow":
        return {"ok": False, "error": f"denied by {pid}: {why}"}
    status = "active" if activate else "retired"
    db.policies.update_one({"_id": policy_id}, {"$set": {"active": activate, "status": status,
                                                         f"{status}_by": actor_id, f"{status}_at": utcnow()}})
    p = db.policies.find_one({"_id": policy_id})
    _version(db, p, status, actor_id)
    gate.invalidate()
    gate.audit(db, "policy_" + status, actor_id=actor_id, policy_id=policy_id, text=p.get("source_text"),
               readback=p.get("readback"), approving_policy=pid)
    return {"ok": True, "status": status}

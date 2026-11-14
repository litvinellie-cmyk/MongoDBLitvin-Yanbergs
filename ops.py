"""Human operations: approve, reject, execute, undo, halt, explain, seed.

Used by both the CLI (approvals.py) and the dashboard.
"""

from __future__ import annotations

from bson import ObjectId
from pymongo import ReturnDocument

import gate
from platforms import platform_for, utcnow
from policy import DEFAULT_POLICIES

DEMO_ACTORS = [
    {"_id": gate.AGENT_ID, "kind": "agent", "name": "Budget bot", "roles": ["agent"], "active": True},
    # Agency staff. Only the agency approves; clients only read their reports.
    {"_id": "user:ellie", "kind": "human", "name": "Ellie (agency owner)", "roles": ["approver", "admin"],
     "grants": [], "active": True, "github_login": "litvinellie-cmyk"},
    {"_id": "user:maya", "kind": "human", "name": "Maya (senior strategist, up to $25/day)",
     "roles": ["senior"], "grants": [], "active": True},
    {"_id": "user:alex", "kind": "human", "name": "Alex (junior, cannot approve)", "roles": ["junior"],
     "grants": [], "active": True},
    {"_id": "user:analyst", "kind": "human", "name": "Priya (analyst, read only)", "roles": ["viewer"],
     "grants": [], "active": True},
    # Clients: can read their own report, nothing else.
    {"_id": "user:harbour-owner", "kind": "human", "name": "Sam (Harbour Coffee client)", "roles": [],
     "grants": [{"client_id": "harbour", "roles": ["client"]}], "active": True},
    {"_id": "user:dental-owner", "kind": "human", "name": "Dr. Lee (Bright Smile client)", "roles": [],
     "grants": [{"client_id": "brightsmile", "roles": ["client"]}], "active": True},
    {"_id": "agent:circuit-breaker", "kind": "agent", "name": "Circuit breaker", "roles": ["breaker"],
     "grants": [], "active": True},
    {"_id": "user:guest", "kind": "human", "name": "Guest viewer", "roles": ["viewer"], "grants": [],
     "active": True},
]


def seed_identity_and_policy(db) -> None:
    for a in DEMO_ACTORS:
        db.actors.replace_one({"_id": a["_id"]}, a, upsert=True)
    for p in DEFAULT_POLICIES:
        db.policies.replace_one({"_id": p["_id"]}, {**p, "active": True, "status": "active"}, upsert=True)
    gate.invalidate()
    import nl_rules
    nl_rules.version_all(db)


def _oid(pid):
    return pid if isinstance(pid, ObjectId) else ObjectId(pid)


def _live_campaign(platform, account, campaign_id):
    for c in platform.fetch_campaigns(account, utcnow()):
        if c["campaign_id"] == campaign_id:
            return c
    return None


def decide_proposal(db, platforms, pid, decision: str, actor_id: str, comment: str = "") -> dict:
    """decision: 'approve' | 'reject'. Approve also executes right away. A comment is required."""
    p = db.proposals.find_one({"_id": _oid(pid)})
    if not p or p["status"] != "pending":
        return {"ok": False, "error": "not found or not pending"}
    if not (comment or "").strip():
        return {"ok": False, "error": "Add a short comment saying why. It goes into the audit trail."}
    a = p["action"]
    usd = (abs(a["new"] - a["old"]) if a["type"] == "set_budget"
           else (a.get("old") or 0) if a["type"] == "pause" else 0.0)
    ctx = {"action_type": decision, "account_id": p["account_id"], "campaign_id": p["campaign_id"],
           "client_id": p.get("client_id"), "platform": p.get("platform"), "change_usd": round(usd, 2)}
    effect, policy_id, why = gate.check(db, actor_id, ctx, action_id=p.get("action_id"))
    if effect != "allow":
        return {"ok": False, "error": f"denied by {policy_id}: {why}"}
    status = "approved" if decision == "approve" else "rejected"
    r = db.proposals.update_one(
        {"_id": p["_id"], "status": "pending"},
        {"$set": {"status": status, "decided_at": utcnow(), "decided_by": actor_id, "comment": comment}},
    )
    if not r.modified_count:
        return {"ok": False, "error": "someone else decided first"}
    import tickets
    tickets.close_for_proposal(db, p, decision, actor_id, comment)
    gate.audit(db, f"proposal_{status}", actor_id=actor_id, action_id=p.get("action_id"),
               proposal_id=str(p["_id"]), campaign_id=p["campaign_id"], policy_id=policy_id,
               client_id=p.get("client_id"), comment=comment)
    if status == "approved":
        return execute_proposal(db, platforms, p["_id"])
    return {"ok": True, "status": status}


def execute_proposal(db, platforms, pid) -> dict:
    # Atomic claim so the dashboard and the watcher never both run it.
    p = db.proposals.find_one_and_update(
        {"_id": _oid(pid), "status": "approved"}, {"$set": {"status": "executing"}},
        return_document=ReturnDocument.AFTER,
    )
    if not p:
        return {"ok": False, "error": "not approved or already claimed"}
    account = db.accounts.find_one({"_id": p["account_id"]})
    platform = platform_for(platforms, account)
    a = p["action"]
    if a["type"] in gate.ITEM_ACTIONS:
        live = {"campaign_id": p["campaign_id"], "tags": [], "status": "ACTIVE"}
    else:
        live = _live_campaign(platform, account, p["campaign_id"])
    if not live or live["status"] != "ACTIVE" or (
        a["type"] == "set_budget" and abs(live["daily_budget"] - a["old"]) > 0.01
    ):
        db.proposals.update_one({"_id": p["_id"]}, {"$set": {"status": "stale", "finished_at": utcnow()}})
        gate.audit(db, "proposal_stale", action_id=p.get("action_id"), campaign_id=p["campaign_id"],
                   client_id=p.get("client_id"),
                   observed=live and {"daily_budget": live["daily_budget"], "status": live["status"]})
        return {"ok": False, "error": "campaign changed since the proposal"}

    # Policy is checked again at execution, under the approver's identity.
    out = gate.attempt(
        db, platform, account, live, a, p["reason"], actor_id=p["decided_by"], mode="approved",
        action_id=p.get("action_id"), on_behalf_of=p.get("requested_by"),
        approval={"proposal_id": str(p["_id"]), "approved_by": p["decided_by"], "comment": p.get("comment"),
                  "requested_by": p.get("requested_by")},
    )
    if out["effect"] != "allow":
        status = "blocked"
    else:
        status = "done" if out["result"]["ok"] else "failed"
    db.proposals.update_one({"_id": p["_id"]},
                            {"$set": {"status": status, "finished_at": utcnow(), "outcome": out}})
    return {"ok": status == "done", "status": status, **out}


def undo(db, platforms, change_id, actor_id: str) -> dict:
    ch = db.change_log.find_one({"_id": _oid(change_id)})
    if not ch or not ch["result"].get("ok"):
        return {"ok": False, "error": "change not found or never applied"}
    if db.change_log.find_one({"reverts": str(ch["_id"]), "result.ok": True}):
        return {"ok": False, "error": "already undone"}
    account = db.accounts.find_one({"_id": ch["account_id"]})
    platform = platform_for(platforms, account)
    a = ch["action"]
    if a["type"] == "reply_review":
        return {"ok": False, "error": "A public review reply can't be taken back automatically. Edit it on the platform."}
    if a["type"] == "sync_exclusions":
        return {"ok": False, "error": "A sync only refreshes who is excluded. There is nothing to undo."}
    if a["type"] in ("fix_utm", "set_frequency_cap"):
        reverse = {**a, "old": a["new"], "new": a["old"]}
        reason = {"text": f"Undo of change {ch['_id']} ({ch['reason']['text']})", "numbers": {"reverts": str(ch["_id"])}}
        out = gate.attempt(db, platform, account, {"campaign_id": ch["campaign_id"], "tags": []}, reverse, reason,
                           actor_id=actor_id, mode="rollback", reverts=str(ch["_id"]))
        return {"ok": out["effect"] == "allow" and out["result"]["ok"], **out}
    if a["type"] in gate.ITEM_ACTIONS:
        reverse = {"type": {"pause_keyword": "resume_keyword", "resume_keyword": "pause_keyword",
                            "add_negative": "remove_negative", "remove_negative": "add_negative"}[a["type"]],
                   "field": "status", "old": a["new"], "new": a["old"], "label": a.get("label")}
        reason = {"text": f"Undo of change {ch['_id']} ({ch['reason']['text']})", "numbers": {"reverts": str(ch["_id"])}}
        out = gate.attempt(db, platform, account, {"campaign_id": ch["campaign_id"], "tags": []}, reverse, reason,
                           actor_id=actor_id, mode="rollback", reverts=str(ch["_id"]))
        return {"ok": out["effect"] == "allow" and out["result"]["ok"], **out}
    live = _live_campaign(platform, account, ch["campaign_id"])
    if not live:
        return {"ok": False, "error": "campaign no longer exists"}
    a = ch["action"]
    if a["type"] == "set_budget":
        reverse = {"type": "set_budget", "field": "daily_budget",
                   "old": live["daily_budget"], "new": a["old"]}
    elif a["type"] == "pause":
        reverse = {"type": "resume", "field": "status", "old": "PAUSED", "new": "ACTIVE"}
    elif a["type"] == "set_targeting":
        reverse = {"type": "set_targeting", "field": "targeting", "old": live.get("targeting"), "new": a["old"]}
    else:
        return {"ok": False, "error": f"cannot undo {a['type']}"}
    reason = {"text": f"Undo of change {ch['_id']} ({ch['reason']['text']})",
              "numbers": {"reverts": str(ch["_id"])}}
    out = gate.attempt(db, platform, account, live, reverse, reason, actor_id=actor_id,
                       mode="rollback", reverts=str(ch["_id"]))
    ok = out["effect"] == "allow" and out["result"]["ok"]
    return {"ok": ok, **out}


def set_halt(db, actor_id: str, halted: bool, reason: str = "", client_id: str | None = None) -> dict:
    """client_id None halts everything (agency admin); a client id halts that client only."""
    scope = f"client:{client_id}" if client_id else "global"
    ctx = {"action_type": "halt" if halted else "unhalt", "client_id": client_id}
    effect, pid, why = gate.check(db, actor_id, ctx)
    if effect != "allow":
        return {"ok": False, "error": f"denied by {pid}: {why}"}
    now = utcnow()
    gate.invalidate()
    db.controls.update_one({"_id": scope}, {"$set": {
        "halted": halted, "halted_by": actor_id if halted else None,
        "reason": reason, "changed_at": now}}, upsert=True)
    gate.invalidate()
    if halted and scope == "global":
        db.runs.update_many({"status": "running"}, {"$set": {"status": "halted", "ended_at": now}})
        # Pending proposals wait; nothing executes while halted.
    gate.audit(db, "kill_switch", actor_id=actor_id, halted=halted, reason=reason,
               scope=scope, client_id=client_id, policy_id=pid)
    return {"ok": True, "halted": halted}


def explain(db, action_id: str) -> dict:
    """Everything needed to reconstruct one action: who asked, which policy,
    who approved, what was sent, what the platform showed afterwards."""
    prop = db.proposals.find_one({"action_id": action_id})
    events = list(db.audit_events.find({"action_id": action_id}).sort([("at", 1), ("_id", 1)]))
    changes = list(db.change_log.find({"action_id": action_id}))
    undone_by = []
    for c in changes:
        undone_by += list(db.change_log.find({"reverts": str(c["_id"])}))
    return {"action_id": action_id, "proposal": prop, "events": events,
            "changes": changes, "undone_by": undone_by}

"""Time machine: what the agent knew and what was in force at any past moment.

Rebuilt from what Atlas already keeps:
  * policy_versions  -> which rules were active
  * audit_events     -> kill switch state, decisions, approvals
  * change_log       -> budgets and statuses (current values with later changes undone)
  * proposals        -> what was waiting for a human
"""

from __future__ import annotations

from datetime import datetime

from platforms import utcnow


def bounds(db, client: str) -> dict:
    q = {} if client == "all" else {"client_id": {"$in": [client, None]}}
    first = db.audit_events.find_one(q, sort=[("at", 1)], projection={"at": 1})
    times = [e["at"] for e in db.audit_events.find(
        {**q, "event": {"$in": ["run_finished", "proposal_approved", "kill_switch", "circuit_breaker",
                                "policy_active", "action_result", "proposal_rejected"]}},
        {"at": 1, "event": 1}).sort("at", 1).limit(300)]
    return {"start": first["at"] if first else utcnow(), "end": utcnow(), "marks": times}


def state_at(db, client: str, t: datetime) -> dict:
    scoped = {} if client == "all" else {"client_id": client}

    # Rules in force: latest version of each policy at or before t.
    rules = {}
    for v in db.policy_versions.find({"at": {"$lte": t}}).sort([("at", 1), ("_id", 1)]):
        rules[v["policy_id"]] = v
    active = [v["doc"] for v in rules.values() if v["active"]]
    custom = [{"id": d["_id"], "text": d.get("source_text"), "readback": d.get("readback")}
              for d in active if d["_id"].startswith("custom-")]

    # Kill switch at t.
    halted = {}
    for e in db.audit_events.find({"event": "kill_switch", "at": {"$lte": t}}).sort("at", 1):
        halted[e.get("scope") or "global"] = e
    halts = [{"scope": k, "by": e["actor_id"], "reason": e.get("reason")} for k, e in halted.items()
             if e.get("halted") and (client == "all" or k in ("global", f"client:{client}"))]

    # Budgets at t: start from now, undo every change made after t (newest first).
    camps = {}
    acc_q = {} if client == "all" else {"client_id": client}
    acc_ids = [a["_id"] for a in db.accounts.find(acc_q, {"_id": 1})]
    for c in db.sandbox_campaigns.find({"account_id": {"$in": acc_ids}}):
        camps[c["_id"]] = {"name": c["name"], "budget": c["daily_budget"], "status": c["status"],
                           "now_budget": c["daily_budget"], "now_status": c["status"],
                           "targeting": c.get("targeting")}
    for ch in db.change_log.find({"result.ok": True, "applied_at": {"$gt": t},
                                  "campaign_id": {"$in": list(camps)}}).sort("applied_at", -1):
        a, c = ch["action"], camps[ch["campaign_id"]]
        if a["type"] == "set_budget":
            c["budget"] = a["old"]
        elif a["type"] == "pause":
            c["status"] = "ACTIVE"
        elif a["type"] == "resume":
            c["status"] = "PAUSED"
        elif a["type"] == "set_targeting":
            c["targeting"] = a["old"]

    waiting = [p for p in db.proposals.find({**scoped, "created_at": {"$lte": t}})
               if not p.get("decided_at") or p["decided_at"] > t]
    events = list(db.audit_events.find({**({} if client == "all" else {"client_id": {"$in": [client, None]}}),
                                        "at": {"$lte": t}}).sort([("at", -1), ("_id", -1)]).limit(15))
    changes = db.change_log.count_documents({**scoped, "result.ok": True, "applied_at": {"$lte": t}})
    return {"at": t, "rules_active": len(active), "custom_rules": custom, "halts": halts,
            "campaigns": [{"id": k, **v, "changed_since": v["budget"] != v["now_budget"] or v["status"] != v["now_status"]}
                          for k, v in sorted(camps.items())],
            "waiting": [{"campaign_id": p["campaign_id"], "type": p["action"]["type"],
                         "label": p["action"].get("label"), "policy_id": p.get("policy_id")} for p in waiting],
            "events": events, "changes_so_far": changes}

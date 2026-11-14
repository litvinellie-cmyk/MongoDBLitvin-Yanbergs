"""The one door every action goes through.

identity -> kill switch -> policy -> send -> verify -> audit

Nothing in this app calls a platform's write methods except `perform` below.
"""

from __future__ import annotations

import uuid

from platforms import utcnow
from policy import action_context, evaluate, roles_for

AGENT_ID = "agent:budget-bot"


def new_action_id() -> str:
    return uuid.uuid4().hex[:12]


def audit(db, event: str, **fields) -> None:
    """Insert only. The audit trail is never updated or deleted by the app."""
    db.audit_events.insert_one({"event": event, "at": utcnow(), **fields})


def halt_state(db, account_id: str | None, client_id: str | None = None) -> dict | None:
    """Kill switch at three levels: everything, one client, one ad account."""
    ids = ["global"] + ([f"client:{client_id}"] if client_id else []) + ([account_id] if account_id else [])
    return db.controls.find_one({"_id": {"$in": ids}, "halted": True})


def get_actor(db, actor_id: str) -> dict | None:
    return db.actors.find_one({"_id": actor_id, "active": True})


def policies(db) -> list[dict]:
    return list(db.policies.find({"active": {"$ne": False}}))


def check(db, actor_id: str, ctx: dict, *, action_id: str | None = None,
          run_id: str | None = None, on_behalf_of: str | None = None) -> tuple[str, str, str]:
    """Identity + kill switch + policy. Always writes a policy_check audit event."""
    actor = get_actor(db, actor_id)
    stop = (halt_state(db, ctx.get("account_id"), ctx.get("client_id"))
            if ctx["action_type"] not in ("halt", "unhalt") else None)
    roles = roles_for(actor, ctx.get("client_id")) if actor else []
    if not actor:
        effect, pid, why = "deny", "unknown-actor", f"{actor_id} is not an active actor."
    elif stop:
        effect, pid, why = ("deny", "kill-switch",
                            f"Agent halted by {stop.get('halted_by')}: {stop.get('reason', '')}")
    else:
        effect, pid, why = evaluate(policies(db), roles, ctx)
    audit(db, "policy_check", actor_id=actor_id, on_behalf_of=on_behalf_of,
          roles=roles, action_id=action_id, run_id=run_id, client_id=ctx.get("client_id"),
          account_id=ctx.get("account_id"), campaign_id=ctx.get("campaign_id"),
          action_type=ctx["action_type"], context=ctx,
          effect=effect, policy_id=pid, why=why)
    return effect, pid, why


def _verify(platform, account, campaign_id, action) -> tuple[bool, dict | None]:
    """Re-read the platform to see whether the change actually took effect."""
    live = {c["campaign_id"]: c for c in platform.fetch_campaigns(account, utcnow())}
    c = live.get(campaign_id)
    if not c:
        return False, None
    t = action["type"]
    if t == "set_budget":
        ok = abs(c["daily_budget"] - action["new"]) < 0.01
    elif t == "pause":
        ok = c["status"] == "PAUSED"
    elif t == "resume":
        ok = c["status"] == "ACTIVE"
    else:
        ok = False
    return ok, {"daily_budget": c["daily_budget"], "status": c["status"]}


def perform(db, platform, account, campaign_id, action, reason, *, actor_id, action_id,
            mode, run_id=None, approval=None, reverts=None, policy_id=None) -> dict:
    # Last look at the kill switch right before the write: this is what
    # makes "stop mid run" real.
    cid = account.get("client_id")
    stop = halt_state(db, account["_id"], cid)
    if stop:
        audit(db, "action_blocked", actor_id=actor_id, action_id=action_id, run_id=run_id, client_id=cid,
              campaign_id=campaign_id, reason="kill switch engaged before send")
        return {"ok": False, "error": "halted"}

    audit(db, "action_sent", actor_id=actor_id, action_id=action_id, run_id=run_id, client_id=cid,
          account_id=account["_id"], campaign_id=campaign_id, action=action, mode=mode)
    try:
        t = action["type"]
        if t == "set_budget":
            platform.set_budget(account, campaign_id, action["new"])
        elif t == "pause":
            platform.pause(account, campaign_id)
        elif t == "resume":
            platform.resume(account, campaign_id)
        else:
            raise ValueError(f"unsupported action {t}")
        verified, observed = _verify(platform, account, campaign_id, action)
        result = {"ok": True, "verified": verified, "observed": observed}
    except Exception as e:
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    audit(db, "action_result", actor_id=actor_id, action_id=action_id, run_id=run_id, client_id=cid,
          campaign_id=campaign_id, result=result)
    now = utcnow()
    db.change_log.insert_one({
        "action_id": action_id, "client_id": cid, "account_id": account["_id"],
        "platform": account["platform"],
        "campaign_id": campaign_id, "action": action, "reason": reason, "mode": mode,
        "actor_id": actor_id, "approval": approval, "policy_id": policy_id,
        "reverts": reverts, "run_id": run_id, "applied_at": now, "result": result,
        "memory_text": f"{action['type']} {campaign_id}: {reason['text']}",
    })
    return result


def attempt(db, platform, account, campaign, action, reason, *, actor_id, mode,
            action_id=None, run_id=None, on_behalf_of=None, approval=None,
            reverts=None) -> dict:
    """check, then perform if allowed. Returns {effect, policy_id, why, action_id, result?}."""
    action_id = action_id or new_action_id()
    ctx = action_context(action, campaign, account)
    effect, pid, why = check(db, actor_id, ctx, action_id=action_id, run_id=run_id,
                             on_behalf_of=on_behalf_of)
    out = {"effect": effect, "policy_id": pid, "why": why, "action_id": action_id}
    if effect == "allow":
        out["result"] = perform(db, platform, account, campaign["campaign_id"], action, reason,
                                actor_id=actor_id, action_id=action_id, mode=mode, run_id=run_id,
                                approval=approval, reverts=reverts, policy_id=pid)
    return out

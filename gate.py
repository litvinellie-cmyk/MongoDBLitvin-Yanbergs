"""The one door every action goes through.

identity -> kill switch -> policy -> send -> verify -> audit

Nothing in this app calls a platform's write methods except `perform` below.
"""

from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager

from platforms import utcnow
from policy import action_context, evaluate, roles_for

AGENT_ID = "agent:budget-bot"
ITEM_ACTIONS = {"pause_keyword", "resume_keyword", "reply_review", "add_negative", "remove_negative",
                "fix_utm", "set_frequency_cap", "sync_exclusions"}


def new_action_id() -> str:
    return uuid.uuid4().hex[:12]


_local = threading.local()
_cache: dict = {}
CACHE_SECONDS = 5.0


def _cached(key, loader, ttl=CACHE_SECONDS):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    val = loader()
    _cache[key] = (time.monotonic(), val)
    return val


def invalidate() -> None:
    """Call after policies, actors or the kill switch change."""
    _cache.clear()


@contextmanager
def batch(db):
    """Buffer audit events during an agent run and write them in bulk.
    perform() flushes before touching any platform, so the record of a
    decision is always in Atlas before the action happens."""
    prev = getattr(_local, "buf", None)
    _local.buf = []
    try:
        yield
    finally:
        flush(db)
        _local.buf = prev


def run_cache() -> dict | None:
    """Per run lookups loaded once (open proposals, recent denials, edits)."""
    return getattr(_local, "rc", None)


def set_run_cache(rc) -> None:
    _local.rc = rc


def flush(db) -> None:
    buf = getattr(_local, "buf", None)
    if buf:
        db.audit_events.insert_many(buf, ordered=True)
        buf.clear()


def audit(db, event: str, **fields) -> None:
    """Insert only. The audit trail is never updated or deleted by the app."""
    doc = {"event": event, "at": utcnow(), **fields}
    buf = getattr(_local, "buf", None)
    if buf is not None:
        buf.append(doc)
    else:
        db.audit_events.insert_one(doc)


def halt_state(db, account_id: str | None, client_id: str | None = None) -> dict | None:
    """Kill switch at three levels: everything, one client, one ad account."""
    ids = ["global"] + ([f"client:{client_id}"] if client_id else []) + ([account_id] if account_id else [])
    return db.controls.find_one({"_id": {"$in": ids}, "halted": True})


def get_actor(db, actor_id: str) -> dict | None:
    actors = _cached(("actors", id(db)), lambda: {a["_id"]: a for a in db.actors.find({"active": True})})
    return actors.get(actor_id)


def policies(db) -> list[dict]:
    return _cached(("policies", id(db)), lambda: list(db.policies.find({"active": {"$ne": False}})))


def halt_state_cached(db, account_id, client_id=None):
    """Kill switch snapshot for decisions (1 second old at most). perform()
    always re-reads it live right before a write."""
    halted = _cached(("controls", id(db)), lambda: list(db.controls.find({"halted": True})), ttl=1.0)
    ids = {"global", f"client:{client_id}" if client_id else None, account_id}
    return next((c for c in halted if c["_id"] in ids), None)


def check(db, actor_id: str, ctx: dict, *, action_id: str | None = None,
          run_id: str | None = None, on_behalf_of: str | None = None) -> tuple[str, str, str]:
    """Identity + kill switch + policy. Always writes a policy_check audit event."""
    actor = get_actor(db, actor_id)
    stop = (halt_state_cached(db, ctx.get("account_id"), ctx.get("client_id"))
            if ctx["action_type"] not in ("halt", "unhalt", "open_ticket", "close_ticket") else None)
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
    elif t == "set_targeting":
        ok = all(c.get("targeting", {}).get(k) == v for k, v in action["new"].items())
    else:
        ok = False
    obs = {"daily_budget": c["daily_budget"], "status": c["status"]}
    if t == "set_targeting":
        obs["targeting"] = c.get("targeting")
    return ok, obs


def perform(db, platform, account, campaign_id, action, reason, *, actor_id, action_id,
            mode, run_id=None, approval=None, reverts=None, policy_id=None) -> dict:
    # Last look at the kill switch right before the write: this is what
    # makes "stop mid run" real.
    cid = account.get("client_id")
    flush(db)  # every decision so far is in Atlas before we touch the platform
    stop = halt_state(db, account["_id"], cid)
    if stop:
        audit(db, "action_blocked", actor_id=actor_id, action_id=action_id, run_id=run_id, client_id=cid,
              campaign_id=campaign_id, reason="kill switch engaged before send")
        return {"ok": False, "error": "halted"}

    db.audit_events.insert_one({"event": "action_sent", "at": utcnow(), "actor_id": actor_id,
                                "action_id": action_id, "run_id": run_id, "client_id": cid,
                                "account_id": account["_id"], "campaign_id": campaign_id,
                                "action": action, "mode": mode})
    try:
        t = action["type"]
        if t == "set_budget":
            platform.set_budget(account, campaign_id, action["new"])
        elif t == "pause":
            platform.pause(account, campaign_id)
        elif t == "resume":
            platform.resume(account, campaign_id)
        elif t == "set_targeting":
            platform.set_targeting(account, campaign_id, action["new"])
        elif t == "pause_keyword":
            platform.pause_keyword(account, campaign_id)
        elif t == "resume_keyword":
            platform.resume_keyword(account, campaign_id)
        elif t == "reply_review":
            platform.reply_review(account, campaign_id, action["new"])
        elif t == "add_negative":
            platform.add_negative(account, campaign_id)
        elif t == "remove_negative":
            platform.remove_negative(account, campaign_id)
        elif t in ("fix_utm", "set_frequency_cap"):
            platform.set_item_field(account, campaign_id, action["field"], action["new"])
        elif t == "sync_exclusions":
            platform.sync_now(account, campaign_id)
        else:
            raise ValueError(f"unsupported action {t}")
        if t in ITEM_ACTIONS:
            verified, observed = platform.verify_item(campaign_id, action)
        else:
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
            reverts=None, extra_ctx: dict | None = None) -> dict:
    """check, then perform if allowed. Returns {effect, policy_id, why, action_id, result?}."""
    action_id = action_id or new_action_id()
    ctx = action_context(action, campaign, account)
    ctx.update(extra_ctx or {})
    effect, pid, why = check(db, actor_id, ctx, action_id=action_id, run_id=run_id,
                             on_behalf_of=on_behalf_of)
    out = {"effect": effect, "policy_id": pid, "why": why, "action_id": action_id}
    if effect == "allow":
        out["result"] = perform(db, platform, account, campaign["campaign_id"], action, reason,
                                actor_id=actor_id, action_id=action_id, mode=mode, run_id=run_id,
                                approval=approval, reverts=reverts, policy_id=pid)
    return out

"""Policy engine. Policies are documents in the `policies` collection.

A policy matches when the actor has one of its roles, the action type is in
its actions, and every condition in `when` holds. Lowest priority number
wins. Nothing matched means deny.

Effects: "allow" | "require_approval" | "deny"
"""

from __future__ import annotations

DEFAULT_POLICIES = [
    {"_id": "deny-writes-to-store", "priority": 5, "roles": ["*"],
     "actions": ["set_budget", "pause", "resume"], "when": {"platform": "shopify"}, "effect": "deny",
     "why": "Shopify is a read only sales source. Nobody changes the store from here."},
    {"_id": "housing-special-ad-category", "priority": 8, "roles": ["*"], "actions": ["set_targeting"],
     "when": {"industry": "Real estate"}, "effect": "deny",
     "why": "Housing ads are a Special Ad Category on Meta and Google: no age, gender or zip code "
            "targeting. Breaking this can get the client's ad account restricted."},
    {"_id": "deny-protected-campaigns", "priority": 10, "roles": ["*"], "actions": ["*"],
     "when": {"campaign_tag": "protected"}, "effect": "deny",
     "why": "Campaigns tagged protected are hands off for every actor."},
    {"_id": "deny-budget-over-200", "priority": 20, "roles": ["*"], "actions": ["set_budget"],
     "when": {"new_budget_gt": 200}, "effect": "deny",
     "why": "No campaign may go above $200 a day without changing this policy."},
    {"_id": "health-targeting-review", "priority": 30, "roles": ["agent"], "actions": ["set_targeting"],
     "when": {"industry": "Medical"}, "effect": "require_approval",
     "why": "Health ads need a human compliance review before any targeting change."},
    {"_id": "memory-says-be-careful", "priority": 85, "roles": ["agent"], "actions": ["set_budget"],
     "when": {"memory_warning": True}, "effect": "require_approval",
     "why": "Similar past moves for this client made results worse, so a human decides this time."},
    {"_id": "reddit-agent-asks-first", "priority": 90, "roles": ["agent"], "actions": ["set_budget"],
     "when": {"platform": "reddit"}, "effect": "require_approval",
     "why": "Reddit is a new channel for these clients, so every agent move there needs a human."},
    {"_id": "agent-small-budget-moves", "priority": 100, "roles": ["agent"], "actions": ["set_budget"],
     "when": {"change_pct_lte": 10}, "effect": "allow",
     "why": "The agent may move a budget by 10% or less on its own."},
    {"_id": "agent-medium-budget-moves", "priority": 110, "roles": ["agent"], "actions": ["set_budget"],
     "when": {"change_pct_lte": 15}, "effect": "require_approval",
     "why": "Moves between 10% and 15% need a human approver."},
    {"_id": "agent-pause-needs-human", "priority": 120, "roles": ["agent"], "actions": ["pause"],
     "when": {}, "effect": "require_approval",
     "why": "The agent may only propose pauses."},
    {"_id": "agent-targeting-needs-human", "priority": 130, "roles": ["agent"], "actions": ["set_targeting"],
     "when": {}, "effect": "require_approval",
     "why": "The agent may only propose audience targeting changes."},
    {"_id": "approver-can-approve", "priority": 200, "roles": ["approver"], "actions": ["approve", "reject", "forget_memory"],
     "when": {}, "effect": "allow", "why": "Approvers decide on proposals and can correct the agent's memory."},
    {"_id": "approver-operations", "priority": 210, "roles": ["approver"],
     "actions": ["set_budget"],
     "when": {"change_pct_lte": 50}, "effect": "allow",
     "why": "Approvers may carry out budget changes up to 50%, including undo."},
    {"_id": "approver-pause-resume", "priority": 215, "roles": ["approver"],
     "actions": ["pause", "resume"], "when": {}, "effect": "allow",
     "why": "Approvers may pause and resume campaigns, including undoing a pause."},
    {"_id": "approver-targeting", "priority": 216, "roles": ["approver"], "actions": ["set_targeting"],
     "when": {}, "effect": "allow", "why": "Approvers may change targeting, inside the compliance rules above."},
    {"_id": "breaker-can-halt", "priority": 225, "roles": ["breaker"], "actions": ["halt"],
     "when": {}, "effect": "allow",
     "why": "The circuit breaker may stop a client on its own, but never restart one. Only a human can."},
    {"_id": "admin-kill-switch", "priority": 220, "roles": ["admin", "approver"],
     "actions": ["halt", "unhalt"], "when": {}, "effect": "allow",
     "why": "Admins and approvers can stop and restart the agent."},
]

DEFAULT_DENY = ("deny", "default-deny", "No policy allows this action for this actor.")


def _when_holds(when: dict, ctx: dict) -> bool:
    for key, val in (when or {}).items():
        if key == "change_pct_lte":
            if ctx.get("change_pct") is None or ctx["change_pct"] > val + 1e-9:
                return False
        elif key == "new_budget_gt":
            if ctx.get("new_budget") is None or not ctx["new_budget"] > val:
                return False
        elif key == "campaign_tag":
            if val not in (ctx.get("campaign_tags") or []):
                return False
        elif key in ("account_id", "client_id", "platform", "industry", "memory_warning"):
            if ctx.get(key) != val:
                return False
        else:
            return False  # unknown condition: fail closed
    return True


def evaluate(policies: list[dict], roles: list[str], ctx: dict) -> tuple[str, str, str]:
    """Returns (effect, policy_id, why)."""
    for p in sorted(policies, key=lambda p: p.get("priority", 1000)):
        if p.get("active") is False:
            continue
        if "*" not in p["roles"] and not set(p["roles"]) & set(roles):
            continue
        if "*" not in p["actions"] and ctx["action_type"] not in p["actions"]:
            continue
        if _when_holds(p.get("when", {}), ctx):
            return p["effect"], p["_id"], p.get("why", "")
    return DEFAULT_DENY


def action_context(action: dict, campaign: dict | None, account: dict) -> dict:
    old = action.get("old")
    new = action.get("new")
    if action["type"] == "set_budget" and old:
        pct = round(abs(new - old) / old * 100, 2)
    elif action["type"] == "pause":
        pct = 100.0
    else:
        pct = 0.0
    return {
        "action_type": action["type"], "change_pct": pct,
        "old_budget": old, "new_budget": new if action["type"] == "set_budget" else None,
        "campaign_tags": (campaign or {}).get("tags", []),
        "campaign_id": (campaign or {}).get("campaign_id"), "account_id": account.get("_id"),
        "client_id": account.get("client_id"), "platform": account.get("platform"),
        "industry": account.get("industry"),
    }


def roles_for(actor: dict, client_id: str | None) -> list[str]:
    """Global roles plus roles granted for this one client. A grant for one
    client never applies to another, which is what keeps tenants apart."""
    roles = set(actor.get("roles", []))
    for g in actor.get("grants", []):
        if client_id and g.get("client_id") == client_id:
            roles |= set(g.get("roles", []))
    return sorted(roles)

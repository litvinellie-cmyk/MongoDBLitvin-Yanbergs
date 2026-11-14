from policy import DEFAULT_POLICIES as P, action_context, evaluate, roles_for


def ctx(type_="set_budget", old=100.0, new=110.0, tags=(), platform="meta"):
    return action_context({"type": type_, "old": old, "new": new},
                          {"campaign_id": "c", "tags": list(tags)},
                          {"_id": "acct", "client_id": "harbour", "platform": platform})


def test_agent_small_move_allowed():
    assert evaluate(P, ["agent"], ctx(new=110))[:2] == ("allow", "agent-small-budget-moves")


def test_agent_medium_move_needs_approval():
    assert evaluate(P, ["agent"], ctx(new=85))[0] == "require_approval"


def test_agent_big_move_denied():
    assert evaluate(P, ["agent"], ctx(new=130))[:2] == ("deny", "default-deny")


def test_protected_denied_for_everyone():
    for roles in (["agent"], ["approver"], ["admin"]):
        assert evaluate(P, roles, ctx(tags=["protected"]))[:2] == ("deny", "deny-protected-campaigns")


def test_budget_ceiling():
    assert evaluate(P, ["approver"], ctx(old=190, new=210))[1] == "deny-budget-over-200"


def test_agent_pause_needs_approval_and_approver_can_pause():
    assert evaluate(P, ["agent"], ctx("pause", new=None))[0] == "require_approval"
    assert evaluate(P, ["approver"], ctx("pause", new=None))[0] == "allow"


def test_viewer_cannot_approve_or_halt():
    assert evaluate(P, ["viewer"], {"action_type": "approve"})[0] == "deny"
    assert evaluate(P, ["viewer"], {"action_type": "halt"})[0] == "deny"
    assert evaluate(P, ["approver"], {"action_type": "approve"})[0] == "allow"


def test_inactive_policy_skipped():
    off = [{**p, "active": False} if p["_id"] == "agent-small-budget-moves" else p for p in P]
    assert evaluate(off, ["agent"], ctx(new=110))[0] == "require_approval"


def test_unknown_condition_fails_closed():
    pol = [{"_id": "x", "priority": 1, "roles": ["*"], "actions": ["*"],
            "when": {"mystery": 1}, "effect": "allow"}]
    assert evaluate(pol, ["agent"], ctx())[0] == "deny"


def test_reddit_agent_always_asks():
    assert evaluate(P, ["agent"], ctx(new=105, platform="reddit"))[:2] == ("require_approval", "reddit-agent-asks-first")


def test_store_is_read_only():
    assert evaluate(P, ["admin", "approver"], ctx(platform="shopify"))[:2] == ("deny", "deny-writes-to-store")


def test_client_grant_only_applies_to_that_client():
    owner = {"roles": [], "grants": [{"client_id": "harbour", "roles": ["approver"]}]}
    assert roles_for(owner, "harbour") == ["approver"]
    assert roles_for(owner, "brightsmile") == []
    assert evaluate(P, roles_for(owner, "brightsmile"), {"action_type": "approve"})[0] == "deny"


def test_senior_dollar_limit():
    small = {"action_type": "approve", "change_usd": 20}
    big = {"action_type": "approve", "change_usd": 40}
    assert evaluate(P, ["senior"], small)[0] == "allow"
    assert evaluate(P, ["senior"], big)[0] == "deny"
    assert evaluate(P, ["approver"], big)[0] == "allow"


def test_junior_and_client_cannot_approve():
    for roles in (["junior"], ["client"], ["viewer"]):
        assert evaluate(P, roles, {"action_type": "approve", "change_usd": 1})[0] == "deny"

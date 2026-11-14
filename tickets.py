"""Real actions on a second system: GitHub Issues as the agency's approval board.

Every request that needs a person opens a real GitHub issue; every decision
comments on it and closes it; the circuit breaker opens an incident issue.
Each step goes through the same policy gate (identity + kill switch + policy),
is verified by re-reading GitHub, and is written to the Atlas audit trail.

Needs GITHUB_TOKEN and GITHUB_REPO in .env. Without them it does nothing.
GitHub failures are logged and never break the agent.
"""

from __future__ import annotations

import json
import os
import urllib.request

import gate

from concurrent.futures import ThreadPoolExecutor

PREFIX = "[Agent]"
# Tickets open in parallel in the background so a slow GitHub never slows the agent.
_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="tickets")
TICKET_TYPES = {"set_budget", "pause"}  # keep the board readable: money moves only


def _cfg():
    from store import load_env
    load_env()
    return os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPO")


def enabled() -> bool:
    t, r = _cfg()
    return bool(t and r)


def _api(method: str, path: str, body: dict | None = None):
    token, repo = _cfg()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ad-agent-audit"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def _act(db, actor_id: str, action_type: str, ctx_extra: dict, do, verify, audit_fields: dict) -> dict:
    """Gate -> send -> verify -> audit, for an action on GitHub."""
    ctx = {"action_type": action_type, "system": "github", **ctx_extra}
    effect, pid, why = gate.check(db, actor_id, ctx, action_id=audit_fields.get("action_id"))
    if effect != "allow":
        return {"ok": False, "error": f"denied by {pid}: {why}"}
    gate.flush(db)
    gate.audit(db, "action_sent", actor_id=actor_id, system="github", action={"type": action_type},
               **audit_fields)
    try:
        res = do()
        ok, observed = verify(res)
        out = {"ok": True, "verified": ok, "observed": observed, "url": res.get("html_url"),
               "number": res.get("number")}
    except Exception as e:
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    gate.audit(db, "action_result", actor_id=actor_id, system="github", result=out, **audit_fields)
    return out


def _money(x):
    return "n/a" if x is None else f"${x:,.2f}"


def open_for_proposal(db, proposal: dict):
    a = proposal["action"]
    if not enabled() or a["type"] not in TICKET_TYPES:
        return None
    return _POOL.submit(_open_for_proposal, db, proposal)


def _open_for_proposal(db, proposal: dict) -> dict | None:
    a = proposal["action"]
    client = (db.clients.find_one({"_id": proposal.get("client_id")}) or {}).get("name", proposal.get("client_id"))
    camp = proposal["campaign_id"].split(":")[-1]
    change = (f"Daily budget {_money(a.get('old'))} → {_money(a.get('new'))}" if a["type"] == "set_budget"
              else f"Pause campaign (now {_money(a.get('old'))}/day)")
    ev = (proposal.get("reason") or {}).get("evidence") or {}
    lines = [
        f"**Client:** {client}  ",
        f"**Platform:** {proposal.get('platform')}  ",
        f"**Campaign:** {camp}  ",
        f"**Change:** {change}  ",
        f"**Requested by:** `{proposal.get('requested_by')}`  ",
        f"**Why a person must decide:** {proposal.get('policy_why') or proposal.get('policy_id')}",
        "", "### Agent's reason", (proposal.get("reason") or {}).get("text", ""),
    ]
    if ev.get("confidence_text"):
        lines += ["", "### Evidence", f"- {ev['confidence_text']}",
                  f"- Expected impact: {(ev.get('impact') or {}).get('summary', 'n/a')}",
                  f"- Risk: {ev.get('risk')}", f"- Tracking: {ev.get('tracking')}"]
    lines += ["", "### Decide here or in the dashboard",
              "Comment `/approve your reason` or `/reject your reason`. Your GitHub account must be linked to someone on "
              "the agency team; the same rules apply as in the dashboard (owner approves anything, senior up to $25 a day, "
              "junior can't). The agent carries it out, verifies it, and closes this issue.",
              "", f"Approval ID `{proposal.get('action_id')}`. Recorded in the MongoDB Atlas audit trail.",
              "", "_Hackathon demo: ad accounts are simulated, this ticket is real._"]
    title = f"{PREFIX} Approval needed: {client} / {camp} ({'pause' if a['type'] == 'pause' else 'budget'})"
    res = _act(db, proposal["requested_by"], "open_ticket",
               {"client_id": proposal.get("client_id"), "campaign_id": proposal["campaign_id"]},
               lambda: _api("POST", "/issues", {"title": title, "body": "\n".join(lines)}),
               lambda r: (_api("GET", f"/issues/{r['number']}").get("state") == "open", {"state": "open"}),
               {"action_id": proposal.get("action_id"), "client_id": proposal.get("client_id"),
                "campaign_id": proposal["campaign_id"]})
    if res.get("ok"):
        db.proposals.update_one({"_id": proposal["_id"]},
                                {"$set": {"ticket": {"number": res["number"], "url": res["url"]}}})
    return res


def close_for_proposal(db, proposal: dict, decision: str, actor_id: str, comment: str):
    if not enabled():
        return None
    return _POOL.submit(_close_for_proposal, db, proposal, decision, actor_id, comment)


def _close_for_proposal(db, proposal: dict, decision: str, actor_id: str, comment: str) -> dict | None:
    t = proposal.get("ticket")
    if not t and enabled() and proposal["action"]["type"] in TICKET_TYPES:
        import time
        for _ in range(10):  # ticket may still be opening in the background
            time.sleep(0.5)
            t = (db.proposals.find_one({"_id": proposal["_id"]}, {"ticket": 1}) or {}).get("ticket")
            if t:
                break
    if not enabled() or not t:
        return None
    n = t["number"]
    verb = "Approved" if decision == "approve" else "Rejected"

    def do():
        _api("POST", f"/issues/{n}/comments", {"body": f"**{verb}** by `{actor_id}`: \"{comment}\"\n\n"
                                                        f"Decision checked against policy and recorded in Atlas."})
        return _api("PATCH", f"/issues/{n}", {"state": "closed",
                                              "state_reason": "completed" if decision == "approve" else "not_planned"})

    return _act(db, actor_id, "close_ticket",
                {"client_id": proposal.get("client_id"), "campaign_id": proposal["campaign_id"]}, do,
                lambda r: (_api("GET", f"/issues/{n}").get("state") == "closed", {"state": "closed"}),
                {"action_id": proposal.get("action_id"), "client_id": proposal.get("client_id"),
                 "campaign_id": proposal["campaign_id"]})


def open_incident(db, actor_id: str, client_id: str, reason: str):
    if not enabled():
        return None
    return _POOL.submit(_open_incident, db, actor_id, client_id, reason)


def _open_incident(db, actor_id: str, client_id: str, reason: str) -> dict | None:
    client = (db.clients.find_one({"_id": client_id}) or {}).get("name", client_id)
    body = (f"**Client halted:** {client}  \n**By:** `{actor_id}`\n\n{reason}\n\n"
            f"All agent actions for this client are blocked until an agency owner resumes it in the dashboard. "
            f"Recorded in the MongoDB Atlas audit trail.\n\n_Hackathon demo: ad accounts are simulated, this ticket is real._")
    res = _act(db, actor_id, "open_ticket", {"client_id": client_id},
               lambda: _api("POST", "/issues", {"title": f"{PREFIX} Incident: spend spike, {client} halted", "body": body}),
               lambda r: (_api("GET", f"/issues/{r['number']}").get("state") == "open", {"state": "open"}),
               {"client_id": client_id})
    if res.get("ok"):
        db.controls.update_one({"_id": f"client:{client_id}"}, {"$set": {"incident": {"number": res["number"], "url": res["url"]}}})
    return res


def close_all_open(note: str = "Demo reset.") -> int:
    """Close leftover agent tickets so each demo starts clean."""
    if not enabled():
        return 0
    n = 0
    page = _api("GET", "/issues?state=open&per_page=100")
    for i in page:
        if i.get("title", "").startswith(PREFIX) and "pull_request" not in i:
            try:
                _api("POST", f"/issues/{i['number']}/comments", {"body": note})
                _api("PATCH", f"/issues/{i['number']}", {"state": "closed", "state_reason": "not_planned"})
                n += 1
            except Exception:
                pass
    return n


# ---------------------------------------------------------------- decisions made on GitHub

import re as _re

COMMAND = _re.compile(r"^\s*/(approve|reject)\b\s*(.*)", _re.I | _re.S)


def sync_github_decisions(db, platforms) -> list[dict]:
    """Read /approve and /reject comments on open agent tickets and carry them out.

    The GitHub login is mapped to an actor (actors.github_login). The decision
    then goes through the exact same path as the dashboard: identity, policy
    (owner, senior limits, junior denied), required reason, execution through
    the gate, verification, audit. Unknown GitHub users are refused and logged.
    """
    import ops
    if not enabled():
        return []
    out = []
    for p in db.proposals.find({"status": "pending", "ticket": {"$exists": True}}):
        n = p["ticket"]["number"]
        seen = set(p.get("github_comments_seen", []))
        try:
            comments = _api("GET", f"/issues/{n}/comments?per_page=50")
        except Exception as e:
            out.append({"ticket": n, "error": str(e)})
            continue
        for c in comments:
            if c["id"] in seen:
                continue
            db.proposals.update_one({"_id": p["_id"]}, {"$addToSet": {"github_comments_seen": c["id"]}})
            m = COMMAND.match(c.get("body") or "")
            if not m:
                continue
            login = c["user"]["login"]
            actor = db.actors.find_one({"github_login": {"$regex": f"^{_re.escape(login)}$", "$options": "i"}})
            decision = "approve" if m.group(1).lower() == "approve" else "reject"
            reason = m.group(2).strip() or ""
            if not actor:
                gate.audit(db, "github_decision_refused", actor_id=f"github:{login}", system="github",
                           action_id=p.get("action_id"), client_id=p.get("client_id"), campaign_id=p["campaign_id"],
                           reason="GitHub user is not linked to anyone on the agency team.")
                try:
                    _api("POST", f"/issues/{n}/comments", {"body": f"`{login}` is not linked to anyone on the agency "
                                                                    f"team, so this decision was refused and logged."})
                except Exception:
                    pass
                out.append({"ticket": n, "login": login, "result": "refused"})
                continue
            gate.audit(db, "github_decision_received", actor_id=actor["_id"], system="github",
                       action_id=p.get("action_id"), client_id=p.get("client_id"), campaign_id=p["campaign_id"],
                       decision=decision, comment=reason, github_login=login, comment_url=c.get("html_url"))
            res = ops.decide_proposal(db, platforms, p["_id"], decision, actor["_id"],
                                      (reason + " (via GitHub)") if reason else "")
            if not res.get("ok") and res.get("error"):
                try:
                    _api("POST", f"/issues/{n}/comments", {"body": f"Not carried out for `{actor['_id']}`: {res['error']}"})
                except Exception:
                    pass
            out.append({"ticket": n, "actor": actor["_id"], "decision": decision, "result": res.get("status") or res.get("error")})
            break  # one decision per ticket
    return out

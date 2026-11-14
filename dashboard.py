"""Demo dashboard. Standard library server, no extra installs.

  python dashboard.py        # http://localhost:8000

"Acting as" picks the identity for every button. This is a demo switcher,
not real login: every request still goes through the same policy checks.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import memory
import ops
import nl_rules
import report
import growth
import tickets
import timemachine
import voice
import tracking
from datetime import datetime
import skills
from evidence import kpis
from platforms import PLATFORM_LABELS, build_platforms, utcnow
from rules import merged_settings
from store import campaign_stats, day_str, get_db
from worker import run_once

import threading
RUN_LOCK = threading.Lock()  # never two agent runs at once
DB = get_db()
PLATFORMS = build_platforms(DB)
PAGE = (Path(__file__).parent / "dashboard.html").read_bytes()


def state(client: str = "all") -> dict:
    """client="all" is the agency view; a client id shows that client only."""
    clients = list(DB.clients.find().sort("name", 1))
    names = {c["_id"]: c["name"] for c in clients}
    acc_q = {"active": True} if client == "all" else {"active": True, "client_id": client}
    accounts = list(DB.accounts.find(acc_q).sort("_id", 1))
    acc_ids = [a["_id"] for a in accounts]
    now = utcnow()
    campaigns = []
    settings = {d["_id"]: merged_settings(d) for d in DB.settings.find({"_id": {"$in": acc_ids}})}
    window = merged_settings(None)["window_days"]
    stats_all = {r["campaign_id"]: r for r in campaign_stats(
        DB, acc_ids, day_str(now - timedelta(days=window - 1)), day_str(now))}
    camps_by_acc = {}
    for c in DB.sandbox_campaigns.find({"account_id": {"$in": acc_ids}}).sort("_id", 1):
        camps_by_acc.setdefault(c["account_id"], []).append(c)
    for acc in accounts:
        s = settings.get(acc["_id"]) or merged_settings(None)
        stats = stats_all
        for c in camps_by_acc.get(acc["_id"], []):
            st = stats.get(c["_id"], {})
            conv = st.get("conversions", 0)
            k = kpis(st) if st else kpis({})
            campaigns.append({
                "kpis": k, "last_ts": st.get("last_ts"),
                "id": c["_id"], "name": c["name"], "status": c["status"], "tags": c.get("tags", []),
                "client": names.get(acc["client_id"], acc["client_id"]), "platform": acc["platform"],
                "platform_label": PLATFORM_LABELS.get(acc["platform"], acc["platform"]),
                "daily_budget": c["daily_budget"], "spend": round(st.get("spend", 0), 2),
                "conversions": conv, "cpl": round(st["spend"] / conv, 2) if conv else None,
                "target_cpl": s["target_cpl"],
            })
    scoped = {} if client == "all" else {"client_id": client}
    ev_q = {} if client == "all" else {"client_id": {"$in": [client, None]}}
    halts = {c["_id"]: c for c in DB.controls.find({"halted": True})}
    connections = [{"client": names.get(a["client_id"]), "platform": a["platform"],
                    "label": PLATFORM_LABELS.get(a["platform"], a["platform"]), "name": a["name"],
                    "mode": "read only" if a["platform"] == "shopify" else "sandbox data"}
                   for a in accounts]
    return {
        "client": client, "clients": clients,
        "halted_global": "global" in halts,
        "halted_client": client != "all" and f"client:{client}" in halts,
        "halted_clients": [k.split(":", 1)[1] for k in halts if k.startswith("client:")],
        "incidents": {k.split(":", 1)[1]: c["incident"] for k, c in halts.items()
                      if k.startswith("client:") and c.get("incident")},
        "actors": list(DB.actors.find({}, {"name": 1, "roles": 1, "kind": 1, "grants": 1})),
        "campaigns": campaigns, "connections": connections,
        "proposals": list(DB.proposals.find({"status": "pending", **scoped}).sort("created_at", 1)),
        "changes": mark_undone(list(DB.change_log.find(
            {} if client == "all" else {"account_id": {"$in": acc_ids}}).sort("applied_at", -1).limit(20))),
        "events": list(DB.audit_events.find(ev_q).sort([("at", -1), ("_id", -1)]).limit(80)),
        "policies": list(DB.policies.find().sort("priority", 1)),
        "accuracy": accuracy(scoped),
        "memories": list(DB.memories.find(scoped, {"embedding": 0}).sort("created_at", -1).limit(30)),
        "breaker_events": list(DB.audit_events.find({"event": "circuit_breaker", **scoped})
                               .sort("at", -1).limit(3)),
        "pending_by_client": {r["_id"]: r["n"] for r in DB.proposals.aggregate([
            {"$match": {"status": "pending"}}, {"$group": {"_id": "$client_id", "n": {"$sum": 1}}}])},
    }


def mark_undone(changes: list[dict]) -> list[dict]:
    ids = [str(c["_id"]) for c in changes]
    undone = {c["reverts"] for c in DB.change_log.find({"reverts": {"$in": ids}, "result.ok": True}, {"reverts": 1})}
    for c in changes:
        c["undone"] = str(c["_id"]) in undone
    return changes


def accuracy(scoped: dict) -> dict:
    """How often past agent decisions improved results (from measured outcomes)."""
    counts = {r["_id"]: r["n"] for r in DB.memories.aggregate([
        {"$match": {**scoped, "verdict": {"$in": ["better", "worse"]}}},
        {"$group": {"_id": "$verdict", "n": {"$sum": 1}}}])}
    better, worse = counts.get("better", 0), counts.get("worse", 0)
    n = better + worse
    return {"better": better, "worse": worse, "pct": round(better / n * 100) if n else None}


def export_csv(client: str) -> bytes:
    import csv
    import io
    q = {} if client == "all" else {"client_id": client}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "client", "event", "actor", "campaign", "action", "effect", "policy", "detail"])
    for e in DB.audit_events.find(q).sort([("at", 1), ("_id", 1)]):
        detail = e.get("why") or e.get("reason") or e.get("summary") or e.get("comment") or ""
        if e.get("result"):
            detail = str(e["result"])
        w.writerow([e["at"].isoformat(), e.get("client_id", ""), e["event"], e.get("actor_id", ""),
                    e.get("campaign_id", ""), e.get("action_type") or (e.get("action") or {}).get("type", ""),
                    e.get("effect", ""), e.get("policy_id", ""), detail])
    return buf.getvalue().encode()


def channels_state(cid: str) -> dict:
    import channels
    if cid == "all":
        return {"needs_client": True}
    client = DB.clients.find_one({"_id": cid}) or {}
    revs = list(DB.reviews.find({"client_id": cid}).sort("at", -1))
    for r in revs:
        r["_id"] = str(r["_id"])
    return {"competitors": channels.competitors(DB, cid), "ai": channels.ai_visibility(DB, cid),
            "seo": channels.seo_overlap(DB, cid), "reviews": revs,
            "creatives": channels.creatives(DB, cid, client.get("industry", "")),
            "tracking": channels.tracking_health(DB, cid)}


def skills_state(cid: str) -> dict:
    if cid == "all":
        return {"needs_client": True}
    return {"marginal": skills.marginal_returns(DB, cid), "inventory": skills.inventory(DB, cid),
            "terms": skills.search_terms(DB, cid), "funnel": skills.funnel(cid),
            "experiments": skills.experiments(DB, cid), "anomalies": skills.anomalies(DB, cid)}


def spike(client_id):
    """Demo: one campaign starts burning money 4x faster, then the agent runs."""
    if not client_id:
        return {"ok": False, "error": "Pick one client first."}
    acc_ids = [a["_id"] for a in DB.accounts.find({"client_id": client_id, "platform": {"$ne": "shopify"}})]
    camp = DB.sandbox_campaigns.find_one({"account_id": {"$in": acc_ids}, "status": "ACTIVE"},
                                         sort=[("daily_budget", -1)])
    if not camp:
        return {"ok": False, "error": "No active campaign to spike."}
    PLATFORMS["sandbox"].simulate_spike(camp["_id"], 4.0, utcnow())
    out = run_once(DB, PLATFORMS)
    return {"ok": True, "spiked": camp["_id"], **out}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":  # read fresh each time so page updates need no restart
            return self._send(200, (Path(__file__).parent / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
        if u.path == "/api/state":
            return self._send(200, state(parse_qs(u.query).get("client", ["all"])[0]))
        if u.path == "/report":
            cid = parse_qs(u.query).get("client", [""])[0]
            return self._send(200, report.render(DB, cid).encode(), "text/html; charset=utf-8")
        if u.path == "/api/export":
            cid = parse_qs(u.query).get("client", ["all"])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Disposition", f'attachment; filename="decision-log-{cid}.csv"')
            self.end_headers()
            self.wfile.write(export_csv(cid))
            return
        if u.path == "/api/tm/bounds":
            return self._send(200, timemachine.bounds(DB, parse_qs(u.query).get("client", ["all"])[0]))
        if u.path == "/api/tm":
            q = parse_qs(u.query)
            t = datetime.fromisoformat(q.get("t", [""])[0].replace("Z", "+00:00"))
            return self._send(200, timemachine.state_at(DB, q.get("client", ["all"])[0], t))
        if u.path == "/api/live":
            since = datetime.fromisoformat(parse_qs(u.query).get("since", [""])[0].replace("Z", "+00:00"))
            evs = list(DB.audit_events.find({"at": {"$gt": since}},
                                            {"event": 1, "at": 1, "actor_id": 1, "campaign_id": 1, "effect": 1,
                                             "policy_id": 1, "action_type": 1, "system": 1, "result.ok": 1,
                                             "result.verified": 1, "result.number": 1, "action.type": 1,
                                             "summary": 1, "client_id": 1}).sort([("at", 1), ("_id", 1)]).limit(400))
            return self._send(200, {"events": evs})
        if u.path == "/api/briefing":
            from store import load_env
            load_env()
            cid = parse_qs(u.query).get("client", ["all"])[0]
            return self._send(200, {"text": voice.briefing_text(DB, cid),
                                    "elevenlabs": bool(os.environ.get("ELEVENLABS_API_KEY"))})
        if u.path == "/api/briefing.mp3":
            cid = parse_qs(u.query).get("client", ["all"])[0]
            try:
                audio = voice.speak(voice.briefing_text(DB, cid))
            except Exception as e:
                return self._send(502, {"error": f"ElevenLabs: {e}"})
            if not audio:
                return self._send(404, {"error": "No ElevenLabs key set."})
            import gate
            gate.audit(DB, "briefing_spoken", actor_id="agent:budget-bot", client_id=None if cid == "all" else cid,
                       system="elevenlabs")
            return self._send(200, audio, "audio/mpeg")
        if u.path == "/api/growth":
            cid = parse_qs(u.query).get("client", ["all"])[0]
            if cid == "all":
                return self._send(200, {"needs_client": True, "goals": {c["_id"]: growth.goal_status(DB, c["_id"])
                                                                        for c in DB.clients.find()}})
            comps = sorted({a["competitor"] for a in DB.competitor_ads.find({"client_id": cid}, {"competitor": 1})})
            client = DB.clients.find_one({"_id": cid}) or {}
            return self._send(200, {"analyze": growth.analyze(DB, cid), "goal": growth.goal_status(DB, cid),
                                    "trust": growth.trust(DB, cid), "aeo": growth.aeo_fixes(DB, cid),
                                    "libraries": [{"name": n, "links": growth.ad_library_links(n)}
                                                  for n in [client.get("name", cid)] + comps],
                                    "plan": DB.plans.find_one({"client_id": cid}, {"_id": 0})})
        if u.path == "/api/tracking":
            cid = parse_qs(u.query).get("client", ["all"])[0]
            if cid == "all":
                return self._send(200, {"needs_client": True})
            return self._send(200, {"utm": tracking.utm_audit(DB, cid), "capi": tracking.capi(DB, cid),
                                    "ladder": tracking.ladder(DB, cid), "syncs": tracking.syncs(DB, cid)})
        if u.path == "/api/skills":
            cid = parse_qs(u.query).get("client", ["all"])[0]
            return self._send(200, skills_state(cid))
        if u.path == "/api/goal":
            q = parse_qs(u.query)
            return self._send(200, skills.goal_plan(DB, q.get("client", [""])[0], float(q.get("goal", ["0"])[0] or 0)))
        if u.path == "/api/audit-site":
            return self._send(200, skills.prospect_audit(parse_qs(u.query).get("url", [""])[0]))
        if u.path == "/api/channels":
            cid = parse_qs(u.query).get("client", ["all"])[0]
            return self._send(200, channels_state(cid))
        if u.path == "/api/explain":
            aid = parse_qs(u.query).get("id", [""])[0]
            return self._send(200, ops.explain(DB, aid))
        self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        actor = body.get("actor", "")
        try:
            p = urlparse(self.path).path
            if p in ("/api/run", "/api/spike") and not RUN_LOCK.acquire(blocking=False):
                return self._send(200, {"ok": False, "busy": True, "error": "The agent is already running. Wait for it to finish."})
            if p == "/api/run":
                try:
                    out = run_once(DB, PLATFORMS)
                finally:
                    RUN_LOCK.release()
            elif p == "/api/decide":
                out = ops.decide_proposal(DB, PLATFORMS, body["id"], body["decision"], actor,
                                          body.get("comment", ""))
            elif p == "/api/undo":
                out = ops.undo(DB, PLATFORMS, body["id"], actor)
            elif p == "/api/github/sync":
                out = {"ok": True, "decisions": tickets.sync_github_decisions(DB, PLATFORMS)}
            elif p == "/api/rules/draft":
                out = nl_rules.draft(DB, body.get("text", ""), actor)
            elif p == "/api/rules/decide":
                out = nl_rules.decide(DB, body["id"], actor, bool(body.get("activate")))
            elif p == "/api/setup/plan":
                plan = growth.setup_plan(body.get("name", "").strip() or "New business", body.get("industry", "Other"),
                                         float(body.get("budget") or 0), body.get("platforms") or [],
                                         body.get("website", ""), float(body["target"]) if body.get("target") else None)
                if body.get("website"):
                    plan["site_audit"] = skills.prospect_audit(body["website"])
                out = {"ok": True, "plan": plan}
            elif p == "/api/setup/create":
                out = growth.create_from_plan(DB, body["plan"], actor)
            elif p == "/api/goal/set":
                out = growth.set_goal(DB, body["client"], int(body.get("target") or 0), actor)
            elif p == "/api/spike":
                try:
                    out = spike(body.get("client"))
                finally:
                    RUN_LOCK.release()
            elif p == "/api/forget":
                out = memory.forget(DB, body["id"], actor)
            elif p == "/api/halt":
                out = ops.set_halt(DB, actor, bool(body["halted"]), body.get("reason", ""),
                                   body.get("client") or None)
            else:
                return self._send(404, {"error": "not found"})
            self._send(200, out)
        except Exception as e:
            self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})


def _github_poller():
    import time
    while True:
        try:
            res = tickets.sync_github_decisions(DB, PLATFORMS)
            for r in res:
                print(f"[github decision] {r}")
        except Exception as e:
            print(f"[github poll] {e}")
        time.sleep(20)


if __name__ == "__main__":
    threading.Thread(target=_github_poller, daemon=True).start()
    port = int(os.environ.get("PORT", "8000"))
    print(f"dashboard on http://localhost:{port}")
    ThreadingHTTPServer((os.environ.get("HOST", "127.0.0.1"), port), Handler).serve_forever()

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
import report
from evidence import kpis
from platforms import PLATFORM_LABELS, build_platforms, utcnow
from rules import merged_settings
from store import campaign_stats, day_str, get_db
from worker import run_once

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
    for acc in accounts:
        s = merged_settings(DB.settings.find_one({"_id": acc["_id"]}))
        stats = {r["campaign_id"]: r for r in campaign_stats(
            DB, acc["_id"], day_str(now - timedelta(days=s["window_days"] - 1)), day_str(now))}
        for c in DB.sandbox_campaigns.find({"account_id": acc["_id"]}).sort("_id", 1):
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
        "actors": list(DB.actors.find({}, {"name": 1, "roles": 1, "kind": 1, "grants": 1})),
        "campaigns": campaigns, "connections": connections,
        "proposals": list(DB.proposals.find({"status": "pending", **scoped}).sort("created_at", 1)),
        "changes": list(DB.change_log.find(
            {} if client == "all" else {"account_id": {"$in": acc_ids}}).sort("applied_at", -1).limit(20)),
        "events": list(DB.audit_events.find(ev_q).sort([("at", -1), ("_id", -1)]).limit(80)),
        "policies": list(DB.policies.find().sort("priority", 1)),
        "accuracy": accuracy(scoped),
        "memories": list(DB.memories.find(scoped, {"embedding": 0}).sort("created_at", -1).limit(30)),
        "breaker_events": list(DB.audit_events.find({"event": "circuit_breaker", **scoped})
                               .sort("at", -1).limit(3)),
        "pending_by_client": {c["_id"]: DB.proposals.count_documents({"status": "pending", "client_id": c["_id"]})
                              for c in clients},
    }


def accuracy(scoped: dict) -> dict:
    """How often past agent decisions improved results (from measured outcomes)."""
    better = DB.memories.count_documents({**scoped, "verdict": "better"})
    worse = DB.memories.count_documents({**scoped, "verdict": "worse"})
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
        if u.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
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
            if p == "/api/run":
                out = run_once(DB, PLATFORMS)
            elif p == "/api/decide":
                out = ops.decide_proposal(DB, PLATFORMS, body["id"], body["decision"], actor,
                                          body.get("comment", ""))
            elif p == "/api/undo":
                out = ops.undo(DB, PLATFORMS, body["id"], actor)
            elif p == "/api/spike":
                out = spike(body.get("client"))
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"dashboard on http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()

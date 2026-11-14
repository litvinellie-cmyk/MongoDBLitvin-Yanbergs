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

import ops
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
            campaigns.append({
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
        "pending_by_client": {c["_id"]: DB.proposals.count_documents({"status": "pending", "client_id": c["_id"]})
                              for c in clients},
    }


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
                out = ops.decide_proposal(DB, PLATFORMS, body["id"], body["decision"], actor)
            elif p == "/api/undo":
                out = ops.undo(DB, PLATFORMS, body["id"], actor)
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

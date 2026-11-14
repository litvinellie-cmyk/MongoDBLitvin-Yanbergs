"""The agent loop: pull, decide, ask policy, act or propose, audit everything.

  python worker.py seed        # actors, policies, sandbox account, 7 days of history
  python worker.py reset-demo  # wipe sandbox demo data and seed again (demo only)
  python worker.py once        # one pass
  python worker.py loop        # every INTERVAL_MINUTES (default 15)
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import timedelta

import gate
from ops import seed_identity_and_policy
from platforms import build_platforms, platform_for, utcnow
from rules import decide, merged_settings
from store import campaign_stats, day_str, edit_history, get_db, has_open_proposal, measure_outcomes


def run_once(db, platforms, now=None) -> dict:
    now = now or utcnow()
    run_id = "run_" + uuid.uuid4().hex[:8]
    counts = {"run_id": run_id, "snapshots": 0, "applied": 0, "proposed": 0, "denied": 0,
              "expired": 0, "outcomes": 0, "halted": False}

    if gate.halt_state(db, None):
        counts["halted"] = True
        gate.audit(db, "run_skipped", actor_id=gate.AGENT_ID, run_id=run_id, reason="kill switch on")
        return counts

    db.runs.insert_one({"_id": run_id, "actor_id": gate.AGENT_ID, "started_at": now, "status": "running"})
    gate.audit(db, "run_started", actor_id=gate.AGENT_ID, run_id=run_id)

    counts["expired"] = db.proposals.update_many(
        {"status": "pending", "expires_at": {"$lte": now}}, {"$set": {"status": "expired"}}
    ).modified_count

    for account in db.accounts.find({"active": True}):
        platform = platform_for(platforms, account)
        client_id = account.get("client_id")
        s = merged_settings(db.settings.find_one({"_id": account["_id"]}))

        try:
            live = platform.fetch_campaigns(account, now)
        except NotImplementedError:
            print(f"[{account['_id']}] {account['platform']} adapter not wired, skipped")
            continue
        if live:
            db.metrics_snapshots.insert_many([{
                "ts": now, "day": day_str(now),
                "meta": {"account_id": account["_id"], "campaign_id": c["campaign_id"],
                         "platform": account["platform"]},
                **{k: c[k] for k in ("status", "daily_budget", "spend", "conversions",
                                     "clicks", "impressions")},
            } for c in live])
            counts["snapshots"] += len(live)
        live_by_id = {c["campaign_id"]: c for c in live}
        budget_total = sum(c["daily_budget"] for c in live if c["status"] == "ACTIVE")

        first = day_str(now - timedelta(days=s["window_days"] - 1))
        for stats in campaign_stats(db, account["_id"], first, day_str(now)):
            # Kill switch is checked between every step, not only at the start.
            if gate.halt_state(db, account["_id"], client_id):
                counts["halted_accounts"] = counts.get("halted_accounts", 0) + 1
                break
            cid = stats["campaign_id"]
            if cid not in live_by_id or has_open_proposal(db, cid):
                continue
            camp = live_by_id[cid]
            stats.update(status=camp["status"], daily_budget=camp["daily_budget"])
            edits_today, last_edit = edit_history(db, cid, now)
            a = decide(stats, s, edits_today=edits_today, last_edit_at=last_edit,
                       now=now, account_budget_total=budget_total)
            if not a:
                continue
            reason = {"text": a.reason, "numbers": a.numbers}

            # Skip if this exact move was already denied today: no spamming the log.
            if db.audit_events.find_one({"event": "policy_check", "campaign_id": cid, "effect": "deny",
                                         "context.new_budget": a.new, "at": {"$gte": now - timedelta(hours=24)},
                                         "actor_id": gate.AGENT_ID}):
                continue

            out = gate.attempt(db, platform, account, camp, a.as_doc(), reason,
                               actor_id=gate.AGENT_ID, mode="auto", run_id=run_id)
            if out["effect"] == "allow":
                counts["applied"] += 1
                if out["result"]["ok"] and a.type == "set_budget":
                    budget_total += a.new - a.old
                print(f"[applied] {cid}: {a.reason} -> {out['result']}")
            elif out["effect"] == "require_approval":
                db.proposals.insert_one({
                    "action_id": out["action_id"], "client_id": client_id, "account_id": account["_id"],
                    "platform": account["platform"], "campaign_id": cid,
                    "action": a.as_doc(), "size_pct": a.size_pct, "reason": reason,
                    "requested_by": gate.AGENT_ID, "policy_id": out["policy_id"], "run_id": run_id,
                    "status": "pending", "created_at": now,
                    "expires_at": now + timedelta(hours=s["proposal_ttl_hours"]),
                })
                gate.audit(db, "proposal_created", actor_id=gate.AGENT_ID, action_id=out["action_id"],
                           client_id=client_id,
                           run_id=run_id, campaign_id=cid, policy_id=out["policy_id"])
                counts["proposed"] += 1
                print(f"[proposed] {cid}: {a.reason}")
            else:
                counts["denied"] += 1
                print(f"[denied by {out['policy_id']}] {cid}: {out['why']}")
        if gate.halt_state(db, None):  # global stop: no more accounts
            counts["halted"] = True
            break

    counts["outcomes"] = measure_outcomes(db, now)
    status = "halted" if counts["halted"] else "finished"
    db.runs.update_one({"_id": run_id, "status": "running"},
                       {"$set": {"status": status, "ended_at": utcnow(), "counts": counts}})
    gate.audit(db, "run_" + status, actor_id=gate.AGENT_ID, run_id=run_id, counts=counts)
    return counts


def seed(db, platforms, reset=False):
    from demo_data import CLIENTS
    if reset:  # demo only: wipes the demo collections
        for coll in ("proposals", "change_log", "audit_events", "runs", "controls", "clients",
                     "accounts", "settings", "sandbox_campaigns", "metrics_snapshots"):
            db[coll].delete_many({})
    seed_identity_and_policy(db)
    now = utcnow()
    for client in CLIENTS:
        db.clients.replace_one({"_id": client["_id"]},
                               {k: v for k, v in client.items() if k != "accounts"}, upsert=True)
        for acc in client["accounts"]:
            account = {"_id": acc["_id"], "client_id": client["_id"], "platform": acc["platform"],
                       "adapter": "sandbox", "external_id": acc["_id"], "name": acc["name"],
                       "active": True}
            platforms["sandbox"].seed_account(db, account, acc.get("settings", {}),
                                              acc.get("campaigns", []), backfill_days=7, now=now)
        print(f"seeded {client['name']} ({len(client['accounts'])} accounts)")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "once"
    db = get_db()
    platforms = build_platforms(db)
    if cmd == "seed":
        seed(db, platforms)
    elif cmd == "reset-demo":
        seed(db, platforms, reset=True)
    elif cmd == "once":
        print(run_once(db, platforms))
    elif cmd == "loop":
        interval = int(os.environ.get("INTERVAL_MINUTES", "15")) * 60
        while True:
            started = time.monotonic()
            try:
                print(utcnow().isoformat(), run_once(db, platforms))
            except Exception as e:
                print(f"run failed: {type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(max(5, interval - (time.monotonic() - started)))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()

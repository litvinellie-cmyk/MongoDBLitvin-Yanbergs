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
import evidence
import memory
import ops
from rules import decide, decide_targeting, merged_settings
from store import campaign_stats, day_str, edit_history, get_db, has_open_proposal, measure_outcomes


BREAKER_ID = "agent:circuit-breaker"


def check_pace(db, account, live, budget_total, s, now, run_id) -> bool:
    """Trip when today's spend is far ahead of where the daily budgets say it should be."""
    frac = (now.hour * 60 + now.minute) / 1440
    spent = sum(c["spend"] for c in live)
    expected = budget_total * frac
    if frac < 0.05 or spent < s["pace_min_spend"] or spent <= s["pace_limit"] * expected:
        return False
    # A human who resumed this client after the last trip today has seen it: don't re-trip.
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    last_trip = db.audit_events.find_one({"event": "circuit_breaker", "account_id": account["_id"],
                                          "at": {"$gte": start}}, sort=[("at", -1)])
    if last_trip and db.audit_events.find_one({"event": "kill_switch", "halted": False,
                                               "client_id": account.get("client_id"),
                                               "at": {"$gt": last_trip["at"]}}):
        return False
    ratio = spent / expected if expected else float("inf")
    worst = max(live, key=lambda c: c["spend"] - c["daily_budget"] * frac)
    reason = (f"{account['name']} spent ${spent:,.2f} so far today, {ratio:.1f}x the expected "
              f"${expected:,.2f} at this hour. Biggest jump: {worst['name']} "
              f"(${worst['spend']:,.2f} against a ${worst['daily_budget']:,.2f}/day budget).")
    gate.audit(db, "circuit_breaker", actor_id=BREAKER_ID, run_id=run_id, client_id=account.get("client_id"),
               account_id=account["_id"], campaign_id=worst["campaign_id"], reason=reason,
               numbers={"spent_today": round(spent, 2), "expected": round(expected, 2),
                        "ratio": round(ratio, 2), "limit": s["pace_limit"]})
    out = ops.set_halt(db, BREAKER_ID, True, "Circuit breaker: " + reason, account.get("client_id"))
    print(f"[circuit breaker] {reason} -> {out}")
    return bool(out.get("ok"))


def act(db, platform, account, camp, a, s, now, run_id, counts, ev=None):
    """Recall memory, ask the gate, then apply, queue or record the denial."""
    cid = camp["campaign_id"]
    client_id = account.get("client_id")
    reason = {"text": a.reason, "numbers": a.numbers, "evidence": ev}
    extra = {}
    if a.type in ("set_budget", "pause"):
        target = a.numbers.get("target_cpl") or 1
        recalled = memory.recall(
            db, client_id=client_id, platform=account["platform"], action_type=a.type,
            direction=memory.direction_of(a.as_doc()), size_pct=a.size_pct,
            cpl_ratio=round(a.numbers["cpl"] / target, 2) if a.numbers.get("cpl") else None)
        warn, summary = memory.warning(recalled)
        if recalled["hits"]:
            reason["memory"] = {"method": recalled["method"], "summary": summary, "warning": warn,
                                "hits": [{"text": h["text"], "verdict": h["verdict"],
                                          "id": str(h["_id"])} for h in recalled["hits"]]}
            reason["text"] += " " + summary
            gate.audit(db, "memory_recalled", actor_id=gate.AGENT_ID, run_id=run_id, client_id=client_id,
                       campaign_id=cid, method=recalled["method"], summary=summary, warning=warn,
                       hits=[h["text"] for h in recalled["hits"]])
        extra["memory_warning"] = warn

    # Skip if this exact move was already denied today: no spamming the log.
    if db.audit_events.find_one({"event": "policy_check", "campaign_id": cid, "effect": "deny",
                                 "action_type": a.type, "context.new_budget": a.new if a.type == "set_budget" else None,
                                 "at": {"$gte": now - timedelta(hours=24)}, "actor_id": gate.AGENT_ID}):
        return None

    out = gate.attempt(db, platform, account, camp, a.as_doc(), reason,
                       actor_id=gate.AGENT_ID, mode="auto", run_id=run_id, extra_ctx=extra)
    if out["effect"] == "allow":
        counts["applied"] += 1
        print(f"[applied] {cid}: {a.reason} -> {out['result']}")
    elif out["effect"] == "require_approval":
        db.proposals.insert_one({
            "action_id": out["action_id"], "client_id": client_id, "account_id": account["_id"],
            "platform": account["platform"], "campaign_id": cid,
            "action": a.as_doc(), "size_pct": a.size_pct, "reason": reason,
            "requested_by": gate.AGENT_ID, "policy_id": out["policy_id"], "policy_why": out["why"],
            "run_id": run_id, "status": "pending", "created_at": now,
            "expires_at": now + timedelta(hours=s["proposal_ttl_hours"]),
        })
        gate.audit(db, "proposal_created", actor_id=gate.AGENT_ID, action_id=out["action_id"],
                   client_id=client_id, run_id=run_id, campaign_id=cid, policy_id=out["policy_id"])
        counts["proposed"] += 1
        print(f"[proposed by {out['policy_id']}] {cid}: {a.reason}")
    else:
        counts["denied"] += 1
        print(f"[denied by {out['policy_id']}] {cid}: {out['why']}")
    return out


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
                **{k: c.get(k, 0) for k in ("status", "daily_budget", "spend", "conversions",
                                            "clicks", "impressions", "reach", "reported_conversions",
                                            "revenue")},
            } for c in live])
            counts["snapshots"] += len(live)
        live_by_id = {c["campaign_id"]: c for c in live}
        budget_total = sum(c["daily_budget"] for c in live if c["status"] == "ACTIVE")

        # Circuit breaker: spend running far ahead of today's pace halts the client.
        if live and not gate.halt_state(db, account["_id"], client_id):
            tripped = check_pace(db, account, live, budget_total, s, now, run_id)
            if tripped:
                counts["breaker_trips"] = counts.get("breaker_trips", 0) + 1

        first = day_str(now - timedelta(days=s["window_days"] - 1))
        current = campaign_stats(db, account["_id"], first, day_str(now))
        prior = {r["campaign_id"]: r for r in campaign_stats(
            db, account["_id"], day_str(now - timedelta(days=2 * s["window_days"] - 1)),
            day_str(now - timedelta(days=s["window_days"])))}
        tot_spend = sum(r["spend"] for r in current)
        tot_conv = sum(r["conversions"] for r in current)
        account_cpa = round(tot_spend / tot_conv, 2) if tot_conv else None
        for stats in current:
            # Kill switch is checked between every step, not only at the start.
            if gate.halt_state(db, account["_id"], client_id):
                counts["halted_accounts"] = counts.get("halted_accounts", 0) + 1
                break
            cid = stats["campaign_id"]
            if cid not in live_by_id:
                continue
            camp = live_by_id[cid]
            stats.update(status=camp["status"], daily_budget=camp["daily_budget"])
            edits_today, last_edit = edit_history(db, cid, now)
            wanted = [decide(stats, s, edits_today=edits_today, last_edit_at=last_edit,
                             now=now, account_budget_total=budget_total),
                      decide_targeting(stats, s, camp)]
            for a in [w for w in wanted if w]:
                if has_open_proposal(db, cid):
                    break
                ev = evidence.build(a, stats, prior.get(cid), account_cpa, s, stats.get("last_ts"))
                out = act(db, platform, account, camp, a, s, now, run_id, counts, ev)
                if out and out["effect"] == "allow" and out["result"]["ok"] and a.type == "set_budget":
                    budget_total += a.new - a.old
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
                       "industry": client["industry"],
                       "adapter": "sandbox", "external_id": acc["_id"], "name": acc["name"],
                       "active": True}
            platforms["sandbox"].seed_account(db, account, acc.get("settings", {}),
                                              acc.get("campaigns", []), backfill_days=14, now=now)
        print(f"seeded {client['name']} ({len(client['accounts'])} accounts)")
    from demo_data import MEMORIES
    if reset:
        db.memories.delete_many({})
    for m in MEMORIES:
        m = dict(m)
        memory.remember(db, source="demo history", when=now - timedelta(days=m.pop("days_ago")), **m)
    print(f"seeded {len(MEMORIES)} memories")


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

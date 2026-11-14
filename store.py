"""Database helpers shared by the worker and the approval runner."""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from pymongo import MongoClient


def load_env(path: str = os.path.join(os.path.dirname(__file__), ".env")) -> None:
    """Read KEY=value lines from .env without overriding real env vars."""
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def get_db():
    load_env()
    uri = os.environ.get("MONGODB_URI", "")
    if not uri.startswith("mongodb") or "<password>" in uri:
        raise SystemExit("Set MONGODB_URI in .env to your Atlas connection string first.")
    client = MongoClient(uri, tz_aware=True, appname="ad-agent", serverSelectionTimeoutMS=10000)
    return client[os.environ.get("DB_NAME", "ad_agent")]


def day_str(dt: datetime) -> str:
    return dt.date().isoformat()


def campaign_stats(db, account_id: str, first_day: str, last_day: str,
                   campaign_id: str | None = None) -> list[dict]:
    """Totals per campaign over [first_day, last_day].

    Snapshots hold day to date totals, so take the last snapshot of each day
    and then add the days together.
    """
    match = {"meta.account_id": account_id, "day": {"$gte": first_day, "$lte": last_day}}
    if campaign_id:
        match["meta.campaign_id"] = campaign_id
    pipeline = [
        {"$match": match},
        {"$sort": {"ts": 1}},
        {"$group": {
            "_id": {"c": "$meta.campaign_id", "d": "$day"},
            "spend": {"$last": "$spend"}, "conversions": {"$last": "$conversions"},
            "clicks": {"$last": "$clicks"}, "daily_budget": {"$last": "$daily_budget"},
            "status": {"$last": "$status"}, "ts": {"$last": "$ts"},
        }},
        {"$sort": {"ts": 1}},
        {"$group": {
            "_id": "$_id.c",
            "spend": {"$sum": "$spend"}, "conversions": {"$sum": "$conversions"},
            "clicks": {"$sum": "$clicks"}, "daily_budget": {"$last": "$daily_budget"},
            "status": {"$last": "$status"}, "days": {"$sum": 1},
        }},
        {"$project": {"_id": 0, "campaign_id": "$_id", "spend": 1, "conversions": 1,
                      "clicks": 1, "daily_budget": 1, "status": 1, "days": 1}},
    ]
    return list(db.metrics_snapshots.aggregate(pipeline))


def edit_history(db, campaign_id: str, now: datetime) -> tuple[int, datetime | None]:
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    q = {"campaign_id": campaign_id, "result.ok": True}
    edits_today = db.change_log.count_documents({**q, "applied_at": {"$gte": start_of_day}})
    last = db.change_log.find_one(q, sort=[("applied_at", -1)], projection={"applied_at": 1})
    return edits_today, (last or {}).get("applied_at")


def has_open_proposal(db, campaign_id: str) -> bool:
    return db.proposals.count_documents(
        {"campaign_id": campaign_id, "status": {"$in": ["pending", "approved", "executing"]}},
        limit=1,
    ) > 0


def measure_outcomes(db, now: datetime, days: int = 3) -> int:
    """For changes old enough, compare CPL in the N full days before vs after."""
    cutoff = (now - timedelta(days=days + 1)).replace(hour=23, minute=59)
    n = 0
    for e in db.change_log.find({"result.ok": True, "outcome": {"$exists": False},
                                 "applied_at": {"$lte": cutoff}}).limit(200):
        d0 = e["applied_at"]
        before = campaign_stats(db, e["account_id"], day_str(d0 - timedelta(days=days)),
                                day_str(d0 - timedelta(days=1)), e["campaign_id"])
        after = campaign_stats(db, e["account_id"], day_str(d0 + timedelta(days=1)),
                               day_str(d0 + timedelta(days=days)), e["campaign_id"])

        def cpl(rows):
            if not rows or rows[0]["conversions"] == 0:
                return None
            return round(rows[0]["spend"] / rows[0]["conversions"], 2)

        b, a = cpl(before), cpl(after)
        if b is not None and a is not None:
            verdict = "better" if a < b else "worse" if a > b else "flat"
            line = f" Outcome: CPL ${b} before, ${a} after ({verdict})."
        else:
            verdict, line = "unknown", " Outcome: not enough leads to judge."
        db.change_log.update_one(
            {"_id": e["_id"]},
            {"$set": {"outcome": {"measured_at": now, "cpl_before": b, "cpl_after": a,
                                  "verdict": verdict, "window_days": days},
                      "memory_text": e.get("memory_text", "") + line}},
        )
        n += 1
    return n

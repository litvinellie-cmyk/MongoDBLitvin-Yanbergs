"""Tracking and audiences: UTMs, click IDs, Conversions API, retargeting.

Simulated for the demo like the ad accounts. Real sources: each platform's
ads API (destination URLs, audiences, frequency), Meta Events Manager /
Google Enhanced Conversions / TikTok Events API / Reddit CAPI diagnostics,
and Shopify order notes for click IDs.

Fixes go through the same gate as everything else:
  * fix_utm            tracking only, no spend change -> agent may apply alone
  * set_frequency_cap  changes delivery -> needs a person
  * sync_exclusions    removes buyers from retargeting -> agent may apply alone
"""

from __future__ import annotations

import re
import urllib.parse
from datetime import timedelta

import gate
from platforms import PLATFORM_LABELS, utcnow

SOURCES = {"meta": "facebook", "tiktok": "tiktok", "reddit": "reddit", "google": "google"}
CLICK_IDS = {"meta": "fbclid", "google": "gclid", "tiktok": "ttclid", "reddit": "rdt_cid"}
SITES = {"harbour": "https://harbourcoffee.example", "brightsmile": "https://brightsmile.example",
         "northside": "https://northsiderealty.example"}

# Deliberately broken examples so the audit has something real to catch.
BROKEN = {
    "harbour-meta:pricey": {"utm_source": "facebok"},                 # typo
    "harbour-tiktok:spark-ads": {"utm_campaign": None},               # missing
    "brightsmile-google:whitening-pmax": {"utm_medium": "CPC"},       # wrong case splits reports
    "northside-meta:luxury-listings": "no_utms",                      # no tags at all
}

CAPI = {  # per account: events last 7 days
    "harbour-meta": {"pixel": 1180, "server": 1102, "deduped": 1040, "emq": 8.1, "click_id_rate": 91},
    "harbour-tiktok": {"pixel": 410, "server": 0, "deduped": 0, "emq": None, "click_id_rate": 64},
    "harbour-reddit": {"pixel": 160, "server": 150, "deduped": 141, "emq": 6.2, "click_id_rate": 78},
    "brightsmile-google": {"pixel": 96, "server": 88, "deduped": 30, "emq": 7.4, "click_id_rate": 88},
    "brightsmile-meta": {"pixel": 74, "server": 70, "deduped": 66, "emq": 5.1, "click_id_rate": 72},
    "northside-meta": {"pixel": 51, "server": 49, "deduped": 47, "emq": 7.9, "click_id_rate": 86},
    "northside-google": {"pixel": 40, "server": 38, "deduped": 36, "emq": 8.4, "click_id_rate": 93},
}
CAPI_NAMES = {"meta": "Meta Conversions API", "google": "Google Enhanced Conversions",
              "tiktok": "TikTok Events API", "reddit": "Reddit Conversions API"}

LADDER = [  # tier, window days, message
    ("Homepage visitors", "1 to 14 days", "Brand story and best sellers"),
    ("Product viewers", "1 to 7 days", "The exact product, with reviews"),
    ("Added to cart", "1 to 3 days", "Reminder plus free shipping threshold"),
    ("Checkout started", "1 to 2 days", "Complete your order, no discount first"),
    ("Past buyers 60 to 90 days", "60 to 90 days", "Reorder reminder (win-back)"),
]
LADDER_DATA = {  # client: per tier (size, budget/day, frequency 7d, cap, cpa)
    "harbour": [(18400, 25, 2.1, 3, 31.0), (6200, 20, 3.4, 4, 18.5), (1450, 15, 9.8, 5, 9.4),
                (380, 8, 4.1, 5, 6.2), (2900, 12, 1.9, 3, 14.1)],
    "brightsmile": [(5200, 12, 2.6, 3, 52.0), (1900, 10, 3.1, 4, 41.0), (0, 0, 0, 0, None),
                    (220, 6, 7.2, 4, 28.0), (640, 8, 1.4, 3, 33.0)],
    "northside": [(9100, 18, 2.2, 3, 78.0), (3400, 14, 2.9, 4, 64.0), (0, 0, 0, 0, None),
                  (150, 5, 2.0, 4, 41.0), (0, 0, 0, 0, None)],
}
SYNCS = {  # client: (source, audience, members, last sync minutes ago, match rate)
    "harbour": [("Shopify buyers (exclusion)", "Meta + TikTok exclusion", 3120, 340, 71),
                ("Klaviyo engaged 30 days", "Meta custom audience", 4410, 55, 68),
                ("Top 5% lifetime value", "Meta lookalike 1%", 156, 1440, 74)],
    "brightsmile": [("Current patients (exclusion)", "Google + Meta exclusion", 2210, 30, 63)],
    "northside": [("Past clients (exclusion)", "Meta exclusion", 410, 20, 58),
                  ("Open house sign-ups", "Meta custom audience", 260, 90, 61)],
}


def _url(client: str, campaign: str, platform: str) -> str:
    q = {"utm_source": SOURCES.get(platform, platform), "utm_medium": "paid_social" if platform != "google" else "cpc",
         "utm_campaign": campaign.split(":")[-1], "utm_content": "v1"}
    b = BROKEN.get(campaign)
    if b == "no_utms":
        q = {}
    elif b:
        for k, v in b.items():
            if v is None:
                q.pop(k, None)
            else:
                q[k] = v
    return SITES[client] + "/" + ("?" + urllib.parse.urlencode(q) if q else "")


def seed(db, now=None) -> None:
    now = now or utcnow()
    for coll in ("ads", "capi_health", "audiences", "audience_syncs"):
        db[coll].delete_many({})
    ads = []
    for c in db.sandbox_campaigns.find():
        acc = db.accounts.find_one({"_id": c["account_id"]})
        ads.append({"client_id": c["client_id"], "account_id": c["account_id"], "platform": acc["platform"],
                    "campaign_id": c["_id"], "name": f"{c['name']} ad 1",
                    "url": _url(c["client_id"], c["_id"], acc["platform"]), "status": "ACTIVE"})
    if ads:
        db.ads.insert_many(ads)
    db.capi_health.insert_many([{"account_id": a, "client_id": a.split("-")[0],
                                 "platform": a.split("-")[1], **v, "at": now} for a, v in CAPI.items()])
    tiers = []
    for cid, rows in LADDER_DATA.items():
        for (tier, window, msg), (size, budget, freq, cap, cpa) in zip(LADDER, rows):
            tiers.append({"client_id": cid, "tier": tier, "window": window, "message": msg, "size": size,
                          "budget": budget, "frequency_7d": freq, "frequency_cap": cap, "cpa": cpa,
                          "status": "ACTIVE" if size else "missing"})
    db.audiences.insert_many(tiers)
    db.audience_syncs.insert_many([{"client_id": cid, "source": s, "audience": a, "members": m,
                                    "last_sync": now - timedelta(minutes=mins), "match_rate": mr}
                                   for cid, rows in SYNCS.items() for s, a, m, mins, mr in rows])


# ---------------------------------------------------------------- checks

def check_utm(url: str, platform: str) -> list[str]:
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    issues = []
    for k in ("utm_source", "utm_medium", "utm_campaign"):
        if k not in q:
            issues.append(f"Missing {k}")
    want = SOURCES.get(platform)
    if "utm_source" in q and want and q["utm_source"] != want:
        issues.append(f"utm_source is \"{q['utm_source']}\", should be \"{want}\"")
    for k, v in q.items():
        if k.startswith("utm_") and v != v.lower():
            issues.append(f"{k} \"{v}\" has capitals, which splits reports in GA4")
    return issues


def fixed_url(url: str, platform: str, campaign_id: str) -> str:
    p = urllib.parse.urlparse(url)
    q = {k: v.lower() for k, v in urllib.parse.parse_qsl(p.query)}
    q["utm_source"] = SOURCES.get(platform, platform)
    q.setdefault("utm_medium", "paid_social" if platform != "google" else "cpc")
    q.setdefault("utm_campaign", campaign_id.split(":")[-1])
    q.setdefault("utm_content", "v1")
    return urllib.parse.urlunparse(p._replace(query=urllib.parse.urlencode(q)))


def utm_audit(db, client_id: str) -> dict:
    rows = []
    for a in db.ads.find({"client_id": client_id}):
        issues = check_utm(a["url"], a["platform"])
        rows.append({"ad": a["name"], "platform": a["platform"], "url": a["url"], "issues": issues,
                     "fix": fixed_url(a["url"], a["platform"], a["campaign_id"]) if issues else None})
    bad = sum(1 for r in rows if r["issues"])
    return {"ads": rows, "bad": bad, "total": len(rows),
            "convention": "utm_source = platform, utm_medium = paid_social or cpc, utm_campaign = campaign name, "
                          "utm_content = ad version. All lowercase."}


def capi(db, client_id: str) -> list[dict]:
    out = []
    for h in db.capi_health.find({"client_id": client_id}, {"_id": 0}):
        checks, status = [], "ok"
        name = CAPI_NAMES.get(h["platform"], h["platform"])
        if not h["server"]:
            status = "problem"
            checks.append(f"{name} is not connected. Only browser pixel events arrive, so ad blockers and iOS "
                          f"privacy hide part of your results.")
        else:
            cover = h["server"] / h["pixel"] * 100
            dedup = h["deduped"] / h["server"] * 100
            checks.append(f"Server events cover {cover:.0f}% of pixel events.")
            if dedup < 80:
                status = "warning"
                checks.append(f"Only {dedup:.0f}% of server events share an event ID with the pixel, so some "
                              f"purchases are counted twice. Send the same event_id from both.")
            else:
                checks.append(f"{dedup:.0f}% deduplicated with the pixel by event ID.")
            if h["emq"] is not None and h["emq"] < 6:
                status = "warning" if status == "ok" else status
                checks.append(f"Event match quality {h['emq']}/10 is low. Send hashed email and phone with each event.")
            elif h["emq"] is not None:
                checks.append(f"Event match quality {h['emq']}/10.")
        cid = CLICK_IDS.get(h["platform"])
        if h["click_id_rate"] < 80:
            status = "warning" if status == "ok" else status
            checks.append(f"Only {h['click_id_rate']}% of orders keep the {cid}. Store it at checkout so sales "
                          f"can be sent back as offline conversions.")
        else:
            checks.append(f"{h['click_id_rate']}% of orders keep the {cid}.")
        out.append({"account": h["account_id"], "platform": h["platform"], "name": name, "status": status,
                    "checks": checks, "pixel": h["pixel"], "server": h["server"], "emq": h["emq"]})
    return out


def ladder(db, client_id: str) -> list[dict]:
    out = []
    for a in db.audiences.find({"client_id": client_id}):
        issue = None
        if a["status"] == "missing":
            issue = "Tier not set up. These people get no follow-up."
        elif a["frequency_7d"] > a["frequency_cap"] * 1.5:
            issue = f"Seen {a['frequency_7d']} times in 7 days against a cap of {a['frequency_cap']}. People are getting tired of the ad."
        out.append({**a, "_id": str(a["_id"]), "issue": issue})
    return out


def syncs(db, client_id: str) -> list[dict]:
    now = utcnow()
    out = []
    for s in db.audience_syncs.find({"client_id": client_id}):
        mins = int((now - s["last_sync"]).total_seconds() / 60)
        stale = "exclusion" in s["source"].lower() and mins > 60
        out.append({"_id": str(s["_id"]), "source": s["source"], "audience": s["audience"], "members": s["members"],
                    "minutes_ago": mins, "match_rate": s["match_rate"], "stale": stale,
                    "note": (f"Last synced {mins // 60}h {mins % 60}m ago. Recent buyers are still being shown "
                             f"retargeting ads." if stale else f"Synced {mins} min ago.")})
    return out


# ---------------------------------------------------------------- agent actions

def run(db, platforms, run_id: str, counts: dict) -> None:
    from channels import _propose, _seen
    from platforms import platform_for
    for a in db.ads.find():
        issues = check_utm(a["url"], a["platform"])
        target = f"ad:{a['_id']}"
        if not issues or _seen(db, target):
            continue
        acc = db.accounts.find_one({"_id": a["account_id"]})
        if gate.halt_state_cached(db, acc["_id"], acc["client_id"]):
            continue
        new = fixed_url(a["url"], a["platform"], a["campaign_id"])
        reason = {"text": f"{a['name']} on {PLATFORM_LABELS.get(a['platform'])}: {'; '.join(issues)}. "
                          f"Fix the tracking link so results show up correctly in analytics.",
                  "numbers": {"issues": issues}}
        _propose(db, platform_for(platforms, acc), acc, target,
                 {"type": "fix_utm", "field": "url", "old": a["url"], "new": new, "label": a["name"]},
                 reason, run_id, counts)
    for t in db.audiences.find({"status": "ACTIVE"}):
        if t["frequency_7d"] <= t["frequency_cap"] * 1.5:
            continue
        target = f"aud:{t['_id']}"
        if _seen(db, target):
            continue
        acc = db.accounts.find_one({"client_id": t["client_id"], "platform": "meta"})
        if not acc or gate.halt_state_cached(db, acc["_id"], acc["client_id"]):
            continue
        new_budget = round(t["budget"] * 0.6, 2)
        reason = {"text": (f"\"{t['tier']}\" retargeting reached a frequency of {t['frequency_7d']} in 7 days "
                           f"(cap {t['frequency_cap']}). Lower its budget from ${t['budget']} to ${new_budget} a day "
                           f"so the same people stop seeing the ad over and over."),
                  "numbers": {"frequency_7d": t["frequency_7d"], "cap": t["frequency_cap"]}}
        _propose(db, platform_for(platforms, acc), acc, target,
                 {"type": "set_frequency_cap", "field": "budget", "old": t["budget"], "new": new_budget,
                  "label": f"{t['tier']} retargeting"}, reason, run_id, counts)
    now = utcnow()
    for s in db.audience_syncs.find({"source": {"$regex": "exclusion", "$options": "i"},
                                     "last_sync": {"$lt": now - timedelta(minutes=60)}}):
        target = f"sync:{s['_id']}"
        if _seen(db, target):
            continue
        acc = db.accounts.find_one({"client_id": s["client_id"], "platform": "meta"})
        if not acc or gate.halt_state_cached(db, acc["_id"], acc["client_id"]):
            continue
        reason = {"text": f"{s['source']} hasn't synced in over an hour, so recent buyers still see retargeting "
                          f"ads. Sync it now.", "numbers": {"members": s["members"]}}
        _propose(db, platform_for(platforms, acc), acc, target,
                 {"type": "sync_exclusions", "field": "last_sync", "old": s["last_sync"].isoformat(),
                  "new": "now", "label": s["source"]}, reason, run_id, counts)

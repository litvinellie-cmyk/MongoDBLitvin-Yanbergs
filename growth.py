"""Growth: setup help, goals, campaign analysis, learning, ad libraries, AI search fixes.

For agencies, marketers and business owners alike. Rules for every number:
  * valid data only: an analysis that lacks enough clicks or conversions, has
    stale data, or has broken tracking says so and gives no performance advice
  * benchmarks are approximate industry ranges and are labeled that way
"""

from __future__ import annotations

import calendar
import json
import urllib.parse
from datetime import timedelta

from evidence import kpis
from platforms import PLATFORM_LABELS, utcnow
from rules import merged_settings
from store import campaign_stats, day_str

# Approximate public ranges, for orientation only (shown as such in the UI).
BENCH = {
    "meta": {"ctr": 1.2, "cpc": 1.0, "cvr": 1.0, "cpm": 12.0, "frequency": 3.0},
    "tiktok": {"ctr": 1.0, "cpc": 0.8, "cvr": 0.8, "cpm": 9.0, "frequency": 3.0},
    "reddit": {"ctr": 0.6, "cpc": 0.9, "cvr": 1.0, "cpm": 6.0, "frequency": 3.0},
    "google": {"ctr": 4.0, "cpc": 2.5, "cvr": 2.5, "cpm": 30.0, "frequency": 5.0},
}
INDUSTRY = {
    "Ecommerce": {"result": "order", "cpa": (25, 55), "split": {"meta": 50, "google": 30, "tiktok": 20}},
    "Medical": {"result": "new patient", "cpa": (60, 120), "split": {"google": 60, "meta": 40}},
    "Real estate": {"result": "lead", "cpa": (50, 120), "split": {"meta": 55, "google": 45}},
    "Local service": {"result": "booking", "cpa": (30, 80), "split": {"google": 60, "meta": 40}},
    "Other": {"result": "lead", "cpa": (30, 90), "split": {"meta": 50, "google": 50}},
}
GOALS = {"harbour": ("orders", 650), "brightsmile": ("new patients", 120), "northside": ("buyer leads", 60)}


# ---------------------------------------------------------------- data quality

def data_quality(st: dict, now) -> dict:
    """Valid data only. Returns ok plus the reasons it isn't."""
    k = kpis(st or {})
    reasons = []
    if k["clicks"] < 100:
        reasons.append(f"Only {k['clicks']} clicks. Need 100+ before judging click rates.")
    if k["conversions"] < 5:
        reasons.append(f"Only {k['conversions']} conversions. Need 5+ before judging cost per result.")
    last = (st or {}).get("last_ts")
    if last and (now - last) > timedelta(hours=6):
        reasons.append(f"Data is {int((now - last).total_seconds() // 3600)} hours old. Run the agent to pull fresh numbers.")
    if k["platform_overcount_pct"] is not None and k["platform_overcount_pct"] > 40:
        reasons.append(f"Platform claims {k['platform_overcount_pct']:.0f}% more conversions than the store or CRM. "
                       f"Fix tracking first; decisions use verified numbers only.")
    return {"ok": not [r for r in reasons if not r.startswith("Platform")], "reasons": reasons}


# ---------------------------------------------------------------- 1. campaign analyzer

def analyze(db, client_id: str) -> list[dict]:
    now = utcnow()
    out = []
    for acc in db.accounts.find({"client_id": client_id, "active": True, "platform": {"$ne": "shopify"}}):
        s = merged_settings(db.settings.find_one({"_id": acc["_id"]}))
        stats = {r["campaign_id"]: r for r in campaign_stats(db, acc["_id"], day_str(now - timedelta(days=6)), day_str(now))}
        b = BENCH.get(acc["platform"], BENCH["meta"])
        for c in db.sandbox_campaigns.find({"account_id": acc["_id"]}):
            st = stats.get(c["_id"], {})
            k = kpis(st)
            q = data_quality(st, now)
            tips = []
            if q["ok"]:
                if k["ctr"] is not None and k["ctr"] < b["ctr"] * 0.7:
                    tips.append(("Creative", f"Click rate {k['ctr']}% is low for {PLATFORM_LABELS[acc['platform']]} "
                                             f"(around {b['ctr']}% is typical). Test a new hook in the first 3 seconds "
                                             f"or a clearer offer in the headline."))
                if k["cvr"] is not None and k["cvr"] < b["cvr"] * 0.6:
                    tips.append(("Landing page", f"Only {k['cvr']}% of clicks convert (around {b['cvr']}% is typical). "
                                                 f"People click but don't buy: check the page matches the ad, load speed, "
                                                 f"and form length."))
                if k["frequency"] is not None and k["frequency"] > b["frequency"]:
                    tips.append(("Audience", f"Each person saw it {k['frequency']} times this week. The audience is "
                                             f"getting tired: widen targeting or refresh the creative."))
                if k["cpm"] is not None and k["cpm"] > b["cpm"] * 1.5:
                    tips.append(("Audience", f"CPM ${k['cpm']} is high. The audience may be too narrow or too "
                                             f"competitive; try a broader or lookalike audience."))
                if k["cpa"] is not None and k["cpa"] > s["target_cpl"] * 1.3 and not tips:
                    tips.append(("Budget", f"Cost per result ${k['cpa']} is above the ${s['target_cpl']:.0f} goal while "
                                           f"clicks and conversion rates look normal. Lower the budget or bid and let "
                                           f"the agent shift money to stronger campaigns."))
                if k["cpa"] is not None and k["cpa"] < s["target_cpl"] * 0.7:
                    tips.append(("Scale", f"Cost per result ${k['cpa']} is well under the ${s['target_cpl']:.0f} goal. "
                                          f"Room to scale: raise budget 10 to 15% a step."))
                if not tips:
                    tips.append(("Healthy", "Within normal ranges. Leave it to learn."))
            score = 100
            if k["conversions"] == 0 and k["spend"] >= 3 * s["target_cpl"]:
                q = {"ok": True, "reasons": []}
                tips = [("Stop the loss", f"Spent ${k['spend']:,.2f} with zero results, {k['spend'] / s['target_cpl']:.1f}x "
                                         f"the ${s['target_cpl']:.0f} goal. That is enough evidence: pause it and move the "
                                         f"budget (the agent has proposed this).")]
                score = 0
            elif not q["ok"]:
                score = None
            else:
                score -= 20 * sum(1 for t in tips if t[0] not in ("Healthy", "Scale"))
            out.append({"campaign": c["name"], "platform": acc["platform"], "status": c["status"], "kpis": k,
                        "quality": q, "score": score, "tips": [{"area": a, "text": t} for a, t in tips]})
    return sorted(out, key=lambda r: (r["score"] is None, r["score"] if r["score"] is not None else 0))


# ---------------------------------------------------------------- 2. goals

def seed_goals(db) -> None:
    db.goals.delete_many({})
    month = utcnow().strftime("%Y-%m")
    db.goals.insert_many([{"client_id": c, "metric": m, "monthly_target": n, "month": month}
                          for c, (m, n) in GOALS.items()])


def goal_status(db, client_id: str) -> dict | None:
    g = db.goals.find_one({"client_id": client_id})
    if not g:
        return None
    now = utcnow()
    first = now.replace(day=1)
    days_in = calendar.monthrange(now.year, now.month)[1]
    elapsed = max((now - first).total_seconds() / 86400, 0.5)
    accs = [a["_id"] for a in db.accounts.find({"client_id": client_id, "platform": {"$ne": "shopify"}})]
    rows = campaign_stats(db, accs, day_str(first), day_str(now)) if accs else []
    mtd = sum(r["conversions"] for r in rows)
    spend = sum(r["spend"] for r in rows)
    if not rows or (mtd == 0 and spend == 0):
        return {"metric": g["metric"], "target": g["monthly_target"], "month_to_date": 0, "projected": 0, "pct": 0,
                "status": "not started", "spend_mtd": 0, "day": int(elapsed), "days_in_month": days_in,
                "plan": "No ad data yet. Connect the ad accounts and finish the setup checklist; tracking starts "
                        "with the first results."}
    projected = mtd / elapsed * days_in
    pct = projected / g["monthly_target"] * 100
    status = "on track" if pct >= 98 else "close" if pct >= 85 else "behind"
    remaining_days = max(days_in - elapsed, 1)
    need_per_day = max(g["monthly_target"] - mtd, 0) / remaining_days
    now_per_day = mtd / elapsed
    plan = None
    if status != "on track" and now_per_day:
        spend_day = spend / elapsed
        need_spend_day = spend_day * (need_per_day / now_per_day) ** (1 / 0.6)
        plan = (f"To hit {g['monthly_target']} {g['metric']}, you need about {need_per_day:.1f} a day for the rest of "
                f"the month (now {now_per_day:.1f}). That takes about ${need_spend_day:,.0f} a day in ads "
                f"(now ${spend_day:,.0f}), or fixing the biggest funnel leak first, which is cheaper.")
    return {"metric": g["metric"], "target": g["monthly_target"], "month_to_date": mtd, "projected": round(projected),
            "pct": round(pct), "status": status, "spend_mtd": round(spend, 2), "plan": plan,
            "day": int(elapsed), "days_in_month": days_in}


def set_goal(db, client_id: str, target: int, actor_id: str) -> dict:
    import gate
    if target <= 0:
        return {"ok": False, "error": "Enter a monthly goal above zero."}
    db.goals.update_one({"client_id": client_id}, {"$set": {"monthly_target": int(target), "set_by": actor_id,
                                                            "set_at": utcnow()}}, upsert=True)
    gate.audit(db, "goal_set", actor_id=actor_id, client_id=client_id, target=int(target))
    return {"ok": True}


# ---------------------------------------------------------------- 3. learning / earned autonomy

def trust(db, client_id: str) -> dict:
    since = utcnow() - timedelta(days=90)
    better = db.memories.count_documents({"client_id": client_id, "verdict": "better", "created_at": {"$gte": since}})
    worse = db.memories.count_documents({"client_id": client_id, "verdict": "worse", "created_at": {"$gte": since}})
    n = better + worse
    acc = better / n * 100 if n else None
    if n >= 4 and acc >= 75:
        level, limit = "high", 15
    elif n >= 3 and acc <= 40:
        level, limit = "low", 0
    else:
        level, limit = "normal", 10
    text = {"high": f"Earned more freedom: {better} of {n} past moves helped, so it may move budgets up to 15% on its own.",
            "low": f"On a short leash: only {better} of {n} past moves helped, so every budget change needs a person.",
            "normal": f"Standard limits: may move budgets up to 10% on its own"
                      + (f" ({better} of {n} past moves helped)." if n else " (no measured results yet).")}[level]
    return {"level": level, "auto_limit_pct": limit, "better": better, "worse": worse, "n": n,
            "accuracy": round(acc) if acc is not None else None, "text": text}


# ---------------------------------------------------------------- 4. setup wizard

def setup_plan(name: str, industry: str, monthly_budget: float, platforms: list[str], website: str = "",
               target_cpa: float | None = None) -> dict:
    ind = INDUSTRY.get(industry, INDUSTRY["Other"])
    lo, hi = ind["cpa"]
    target = target_cpa or round((lo + hi) / 2)
    plats = [p for p in platforms if p in BENCH] or list(ind["split"])
    weights = {p: ind["split"].get(p, 15) for p in plats}
    tot = sum(weights.values())
    daily = monthly_budget / 30
    split = [{"platform": p, "label": PLATFORM_LABELS[p], "pct": round(w / tot * 100),
              "per_day": round(daily * w / tot, 2)} for p, w in weights.items()]
    results_lo, results_hi = int(monthly_budget / (target * 1.25)), int(monthly_budget / (target * 0.8))
    structure = [
        {"name": "Prospecting", "share": 70, "what": "Broad or lookalike audiences, 2 to 3 creatives, one clear offer."},
        {"name": "Retargeting", "share": 20, "what": "Product viewers 1 to 7 days, cart 1 to 3 days, frequency cap 4."},
        {"name": "Brand search" if "google" in plats else "Testing", "share": 10,
         "what": "Bid on your own name so competitors can't take it." if "google" in plats else "One new idea at a time."},
    ]
    checklist = [
        "Install the pixel and the Conversions API on every platform, with the same event ID on both.",
        f"Track one main conversion: a {ind['result']}. Turn off counting of micro events for optimization.",
        "Use one UTM rule: utm_source = platform, utm_medium = paid_social or cpc, utm_campaign = campaign name.",
        "Exclude existing customers from prospecting and sync buyers from your store or CRM.",
        "Store click IDs (fbclid, gclid, ttclid) with each order or lead for offline conversions.",
        f"Set the goal: {ind['result']}s at ${target} each. The agent won't scale above it.",
        "Leave new campaigns for 7 days or 50 conversions before judging them (learning phase).",
    ]
    if monthly_budget / 30 < 20 * len(plats):
        warn = (f"${monthly_budget:,.0f} a month is thin for {len(plats)} platforms (under $20 a day each). "
                f"Start with one or two so each can learn.")
    else:
        warn = None
    return {"business": name, "industry": industry, "result_word": ind["result"], "target_cpa": target,
            "cpa_range": [lo, hi], "monthly_budget": monthly_budget, "split": split, "structure": structure,
            "expected_results": [results_lo, results_hi], "checklist": checklist, "warning": warn,
            "website": website,
            "note": f"Typical cost per {ind['result']} in this industry is roughly ${lo} to ${hi}. "
                    f"The first 2 weeks are for learning; the agent starts optimizing once data is valid."}


def create_from_plan(db, plan: dict, actor_id: str) -> dict:
    import gate
    import re
    cid = re.sub(r"[^a-z0-9]+", "", plan["business"].lower())[:16] or "client"
    if db.clients.find_one({"_id": cid}):
        return {"ok": False, "error": "A client with that name already exists."}
    db.clients.insert_one({"_id": cid, "name": plan["business"], "industry": plan["industry"],
                           "website": plan.get("website"), "created_by": actor_id, "created_at": utcnow()})
    for s in plan["split"]:
        aid = f"{cid}-{s['platform']}"
        db.accounts.insert_one({"_id": aid, "client_id": cid, "platform": s["platform"], "industry": plan["industry"],
                                "adapter": "sandbox", "external_id": aid, "name": f"{plan['business']} on {s['label']}",
                                "active": True, "status_note": "not connected yet"})
        db.settings.replace_one({"_id": aid}, {"_id": aid, "target_cpl": float(plan["target_cpa"]),
                                               "daily_spend_cap": round(s["per_day"] * 1.1, 2)}, upsert=True)
    db.goals.update_one({"client_id": cid}, {"$set": {
        "metric": plan["result_word"] + "s", "monthly_target": int(sum(plan["expected_results"]) / 2),
        "month": utcnow().strftime("%Y-%m")}}, upsert=True)
    db.plans.insert_one({**plan, "client_id": cid, "created_by": actor_id, "created_at": utcnow()})
    gate.audit(db, "client_created", actor_id=actor_id, client_id=cid, plan=plan["business"])
    return {"ok": True, "client_id": cid}


# ---------------------------------------------------------------- 5. ad libraries (real links)

def ad_library_links(name: str, country: str = "CA") -> list[dict]:
    q = urllib.parse.quote(name)
    return [
        {"label": "Meta Ad Library", "url": f"https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country={country}&q={q}&search_type=keyword_unordered"},
        {"label": "Google Ads Transparency", "url": f"https://adstransparency.google.com/?region={country}"},
        {"label": "TikTok Creative Center", "url": "https://ads.tiktok.com/business/creativecenter/inspiration/topads/pc/en"},
        {"label": "LinkedIn Ad Library", "url": f"https://www.linkedin.com/ad-library/search?companyName={q}"},
    ]


# ---------------------------------------------------------------- 6. AEO / GEO content fixes

def aeo_fixes(db, client_id: str) -> list[dict]:
    client = db.clients.find_one({"_id": client_id}) or {}
    name = client.get("name", client_id)
    today = utcnow().date().isoformat()
    misses = {}
    for r in db.ai_visibility.find({"client_id": client_id, "day": today}):
        if not r["mentioned"]:
            misses.setdefault(r["query"], []).append(r["engine"])
    sources = {}
    for r in db.ai_visibility.find({"client_id": client_id, "day": today}):
        for s in r["cited_sources"]:
            sources[s] = sources.get(s, 0) + 1
    top = [s for s, _ in sorted(sources.items(), key=lambda kv: -kv[1])[:3]]
    out = []
    for q, engines in sorted(misses.items(), key=lambda kv: -len(kv[1])):
        if len(engines) < 2:
            continue
        question = q[0].upper() + q[1:] + "?"
        answer = (f"{name} is a strong option for this. [Add 2 to 3 specific facts: location, prices, hours, "
                  f"what makes you different, and one proof point such as a rating or award.]")
        schema = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": question, "acceptedAnswer": {"@type": "Answer", "text": answer}}]}
        out.append({"query": q, "missing_on": engines, "question": question, "answer_draft": answer,
                    "schema": json.dumps(schema, indent=2),
                    "get_cited_on": top,
                    "steps": [f"Add this question and a direct 40 to 60 word answer to your website.",
                              "Add the FAQPage schema below to that page.",
                              f"Get mentioned where AI looks for this question: {', '.join(top)}.",
                              "Keep name, address, phone and hours identical on Google, Yelp and Apple Maps."]})
    return out

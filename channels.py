"""Beyond ads: competitors, AI search (AEO/GEO), SEO, reviews, creatives, tracking.

All data here is simulated for the demo, the same way the ad accounts are.
Real sources noted per section. Two parts take real actions and therefore go
through the same policy gate, approval queue and audit trail as budgets:
  * pausing a paid keyword the client already ranks for organically
  * posting a reply to a customer review
"""

from __future__ import annotations

import re
from datetime import timedelta

import gate
from platforms import utcnow

# ---------------------------------------------------------------- demo data

COMPETITORS = {  # made up rivals
    "harbour": [
        {"name": "Northbean Roasters", "ads": [
            ("Free shipping on every bag this week", "offer", 34, "meta"),
            ("Our single origin Ethiopia is back", "product", 61, "meta"),
            ("Subscribe and save 20%", "offer", 9, "tiktok")]},
        {"name": "Cask & Crema", "ads": [
            ("Barista at home starter kit", "bundle", 22, "meta"),
            ("Holiday gift boxes from $39", "seasonal", 3, "google")]},
    ],
    "brightsmile": [
        {"name": "Lakeshore Dental Group", "ads": [
            ("New patients: exam and cleaning $99", "offer", 88, "google"),
            ("Invisalign from $99 a month", "offer", 12, "meta")]},
        {"name": "Smile Studio", "ads": [("Emergency dentist open Saturdays", "service", 40, "google")]},
    ],
    "northside": [
        {"name": "Keyline Realty", "ads": [
            ("What is your home worth? Free estimate", "lead magnet", 120, "meta"),
            ("Open house this Sunday in Leslieville", "event", 2, "meta")]},
        {"name": "Urban Nest Homes", "ads": [("First time buyer guide", "lead magnet", 45, "google")]},
    ],
}

BRAND_BIDDERS = {"harbour": "Northbean Roasters", "brightsmile": None, "northside": "Keyline Realty"}

AI_QUERIES = {
    "harbour": ["best coffee roaster in Toronto", "where to buy fresh coffee beans online Canada",
                "best coffee subscription Canada"],
    "brightsmile": ["best dentist near Yonge and Eglinton", "emergency dentist Toronto Saturday",
                    "affordable teeth whitening Toronto"],
    "northside": ["best real estate agent Leslieville", "how to sell my house fast in Toronto",
                  "first time home buyer realtor Toronto"],
}
ENGINES = ["ChatGPT", "Perplexity", "Gemini", "Google AI Overview", "Claude"]

KEYWORDS = {  # keyword, paid cpc, paid spend 7d, organic rank, client
    "harbour": [("harbour coffee", 0.42, 64.0, 1), ("buy coffee beans online", 1.85, 212.0, 2),
                ("toronto coffee roaster", 1.20, 96.0, 4), ("coffee subscription", 2.40, 180.0, 14)],
    "brightsmile": [("bright smile dental", 0.90, 48.0, 1), ("dentist yonge eglinton", 4.10, 290.0, 2),
                    ("teeth whitening toronto", 3.60, 210.0, 9)],
    "northside": [("northside realty", 0.70, 22.0, 1), ("leslieville realtor", 3.20, 160.0, 3),
                  ("sell my house toronto", 5.80, 340.0, 18)],
}

REVIEWS = {
    "harbour": [("Mira K.", 5, "Best espresso beans I've found in the city. Shipping was fast."),
                ("Tom R.", 2, "Order arrived late and the bag was torn. Coffee was good though."),
                ("Jae P.", 4, "Great subscription, wish there were more decaf options.")],
    "brightsmile": [("Anita S.", 5, "Dr. Lee was so gentle, my kids actually like going now."),
                    ("Greg W.", 1, "Waited 45 minutes past my appointment time. Not acceptable.")],
    "northside": [("Lena M.", 5, "Sold our condo in 9 days, above asking. Highly recommend."),
                  ("Raj D.", 3, "Good agent but slow to reply on weekends.")],
}

CREATIVES = {
    "harbour": [("Winter blend is here", "Cozy up with our new Winter Blend. Roasted in Toronto, shipped fresh.", "meta"),
                ("Best coffee in Canada", "The best coffee in Canada, guaranteed. Order now!", "tiktok")],
    "brightsmile": [("Whitening special", "Get a whiter smile in one visit. Results guaranteed, 100% painless!", "meta"),
                    ("New patients welcome", "Gentle family dentistry near Yonge and Eglinton. Book online.", "google")],
    "northside": [("Perfect for young families", "Perfect homes for young families with kids in Leslieville.", "meta"),
                  ("Free home estimate", "Find out what your home is worth in 2 minutes.", "meta")],
}

# Words that break platform policy, consumer law, or ad category rules.
CREATIVE_RULES = [
    (r"\bguarantee(d)?\b", "all", "Absolute guarantees are risky claims and often rejected."),
    (r"\bbest\b.*\b(in|of) (canada|toronto|the city)\b", "all", "Superlative claims need proof (Competition Act)."),
    (r"\b100% painless\b|\bpainless\b", "Medical", "Health outcome promises are not allowed in health ads."),
    (r"\bresults\b", "Medical", "Implied medical results need review."),
    (r"\b(young )?famil(y|ies)\b|\bkids\b|\bsingles?\b|\bretirees?\b", "Real estate",
     "Housing ads may not suggest who should live there (Fair Housing, Meta Special Ad Category)."),
    (r"\border now!|\bact now\b", "all", "Pressure language lowers quality scores."),
]
BRAND_VOICE_BANNED = {"harbour": ["guaranteed", "cheap"], "brightsmile": ["painless"], "northside": []}


# ---------------------------------------------------------------- seed

def _h(*parts) -> float:
    import hashlib
    return hashlib.sha256("|".join(map(str, parts)).encode()).digest()[0] / 255


def seed(db, now=None) -> None:
    now = now or utcnow()
    for coll in ("competitor_ads", "ai_visibility", "seo_keywords", "reviews", "creatives"):
        db[coll].delete_many({})
    ads = []
    for cid, comps in COMPETITORS.items():
        for comp in comps:
            for text, kind, days, plat in comp["ads"]:
                ads.append({"client_id": cid, "competitor": comp["name"], "text": text, "kind": kind,
                            "platform": plat, "first_seen": now - timedelta(days=days),
                            "days_running": days, "source": "Meta Ad Library / Google Ads Transparency (simulated)"})
    db.competitor_ads.insert_many(ads)

    vis = []
    for cid, queries in AI_QUERIES.items():
        rivals = [c["name"] for c in COMPETITORS[cid]]
        for q in queries:
            for e in ENGINES:
                for back in range(14):
                    day = now - timedelta(days=back)
                    trend = 0.15 if back < 7 else 0.0  # visibility improved this week
                    mentioned = _h(cid, q, e, back) < 0.45 + trend
                    vis.append({
                        "client_id": cid, "query": q, "engine": e, "day": day.date().isoformat(), "at": day,
                        "mentioned": mentioned, "position": (1 + int(_h(q, e, back, "p") * 4)) if mentioned else None,
                        "rivals_mentioned": [r for r in rivals if _h(r, q, e, back) < 0.55],
                        "cited_sources": [s for s in ("reddit.com", "yelp.ca", "blogto.com", "google.com/maps",
                                                      "tripadvisor.ca") if _h(s, q, e) < 0.4],
                        "wrong_info": "Lists old Saturday hours (closed instead of 9 to 2)"
                        if cid == "brightsmile" and e == "Gemini" and back == 0 and q.startswith("emergency") else None,
                    })
    db.ai_visibility.insert_many(vis)

    kws = []
    for cid, items in KEYWORDS.items():
        for kw, cpc, spend, rank in items:
            kws.append({"client_id": cid, "keyword": kw, "cpc": cpc, "paid_spend_7d": spend,
                        "paid_clicks_7d": int(spend / cpc), "organic_rank": rank,
                        "organic_clicks_7d": int((40 if rank == 1 else 22 if rank <= 3 else 6 if rank <= 10 else 1)
                                                 * spend / 50), "status": "ACTIVE",
                        "branded": rank == 1 and cid[:5] in kw.replace(" ", "")[:12]})
    db.seo_keywords.insert_many(kws)

    revs = []
    for cid, items in REVIEWS.items():
        for i, (who, stars, text) in enumerate(items):
            revs.append({"client_id": cid, "author": who, "stars": stars, "text": text,
                         "source": "Google Business Profile (simulated)", "at": now - timedelta(hours=6 + i * 20),
                         "reply": None})
    db.reviews.insert_many(revs)

    cr = []
    for cid, items in CREATIVES.items():
        for title, body, plat in items:
            cr.append({"client_id": cid, "title": title, "body": body, "platform": plat, "status": "draft"})
    db.creatives.insert_many(cr)


# ---------------------------------------------------------------- insights

def competitors(db, client_id: str) -> dict:
    now = utcnow()
    ads = list(db.competitor_ads.find({"client_id": client_id}, {"_id": 0}).sort("days_running", -1))
    new = [a for a in ads if a["days_running"] <= 7]
    winners = [a for a in ads if a["days_running"] >= 30]
    offers = [a for a in ads if a["kind"] in ("offer", "bundle")]
    bidder = BRAND_BIDDERS.get(client_id)
    return {"ads": ads, "new_this_week": new, "long_running": winners, "offers": offers,
            "brand_bidder": bidder,
            "summary": (f"{len(new)} new competitor ad{'' if len(new) == 1 else 's'} this week. {len(winners)} have run 30+ days, "
                        f"which usually means they work." + (f" {bidder} is bidding on your brand name in Google."
                                                            if bidder else "")),
            "as_of": now}


def ai_visibility(db, client_id: str) -> dict:
    rows = list(db.ai_visibility.find({"client_id": client_id}, {"_id": 0}))
    today = utcnow().date()
    def share(rs):
        return round(sum(r["mentioned"] for r in rs) / len(rs) * 100) if rs else 0
    this = [r for r in rows if (today - __import__("datetime").date.fromisoformat(r["day"])).days < 7]
    last = [r for r in rows if 7 <= (today - __import__("datetime").date.fromisoformat(r["day"])).days < 14]
    latest = {}
    for r in sorted(rows, key=lambda r: r["day"]):
        latest[(r["query"], r["engine"])] = r
    by_engine = {e: share([r for r in this if r["engine"] == e]) for e in ENGINES}
    rivals = {}
    for r in this:
        for n in r["rivals_mentioned"]:
            rivals[n] = rivals.get(n, 0) + 1
    rival_share = {n: round(c / len(this) * 100) for n, c in rivals.items()} if this else {}
    sources = {}
    for r in this:
        for s in r["cited_sources"]:
            sources[s] = sources.get(s, 0) + 1
    wrong = [r for r in latest.values() if r.get("wrong_info")]
    return {"share_this_week": share(this), "share_last_week": share(last), "by_engine": by_engine,
            "rival_share": rival_share, "top_sources": sorted(sources.items(), key=lambda kv: -kv[1])[:5],
            "grid": [{"query": q, "engine": e, "mentioned": r["mentioned"], "position": r["position"]}
                     for (q, e), r in sorted(latest.items())],
            "wrong_info": [{"query": r["query"], "engine": r["engine"], "issue": r["wrong_info"]} for r in wrong],
            "queries": AI_QUERIES.get(client_id, [])}


def seo_overlap(db, client_id: str) -> dict:
    kws = list(db.seo_keywords.find({"client_id": client_id}))
    out = []
    for k in kws:
        if k["organic_rank"] <= 2 and k["status"] == "ACTIVE":
            save = round(k["paid_spend_7d"] * 0.7, 2)  # assume ~30% of paid clicks are incremental
            out.append({**k, "_id": str(k["_id"]), "suggest": "pause paid", "save_week": save})
        else:
            out.append({**k, "_id": str(k["_id"]), "suggest": "keep paid" if k["status"] == "ACTIVE" else "paused",
                        "save_week": 0})
    return {"keywords": out, "potential_savings_week": round(sum(k["save_week"] for k in out), 2)}


def check_creative(text: str, industry: str, client_id: str) -> list[str]:
    issues = []
    low = text.lower()
    for pattern, scope, why in CREATIVE_RULES:
        if scope in ("all", industry) and re.search(pattern, low):
            issues.append(why)
    for w in BRAND_VOICE_BANNED.get(client_id, []):
        if w in low:
            issues.append(f"Brand voice: the client asked never to use \"{w}\".")
    return issues


def creatives(db, client_id: str, industry: str) -> list[dict]:
    out = []
    for c in db.creatives.find({"client_id": client_id}):
        issues = check_creative(c["title"] + " " + c["body"], industry, client_id)
        out.append({"_id": str(c["_id"]), "title": c["title"], "body": c["body"], "platform": c["platform"],
                    "issues": issues, "verdict": "blocked" if issues else "ok to submit"})
    return out


def tracking_health(db, client_id: str) -> list[dict]:
    from store import campaign_stats, day_str
    now = utcnow()
    out = []
    for acc in db.accounts.find({"client_id": client_id, "active": True}):
        if acc["platform"] == "shopify":
            out.append({"account": acc["name"], "platform": acc["platform"], "status": "ok",
                        "checks": ["Order webhook received 4 minutes ago."]})
            continue
        st = campaign_stats(db, acc["_id"], day_str(now - timedelta(days=6)), day_str(now))
        rep = sum(r.get("reported_conversions") or 0 for r in st)
        ver = sum(r.get("conversions") or 0 for r in st)
        checks, status = [], "ok"
        gap = (rep - ver) / ver * 100 if ver else None
        if gap is not None and gap > 25:
            status = "warning"
            checks.append(f"Platform reports {rep} conversions vs {ver} verified ({gap:+.0f}%). Check deduplication "
                          f"and the Conversions API.")
        else:
            checks.append(f"Platform vs verified conversions within 25% ({rep} vs {ver}).")
        if acc["_id"] == "northside-google":
            status = "problem"
            checks.append("Ad 'buyer-search' points to /listings/spring-2026, which returns 404. Money is going to a dead page.")
        if acc["_id"] == "brightsmile-meta":
            status = "warning" if status == "ok" else status
            checks.append("Pixel 'Lead' event last fired 26 hours ago. Bookings may not be tracked.")
        if not checks[1:] and status == "ok":
            checks.append("Pixel and Conversions API firing normally. UTM tags present on all live ads.")
        out.append({"account": acc["name"], "platform": acc["platform"], "status": status, "checks": checks})
    return out


def draft_reply(r: dict, client_name: str) -> str:
    first = r["author"].split()[0]
    if r["stars"] >= 4:
        return f"Thank you so much, {first}! We're really glad you had a great experience. See you again soon. The {client_name} team"
    if r["stars"] == 3:
        return (f"Thanks for the honest feedback, {first}. We hear you and we're working on it. "
                f"Please reach out anytime so we can make it right. The {client_name} team")
    return (f"{first}, we're sorry about this. That's not the experience we want for anyone. "
            f"Could you contact us directly so we can fix it? The {client_name} team")


# ---------------------------------------------------------------- agent actions

def run(db, platforms, run_id: str, counts: dict) -> None:
    """Propose keyword pauses and review replies through the normal gate."""
    from platforms import platform_for
    for client in db.clients.find():
        cid = client["_id"]
        acc = db.accounts.find_one({"client_id": cid, "platform": "google"}) or \
            db.accounts.find_one({"client_id": cid, "platform": {"$ne": "shopify"}})
        if not acc or gate.halt_state_cached(db, acc["_id"], cid):
            continue
        plat = platform_for(platforms, acc)
        for k in db.seo_keywords.find({"client_id": cid, "status": "ACTIVE", "organic_rank": {"$lte": 2}}):
            target = f"keyword:{k['_id']}"
            if _seen(db, target):
                continue
            save = round(k["paid_spend_7d"] * 0.7, 2)
            reason = {"text": (f"You rank #{k['organic_rank']} organically for \"{k['keyword']}\" and still paid "
                               f"${k['paid_spend_7d']:,.2f} for it in 7 days. Pausing the paid keyword should save "
                               f"about ${save:,.2f} a week; most of those clicks come in free anyway."),
                      "numbers": {"organic_rank": k["organic_rank"], "paid_spend_7d": k["paid_spend_7d"],
                                  "save_week": save}}
            _propose(db, plat, acc, target, {"type": "pause_keyword", "field": "status", "old": "ACTIVE",
                                             "new": "PAUSED", "label": k["keyword"]}, reason, run_id, counts)
        for r in db.reviews.find({"client_id": cid, "reply": None}):
            target = f"review:{r['_id']}"
            if _seen(db, target):
                continue
            text = draft_reply(r, client["name"])
            urgent = r["stars"] <= 2
            reason = {"text": (f"{'Urgent: ' if urgent else ''}{r['stars']} star review from {r['author']}: "
                               f"\"{r['text']}\" Draft reply: \"{text}\""),
                      "numbers": {"stars": r["stars"]}}
            _propose(db, plat, acc, target, {"type": "reply_review", "field": "reply", "old": None, "new": text,
                                             "label": f"{r['stars']} star review from {r['author']}"},
                     reason, run_id, counts)


def _seen(db, target) -> bool:
    """Already proposed or already decided by policy: don't ask twice."""
    rc = gate.run_cache()
    if rc is not None:
        return target in rc["open"] or any(c[0] == target for c in rc["checked"])
    return bool(db.proposals.find_one({"campaign_id": target, "status": {"$in": ["pending", "approved", "executing"]}})
                or db.audit_events.find_one({"campaign_id": target, "event": "policy_check", "actor_id": gate.AGENT_ID}))


def _propose(db, plat, acc, target, action, reason, run_id, counts):
    item = {"campaign_id": target, "tags": []}
    out = gate.attempt(db, plat, acc, item, action, reason, actor_id=gate.AGENT_ID, mode="auto", run_id=run_id)
    if out["effect"] == "require_approval":
        now = utcnow()
        db.proposals.insert_one({
            "action_id": out["action_id"], "client_id": acc["client_id"], "account_id": acc["_id"],
            "platform": acc["platform"], "campaign_id": target, "action": action, "size_pct": 0,
            "reason": reason, "requested_by": gate.AGENT_ID, "policy_id": out["policy_id"],
            "policy_why": out["why"], "run_id": run_id, "status": "pending", "created_at": now,
            "expires_at": now + timedelta(hours=48)})
        if gate.run_cache() is not None:
            gate.run_cache()["open"].add(target)
        gate.audit(db, "proposal_created", actor_id=gate.AGENT_ID, action_id=out["action_id"],
                   client_id=acc["client_id"], run_id=run_id, campaign_id=target, policy_id=out["policy_id"])
        counts["proposed"] += 1
    elif out["effect"] == "allow":
        counts["applied"] += 1
    else:
        counts["denied"] += 1

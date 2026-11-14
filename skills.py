"""Optimization skills.

Computed from the account data where possible; demo inputs (stock, search
terms, funnel, past experiments) are simulated like the ad accounts. The
prospect audit is real: it fetches a live website.

Skills that take an action (pause an ad for an out of stock product, add a
negative keyword) go through the same gate, approvals and audit trail.
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.parse
import urllib.request
from datetime import timedelta

import gate
from evidence import kpis
from platforms import PLATFORM_LABELS, utcnow
from rules import merged_settings
from store import campaign_stats, day_str

# Leads grow roughly with budget^0.6 in paid social and search (diminishing
# returns). Shown to the user as a stated assumption, not a fact.
ELASTICITY = 0.6

PRODUCTS = {  # harbour only: campaign -> product it advertises
    "harbour": [("Winter Blend 1kg", "harbour-meta:steady", 0), ("Ethiopia Yirgacheffe", "harbour-meta:winner", 42),
                ("Espresso Starter Kit", "harbour-tiktok:ugc-videos", 7), ("Decaf Colombia", "harbour-meta:pricey", 120)],
}

SEARCH_TERMS = {  # google accounts: term, clicks, cost, conversions
    "brightsmile-google": [("free dental clinic", 48, 196.8, 0), ("dentist jobs toronto", 22, 90.2, 0),
                           ("dental implants cost toronto", 31, 127.1, 4), ("how to whiten teeth at home", 40, 164.0, 0),
                           ("emergency dentist open now", 18, 73.8, 3), ("dental hygienist school", 9, 36.9, 0)],
    "northside-google": [("rent apartment leslieville", 55, 176.0, 0), ("realtor salary ontario", 14, 44.8, 0),
                         ("sell house fast toronto", 26, 150.8, 2), ("leslieville homes for sale", 33, 105.6, 1)],
}

FUNNELS = {
    "harbour": ("Ecommerce", [("Ad click", 4800), ("Product page", 3550), ("Add to cart", 610),
                              ("Checkout started", 420), ("Purchase", 142)],
                {"Add to cart": 12.0, "Checkout started": 55.0, "Purchase": 60.0}),
    "brightsmile": ("Medical", [("Ad click", 1900), ("Booking page", 820), ("Form started", 310),
                                ("Appointment booked", 41)], {"Form started": 40.0, "Appointment booked": 45.0}),
    "northside": ("Real estate", [("Ad click", 1500), ("Listing page", 1180), ("Contact form opened", 160),
                                  ("Lead submitted", 20)], {"Contact form opened": 14.0, "Lead submitted": 35.0}),
}

EXPERIMENTS = [
    ("harbour", "Free shipping vs 10% off on Meta", "Free shipping lifts conversion rate", "won",
     "Free shipping: CVR 2.9% vs 2.2% (95% significant). Rolled out to all Meta ads.", 44),
    ("harbour", "UGC video vs studio video on TikTok", "Creator style beats polished", "won",
     "UGC: CTR 1.8% vs 0.9%, CPA $24 vs $41.", 30),
    ("brightsmile", "Price in headline ($99 exam)", "Showing price brings cheaper leads", "lost",
     "No significant difference after 3 weeks (CPA $61 vs $59). Kept original.", 21),
    ("northside", "Home value tool vs open house ad", "Value tool brings more seller leads", "running",
     "Day 9 of 21. 11 vs 7 leads so far, not significant yet.", 9),
]


def seed(db, now=None) -> None:
    now = now or utcnow()
    for coll in ("products", "search_terms", "experiments"):
        db[coll].delete_many({})
    db.products.insert_many([{"client_id": c, "name": n, "campaign_id": camp, "stock": st,
                              "source": "Shopify (simulated)"} for c, items in PRODUCTS.items()
                             for n, camp, st in items])
    db.search_terms.insert_many([{"account_id": acc, "client_id": acc.split("-")[0], "term": t, "clicks": cl,
                                  "cost_7d": co, "conversions": cv, "status": "active"}
                                 for acc, items in SEARCH_TERMS.items() for t, cl, co, cv in items])
    db.experiments.insert_many([{"client_id": c, "name": n, "hypothesis": h, "status": s, "result": r,
                                 "started_at": now - timedelta(days=d)} for c, n, h, s, r, d in EXPERIMENTS])


# ---------------------------------------------------------------- 1. marginal returns + 6. goal planner

def _campaign_rows(db, client_id):
    now = utcnow()
    rows = []
    for acc in db.accounts.find({"client_id": client_id, "active": True, "platform": {"$ne": "shopify"}}):
        s = merged_settings(db.settings.find_one({"_id": acc["_id"]}))
        stats = {r["campaign_id"]: r for r in campaign_stats(
            db, acc["_id"], day_str(now - timedelta(days=6)), day_str(now))}
        for c in db.sandbox_campaigns.find({"account_id": acc["_id"], "status": "ACTIVE"}):
            k = kpis(stats.get(c["_id"], {}))
            rows.append({"campaign_id": c["_id"], "name": c["name"], "platform": acc["platform"],
                         "budget": c["daily_budget"], "spend": k["spend"], "conversions": k["conversions"],
                         "cpa": k["cpa"], "target": s["target_cpl"]})
    return rows


def marginal_returns(db, client_id: str) -> dict:
    rows = _campaign_rows(db, client_id)
    out = []
    for r in rows:
        if not r["conversions"] or not r["spend"]:
            out.append({**r, "marginal_cpa": None, "next10_leads_week": 0.0})
            continue
        mcpa = r["cpa"] / ELASTICITY            # cost of the *next* lead, not the average one
        out.append({**r, "marginal_cpa": round(mcpa, 2), "next10_leads_week": round(70 / mcpa, 2)})
    ranked = sorted([o for o in out if o["marginal_cpa"]], key=lambda o: o["marginal_cpa"])
    move = None
    if len(ranked) >= 2 and ranked[-1]["marginal_cpa"] > ranked[0]["marginal_cpa"] * 1.3:
        best, worst = ranked[0], ranked[-1]
        gain = 70 / best["marginal_cpa"] - 70 / worst["marginal_cpa"]
        move = {"from": worst["name"], "to": best["name"], "amount_day": 10,
                "text": (f"Moving $10 a day from {worst['name']} (next lead costs about ${worst['marginal_cpa']:,.0f}) "
                         f"to {best['name']} (next lead about ${best['marginal_cpa']:,.0f}) should bring about "
                         f"{gain:.1f} more leads a week for the same total spend.")}
    return {"campaigns": out, "suggestion": move,
            "assumption": f"Leads grow with budget to the power of {ELASTICITY} (diminishing returns), "
                          f"so the next lead costs about {1 / ELASTICITY:.1f}x the average."}


def goal_plan(db, client_id: str, goal_per_week: float) -> dict:
    rows = [r for r in _campaign_rows(db, client_id) if r["conversions"]]
    if not rows:
        return {"ok": False, "error": "Not enough conversion data."}
    now_leads = sum(r["conversions"] for r in rows)
    now_spend = sum(r["spend"] for r in rows)
    if goal_per_week <= 0:
        return {"ok": False, "error": "Enter a weekly goal."}
    # leads = a * spend^e  ->  spend = now_spend * (goal / now_leads)^(1/e)
    need = now_spend * (goal_per_week / now_leads) ** (1 / ELASTICITY)
    return {"ok": True, "current_leads_week": now_leads, "current_spend_week": round(now_spend, 2),
            "goal": goal_per_week, "needed_spend_week": round(need, 2),
            "needed_budget_day": round(need / 7, 2), "blended_cpa_at_goal": round(need / goal_per_week, 2),
            "text": (f"To get {goal_per_week:.0f} a week (now {now_leads}), plan about ${need:,.0f} a week "
                     f"(${need / 7:,.0f} a day). Cost per result rises to about ${need / goal_per_week:,.2f} "
                     f"because extra budget buys pricier leads.")}


# ---------------------------------------------------------------- 4. funnel leaks

def funnel(client_id: str) -> dict:
    if client_id not in FUNNELS:
        return {"steps": []}
    industry, steps, bench = FUNNELS[client_id]
    out, worst = [], None
    for i, (name, n) in enumerate(steps):
        rate = round(n / steps[i - 1][1] * 100, 1) if i else 100.0
        b = bench.get(name)
        gap = round(rate - b, 1) if b else None
        row = {"step": name, "count": n, "rate": rate, "benchmark": b, "gap": gap}
        out.append(row)
        if gap is not None and (worst is None or gap < worst["gap"]):
            worst = row
    tip = None
    if worst and worst["gap"] < 0:
        fixes = {"Add to cart": "Product page: add reviews, clearer price, and a shipping threshold above the fold.",
                 "Checkout started": "Cart: show total with shipping early, add Shop Pay and Apple Pay.",
                 "Purchase": "Checkout: remove forced account creation, cut form fields.",
                 "Form started": "Booking page: show next available times instead of a long form.",
                 "Appointment booked": "Form: fewer fields, confirm by text, call back within 5 minutes.",
                 "Contact form opened": "Listing page: add a sticky 'Book a showing' button.",
                 "Lead submitted": "Form: ask only name, phone and preferred time."}
        tip = (f"Biggest leak: {worst['step']} at {worst['rate']}% vs a {worst['benchmark']}% benchmark. "
               f"{fixes.get(worst['step'], '')} Fixing this is likely worth more than any ad change.")
    return {"industry": industry, "steps": out, "tip": tip}


# ---------------------------------------------------------------- 5. experiments

def sample_size(baseline_cvr_pct: float, lift_pct: float) -> int:
    """Visitors per variant for 95% confidence, 80% power (two proportion z test)."""
    p1 = baseline_cvr_pct / 100
    p2 = p1 * (1 + lift_pct / 100)
    pbar = (p1 + p2) / 2
    za, zb = 1.96, 0.84
    n = ((za * math.sqrt(2 * pbar * (1 - pbar)) + zb * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2) / (p2 - p1) ** 2
    return int(math.ceil(n))


def experiments(db, client_id: str) -> dict:
    rows = _campaign_rows(db, client_id)
    clicks_week = 0
    for acc in db.accounts.find({"client_id": client_id, "platform": {"$ne": "shopify"}}):
        now = utcnow()
        clicks_week += sum(r["clicks"] for r in campaign_stats(db, acc["_id"], day_str(now - timedelta(days=6)), day_str(now)))
    conv = sum(r["conversions"] for r in rows)
    cvr = conv / clicks_week * 100 if clicks_week else 1.0
    n = sample_size(cvr, 20)
    weeks = (2 * n) / clicks_week if clicks_week else None
    past = list(db.experiments.find({"client_id": client_id}, {"_id": 0}).sort("started_at", -1))
    ideas = {"harbour": "Bundle offer (2 bags) vs free shipping on Meta",
             "brightsmile": "Next available time shown in the ad vs generic 'Book now'",
             "northside": "Neighbourhood price report vs home value tool"}
    return {"past": past, "next": {"name": ideas.get(client_id, "New offer test"), "baseline_cvr": round(cvr, 2),
                                   "lift": 20, "per_variant": n, "weeks": round(weeks, 1) if weeks else None,
                                   "text": (f"To detect a 20% lift from a {cvr:.2f}% conversion rate, each version needs "
                                            f"about {n:,} clicks, so roughly {weeks:.1f} weeks at current traffic."
                                            if weeks else "Not enough traffic yet.")}}


# ---------------------------------------------------------------- 7. anomaly radar

def anomalies(db, client_id: str) -> list[dict]:
    now = utcnow()
    out = []
    for acc in db.accounts.find({"client_id": client_id, "active": True, "platform": {"$ne": "shopify"}}):
        base = {r["campaign_id"]: kpis(r) for r in campaign_stats(
            db, acc["_id"], day_str(now - timedelta(days=14)), day_str(now - timedelta(days=1)))}
        today = {r["campaign_id"]: r for r in campaign_stats(db, acc["_id"], day_str(now), day_str(now))}
        frac = max((now.hour * 60 + now.minute) / 1440, 0.05)
        for cid, t in today.items():
            b = base.get(cid)
            if not b:
                continue
            tk = kpis(t)
            days = 14
            checks = [("CPC", tk["cpc"], b["cpc"], 1.35, "higher"), ("CTR", tk["ctr"], b["ctr"], 0.7, "lower"),
                      ("CPM", tk["cpm"], b["cpm"], 1.4, "higher")]
            for name, now_v, base_v, limit, way in checks:
                if now_v and base_v and ((way == "higher" and now_v > base_v * limit) or
                                         (way == "lower" and now_v < base_v * limit)):
                    out.append({"campaign": cid.split(":")[-1], "platform": acc["platform"], "metric": name,
                                "now": now_v, "normal": base_v,
                                "text": f"{name} is {now_v} today vs {base_v} normally."})
            spend_pace = t["spend"] / (b["spend"] / days * frac) if b["spend"] else 0
            if spend_pace > 1.8:
                out.append({"campaign": cid.split(":")[-1], "platform": acc["platform"], "metric": "Spend pace",
                            "now": round(spend_pace, 1), "normal": 1.0,
                            "text": f"Spending {spend_pace:.1f}x faster than normal for this hour."})
    return out


# ---------------------------------------------------------------- 8. prospect audit (real)

AI_BOTS = ["GPTBot", "ClaudeBot", "PerplexityBot", "Google-Extended", "CCBot"]


def _get(url: str, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; AuditBot/1.0)"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read(2_000_000)
        return r.status, body.decode("utf-8", "replace"), time.time() - t0, r.geturl()


def prospect_audit(url: str) -> dict:
    if not url:
        return {"ok": False, "error": "Enter a website."}
    if not re.match(r"^https?://", url):
        url = "https://" + url
    p = urllib.parse.urlparse(url)
    if not p.hostname or p.hostname in ("localhost", "127.0.0.1") or p.hostname.endswith(".local"):
        return {"ok": False, "error": "Enter a public website."}
    try:
        status, html, secs, final = _get(url)
    except Exception as e:
        return {"ok": False, "error": f"Could not load the site: {e}"}
    base = f"{p.scheme}://{p.netloc}"
    find = lambda pat: (re.search(pat, html, re.I | re.S) or [None, None])[1]
    title = find(r"<title[^>]*>(.*?)</title>")
    desc = find(r'<meta[^>]+name=["\']?description["\']?[^>]+content=["\']([^"\'>]*)')
    h1s = re.findall(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S)
    schema_types = sorted(set(re.findall(r'"@type"\s*:\s*"([A-Za-z]+)"', html)))
    checks = []

    def add(area, name, ok, detail):
        checks.append({"area": area, "check": name, "ok": ok, "detail": detail})

    add("Speed", "Page load", secs < 2.5, f"{secs:.1f}s to download ({len(html) // 1024} KB of HTML)")
    add("SEO", "Title", bool(title) and 15 <= len(title.strip()) <= 65,
        f"\"{(title or '').strip()[:80]}\" ({len((title or '').strip())} chars)" if title else "Missing")
    add("SEO", "Meta description", bool(desc) and 70 <= len(desc) <= 165,
        f"{len(desc)} chars" if desc else "Missing, so Google writes its own")
    add("SEO", "One H1", len(h1s) == 1, f"{len(h1s)} H1 tags")
    add("SEO", "Mobile viewport", bool(re.search(r"name=[\"']?viewport", html, re.I)), "")
    add("SEO", "Canonical tag", bool(re.search(r"rel=[\"']?canonical", html, re.I)), "")
    add("Social", "Open Graph image", "og:image" in html, "Controls how links look when shared")
    add("AI search", "Structured data", bool(schema_types),
        ", ".join(schema_types[:8]) if schema_types else "No schema.org data, so AI and Google know less about the business")
    add("AI search", "FAQ content", "FAQPage" in schema_types or bool(re.search(r"faq|frequently asked", html, re.I)),
        "Question and answer content is what AI answers quote")
    try:
        _, robots, _, _ = _get(base + "/robots.txt", 6)
        blocked = []
        for bot in AI_BOTS:
            m = re.search(rf"User-agent:\s*{re.escape(bot)}\s*(?:\n(?!User-agent).*)*", robots, re.I)
            if m and re.search(r"Disallow:\s*/\s*$", m.group(0), re.I | re.M):
                blocked.append(bot)
        add("AI search", "AI crawlers allowed", not blocked,
            "Blocked: " + ", ".join(blocked) if blocked else "robots.txt does not block GPTBot, ClaudeBot, PerplexityBot")
        add("SEO", "Sitemap listed", "sitemap" in robots.lower(), "")
    except Exception:
        add("SEO", "robots.txt", False, "Not found")
    try:
        st, _, _, _ = _get(base + "/llms.txt", 6)
        add("AI search", "llms.txt", st == 200, "Present" if st == 200 else "Not present")
    except Exception:
        add("AI search", "llms.txt", False, "Not present (optional, helps AI assistants read the site)")
    pixels = {"Meta Pixel": "fbq(" in html or "connect.facebook.net" in html,
              "Google Tag": "gtag(" in html or "googletagmanager.com" in html,
              "TikTok Pixel": "analytics.tiktok.com" in html, "Klaviyo": "klaviyo" in html.lower(),
              "Shopify": "cdn.shopify.com" in html}
    add("Tracking", "Ad pixels found", any(pixels.values()),
        ", ".join(k for k, v in pixels.items() if v) or "None found in the page HTML (may load through a tag manager)")
    score = round(sum(c["ok"] for c in checks) / len(checks) * 100)
    return {"ok": True, "url": final, "status": status, "score": score, "checks": checks,
            "summary": f"{sum(c['ok'] for c in checks)} of {len(checks)} checks passed. "
                       f"Top fixes: " + "; ".join(c["check"] for c in checks if not c["ok"])[:300]}


# ---------------------------------------------------------------- actions through the gate

def run(db, platforms, run_id: str, counts: dict) -> None:
    from channels import _propose
    from platforms import platform_for
    # 2. inventory: pause ads for products that are out of stock
    for prod in db.products.find({"stock": 0}):
        camp = db.sandbox_campaigns.find_one({"_id": prod["campaign_id"], "status": "ACTIVE"})
        if not camp:
            continue
        acc = db.accounts.find_one({"_id": camp["account_id"]})
        if gate.halt_state(db, acc["_id"], acc["client_id"]) or \
                db.proposals.find_one({"campaign_id": camp["_id"], "status": {"$in": ["pending", "approved", "executing"]}}) or \
                db.audit_events.find_one({"event": "policy_check", "campaign_id": camp["_id"], "action_type": "pause",
                                          "actor_id": gate.AGENT_ID, "context.reason_kind": "stock"}):
            continue
        reason = {"text": (f"{prod['name']} is out of stock in Shopify, but {camp['name']} keeps advertising it at "
                           f"${camp['daily_budget']:,.2f} a day. Pause until it's back in stock."),
                  "numbers": {"stock": 0, "daily_budget": camp["daily_budget"]}}
        out = gate.attempt(db, platform_for(platforms, acc), acc, {"campaign_id": camp["_id"], "tags": camp.get("tags", [])},
                           {"type": "pause", "field": "status", "old": camp["daily_budget"], "new": None,
                            "label": f"out of stock: {prod['name']}"},
                           reason, actor_id=gate.AGENT_ID, mode="auto", run_id=run_id,
                           extra_ctx={"reason_kind": "stock"})
        if out["effect"] == "require_approval":
            now = utcnow()
            db.proposals.insert_one({
                "action_id": out["action_id"], "client_id": acc["client_id"], "account_id": acc["_id"],
                "platform": acc["platform"], "campaign_id": camp["_id"],
                "action": {"type": "pause", "field": "status", "old": camp["daily_budget"], "new": None,
                           "label": f"out of stock: {prod['name']}"}, "size_pct": 100, "reason": reason,
                "requested_by": gate.AGENT_ID, "policy_id": out["policy_id"], "policy_why": out["why"],
                "run_id": run_id, "status": "pending", "created_at": now, "expires_at": now + timedelta(hours=48)})
            counts["proposed"] += 1
    # 3. search terms: negatives for terms that cost money and never convert
    for t in db.search_terms.find({"status": "active", "conversions": 0, "cost_7d": {"$gte": 30}}):
        acc = db.accounts.find_one({"_id": t["account_id"]})
        target = f"term:{t['_id']}"
        if not acc or gate.halt_state(db, acc["_id"], acc["client_id"]) or \
                db.audit_events.find_one({"campaign_id": target, "event": "policy_check", "actor_id": gate.AGENT_ID}):
            continue
        reason = {"text": (f"Search term \"{t['term']}\" cost ${t['cost_7d']:,.2f} for {t['clicks']} clicks in 7 days "
                           f"with 0 conversions, and the intent doesn't match. Add it as a negative keyword."),
                  "numbers": {"cost_7d": t["cost_7d"], "clicks": t["clicks"]}}
        _propose(db, platform_for(platforms, acc), acc, target,
                 {"type": "add_negative", "field": "status", "old": "active", "new": "negative", "label": t["term"]},
                 reason, run_id, counts)


def search_terms(db, client_id: str) -> list[dict]:
    out = []
    for t in db.search_terms.find({"client_id": client_id}):
        cpa = t["cost_7d"] / t["conversions"] if t["conversions"] else None
        out.append({"term": t["term"], "clicks": t["clicks"], "cost_7d": t["cost_7d"], "conversions": t["conversions"],
                    "cpa": round(cpa, 2) if cpa else None, "status": t["status"],
                    "verdict": "negative" if t["status"] == "negative" else
                    "waste" if not t["conversions"] and t["cost_7d"] >= 30 else "keep"})
    return out


def inventory(db, client_id: str) -> list[dict]:
    out = []
    for p in db.products.find({"client_id": client_id}, {"_id": 0}):
        c = db.sandbox_campaigns.find_one({"_id": p["campaign_id"]}) or {}
        out.append({**p, "campaign": c.get("name"), "campaign_status": c.get("status"),
                    "budget": c.get("daily_budget"),
                    "verdict": ("pause ads" if p["stock"] == 0 and c.get("status") == "ACTIVE" else
                                "low stock: don't scale" if 0 < p["stock"] < 15 else
                                "paused (no stock)" if p["stock"] == 0 else "ok")})
    return out

"""Plain language client report. No jargon: the client sees where the money
went, what it brought, and what the agency changed and why.

Served by the dashboard at /report?client=<id>. Print to PDF from the browser.
"""

from __future__ import annotations

import html
from datetime import timedelta

from evidence import kpis
from platforms import PLATFORM_LABELS, utcnow
from rules import merged_settings
from store import campaign_stats, day_str

RESULT_WORD = {"Ecommerce": ("order", "orders"), "Medical": ("new patient", "new patients"),
               "Real estate": ("buyer lead", "buyer leads")}


def _m(x):
    return "n/a" if x is None else f"${x:,.2f}"


def _plain_change(c: dict, names: dict, target: float, word: str) -> str:
    a = c["action"]
    camp = names.get(c["campaign_id"], c["campaign_id"].split(":")[-1])
    plat = PLATFORM_LABELS.get(c.get("platform"), c.get("platform"))
    nums = (c.get("reason") or {}).get("numbers") or {}
    if c.get("reverts"):
        return f"We reversed an earlier change to {camp} on {plat} after reviewing it."
    if a["type"] == "set_budget":
        diff = a["new"] - a["old"]
        cpl = nums.get("cpl")
        if diff > 0:
            return (f"We added ${diff:,.2f} a day to {camp} on {plat}, because it brings each "
                    f"{word} for {_m(cpl)}, better than your {_m(target)} goal.")
        return (f"We moved ${-diff:,.2f} a day out of {camp} on {plat}, because each {word} there "
                f"cost {_m(cpl)}, above your {_m(target)} goal.")
    if a["type"] == "pause":
        return f"We paused {camp} on {plat}, because it spent money without bringing any {word}s."
    if a["type"] == "resume":
        return f"We turned {camp} on {plat} back on."
    if a["type"] == "set_targeting":
        n = a["new"]
        return f"We focused {camp} on {plat} on people aged {n['age_min']} to {n['age_max']}, who were responding best."
    return f"We made a change to {camp} on {plat}."


def render(db, client_id: str, days: int = 7) -> str:
    client = db.clients.find_one({"_id": client_id})
    if not client:
        return "<p>Client not found.</p>"
    one, many = RESULT_WORD.get(client.get("industry"), ("result", "results"))
    now = utcnow()
    accounts = [a for a in db.accounts.find({"client_id": client_id, "active": True}) if a["platform"] != "shopify"]
    by_platform, rows, target, budget_day = {}, [], None, 0.0
    names = {}
    for acc in accounts:
        s = merged_settings(db.settings.find_one({"_id": acc["_id"]}))
        target = target or s["target_cpl"]
        cur = campaign_stats(db, acc["_id"], day_str(now - timedelta(days=days - 1)), day_str(now))
        prev = campaign_stats(db, acc["_id"], day_str(now - timedelta(days=2 * days - 1)), day_str(now - timedelta(days=days)))
        for c in db.sandbox_campaigns.find({"account_id": acc["_id"]}):
            names[c["_id"]] = c["name"]
            if c["status"] == "ACTIVE":
                budget_day += c["daily_budget"]
        agg = {f: sum(r.get(f) or 0 for r in cur) for f in ("spend", "conversions", "revenue", "clicks", "impressions")}
        pagg = {f: sum(r.get(f) or 0 for r in prev) for f in ("spend", "conversions", "revenue")}
        p = by_platform.setdefault(acc["platform"], {"spend": 0, "conversions": 0, "revenue": 0,
                                                     "pspend": 0, "pconv": 0, "target": s["target_cpl"]})
        for f in ("spend", "conversions", "revenue"):
            p[f] += agg[f]
        p["pspend"] += pagg["spend"]
        p["pconv"] += pagg["conversions"]

    spend = sum(p["spend"] for p in by_platform.values())
    conv = sum(p["conversions"] for p in by_platform.values())
    rev = sum(p["revenue"] for p in by_platform.values())
    pspend = sum(p["pspend"] for p in by_platform.values())
    pconv = sum(p["pconv"] for p in by_platform.values())
    cpa = spend / conv if conv else None
    pcpa = pspend / pconv if pconv else None
    better = cpa is not None and pcpa is not None and cpa < pcpa

    summary = (f"This week your ads cost {_m(spend)} and brought {conv} {many}"
               + (f" at {_m(cpa)} each" if cpa else "")
               + (f", {'better' if better else 'more'} than last week's {_m(pcpa)}" if pcpa else "")
               + (f". Your goal is {_m(target)} per {one}." if target else "."))
    if rev:
        summary += f" Every $1 spent brought back ${rev / spend:,.2f} in sales."

    maxs = max([p["spend"] for p in by_platform.values()] or [1]) or 1
    bars = []
    for plat, p in sorted(by_platform.items(), key=lambda kv: -kv[1]["spend"]):
        pc = p["spend"] / p["conversions"] if p["conversions"] else None
        light = ("green", "Working well") if pc and pc <= p["target"] else \
                ("yellow", "Being tuned") if pc and pc <= p["target"] * 1.3 else ("red", "Fixing")
        if not p["conversions"]:
            light = ("red", "Fixing")
        bars.append(f"""<div class="bar"><div class="lbl">{html.escape(PLATFORM_LABELS.get(plat, plat))}
          <span class="light {light[0]}">{light[1]}</span></div>
          <div class="track"><div class="fill" style="width:{p['spend'] / maxs * 100:.0f}%"></div></div>
          <div class="nums">{_m(p['spend'])} spent · {p['conversions']} {many} · {_m(pc)} each</div></div>""")

    changes = list(db.change_log.find({"client_id": client_id, "result.ok": True,
                                       "applied_at": {"$gte": now - timedelta(days=days)}}).sort("applied_at", -1))
    change_items = "".join(f"<li><span class='when'>{c['applied_at']:%b %d}</span> "
                           f"{html.escape(_plain_change(c, names, target or 0, one))}</li>" for c in changes) \
        or "<li>No changes this week. Everything stayed on track.</li>"
    pending = list(db.proposals.find({"client_id": client_id, "status": "pending"}))
    next_items = "".join(
        f"<li>Reviewing a change to {html.escape(names.get(p['campaign_id'], p['campaign_id']))} "
        f"on {html.escape(PLATFORM_LABELS.get(p.get('platform'), p.get('platform')))}. Your strategist decides before anything happens.</li>"
        for p in pending[:4]) or "<li>No open questions right now.</li>"
    week_budget = budget_day * days
    pace = spend / week_budget * 100 if week_budget else 0

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(client['name'])} Weekly Report</title>
<style>
:root {{ --ink:#001e2b; --muted:#5c6c75; --line:#e8edeb; --bg:#f9fbfa; --panel:#fff; --accent:#00684a; --brand:#00ed64; }}
@media (prefers-color-scheme: dark) {{ :root {{ --ink:#e8edeb; --muted:#889397; --line:#1c2d38; --bg:#001e2b; --panel:#112733; --accent:#00ed64; --brand:#00ed64; color-scheme: dark; }} }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:var(--bg); color:var(--ink);
  font:16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
.wrap {{ max-width:760px; margin:0 auto; padding:28px 16px 60px; }}
h1 {{ font-size:26px; margin:0; display:flex; align-items:center; gap:10px; }}
h1::before {{ content:""; width:10px; height:26px; border-radius:3px; background:var(--brand); }} .sub {{ color:var(--muted); margin:4px 0 22px; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:18px 20px; margin-bottom:16px; }}
.big {{ font-size:19px; font-weight:500; }}
h2 {{ font-size:14px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin:0 0 12px; }}
.stats {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(150px,1fr)); gap:12px; }}
.stat b {{ display:block; font-size:24px; }} .stat span {{ color:var(--muted); font-size:13px; }}
.bar {{ margin-bottom:14px; }} .lbl {{ display:flex; justify-content:space-between; font-weight:600; }}
.track {{ height:10px; background:var(--line); border-radius:6px; margin:6px 0 4px; overflow:hidden; }}
.fill {{ height:100%; background:var(--accent); border-radius:6px; }}
.nums {{ color:var(--muted); font-size:14px; }}
.light {{ font-size:12px; padding:2px 8px; border-radius:999px; font-weight:600; }}
.green {{ background:#e3fcf7; color:#00684a; }} .yellow {{ background:#fbf0d9; color:#8a5a00; }} .red {{ background:#fde8e6; color:#b42318; }}
ul {{ margin:0; padding-left:20px; }} li {{ margin-bottom:8px; }} .when {{ color:var(--muted); font-size:13px; margin-right:6px; }}
.print {{ float:right; font:inherit; font-size:14px; padding:6px 12px; border-radius:8px; border:1px solid var(--line); background:var(--panel); color:var(--ink); cursor:pointer; }}
@media print {{ .print {{ display:none; }} body {{ background:#fff; }} }}
</style></head><body><div class="wrap">
<button class="print" onclick="window.print()">Save as PDF</button>
<h1>{html.escape(client['name'])}</h1>
<div class="sub">Your ads, week ending {now:%B %d, %Y}</div>
<div class="card big">{html.escape(summary)}</div>
<div class="card"><h2>At a glance</h2><div class="stats">
  <div class="stat"><b>{_m(spend)}</b><span>spent on ads</span></div>
  <div class="stat"><b>{conv}</b><span>{many}</span></div>
  <div class="stat"><b>{_m(cpa)}</b><span>cost per {one} (goal {_m(target)})</span></div>
  {f'<div class="stat"><b>${rev / spend:,.2f}</b><span>back for every $1 spent</span></div>' if rev and spend else ''}
  <div class="stat"><b>{pace:.0f}%</b><span>of this week's budget used</span></div>
</div></div>
<div class="card"><h2>Where your money went</h2>{''.join(bars)}</div>
<div class="card"><h2>What we changed and why</h2><ul>{change_items}</ul></div>
<div class="card"><h2>What we're looking at next</h2><ul>{next_items}</ul></div>
<div class="sub">Every change above was checked against your account rules and approved by your agency team. Questions? Reply to your strategist.</div>
</div></body></html>"""

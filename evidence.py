"""Evidence for every decision: the numbers, how sure we are, and what we expect.

All math is plain and explainable on purpose:
  * confidence: conversions are treated as a Poisson count. If the campaign
    really performed at target, we would expect spend / target conversions.
    The z score of what we actually saw against that gives the confidence.
  * cost per acquisition range: 95% interval on the conversion count.
"""

from __future__ import annotations

import math


def _div(a, b):
    return a / b if b else None


def kpis(st: dict) -> dict:
    spend = float(st.get("spend") or 0)
    imp = int(st.get("impressions") or 0)
    clicks = int(st.get("clicks") or 0)
    conv = int(st.get("conversions") or 0)
    rep = int(st.get("reported_conversions") or 0)
    rev = float(st.get("revenue") or 0)
    reach = int(st.get("reach") or 0)
    out = {
        "spend": round(spend, 2), "impressions": imp, "reach": reach, "clicks": clicks,
        "conversions": conv, "reported_conversions": rep, "revenue": round(rev, 2),
        "cpm": _div(spend * 1000, imp), "cpc": _div(spend, clicks), "ctr": _div(clicks * 100, imp),
        "cvr": _div(conv * 100, clicks), "cpa": _div(spend, conv), "roas": _div(rev, spend) if rev else None,
        "aov": _div(rev, conv) if rev else None, "frequency": _div(imp, reach),
        "platform_overcount_pct": _div((rep - conv) * 100, conv) if conv else None,
    }
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in out.items()}


def _phi(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def confidence_vs_target(spend: float, conv: int, target: float, worse: bool) -> float | None:
    """How sure we are that true cost per lead is worse (or better) than target."""
    if not target or spend <= 0:
        return None
    expected = spend / target                # conversions we'd see if exactly on target
    if expected <= 0:
        return None
    z = (expected - conv) / math.sqrt(expected)
    p = _phi(z) if worse else _phi(-z)
    return round(p * 100, 1)


def cpa_range(spend: float, conv: int) -> tuple[float | None, float | None]:
    if conv <= 0:
        return None, None
    hi_c = conv + 1.96 * math.sqrt(conv)
    lo_c = max(conv - 1.96 * math.sqrt(conv), 0.5)
    return round(spend / hi_c, 2), round(spend / lo_c, 2)


def build(action, st: dict, prev: dict | None, account_cpa: float | None, settings: dict,
          last_ts=None) -> dict:
    """Everything a strategist needs to judge one proposed change."""
    k = kpis(st)
    target = float(settings["target_cpl"])
    days = settings["window_days"]
    cpa = k["cpa"]
    t = action.type
    a = action.as_doc()

    worse = t in ("pause",) or (t == "set_budget" and a["new"] < a["old"])
    conf = confidence_vs_target(k["spend"], k["conversions"], target, worse) if t != "set_targeting" else None
    lo, hi = cpa_range(k["spend"], k["conversions"])

    pk = kpis(prev) if prev else None
    prev_cpa = pk["cpa"] if pk else None
    if cpa and prev_cpa:
        change = (cpa - prev_cpa) / prev_cpa * 100
        trend = "getting worse" if change > 10 else "improving" if change < -10 else "stable"
    else:
        change, trend = None, "not enough history"

    # Expected weekly impact, deliberately conservative for increases
    # (extra budget usually buys slightly pricier leads).
    impact = {}
    if t == "set_budget":
        delta = a["new"] - a["old"]
        weekly = delta * 7
        marginal = (cpa or target) * (1.2 if delta > 0 else 1.0)
        impact = {"spend_per_week": round(weekly, 2),
                  "leads_per_week": round(weekly / marginal, 1) if marginal else None}
        if delta < 0:
            impact["summary"] = (f"Saves about ${-weekly:,.0f} a week for about "
                                 f"{-impact['leads_per_week']:.1f} fewer leads, at ${cpa:,.2f} each.")
        else:
            impact["summary"] = (f"Spends about ${weekly:,.0f} more a week for about "
                                 f"{impact['leads_per_week']:.1f} more leads.")
    elif t == "pause":
        impact = {"spend_per_week": round(-(a.get("old") or 0) * 7, 2), "leads_per_week": 0.0,
                  "summary": f"Stops about ${(a.get('old') or 0) * 7:,.0f} a week that brought no leads."}
    elif t == "set_targeting":
        seg = action.numbers.get("segment_cpl")
        impact = {"summary": f"If the best age group holds, cost per lead moves toward ${seg:,.2f}."}

    size = action.size_pct
    resets_learning = t == "set_targeting" or size > 20
    if t == "pause" or (conf is not None and conf < 70):
        risk = "high"
    elif size >= 15 or resets_learning or (conf is not None and conf < 85) or t == "set_targeting":
        risk = "medium"
    else:
        risk = "low"

    alternatives = []
    if t == "set_budget":
        step = settings["max_change_pct"]
        alternatives.append(f"Keep the budget: rejected, cost per lead is "
                            f"{'above' if worse else 'below'} the ${target:,.2f} target "
                            f"with {conf or 0:.0f}% confidence.")
        alternatives.append(f"Move more than {step:.0f}%: rejected, bigger steps risk the platform's learning phase.")
        if worse:
            alternatives.append("Pause it: rejected, it still brings leads, just too expensive ones.")
    elif t == "pause":
        alternatives.append("Cut the budget instead: rejected, zero leads means any budget is wasted.")
        alternatives.append("Wait longer: rejected, spend is already several times the target cost per lead.")
    elif t == "set_targeting":
        alternatives.append("Keep broad targeting: rejected, one age group converts much cheaper.")
        alternatives.append("Exclude ages instead of narrowing: similar effect, harder to reverse.")

    rep, ver = k["reported_conversions"], k["conversions"]
    tracking = ("Platform and store numbers match." if not ver or k["platform_overcount_pct"] is None
                or abs(k["platform_overcount_pct"]) < 10 else
                f"Platform claims {rep} conversions, store or CRM shows {ver} "
                f"({k['platform_overcount_pct']:+.0f}%). Decision uses the verified number.")

    return {
        "window_days": days, "kpis": k, "target_cpa": target,
        "confidence_pct": conf,
        "confidence_text": (f"{conf:.0f}% confident cost per lead is {'above' if worse else 'below'} target, "
                            f"based on {k['conversions']} conversions and {k['clicks']} clicks."
                            if conf is not None else None),
        "cpa_range_95": [lo, hi],
        "baseline": {"previous_period_cpa": prev_cpa, "account_avg_cpa": account_cpa,
                     "change_vs_previous_pct": round(change, 1) if change is not None else None,
                     "trend": trend},
        "impact": impact, "risk": risk,
        "learning_phase": ("This change likely resets the platform's learning phase."
                           if resets_learning else "Change is under 20%, so the learning phase should hold."),
        "alternatives": alternatives, "tracking": tracking,
        "data_as_of": last_ts,
    }

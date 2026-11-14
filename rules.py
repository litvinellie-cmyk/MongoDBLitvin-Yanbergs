"""Guardrails and decisions. Pure functions, no database, so they can be tested."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

DEFAULT_SETTINGS = {
    "target_cpl": 40.0,                    # dollars per lead we aim for
    "daily_spend_cap": 300.0,              # sum of active daily budgets may not exceed this
    "window_days": 7,                      # how far back stats look
    "band_pct": 20,                        # do nothing while CPL is within +/-20% of target
    "max_change_pct": 15,                  # biggest single budget move
    "auto_apply_max_pct": 10,              # moves at or under this run without approval
    "auto_mode": True,                     # False sends everything to the approval queue
    "max_edits_per_campaign_per_day": 1,   # protects the platforms' learning phase
    "min_hours_between_edits": 24,
    "min_spend_before_action": 100.0,      # no decisions on tiny samples
    "min_conversions_before_action": 5,
    "loss_multiple": 3.0,                  # spend this many x target with 0 leads -> propose pause
    "min_daily_budget": 5.0,
    "proposal_ttl_hours": 48,
}


def merged_settings(doc: dict | None) -> dict:
    s = dict(DEFAULT_SETTINGS)
    if doc:
        s.update({k: v for k, v in doc.items() if k != "_id"})
    return s


@dataclass
class Action:
    campaign_id: str
    type: str                  # "set_budget" | "pause"
    old: float | None
    new: float | None
    size_pct: float
    reason: str
    numbers: dict = field(default_factory=dict)
    auto: bool = False

    def as_doc(self) -> dict:
        return {"type": self.type, "field": "daily_budget", "old": self.old, "new": self.new}


def frequency_blocked(s: dict, edits_today: int, last_edit_at: datetime | None, now: datetime) -> bool:
    if edits_today >= s["max_edits_per_campaign_per_day"]:
        return True
    if last_edit_at and now - last_edit_at < timedelta(hours=s["min_hours_between_edits"]):
        return True
    return False


def decide(
    c: dict,
    s: dict,
    *,
    edits_today: int,
    last_edit_at: datetime | None,
    now: datetime,
    account_budget_total: float,
) -> Action | None:
    """c: {campaign_id, status, spend, conversions, clicks, daily_budget} over the window."""
    if c.get("status") != "ACTIVE":
        return None
    if frequency_blocked(s, edits_today, last_edit_at, now):
        return None

    spend = float(c.get("spend", 0))
    conv = int(c.get("conversions", 0))
    budget = float(c.get("daily_budget", 0))
    target = float(s["target_cpl"])
    days = s["window_days"]
    numbers = {"window_days": days, "spend": round(spend, 2), "conversions": conv,
               "clicks": int(c.get("clicks", 0)), "target_cpl": target, "daily_budget": budget}

    # Loss control: money gone, nothing back. Always needs a human.
    if conv == 0 and spend >= s["loss_multiple"] * target:
        return Action(
            c["campaign_id"], "pause", budget, None, 100.0,
            f"Spent ${spend:,.2f} in the last {days} days with 0 leads. "
            f"That is {spend / target:.1f}x the ${target:,.2f} target cost per lead.",
            numbers, auto=False,
        )

    # Sample size: not enough data to tell signal from noise.
    if spend < s["min_spend_before_action"] or conv < s["min_conversions_before_action"]:
        return None

    cpl = spend / conv
    numbers["cpl"] = round(cpl, 2)
    band = s["band_pct"] / 100
    if cpl > target * (1 + band):
        direction = -1
    elif cpl < target * (1 - band):
        direction = 1
    else:
        return None

    new = budget * (1 + direction * s["max_change_pct"] / 100)
    if direction > 0:
        headroom = s["daily_spend_cap"] - account_budget_total
        if headroom <= 0:
            return None
        new = min(new, budget + headroom)
    new = round(max(new, s["min_daily_budget"]), 2)
    if budget <= 0 or abs(new - budget) < 0.01:
        return None

    size = abs(new - budget) / budget * 100
    verb = "up" if direction > 0 else "down"
    why = "below" if direction > 0 else "above"
    reason = (
        f"Cost per lead was ${cpl:,.2f} over the last {days} days "
        f"({conv} leads on ${spend:,.2f}), {why} the ${target:,.2f} target. "
        f"Daily budget {verb} {size:.0f}% from ${budget:,.2f} to ${new:,.2f}."
    )
    auto = bool(s["auto_mode"]) and size <= s["auto_apply_max_pct"] + 1e-9
    return Action(c["campaign_id"], "set_budget", budget, new, round(size, 1), reason, numbers, auto)

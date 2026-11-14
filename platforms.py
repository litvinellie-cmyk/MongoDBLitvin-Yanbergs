"""Ad platform adapters. All money in dollars here; adapters convert.

Each adapter returns campaigns as:
  {campaign_id, name, status ("ACTIVE"/"PAUSED"), daily_budget,
   spend, conversions, clicks, impressions}   <- today's totals so far
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone


class Platform:
    name = "base"

    def fetch_campaigns(self, account: dict, now: datetime) -> list[dict]:
        raise NotImplementedError

    def set_budget(self, account: dict, campaign_id: str, new_budget: float) -> None:
        raise NotImplementedError

    def pause(self, account: dict, campaign_id: str) -> None:
        raise NotImplementedError

    def resume(self, account: dict, campaign_id: str) -> None:
        raise NotImplementedError


class MetaPlatform(Platform):
    """Meta Marketing API. Not wired yet.

    fetch:   GET /act_{id}/insights?level=campaign&date_preset=today
             fields=campaign_id,spend,clicks,impressions,actions
             plus GET /act_{id}/campaigns?fields=id,name,status,daily_budget
    budget:  POST /{campaign_id} daily_budget=<cents>  (campaign budget on);
             for ad set budgets, POST /{adset_id} instead
    pause:   POST /{campaign_id} status=PAUSED
    Use a sandbox ad account for the hackathon.
    """

    name = "meta"


class GooglePlatform(Platform):
    """Google Ads API. Not wired yet.

    fetch:   GoogleAdsService.search with GAQL
             SELECT campaign.id, campaign.name, campaign.status,
                    campaign_budget.amount_micros, metrics.cost_micros,
                    metrics.conversions, metrics.clicks, metrics.impressions
             FROM campaign WHERE segments.date DURING TODAY
    budget:  CampaignBudgetService.mutate_campaign_budgets amount_micros
             (a shared budget moves every campaign on it, so check that first)
    pause:   CampaignService.mutate_campaigns status=PAUSED
    Use a Google Ads test manager account for the hackathon.
    """

    name = "google"


def _noise(*parts: str) -> float:
    """Stable pseudo random factor in [0.75, 1.25] for a key."""
    h = hashlib.sha256("|".join(parts).encode()).digest()
    return 0.75 + (h[0] / 255) * 0.5


class SandboxPlatform(Platform):
    """Fake platform backed by the sandbox_campaigns collection.

    Each campaign has a hidden true CPL that gets worse as budget grows
    (diminishing returns), so the agent's moves have visible effects.
    """

    name = "sandbox"

    def __init__(self, db):
        self.col = db.sandbox_campaigns

    @staticmethod
    def _today_totals(c: dict, day: str, frac: float) -> dict:
        budget = c["daily_budget"] if c["status"] == "ACTIVE" else 0.0
        spend = round(budget * frac * _noise(c["_id"], day, "s") ** 0.3, 2)
        true_cpl = c["true_cpl"] * (max(c["daily_budget"], 1) / c["base_budget"]) ** 0.4
        # add a stable fraction so small daily numbers are not always floored to 0
        frac_up = _noise(c["_id"], day, "f") * 2 - 1.5  # [0, 1)
        conv = int(spend / (true_cpl * _noise(c["_id"], day, "c")) + max(0.0, frac_up))
        clicks = int(spend * c["clicks_per_dollar"])
        return {"spend": spend, "conversions": conv, "clicks": clicks, "impressions": clicks * 40}

    def fetch_campaigns(self, account, now):
        day = now.date().isoformat()
        frac = (now.hour * 60 + now.minute) / 1440
        out = []
        for c in self.col.find({"account_id": account["_id"]}):
            out.append({
                "campaign_id": c["_id"], "name": c["name"], "status": c["status"],
                "daily_budget": c["daily_budget"], "tags": c.get("tags", []),
                **self._today_totals(c, day, frac),
            })
        return out

    def set_budget(self, account, campaign_id, new_budget):
        r = self.col.update_one({"_id": campaign_id, "account_id": account["_id"]},
                                {"$set": {"daily_budget": float(new_budget)}})
        if r.matched_count != 1:
            raise RuntimeError(f"sandbox campaign {campaign_id} not found")

    def _set_status(self, account, campaign_id, status):
        r = self.col.update_one({"_id": campaign_id, "account_id": account["_id"]},
                                {"$set": {"status": status}})
        if r.matched_count != 1:
            raise RuntimeError(f"sandbox campaign {campaign_id} not found")

    def pause(self, account, campaign_id):
        self._set_status(account, campaign_id, "PAUSED")

    def resume(self, account, campaign_id):
        self._set_status(account, campaign_id, "ACTIVE")

    def seed_account(self, db, account: dict, settings: dict, campaigns: list,
                     backfill_days: int, now: datetime) -> None:
        """Create one demo ad account with campaigns and N past days of snapshots."""
        aid = account["_id"]
        db.accounts.replace_one({"_id": aid}, account, upsert=True)
        db.settings.replace_one({"_id": aid}, {"_id": aid, **settings}, upsert=True)
        for name, budget, cpl, cpd, tags in campaigns:
            self.col.replace_one(
                {"_id": f"{aid}:{name}"},
                {"_id": f"{aid}:{name}", "account_id": aid, "client_id": account["client_id"],
                 "name": name, "status": "ACTIVE", "daily_budget": budget, "base_budget": budget,
                 "true_cpl": cpl, "clicks_per_dollar": cpd, "tags": tags},
                upsert=True,
            )
        docs = []
        for back in range(backfill_days, 0, -1):
            day_end = (now - timedelta(days=back)).replace(hour=23, minute=59, second=0, microsecond=0)
            for c in self.col.find({"account_id": aid}):
                t = self._today_totals(c, day_end.date().isoformat(), 1.0)
                docs.append({
                    "ts": day_end, "day": day_end.date().isoformat(),
                    "meta": {"account_id": aid, "campaign_id": c["_id"], "platform": account["platform"]},
                    "status": c["status"], "daily_budget": c["daily_budget"], **t,
                })
        if docs:
            db.metrics_snapshots.insert_many(docs)


class TikTokPlatform(Platform):
    """TikTok Marketing API. Not wired yet.

    fetch:   GET /open_api/v1.3/report/integrated/get/ (data_level=AUCTION_CAMPAIGN)
    budget:  POST /open_api/v1.3/campaign/update/ budget=<amount>
    pause:   POST /open_api/v1.3/campaign/status/update/ operation_status=DISABLE
    """

    name = "tiktok"


class RedditPlatform(Platform):
    """Reddit Ads API v3. Not wired yet.

    fetch:   POST /api/v3/ad_accounts/{id}/reports (level CAMPAIGN)
    budget:  PATCH /api/v3/ad_groups/{id} goal_value (budgets live on ad groups)
    pause:   PATCH /api/v3/campaigns/{id} configured_status=PAUSED
    """

    name = "reddit"


class ShopifyStore(Platform):
    """Shopify Admin API, read only: orders and revenue to check what the ad
    platforms claim. Writes are refused here and denied by policy."""

    name = "shopify"

    def fetch_campaigns(self, account, now):
        return []


PLATFORM_LABELS = {"meta": "Facebook + Instagram", "google": "Google Ads", "tiktok": "TikTok",
                   "reddit": "Reddit Ads", "shopify": "Shopify", "sandbox": "Sandbox"}


def build_platforms(db) -> dict[str, Platform]:
    return {"sandbox": SandboxPlatform(db), "meta": MetaPlatform(), "google": GooglePlatform(),
            "tiktok": TikTokPlatform(), "reddit": RedditPlatform(), "shopify": ShopifyStore()}


def platform_for(platforms: dict, account: dict) -> Platform:
    """Demo accounts set adapter="sandbox" to run on simulated data while
    keeping their real platform name for policy and display."""
    return platforms[account.get("adapter") or account["platform"]]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)

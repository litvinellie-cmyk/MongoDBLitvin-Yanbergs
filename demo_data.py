"""Three made up clients for the demo. Every number is simulated.

Campaign tuple: (name, daily budget, hidden true cost per lead, clicks per $, tags)
"""

CLIENTS = [
    {
        "_id": "harbour", "name": "Harbour Coffee Co.", "industry": "Ecommerce",
        "accounts": [
            {"_id": "harbour-meta", "platform": "meta", "name": "Harbour on Facebook + Instagram",
             "settings": {"value_per_conversion": 38.0, "target_cpl": 40.0, "daily_spend_cap": 605.0},
             "campaigns": [
                 ("winner", 150.0, 22.0, 3.0, [],              # cheap: +10% on its own
                  {"insights": {"best_age": {"age_min": 25, "age_max": 34, "cpl": 14.80}}}),
                 ("steady", 120.0, 41.0, 2.5, []),            # on target: left alone
                 ("pricey", 160.0, 75.0, 2.0, []),            # expensive: -15% needs approval
                 ("dud", 60.0, 10_000.0, 1.5, []),            # no leads: pause needs approval
                 ("brand", 100.0, 70.0, 2.0, ["protected"]),  # protected: denied
             ]},
            {"_id": "harbour-tiktok", "platform": "tiktok", "name": "Harbour on TikTok",
             "settings": {"value_per_conversion": 38.0, "target_cpl": 40.0, "daily_spend_cap": 148.0},
             "campaigns": [
                 ("ugc-videos", 80.0, 28.0, 4.0, []),         # cheap: +10% on its own
                 ("spark-ads", 60.0, 70.0, 3.0, []),          # expensive: needs approval
             ]},
            {"_id": "harbour-reddit", "platform": "reddit", "name": "Harbour on Reddit",
             "settings": {"value_per_conversion": 38.0, "target_cpl": 40.0, "daily_spend_cap": 60.0},
             "campaigns": [
                 ("r-coffee", 50.0, 24.0, 2.0, []),           # cheap, but Reddit always asks
             ]},
            {"_id": "harbour-shopify", "platform": "shopify", "name": "Harbour Shopify store",
             "settings": {"value_per_conversion": 38.0, "target_cpl": 40.0, "daily_spend_cap": 0.0}, "campaigns": []},
        ],
    },
    {
        "_id": "brightsmile", "name": "Bright Smile Dental", "industry": "Medical",
        "accounts": [
            {"_id": "brightsmile-google", "platform": "google", "name": "Bright Smile on Google Ads",
             "settings": {"value_per_conversion": 220.0, "target_cpl": 60.0, "daily_spend_cap": 220.0},
             "campaigns": [
                 ("implants-search", 120.0, 58.0, 1.5, []),   # on target
                 ("whitening-pmax", 90.0, 110.0, 2.0, []),    # expensive: needs approval
             ]},
            {"_id": "brightsmile-meta", "platform": "meta", "name": "Bright Smile on Facebook + Instagram",
             "settings": {"value_per_conversion": 220.0, "target_cpl": 60.0, "daily_spend_cap": 77.0},
             "campaigns": [
                 ("new-patient-offer", 70.0, 35.0, 2.5, [],   # cheap: +10%; targeting needs review
                  {"insights": {"best_age": {"age_min": 35, "age_max": 54, "cpl": 24.10}}}),
             ]},
        ],
    },
    {
        "_id": "northside", "name": "Northside Realty", "industry": "Real estate",
        "accounts": [
            {"_id": "northside-meta", "platform": "meta", "name": "Northside on Facebook + Instagram",
             "settings": {"value_per_conversion": 0.0, "target_cpl": 90.0, "daily_spend_cap": 260.0},
             "campaigns": [
                 ("open-house", 100.0, 55.0, 2.0, [],         # cheap: +10%; age targeting DENIED
                  {"insights": {"best_age": {"age_min": 25, "age_max": 40, "cpl": 38.50}}}),
                 ("luxury-listings", 150.0, 10_000.0, 1.0, []),  # no leads: pause needs approval
             ]},
            {"_id": "northside-google", "platform": "google", "name": "Northside on Google Ads",
             "settings": {"value_per_conversion": 0.0, "target_cpl": 90.0, "daily_spend_cap": 110.0},
             "campaigns": [
                 ("buyer-search", 110.0, 92.0, 1.2, []),      # on target
             ]},
        ],
    },
]


# Past decisions and what happened after them (made up history for the demo).
# The TikTok increases went badly before, so the agent's next TikTok increase
# for Harbour is sent to a human instead of running on its own.
def _m(client, platform, action, direction, size, ratio, verdict, before, after, days):
    return {"client_id": client, "platform": platform, "action_type": action, "direction": direction,
            "size_pct": size, "cpl_ratio": ratio, "verdict": verdict, "cpl_before": before,
            "cpl_after": after, "days_ago": days}


MEMORIES = [
    _m("harbour", "tiktok", "set_budget", "increase", 10, 0.65, "worse", 26.0, 41.5, 52),
    _m("harbour", "tiktok", "set_budget", "increase", 15, 0.70, "worse", 28.0, 39.0, 33),
    _m("harbour", "tiktok", "set_budget", "increase", 10, 0.60, "worse", 24.5, 36.2, 12),
    _m("harbour", "meta", "set_budget", "increase", 10, 0.55, "better", 22.0, 20.4, 40),
    _m("harbour", "meta", "set_budget", "decrease", 15, 1.60, "better", 66.0, 49.0, 25),
    _m("harbour", "meta", "set_budget", "decrease", 15, 1.80, "better", 72.0, 55.0, 9),
    _m("brightsmile", "google", "set_budget", "decrease", 15, 1.70, "better", 104.0, 81.0, 20),
    _m("brightsmile", "meta", "set_budget", "increase", 10, 0.60, "better", 37.0, 35.5, 15),
    _m("northside", "meta", "set_budget", "increase", 10, 0.65, "better", 58.0, 54.0, 18),
    _m("northside", "meta", "pause", "pause", 100, None, "better", None, None, 30),
]

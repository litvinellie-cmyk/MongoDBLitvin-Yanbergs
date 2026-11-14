"""Three made up clients for the demo. Every number is simulated.

Campaign tuple: (name, daily budget, hidden true cost per lead, clicks per $, tags)
"""

CLIENTS = [
    {
        "_id": "harbour", "name": "Harbour Coffee Co.", "industry": "Ecommerce",
        "accounts": [
            {"_id": "harbour-meta", "platform": "meta", "name": "Harbour on Facebook + Instagram",
             "settings": {"target_cpl": 40.0, "daily_spend_cap": 605.0},
             "campaigns": [
                 ("winner", 150.0, 22.0, 3.0, []),            # cheap: +10% on its own
                 ("steady", 120.0, 41.0, 2.5, []),            # on target: left alone
                 ("pricey", 160.0, 75.0, 2.0, []),            # expensive: -15% needs approval
                 ("dud", 60.0, 10_000.0, 1.5, []),            # no leads: pause needs approval
                 ("brand", 100.0, 70.0, 2.0, ["protected"]),  # protected: denied
             ]},
            {"_id": "harbour-tiktok", "platform": "tiktok", "name": "Harbour on TikTok",
             "settings": {"target_cpl": 40.0, "daily_spend_cap": 148.0},
             "campaigns": [
                 ("ugc-videos", 80.0, 28.0, 4.0, []),         # cheap: +10% on its own
                 ("spark-ads", 60.0, 70.0, 3.0, []),          # expensive: needs approval
             ]},
            {"_id": "harbour-reddit", "platform": "reddit", "name": "Harbour on Reddit",
             "settings": {"target_cpl": 40.0, "daily_spend_cap": 60.0},
             "campaigns": [
                 ("r-coffee", 50.0, 24.0, 2.0, []),           # cheap, but Reddit always asks
             ]},
            {"_id": "harbour-shopify", "platform": "shopify", "name": "Harbour Shopify store",
             "settings": {"target_cpl": 40.0, "daily_spend_cap": 0.0}, "campaigns": []},
        ],
    },
    {
        "_id": "brightsmile", "name": "Bright Smile Dental", "industry": "Medical",
        "accounts": [
            {"_id": "brightsmile-google", "platform": "google", "name": "Bright Smile on Google Ads",
             "settings": {"target_cpl": 60.0, "daily_spend_cap": 220.0},
             "campaigns": [
                 ("implants-search", 120.0, 58.0, 1.5, []),   # on target
                 ("whitening-pmax", 90.0, 110.0, 2.0, []),    # expensive: needs approval
             ]},
            {"_id": "brightsmile-meta", "platform": "meta", "name": "Bright Smile on Facebook + Instagram",
             "settings": {"target_cpl": 60.0, "daily_spend_cap": 77.0},
             "campaigns": [
                 ("new-patient-offer", 70.0, 35.0, 2.5, []),  # cheap: +10% on its own
             ]},
        ],
    },
    {
        "_id": "northside", "name": "Northside Realty", "industry": "Real estate",
        "accounts": [
            {"_id": "northside-meta", "platform": "meta", "name": "Northside on Facebook + Instagram",
             "settings": {"target_cpl": 90.0, "daily_spend_cap": 260.0},
             "campaigns": [
                 ("open-house", 100.0, 55.0, 2.0, []),        # cheap: +10% on its own
                 ("luxury-listings", 150.0, 10_000.0, 1.0, []),  # no leads: pause needs approval
             ]},
            {"_id": "northside-google", "platform": "google", "name": "Northside on Google Ads",
             "settings": {"target_cpl": 90.0, "daily_spend_cap": 110.0},
             "campaigns": [
                 ("buyer-search", 110.0, 92.0, 1.2, []),      # on target
             ]},
        ],
    },
]

"""Python version of schemas.js, for machines without mongosh.

  python setup_atlas.py         # collections, validators, indexes, search indexes
  python setup_atlas.py check   # connection test only

Safe to rerun.
"""

from __future__ import annotations

import sys

from pymongo.errors import OperationFailure
from pymongo.operations import SearchIndexModel

from store import get_db

NUM = {"bsonType": ["double", "int", "long"], "minimum": 0}

VALIDATED = {
    "accounts": {
        "bsonType": "object", "required": ["_id", "platform", "external_id", "name", "active"],
        "properties": {"_id": {"bsonType": "string"},
                       "platform": {"enum": ["meta", "google", "tiktok", "reddit", "shopify", "sandbox"]},
                       "client_id": {"bsonType": "string"},
                       "active": {"bsonType": "bool"}},
    },
    "settings": {
        "bsonType": "object", "required": ["_id", "target_cpl", "daily_spend_cap"],
        "properties": {"target_cpl": NUM, "daily_spend_cap": NUM},
    },
    "proposals": {
        "bsonType": "object",
        "required": ["account_id", "campaign_id", "action", "reason", "status", "created_at"],
        "properties": {
            "status": {"enum": ["pending", "approved", "executing", "done", "failed",
                                "rejected", "expired", "stale", "blocked"]},
            "action": {"bsonType": "object", "required": ["type"],
                       "properties": {"type": {"enum": ["set_budget", "pause", "resume", "set_targeting"]}}},
            "reason": {"bsonType": "object", "required": ["text", "numbers"]},
        },
    },
    "change_log": {
        "bsonType": "object",
        "required": ["action_id", "account_id", "campaign_id", "action", "reason",
                     "mode", "actor_id", "applied_at", "result"],
        "properties": {"mode": {"enum": ["auto", "approved", "rollback"]},
                       "result": {"bsonType": "object", "required": ["ok"]}},
    },
    "actors": {
        "bsonType": "object", "required": ["_id", "kind", "roles", "active"],
        "properties": {
            "_id": {"bsonType": "string", "pattern": "^(agent|user):"},
            "kind": {"enum": ["agent", "human"]},
            "roles": {"bsonType": "array",
                      "items": {"enum": ["agent", "approver", "admin", "viewer", "breaker"]}},
            "active": {"bsonType": "bool"},
        },
    },
    "policies": {
        "bsonType": "object", "required": ["_id", "priority", "roles", "actions", "effect"],
        "properties": {"priority": {"bsonType": ["int", "long"]},
                       "roles": {"bsonType": "array"}, "actions": {"bsonType": "array"},
                       "when": {"bsonType": "object"},
                       "effect": {"enum": ["allow", "require_approval", "deny"]},
                       "active": {"bsonType": "bool"}},
    },
    "audit_events": {
        "bsonType": "object", "required": ["event", "at"],
        "properties": {"event": {"bsonType": "string"}, "at": {"bsonType": "date"}},
    },
}
PLAIN = ["clients", "memories", "leads", "brand_docs", "sandbox_campaigns", "controls", "runs"]

INDEXES = {
    "proposals": [([("client_id", 1), ("status", 1), ("created_at", -1)], {}),
                  ([("status", 1), ("created_at", -1)], {}),
                  ([("campaign_id", 1)], {"name": "one_open_proposal_per_campaign", "unique": True,
                    "partialFilterExpression": {"status": {"$in": ["pending", "approved", "executing"]}}}),
                  ([("action_id", 1)], {})],
    "change_log": [([("client_id", 1), ("applied_at", -1)], {}),
                   ([("campaign_id", 1), ("applied_at", -1)], {}),
                   ([("action_id", 1)], {}), ([("reverts", 1)], {}),
                   ([("result.ok", 1), ("applied_at", 1)], {})],
    "audit_events": [([("client_id", 1), ("at", -1)], {}), ([("at", -1)], {}), ([("action_id", 1), ("at", 1)], {}),
                     ([("campaign_id", 1), ("event", 1), ("at", -1)], {})],
    "metrics_snapshots": [([("meta.account_id", 1), ("meta.campaign_id", 1), ("ts", -1)], {})],
    "leads": [([("account_id", 1), ("campaign_id", 1), ("created_at", -1)], {})],
    "memories": [([("expires_at", 1)], {"expireAfterSeconds": 0}),
                 ([("client_id", 1), ("action_type", 1), ("platform", 1), ("direction", 1)], {})],
}


def main():
    db = get_db()
    db.command("ping")
    print(f"connected to database '{db.name}'")
    if sys.argv[1:] == ["check"]:
        return
    have = set(db.list_collection_names())

    if "metrics_snapshots" not in have:
        db.create_collection("metrics_snapshots",
                             timeseries={"timeField": "ts", "metaField": "meta", "granularity": "minutes"},
                             expireAfterSeconds=60 * 60 * 24 * 120)
        print("created metrics_snapshots (time series)")

    for name, schema in VALIDATED.items():
        if name in have:
            db.command("collMod", name, validator={"$jsonSchema": schema})
            print(f"updated validator on {name}")
        else:
            db.create_collection(name, validator={"$jsonSchema": schema})
            print(f"created {name}")
    for name in PLAIN:
        if name not in have:
            db.create_collection(name)
            print(f"created {name}")

    for coll, specs in INDEXES.items():
        for keys, opts in specs:
            db[coll].create_index(keys, **opts)
    print("indexes ok")

    for coll, name, filters in [("change_log", "memory_vec", ["account_id", "action.type"]),
                                ("brand_docs", "brand_vec", []),
                                ("memories", "memories_vec", ["client_id", "action_type", "platform"])]:
        try:
            if name in {i["name"] for i in db[coll].list_search_indexes()}:
                continue
            fields = [{"type": "vector", "path": "embedding", "numDimensions": 1024,
                       "similarity": "cosine"}] + [{"type": "filter", "path": p} for p in filters]
            db[coll].create_search_index(SearchIndexModel(
                definition={"fields": fields}, name=name, type="vectorSearch"))
            print(f"created search index {name}")
        except OperationFailure as e:
            print(f"search index {name} skipped: {e.details.get('errmsg', e)}")


if __name__ == "__main__":
    main()

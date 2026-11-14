// Run once against Cluster0 with mongosh:
//   mongosh "$MONGODB_URI" --file schemas.js
// Safe to rerun: existing collections and indexes are skipped.

const DB_NAME = process.env.DB_NAME || "ad_agent";
const d = db.getSiblingDB(DB_NAME);
const existing = new Set(d.getCollectionNames());

function create(name, options) {
  if (existing.has(name)) {
    print(`skip ${name} (exists)`);
    return;
  }
  d.createCollection(name, options || {});
  print(`created ${name}`);
}

// 1. Connected ad accounts. Tokens are NOT stored here in plain text:
//    token_ref points at a secret manager entry or an encrypted field.
create("accounts", {
  validator: { $jsonSchema: {
    bsonType: "object",
    required: ["_id", "platform", "external_id", "name", "active"],
    properties: {
      _id: { bsonType: "string" },
      platform: { enum: ["meta", "google", "tiktok", "reddit", "shopify", "sandbox"] },
      client_id: { bsonType: "string" },
      external_id: { bsonType: "string" },
      name: { bsonType: "string" },
      active: { bsonType: "bool" },
      token_ref: { bsonType: "string" },
    },
  } },
});

// 2. Guardrails per account. _id = account _id. Missing fields fall back
//    to DEFAULT_SETTINGS in rules.py.
create("settings", {
  validator: { $jsonSchema: {
    bsonType: "object",
    required: ["_id", "target_cpl", "daily_spend_cap"],
    properties: {
      _id: { bsonType: "string" },
      target_cpl: { bsonType: ["double", "int"], minimum: 0 },
      daily_spend_cap: { bsonType: ["double", "int"], minimum: 0 },
      max_change_pct: { bsonType: ["double", "int"], minimum: 1, maximum: 50 },
      auto_apply_max_pct: { bsonType: ["double", "int"], minimum: 0, maximum: 50 },
      auto_mode: { bsonType: "bool" },
      max_edits_per_campaign_per_day: { bsonType: "int", minimum: 0 },
      min_hours_between_edits: { bsonType: ["double", "int"], minimum: 0 },
      min_spend_before_action: { bsonType: ["double", "int"], minimum: 0 },
      min_conversions_before_action: { bsonType: "int", minimum: 0 },
    },
  } },
});

// 3. Raw pulls every 15 minutes. Each snapshot holds the platform's
//    day to date totals for one campaign; `day` is the account's local date.
create("metrics_snapshots", {
  timeseries: { timeField: "ts", metaField: "meta", granularity: "minutes" },
  expireAfterSeconds: 60 * 60 * 24 * 120, // keep 120 days, stays well under 10 GB
});

// 4. Approval queue for big moves, pauses and new campaigns.
create("proposals", {
  validator: { $jsonSchema: {
    bsonType: "object",
    required: ["account_id", "campaign_id", "action", "reason", "status", "created_at"],
    properties: {
      status: { enum: ["pending", "approved", "executing", "done", "failed", "rejected", "expired", "stale", "blocked"] },
      action: { bsonType: "object", required: ["type"],
        properties: { type: { enum: ["set_budget", "pause", "resume"] } } },
      reason: { bsonType: "object", required: ["text", "numbers"] },
    },
  } },
});

// 5. Every change that was sent to a platform, auto or approved.
//    Insert only: the app never updates a row except to add `outcome`.
create("change_log", {
  validator: { $jsonSchema: {
    bsonType: "object",
    required: ["account_id", "campaign_id", "action", "reason", "mode", "applied_at", "result"],
    properties: {
      mode: { enum: ["auto", "approved", "rollback"] },
      result: { bsonType: "object", required: ["ok"] },
    },
  } },
});

// 6. Leads tied to source campaign and creative.
create("clients");
create("leads");

// 7. Brand voice guide chunks for creative checks.
create("brand_docs");

// 8. Fake ad platform state for the hackathon demo.
create("sandbox_campaigns");

// 9. Who can act: the agent and each human, with roles.
create("actors", {
  validator: { $jsonSchema: {
    bsonType: "object",
    required: ["_id", "kind", "roles", "active"],
    properties: {
      _id: { bsonType: "string", pattern: "^(agent|user):" },
      kind: { enum: ["agent", "human"] },
      roles: { bsonType: "array", items: { enum: ["agent", "approver", "admin", "viewer"] } },
      active: { bsonType: "bool" },
    },
  } },
});

// 10. Policy as data. Checked before every action; first match by priority wins.
create("policies", {
  validator: { $jsonSchema: {
    bsonType: "object",
    required: ["_id", "priority", "roles", "actions", "effect"],
    properties: {
      priority: { bsonType: "int" },
      roles: { bsonType: "array" },
      actions: { bsonType: "array" },
      when: { bsonType: "object" },
      effect: { enum: ["allow", "require_approval", "deny"] },
      active: { bsonType: "bool" },
    },
  } },
});

// 11. Audit trail. Every policy check, send, result, approval, halt.
create("audit_events", {
  validator: { $jsonSchema: {
    bsonType: "object", required: ["event", "at"],
    properties: { event: { bsonType: "string" }, at: { bsonType: "date" } },
  } },
});

// 12. Kill switch ("global" or an account id) and agent runs.
create("controls");
create("runs");

// Indexes
d.proposals.createIndex({ status: 1, created_at: -1 });
d.proposals.createIndex(
  { campaign_id: 1 },
  { name: "one_open_proposal_per_campaign", unique: true,
    partialFilterExpression: { status: { $in: ["pending", "approved", "executing"] } } },
);
d.change_log.createIndex({ campaign_id: 1, applied_at: -1 });
d.change_log.createIndex({ "result.ok": 1, outcome: 1, applied_at: 1 });
d.audit_events.createIndex({ at: -1 });
d.audit_events.createIndex({ action_id: 1, at: 1 });
d.audit_events.createIndex({ campaign_id: 1, event: 1, at: -1 });
d.change_log.createIndex({ action_id: 1 });
d.change_log.createIndex({ reverts: 1 });
d.proposals.createIndex({ action_id: 1 });
d.leads.createIndex({ account_id: 1, campaign_id: 1, created_at: -1 });
d.metrics_snapshots.createIndex({ "meta.account_id": 1, "meta.campaign_id": 1, ts: -1 });
print("indexes ok");

// Vector Search indexes (agent memory + brand voice). Needs embeddings in
// `embedding`; 1024 dims matches voyage-3 class models. Change to your model.
function vectorIndex(coll, name, filters) {
  const have = d[coll].getSearchIndexes().map((i) => i.name);
  if (have.includes(name)) { print(`skip search index ${name}`); return; }
  d[coll].createSearchIndex(name, "vectorSearch", {
    fields: [
      { type: "vector", path: "embedding", numDimensions: 1024, similarity: "cosine" },
      ...filters.map((p) => ({ type: "filter", path: p })),
    ],
  });
  print(`created search index ${name}`);
}
try {
  vectorIndex("change_log", "memory_vec", ["account_id", "action.type"]);
  vectorIndex("brand_docs", "brand_vec", []);
} catch (e) {
  print(`search index step skipped: ${e.message}`);
}

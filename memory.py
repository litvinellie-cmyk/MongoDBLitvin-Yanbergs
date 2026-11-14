"""Agent memory: what happened after similar moves before.

Each memory is one past decision and its measured outcome, stored in the
`memories` collection. Rules that keep it governed:

  * scoped: a memory belongs to one client and is only ever recalled for that client
  * expiring: expires_at + a TTL index drop it after 180 days
  * correctable: a human can forget one, and that is written to the audit trail
  * provable: when memory changes a decision, the policy check records it

Recall uses Atlas Vector Search when VOYAGE_API_KEY is set (voyage-3.5,
1024 dims). Without a key it falls back to a structured query, so the demo
works either way. The result says which path was used.
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import timedelta

from platforms import utcnow

TTL_DAYS = 180
VOYAGE_MODEL = "voyage-3.5"


def describe(platform: str, action_type: str, direction: str, size_pct: float, cpl_ratio: float | None) -> str:
    ratio = f"cost per lead at {cpl_ratio:.2f}x target" if cpl_ratio else "no leads"
    return f"{platform} {action_type} {direction} {size_pct:.0f}% with {ratio}"


def direction_of(action: dict) -> str:
    if action["type"] == "set_budget" and action.get("old") is not None:
        return "increase" if action["new"] > action["old"] else "decrease"
    return action["type"]


def embed(texts: list[str], input_type: str) -> list[list[float]] | None:
    key = os.environ.get("VOYAGE_API_KEY")
    if not key or not texts:
        return None
    req = urllib.request.Request(
        "https://api.voyageai.com/v1/embeddings",
        data=json.dumps({"input": texts, "model": VOYAGE_MODEL, "input_type": input_type}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)["data"]
        return [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]
    except Exception as e:  # memory must never break the agent
        print(f"embedding skipped: {e}")
        return None


def remember(db, *, client_id, platform, action_type, direction, size_pct, cpl_ratio,
             verdict, cpl_before, cpl_after, source: str, when=None) -> None:
    text = describe(platform, action_type, direction, size_pct, cpl_ratio)
    doc = {
        "client_id": client_id, "platform": platform, "action_type": action_type,
        "direction": direction, "size_pct": size_pct, "cpl_ratio": cpl_ratio,
        "verdict": verdict, "cpl_before": cpl_before, "cpl_after": cpl_after,
        "text": f"{text}. Result: {verdict}.", "source": source,
        "created_at": when or utcnow(), "expires_at": (when or utcnow()) + timedelta(days=TTL_DAYS),
    }
    vec = embed([text], "document")
    if vec:
        doc["embedding"] = vec[0]
    db.memories.insert_one(doc)


def recall(db, *, client_id, platform, action_type, direction, size_pct, cpl_ratio, k=3) -> dict:
    """Top k similar past decisions for this client only."""
    text = describe(platform, action_type, direction, size_pct, cpl_ratio)
    base = {"client_id": client_id, "action_type": action_type}
    vec = embed([text], "query")
    if vec:
        try:
            hits = list(db.memories.aggregate([
                {"$vectorSearch": {"index": "memories_vec", "path": "embedding", "queryVector": vec[0],
                                   "numCandidates": 100, "limit": k, "filter": base}},
                {"$project": {"embedding": 0, "score": {"$meta": "vectorSearchScore"}}},
            ]))
            return {"method": "vector search", "query": text, "hits": hits}
        except Exception as e:
            print(f"vector search unavailable, using fallback: {e}")
    cands = list(db.memories.find({**base, "platform": platform, "direction": direction},
                                  {"embedding": 0}).limit(50))
    cands.sort(key=lambda m: abs((m.get("cpl_ratio") or 0) - (cpl_ratio or 0)))
    return {"method": "structured match", "query": text, "hits": cands[:k]}


def warning(recalled: dict) -> tuple[bool, str]:
    """Memory turns into a warning when most similar past moves went badly."""
    hits = recalled["hits"]
    if len(hits) < 2:
        return False, ""
    worse = [h for h in hits if h.get("verdict") == "worse"]
    better = [h for h in hits if h.get("verdict") == "better"]
    summary = f"Memory: {len(hits)} similar past moves for this client, {len(better)} better, {len(worse)} worse."
    return len(worse) > len(hits) / 2, summary


def forget(db, memory_id, actor_id: str) -> dict:
    import gate
    from bson import ObjectId
    m = db.memories.find_one({"_id": ObjectId(memory_id)}, {"embedding": 0})
    if not m:
        return {"ok": False, "error": "memory not found"}
    effect, pid, why = gate.check(db, actor_id, {"action_type": "forget_memory", "client_id": m["client_id"]})
    if effect != "allow":
        return {"ok": False, "error": f"denied by {pid}: {why}"}
    db.memories.delete_one({"_id": m["_id"]})
    gate.audit(db, "memory_forgotten", actor_id=actor_id, client_id=m["client_id"], memory=m, policy_id=pid)
    return {"ok": True}

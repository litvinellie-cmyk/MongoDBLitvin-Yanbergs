"""Command line for the humans. Every command runs as a named actor.

  python approvals.py list
  python approvals.py approve <proposal_id> <actor> [comment]   e.g. user:ellie
  python approvals.py reject  <proposal_id> <actor>
  python approvals.py undo    <change_id>   <actor>
  python approvals.py halt    <actor> "<reason>"
  python approvals.py unhalt  <actor>
  python approvals.py explain <action_id>
  python approvals.py watch      # executes proposals approved elsewhere (Change Stream)
"""

from __future__ import annotations

import json
import sys

import ops
from platforms import build_platforms
from store import get_db


def list_pending(db):
    for p in db.proposals.find({"status": "pending"}).sort("created_at", 1):
        a = p["action"]
        print(f"{p['_id']}  {p['campaign_id']}  {a['type']} {a.get('old')} -> {a.get('new')}"
              f"  [{p.get('policy_id')}]\n    {p['reason']['text']}")


def watch(db, platforms):
    for p in db.proposals.find({"status": "approved"}, {"_id": 1}):
        print(ops.execute_proposal(db, platforms, p["_id"]))
    pipeline = [{"$match": {"operationType": {"$in": ["update", "replace"]},
                            "fullDocument.status": "approved"}}]
    print("watching for approvals...")
    with db.proposals.watch(pipeline, full_document="updateLookup") as stream:
        for change in stream:
            print(ops.execute_proposal(db, platforms, change["documentKey"]["_id"]))


def main():
    db = get_db()
    platforms = build_platforms(db)
    a = sys.argv[1:]
    cmd = a[0] if a else ""
    if cmd == "list":
        list_pending(db)
    elif cmd in ("approve", "reject") and len(a) >= 3:
        print(ops.decide_proposal(db, platforms, a[1], cmd, a[2], " ".join(a[3:]) or f"{cmd}d from the command line"))
    elif cmd == "undo" and len(a) == 3:
        print(ops.undo(db, platforms, a[1], a[2]))
    elif cmd == "halt" and len(a) >= 2:
        print(ops.set_halt(db, a[1], True, " ".join(a[2:])))
    elif cmd == "unhalt" and len(a) == 2:
        print(ops.set_halt(db, a[1], False))
    elif cmd == "explain" and len(a) == 2:
        print(json.dumps(ops.explain(db, a[1]), default=str, indent=2))
    elif cmd == "watch":
        watch(db, platforms)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()

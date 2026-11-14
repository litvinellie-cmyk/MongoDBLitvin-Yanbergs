"""Backup only: runs the dashboard on an in-memory fake MongoDB.

Use this if Atlas access breaks during judging. The real demo must run on
the hackathon Atlas sandbox.

  pip install mongomock
  python offline_demo.py      # http://localhost:8000
"""

import os

import mongomock

import store

_db = mongomock.MongoClient(tz_aware=True)[os.environ.get("DB_NAME", "ad_agent")]
store.get_db = lambda: _db

import dashboard  # noqa: E402  (must import after patching get_db)
import worker  # noqa: E402
from platforms import build_platforms  # noqa: E402

if __name__ == "__main__":
    from http.server import ThreadingHTTPServer
    worker.seed(_db, build_platforms(_db), reset=True)
    port = int(os.environ.get("PORT", "8000"))
    print(f"OFFLINE demo (in memory, not Atlas) on http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), dashboard.Handler).serve_forever()

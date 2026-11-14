"""Voice briefing with ElevenLabs.

The agent writes a short spoken update from live Atlas data (goal pace,
decisions waiting, what it changed, halts and incidents, what it learned)
and ElevenLabs turns it into speech. Read only: it never changes anything.

Needs ELEVENLABS_API_KEY in .env (optional ELEVENLABS_VOICE_ID). Without a
key the dashboard falls back to the browser's built-in voice and says so.
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import timedelta

from platforms import utcnow

DEFAULT_VOICE = "21m00Tcm4TlvDq8ikWAM"  # a premade ElevenLabs voice; override with ELEVENLABS_VOICE_ID
MODEL = "eleven_flash_v2_5"


def _num(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def briefing_text(db, client_id: str) -> str:
    import growth
    now = utcnow()
    hour = (now.hour - 5) % 24  # Toronto time for the greeting
    hello = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    clients = list(db.clients.find()) if client_id == "all" else [db.clients.find_one({"_id": client_id})]
    clients = [c for c in clients if c]
    parts = [f"{hello}. Here is your ad briefing."]
    for c in clients:
        cid = c["_id"]
        g = growth.goal_status(db, cid)
        if g and g["status"] != "not started":
            parts.append(f"{c['name']}: {g['month_to_date']} of {g['target']} {g['metric']} so far, "
                         f"{'on track' if g['status'] == 'on track' else 'close to target' if g['status'] == 'close' else 'behind'} "
                         f"at about {g['pct']} percent of goal.")
        halt = db.controls.find_one({"_id": f"client:{cid}", "halted": True})
        if halt:
            who = "the circuit breaker" if "breaker" in (halt.get("halted_by") or "") else "a team member"
            parts.append(f"{c['name']} is paused by {who}. Nothing will change until you resume it.")
    q = {"status": "pending"} if client_id == "all" else {"status": "pending", "client_id": client_id}
    waiting = db.proposals.count_documents(q)
    if waiting:
        parts.append(f"{_num(waiting, 'decision')} {'is' if waiting == 1 else 'are'} waiting for your approval.")
        top = db.proposals.find_one({**q, "action.type": "pause"}) or db.proposals.find_one(q)
        if top:
            name = (top["action"].get("label") or top["campaign_id"].split(":")[-1]).replace("-", " ")
            first = top["reason"]["text"].split(". ")[0].rstrip(".")
            parts.append(f"The most important one: {first}.")
    cq = {"result.ok": True, "mode": "auto", "applied_at": {"$gte": now - timedelta(hours=24)}}
    if client_id != "all":
        cq["client_id"] = client_id
    auto = db.change_log.count_documents(cq)
    if auto:
        parts.append(f"In the last day the agent made {_num(auto, 'small change')} on its own, each within your rules and verified.")
    if client_id != "all":
        t = growth.trust(db, client_id)
        parts.append({"high": "Its recent moves for this client mostly helped, so it has earned a little more freedom.",
                      "low": "Its recent moves for this client mostly didn't help, so it is asking you before every budget change.",
                      "normal": ""}[t["level"]])
    parts.append("Every decision is recorded and can be undone from the dashboard.")
    return " ".join(p for p in parts if p)


def speak(text: str) -> bytes | None:
    from store import load_env
    load_env()
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        return None
    voice = os.environ.get("ELEVENLABS_VOICE_ID") or DEFAULT_VOICE
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128",
        data=json.dumps({"text": text, "model_id": MODEL,
                         "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}}).encode(),
        headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()

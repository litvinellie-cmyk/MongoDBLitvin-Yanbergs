# Budget Agent You Can Audit

An AI ad budget agent for agencies, with MongoDB Atlas as its identity, policy and audit layer.

Track 1, Agents You Can Audit. Agents in Context Toronto, MongoDB hackathon, Nov 14 2026.

An agent manages ad budgets for an agency with many clients, across Facebook + Instagram, Google Ads, TikTok and Reddit Ads, with Shopify connected as a read only sales source. It moves money between campaigns based on cost per lead. Every action it takes is tied to an identity, checked against a policy stored in MongoDB Atlas before it runs, verified against the ad platform afterwards, and written to an audit trail a reviewer can replay. A human can halt it mid run or undo any change.

## The three Track 1 questions

| Question | How this answers it |
|---|---|
| How do you decide what an agent may do and enforce it before the action? | Policies are documents in the `policies` collection, with schema validation. Every action goes through one gate (`gate.py`): identity, then kill switch, then policy. The result is allow, require approval, or deny, and if nothing matches it's denied. The policy is checked again when an approved action actually runs. |
| How do you record what the agent did, why, and under whose authority? | `audit_events` gets an entry for every policy check, approval, send, platform result and halt, all linked by `action_id`. The **Explain** view rebuilds the full chain: who asked, which policy ruled, who approved, what was sent, and what the platform showed afterwards. |
| How do you stop or roll back an agent mid run? | The kill switch lives in the `controls` collection. It's checked between every campaign and again right before every write. **Undo** sends the reverse change through the same gate, under the identity of the human who clicked it. |

## Built for agencies: many clients, many platforms

| Feature | How it works |
|---|---|
| Clients kept apart | Every account, proposal, change and audit entry carries a `client_id`. People get roles per client (`grants`), so a client owner can approve and halt their own ads and nothing else. |
| Agency view | "All clients" shows every client's spend, platforms and approval queue. Clicking a client card scopes everything to that client. |
| Rules per platform | Policies can target a platform or a client. In the demo every Reddit move needs a human, and Shopify is read only for everyone. |
| Kill switch at three levels | Everything, one client, or one ad account. |
| Adapters | Meta, Google, TikTok, Reddit and Shopify adapters list their API calls. Demo accounts run on simulated data (`adapter: "sandbox"`), with their real platform name kept for policy and display. |

Demo clients (all made up): Harbour Coffee Co. (FB + IG, TikTok, Reddit, Shopify), Bright Smile Dental (Google, FB + IG), Northside Realty (FB + IG, Google).

## Demo script (1 minute)

0. Start in **All clients** to show the agency view, then click **Harbour Coffee Co.**
1. **Run agent now.** Budget bot raises "winner" 10% on its own (policy allows), proposes cutting "pricey" 15% and pausing "dud" (policy requires a human), and gets **denied** on "brand" because it's tagged protected.
2. Switch "Acting as" to **Dr. Lee (Bright Smile owner)** and approve a Harbour change. Denied: a client owner can't touch another client's ads. The denial goes into the audit trail.
3. Switch to **Sam (Harbour Coffee owner)** and approve "pricey". The policy is checked again under her identity, the change is sent, and the platform re-read shows **VERIFIED**.
4. Click **explain** on that change to see the full reconstruction.
5. Click **Halt agent**, then **Run agent now**. Nothing runs, and that's logged too.
6. **Resume**, then **undo** the "pricey" change. The budget goes back to $160, with the undo linked to the original change.

## How it fits together

| Atlas feature | Used for |
|---|---|
| Collections + `$jsonSchema` validation | `actors`, `policies`, `audit_events`, `proposals`, `change_log`, `settings`, `accounts` |
| Time series collection | `metrics_snapshots`, the campaign numbers pulled every 15 minutes |
| Aggregation pipeline | 7 day spend, leads and CPL per campaign, plus before and after outcomes |
| Change Streams | `approvals.py watch` carries out proposals approved from anywhere |
| Unique partial index | Only one open proposal per campaign |
| Vector Search index | `change_log.memory_text` stores past decisions and outcomes, ready for the agent to look up similar cases |

## Files

| File | Role |
|---|---|
| `gate.py` | The only path to the ad platform: identity, kill switch, policy, send, verify, audit |
| `policy.py` | Policy evaluation (pure, tested) and the default policies |
| `rules.py` | When the agent wants to act: sample size, dead zone, step size, spend cap, edit frequency (pure, tested) |
| `worker.py` | Agent loop and seeding |
| `ops.py` | Approve, reject, execute, undo, halt, explain |
| `dashboard.py` + `dashboard.html` | Demo UI on the standard library server, no extra installs |
| `approvals.py` | The same operations from the command line |
| `platforms.py` | Sandbox ad platform, plus Meta and Google adapters with their API calls noted |
| `schemas.js` | Creates every collection, validator and index on Atlas |
| `offline_demo.py` | Backup only: the same UI on an in-memory database |

## Run it

Copy `.env.example` to `.env` and fill in `MONGODB_URI` from the hackathon Atlas sandbox. Then, from the project folder:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python setup_atlas.py
.venv/bin/python worker.py reset-demo
.venv/bin/python dashboard.py
```

`setup_atlas.py` creates the collections and indexes (`schemas.js` does the same if you have `mongosh`). `reset-demo` loads actors, policies, 5 sandbox campaigns and 7 days of history; run it before each demo for a clean start. The dashboard opens at http://localhost:8000.

## Tests

`python -m pytest test_rules.py test_policy.py` runs 19 tests on the decision rules and policy engine.

## Limits

The ad platform in the demo is a sandbox, so no real money moves. The Meta and Google adapters list their exact API calls but aren't wired up. "Acting as" is a demo switch, not real login; a real version would put Atlas or SSO identity in front of it. Embeddings for the memory index aren't generated yet.

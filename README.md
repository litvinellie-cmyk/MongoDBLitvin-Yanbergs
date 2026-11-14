# Budget Agent You Can Audit

Pitch page: open `pitch.html` in a browser.

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

## Two views: the agency decides, the client understands

| Agency dashboard (for you and your team) | Client report (for the business owner) |
|---|---|
| Spend, impressions, reach, frequency, CPM, CPC, CTR, verified vs platform-reported conversions, CVR, CPA vs target, ROAS, AOV, for every campaign | One sentence summary: what was spent, what it brought, cost per customer vs goal and last week |
| Every proposal carries **evidence**: statistical confidence vs target, 95% CPA range, previous period and account baselines, trend, expected weekly impact, risk level, learning phase effect, tracking check, and the options considered | Where the money went, by platform, with a traffic light (working well, being tuned, fixing) |
| Approvals by agency staff only, each with a required comment: owner approves anything, senior strategist up to $25 a day, junior cannot approve, analyst read only | "What we changed and why" in plain sentences, plus what is under review next |
| Agent accuracy (share of past moves that helped), full decision log export as CSV | Save as PDF button |

Clients never approve anything. Only agency staff do.

## Channels beyond ads

Each client has a **Channels** section with six tabs (simulated data in the demo):

| Tab | What it shows | Real source |
|---|---|---|
| AI search (AEO/GEO) | Share of ChatGPT, Perplexity, Gemini, Google AI Overview and Claude answers that mention the client, vs last week and vs competitors; a question by engine grid; the sites AI cites most; wrong information alerts (for example old opening hours) | Daily prompts to each engine |
| Competitors | Rival ads with type, platform and days running, new ads this week, likely winners (30+ days), and brand bidding alerts | Meta Ad Library, Google Ads Transparency Center |
| SEO vs paid | Keywords where the client already ranks #1 or #2 organically but still pays; weekly savings estimate. The agent proposes pausing them, through the normal approval queue | Search Console + Google Ads |
| Reviews | New reviews with drafted replies. Replies are public, so they always need a person on the agency team, and can't be undone automatically | Google Business Profile |
| Creative check | Each draft ad checked for risky claims (guarantees, superlatives), health claims, housing ad wording (Fair Housing), and the client's banned brand words | Rules library |
| Tracking health | Platform vs verified conversions, pixel events that stopped firing, ads pointing to broken pages | Pixel, Conversions API, URL checks |

Keyword pauses and review replies use the same gate, approval roles, verification and audit trail as budget changes.

## Rules in plain English and the time machine

| Feature | What it does |
|---|---|
| Rules in plain English | Staff type a rule such as "never spend more than $150 a day on dental clients" or "always ask before pausing anything on Reddit". It becomes a draft policy with a plain readback of exactly what it will do. Only the agency owner can switch it on or retire it, and every version is stored in `policy_versions`. The parser covers common agency rules; with an LLM key the same draft and approve flow would accept any wording, and the safety comes from a human approving the readback |
| Time machine | Drag a slider to any past moment and see what the agent knew then: rules in force, kill switch state, budgets then vs now, what was waiting for a human, and the last events. Rebuilt from `policy_versions`, `audit_events`, `change_log` and `proposals` |

## Tracking and audiences

| Tab | What it does | Agent action |
|---|---|---|
| UTM audit | Checks every ad's link against one naming rule (source, medium, campaign, content, lowercase) and catches typos like "facebok", missing tags and capitals that split GA4 reports | Fixes broken links on its own (tracking only, no spend change), logged and undoable |
| Conversions API | Per platform: server vs browser events, deduplication by event ID, event match quality, and how many orders keep the click ID (fbclid, gclid, ttclid, rdt_cid) for offline conversions | Flags missing or weak setups |
| Retargeting ladder | Homepage visitors, product viewers, cart, checkout, past buyers: each with its own window, message, budget, frequency and cost per sale | Overexposed tiers get a lower budget, after staff approval |
| Audience sync | Shopify buyers, Klaviyo segments and top customers pushed to the ad platforms (hashed), with match rate and last sync | Stale buyer exclusions are synced on its own |

## Optimization skills

| Skill | What it does |
|---|---|
| Next dollar returns | Estimates what the *next* $10 a day buys in each campaign (marginal cost per lead, diminishing returns stated as an assumption) and suggests shifting money to where it works hardest |
| Goal planner | "To get 180 orders a week, plan about $8,000 a week": spend needed for a goal, with the rising cost per result |
| Stock-aware ads | Reads Shopify stock; ads for sold out products are proposed for pausing through the approval queue |
| Search term mining | Finds search terms that cost money and never convert; proposes negative keywords through the approval queue |
| Funnel leaks | Click to purchase (or booking) funnel vs benchmarks, with the biggest leak and a concrete fix |
| Experiments | Registry of past tests in Atlas, plus the next test with the sample size and weeks needed (95% confidence, 80% power) |
| Anomaly radar | Today's CPC, CTR, CPM and spend pace vs the last 14 days |
| Prospect audit (live) | Fetches any real website and checks speed, SEO basics, AI search readiness (schema, FAQ, llms.txt, AI crawler access in robots.txt) and ad pixels. For sales calls |

## Three problems agencies actually have

| Problem | What the agent does | MongoDB feature |
|---|---|---|
| **Runaway spend.** A bug or bad bid burns a month of budget overnight. | **Circuit breaker:** when an account spends faster than 2x its expected pace for the hour, the breaker halts that client by itself and says why. Policy lets it halt, never resume: only a human restarts a client. | Policies as documents, audit trail |
| **Ad law.** Housing ads can't target by age, gender or zip code (Special Ad Category). Health ads need review. One slip can get an ad account restricted. | **Compliance policies:** age targeting on a real estate client is denied for everyone, agency included. On a medical client it goes to a human compliance review. | Policies with `industry` conditions |
| **Repeating mistakes.** Staff change, and nobody remembers that TikTok scaling backfired last month. | **Memory:** before acting, the agent recalls similar past moves for that client and their results. If most went badly, a policy sends the move to a human, so memory visibly changes behavior. Memories are scoped per client, expire after 180 days (TTL index), and can be forgotten by an approver, with the deletion logged. | Atlas Vector Search (`memories_vec`, with Voyage embeddings when `VOYAGE_API_KEY` is set), TTL index |

## Demo script (1 minute)

0. Start in **All clients** to show the agency view, then click **Harbour Coffee Co.**
1. **Run agent now.** Budget bot raises "winner" 10% on its own (policy allows), proposes cutting "pricey" 15% and pausing "dud" (policy requires a human), and gets **denied** on "brand" because it's tagged protected.
2. Open the **Evidence** on the "pricey" card: 99% confident it's above target, CPA range, baseline, expected impact, risk.
3. Switch "Acting as" to **Alex (junior)** and approve: denied. Switch to **Ellie (agency owner)** and approve "pricey" with a comment. The policy is checked again under her identity, the change is sent, and the platform re-read shows **VERIFIED**.
4. Click **explain** on that change to see the full reconstruction.
5. Click **Halt agent**, then **Run agent now**. Nothing runs, and that's logged too.
6. **Resume**, then **undo** the "pricey" change. The budget goes back to $160, with the undo linked to the original change.
7. On Harbour, show the TikTok "ugc-videos" card: **Memory changed this decision**, because 3 similar TikTok increases made things worse.
8. Click **Client report** to show what the client sees: plain language, no jargon.
9. Open **Channels**: AI search visibility, the competitor ads, the blocked "Perfect for young families" housing ad in Creative check.
10. Open **Optimization skills**: next dollar returns, then run a live **Prospect audit** on any real website.
11. Switch to **Northside Realty**: the agent's age targeting was **denied** by the housing ad rules. Click **Simulate spend spike**: the circuit breaker halts Northside on its own, with a red banner explaining why.

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

## Speed

A full agent run (3 clients, 8 accounts, all channels and skills) takes about 15 seconds against a remote Atlas cluster, down from 51. Rules and people are cached for a few seconds, per-run lookups are loaded once, and audit events are written in bulk. Audit events are always flushed to Atlas before anything is sent to an ad platform, and the kill switch is re-read live right before every write.

## Tests

`python -m pytest test_rules.py test_policy.py` runs 19 tests on the decision rules and policy engine.

## Limits

The ad platform in the demo is a sandbox, so no real money moves. The Meta and Google adapters list their exact API calls but aren't wired up. "Acting as" is a demo switch, not real login; a real version would put Atlas or SSO identity in front of it. Embeddings for the memory index aren't generated yet.

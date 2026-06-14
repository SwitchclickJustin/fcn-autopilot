# FCN Auto-Pilot — Design Spec

**Date:** 2026-06-14
**Status:** Approved Design
**Author:** Agent + User

## Overview

A 24/7 hosted service that drives FreeChatNow via Browser Use Cloud. The user controls multiple personas with per-profile identities (proxy, user-agent, fingerprint), joins group chat rooms, auto-responds to both group chat and DMs, and has a supervisor LLM that learns from ban events to adjust behavior. A live embedded browser view lets the user watch everything in real-time. The auto-pilot is toggleable — on when they want it, off when they don't.

## Architecture

### Deployment

- **Host:** Railway (single service instance)
- **Browser:** Browser Use Cloud (remote Chromium, provisioned on demand)
- **Database:** SQLite (persistent volume on Railway at `/data/fcn.db`)
- **Frontend:** Server-rendered HTML via Jinja2 (served by FastAPI)
- **Live View:** Embedded Browser Use Cloud CDP viewer or screenshot stream

### Tech Stack

| Component | Choice | Why |
|-----------|--------|-----|
| Web framework | FastAPI | Async, native WebSocket, Pydantic validation |
| Database | SQLite + aiosqlite | Zero-config, Railway persistent volume |
| Templating | Jinja2 | Server-rendered, no JS framework needed |
| Real-time | WebSocket | Push chat messages, auto-pilot status |
| Browser | Browser Use Cloud | Remote Chromium, proxy support |
| LLM providers | OpenRouter / OpenAI / Anthropic / Custom | Plugin system — user brings their own |
| Container | Docker | Railway standard |

### Project Structure

```
fcn-assistant/
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI app, HTTP routes, WebSocket
│   ├── browser.py              # Browser Use Cloud driver + session lifecycle
│   ├── autopilot.py            # Auto-pilot loop (group chat + DMs)
│   ├── supervisor.py           # Supervisor LLM + behavior rules engine
│   ├── providers.py            # LLM provider registry (multi-model)
│   ├── database.py             # SQLite schema + async queries
│   ├── models.py               # Pydantic schemas for all entities
│   └── templates/
│       ├── base.html
│       ├── dashboard.html      # Live browser view + chat feed + auto-pilot toggle
│       ├── personas.html       # Persona CRUD with identity config
│       ├── providers.html      # LLM provider management
│       ├── supervisor.html     # Rules, ban history, behavior insights
│       └── history.html        # Searchable conversation log
├── static/
│   └── style.css
├── railway.json
├── Dockerfile
├── requirements.txt
└── .env.example
```

## Database Schema

### Tables

**personas** — Chat identities with per-profile fingerprinting.

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PK | UUID |
| name | TEXT | Display name (e.g., "Cool Chris") |
| username | TEXT | FCN username |
| gender | TEXT | m / f / o |
| bio | TEXT | Personality description for AI |
| default_tone | TEXT | casual / flirty / teasing / deep / funny / direct |
| default_length | TEXT | short / medium / long |
| proxy_country | TEXT | us / gb / de / custom |
| proxy_custom | TEXT | Decodo or custom SOCKS5 URL |
| user_agent | TEXT | Custom UA string or 'random' |
| timezone | TEXT | e.g., America/Chicago |
| language | TEXT | e.g., en-US |
| fingerprint_rotation | TEXT | never / daily / per_session |
| cooldown_min | INTEGER | Minimum seconds between messages |
| cooldown_max | INTEGER | Maximum seconds between messages |
| daily_cap | INTEGER | Max messages per day |
| auto_reply_dms | INTEGER | 0/1 |
| dm_gender_filter | TEXT | JSON array of allowed genders |
| dm_blocklist | TEXT | JSON array of blocked usernames |
| created_at | TIMESTAMP |
| updated_at | TIMESTAMP |

**llm_providers** — Bring-your-own AI models.

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PK | UUID |
| name | TEXT | User label (e.g., "main", "supervisor") |
| provider_type | TEXT | openrouter / openai / anthropic / custom / ollama |
| model | TEXT | e.g., gpt-4o-mini, claude-3-haiku |
| api_key | TEXT | Encrypted at rest |
| base_url | TEXT | For custom OpenAI-compatible endpoints |
| temperature | REAL | 0.0 – 2.0 |
| role | TEXT | chat / supervisor / fallback |
| enabled | INTEGER | 0/1 |
| priority | INTEGER | Failover order |
| created_at | TIMESTAMP |

**sessions** — Active browser sessions (one per persona login).

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PK | UUID |
| persona_id | TEXT FK | References personas(id) |
| username | TEXT | Snapshot of persona's username at login |
| room_ids | TEXT | JSON array of joined rooms |
| status | TEXT | active / idle / banned / error / reconnecting |
| auto_pilot | INTEGER | 0/1 toggle |
| browser_session_id | TEXT | Browser Use Cloud session reference |
| browser_live_url | TEXT | CDP viewer URL for embedded live view |
| messages_sent_today | INTEGER | Counter, resets daily |
| last_message_at | TIMESTAMP |
| started_at | TIMESTAMP |
| last_seen_at | TIMESTAMP |

**chat_log** — Every message sent and received.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK AUTO |  |
| session_id | TEXT FK | References sessions(id) |
| chat_type | TEXT | group / dm |
| source | TEXT | user / ai / system |
| other_user | TEXT | Username of other party (DMs) or NULL (group) |
| message | TEXT | The message content |
| tone_used | TEXT | Tone when generated |
| supervisor_approved | INTEGER | 0/1 |
| supervisor_note | TEXT | If blocked, why |
| created_at | TIMESTAMP |

**ban_events** — Supervisor learning data.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK AUTO |  |
| session_id | TEXT FK |  |
| persona_id | TEXT FK |  |
| event_type | TEXT | kicked / banned / warning / error |
| likely_reason | TEXT | Supervisor's analysis |
| context_before | TEXT | JSON: last 10 messages |
| context_after | TEXT | JSON: ban page content |
| cooldown_adjustment | INTEGER | Seconds added to cooldown |
| fingerprint_adjustment | TEXT | JSON: what identity to change |
| proxy_adjustment | TEXT | Proxy region to switch to |
| created_at | TIMESTAMP |

**supervisor_rules** — Learned behavior patterns.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK AUTO |  |
| persona_id | TEXT FK | NULL = global rule |
| rule_name | TEXT UNIQUE | too_fast / repetitive / direct_solicit / dm_spam / image_type / time_ban / ip_flag |
| description | TEXT |  |
| trigger_pattern | TEXT | JSON describing the trigger |
| action | TEXT | block / warn / modify / slow_down / rotate_identity |
| severity | INTEGER | 1–10 |
| enabled | INTEGER | 0/1 |
| trigger_count | INTEGER | Times this rule has fired |
| last_triggered | TIMESTAMP |
| created_at | TIMESTAMP |

## Web UI

### Layout (3-column dashboard)

```
┌──────────┬──────────────────────────┬───────────────────┐
│  STATUS  │  LIVE BROWSER VIEW       │  CONTROLS         │
│  PANEL   │  (embedded FCN page)     │                   │
│          │                          │  ── Auto-Pilot ── │
│ 🟢 Live  │  ┌────────────────────┐  │  🤖 [🔴 OFF]    │
│ 👤 Chris │  │                    │  │  Persona: Chris   │
│ 📍 Flirt │  │  Actual FCN.com    │  │  Room: SextChat   │
│          │  │  rendered live     │  │  Cooldown: 60-90s │
│ Today: 3 │  │                    │  │  Daily: 3/50 msgs │
│          │  └────────────────────┘  │                   │
│ ⚡ Bans  │                          │  ── Quick Send ── │
│  0       │                          │  [Type message..] │
│          │                          │  [Send]           │
│          │                          │                   │
│          │                          │  ── Actions ──── │
│          │                          │  [Suggest] ✨     │
│          │                          │  [Rotate IP] 🔄  │
│          │                          │  [Stop Bot] 🛑   │
└──────────┴──────────────────────────┴───────────────────┘
```

### Pages

| Page | Purpose |
|------|---------|
| **Dashboard** | Live browser view, auto-pilot toggle, quick send |
| **Personas** | Create/edit personas with full identity config |
| **Providers** | Add/manage LLM API keys and models |
| **Supervisor** | View learned rules, ban history, override controls |
| **History** | Searchable conversation log with filters |

### Auto-Pilot Toggle

- Big red/green button in the dashboard
- When OFF: browser stays connected, chat feed updates, user sends manually
- When ON: auto-pilot loop starts, AI generates + sends responses, supervisor monitors
- User can override any time by typing manually (pauses auto-pilot for 30s)

## Auto-Pilot Loop

```
1. Read GROUP CHAT messages (every 3s via JS eval)
2. Check for DM notifications (new DM tabs/indicators)
3. For each new message addressed to persona:
   a. Build context (last 10 messages in that thread)
   b. Generate response via chat provider (persona + tone + context)
   c. Send to supervisor for pre-flight check
   d. If approved: wait random cooldown → send
   e. If blocked: log reason, don't send, adjust behavior
4. For DMs: open DM tab, read context, generate, send
5. Log everything to chat_log
6. Every 10 messages: run supervisor pattern scan
```

### Cooldown Logic

- Random delay between `cooldown_min` and `cooldown_max` (configurable per persona)
- Supervisor can increase these on ban events (e.g., "messaged too fast" → double cooldown)
- After a ban: heavy cooldown for first 5 messages of new session (5-10 min)

## Supervisor Engine

### Pre-flight Check (before every send)

```
Message → Supervisor LLM →
  ├── "Is this message likely to get us kicked/banned?"
  ├── Check against known rules (too_fast, repetitive, keywords)
  ├── Check recent ban history for this persona
  └── Result: PASS → send | BLOCK → log + don't send | MODIFY → rephrase
```

### Post-mortem (after ban/kick)

```
Ban detected →
  1. Capture last 10 messages + ban page content
  2. Send to supervisor LLM: "Why were we banned?"
  3. Extract: likely_reason, cooldown_adjustment, fingerprint_adjustment
  4. Create ban_events record
  5. Update supervisor_rules (create or increment trigger_count)
  6. Rotate identity (new proxy + UA) if IP-related
  7. New session with adjusted behavior
```

### Pattern Learning

The supervisor tracks over time:
- **Message frequency** per session → cooldown sweet spot
- **Time-of-day bans** → avoid certain hours
- **Keyword patterns** → build blocklist automatically
- **Response style** → adjust tone if certain styles trigger moderation
- **DM response rate** → slow down if too many DMs in short window

## Per-Profile Identity System

Each persona has a full digital fingerprint that gets applied when provisioning a Browser Use Cloud session:

### Proxy

- Browser Use Cloud supports `--proxy` flag per session
- Options: built-in locations (us / gb / de / jp) or custom SOCKS5/HTTP (Decodo)
- On ban: rotate to different proxy region
- Persona config: `proxy_country` + `proxy_custom`

### User-Agent

- Custom per persona or 'random' (rotates each session)
- Options: Chrome/Win, Safari/Mac, Firefox/Linux, Safari/iOS, Chrome/Android
- Persona config: `user_agent`

### Fingerprint

- Timezone, language, screen resolution — all match the proxy region
- `fingerprint_rotation`: how often to regenerate
- Browser Use Cloud handles most of this via its anti-detection profiles

### Browser Provisioning Flow

```
1. User selects persona → "Start Session"
2. Server calls browser-use cloud connect --proxy <persona.proxy>
3. Apply persona's user-agent via CDP
4. Navigate to freechatnow.com
5. Fill login form with persona's username + gender + birthdate
6. Click "Chat As Guest"
7. Join persona's selected rooms
8. Begin monitoring
9. Return live CDP viewer URL to frontend
```

## Error Recovery

| Failure | Recovery |
|---------|----------|
| Browser Use Cloud disconnect | Auto-reconnect × 3 (exponential backoff: 5s, 15s, 45s) |
| FCN kick/ban | Supervisor logs → rotate identity → new session |
| LLM provider fails | Failover to next priority provider |
| Server restart | SQLite persists → resume from last state |
| FCN down | Poll every 60s × 5, then exponential backoff (5 min, 15 min, 45 min) |

## Railway Deployment

### Environment Variables

```
BROWSER_USE_API_KEY=bu_4u7...key
DATABASE_URL=sqlite:///data/fcn.db
SESSION_SECRET=<auto-generate>
```

### Dockerfile

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Railway Config

- Persistent volume mounted at `/data` (SQLite)
- Health check: `/health` → 200 OK
- Restart: ON_FAILURE

### requirements.txt

```
fastapi
uvicorn[standard]
jinja2
aiosqlite
websockets
httpx
pydantic
python-multipart
```

## Implementation Order

1. **Project scaffold** — FastAPI app, SQLite schema, Dockerfile
2. **Providers module** — LLM provider registry (OpenRouter, OpenAI, etc.)
3. **Personas module** — CRUD + identity config
4. **Browser module** — Browser Use Cloud driver (connect, provision, navigate)
5. **Login flow** — FCN login with persona identity
6. **Chat reading** — JS eval to extract group + DM messages
7. **Sending messages** — JS eval to fill chat input + send
8. **Auto-pilot loop** — Context building, response generation, cooldown
9. **Supervisor engine** — Pre-flight check, post-mortem, rule learning
10. **Web UI** — Dashboard with embedded live view, all management pages
11. **Error recovery** — Reconnection, ban recovery, failover
12. **Railway deployment** — Dockerfile, env config, health checks
13. **Testing** — Manual walkthrough of full flow

## Open Questions

- Decodo proxy format: SOCKS5 URL or HTTP? Need to confirm how Decodo provides proxies and format for Browser Use Cloud's `--proxy` flag.
- Browser Use Cloud live viewer URL format — need to verify the CDP viewer endpoint returns an embeddable URL.
- FCN DM detection — need to reverse-engineer how FCN indicates new DMs in the Vue app (likely a DOM change or notification badge).
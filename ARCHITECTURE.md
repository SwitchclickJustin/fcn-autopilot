# FCN Auto-Pilot — Project Overview

## Location

```
~/FCNAssistant/
```

GitHub: `https://github.com/SwitchclickJustin/fcn-autopilot`
Deployed on Railway (auto-deploys from `main` branch).

## Architecture

```
User clicks "Start Session"
    │
    ▼
FastAPI (/api/session/start)
    │
    ▼
BotOrchestrator.start_bot(persona)
    │
    ├─ Step 1: SDK Profiles (persistent cookies, 1 per persona)
    │   client.profiles.create(name="fcn-Flirtyalexa9")
    │
    ├─ Step 2: Browser provisioning (~3s, returns live_url immediately)
    │   client.browsers.create(customProxy={Decoda proxy})
    │
    ├─ Step 3: SDK agent login (background, 30-60s)
    │   client.run("Go to freechatnow.com, log in as guest...")
    │
    ├─ Step 4: CDP connection (fast JS loop, zero cost per tick)
    │   Playwright CDP → read_chat / send_message via evaluate()
    │
    └─ Step 5: Auto-pilot loop (every 3s per bot)
        CDP: read chat → LLM generate → CDP send
```

## File Map

| File | Purpose |
|------|---------|
| `app/browser.py` | **Core orchestrator** — BotOrchestrator + BotWorker + SDK integration |
| `app/autopilot.py` | Auto-pilot engine — delegates to orchestrator, handles LLM/DB logging |
| `app/main.py` | FastAPI app — API endpoints for session start/stop/state |
| `app/providers.py` | LLM provider registry (OpenRouter, etc.) |
| `app/supervisor.py` | Content safety filter for generated messages |
| `app/config.py` | Settings via env vars (pydantic-settings) |
| `app/database.py` | SQLite/Postgres DB for personas, sessions, chat logs |
| `app/models.py` | Pydantic models for personas, sessions |
| `railway.json` | Railway deploy config |
| `Dockerfile` | Docker build (python:3.11-slim) |
| `requirements.txt` | Python deps including `browser-use-sdk>=3.8.0` |

## Key File: `app/browser.py` (472 lines)

Located at: `~/FCNAssistant/app/browser.py`

### Classes

**`BotWorker`** — per-bot runtime state
```python
class BotWorker:
    username: str          # "Flirtyalexa9"
    profile_id: str        # SDK profile UUID (persistent cookies)
    session_id: str        # SDK session UUID
    browser_id: str        # Browser Use browser UUID
    live_url: str          # Embedded live view URL
    status: str            # "created" | "logging_in" | "running" | "error"
    _page, _cdp, _playwright  # CDP connection (for fast JS evaluate loop)
```

**`BotOrchestrator`** — manages 50 concurrent bots
```python
class BotOrchestrator:
    _client       # Lazy AsyncBrowserUse SDK client
    _semaphore    # asyncio.Semaphore(50) — concurrency cap
    _workers      # dict[str, BotWorker] — one per username
```

Key methods:
- `start_bot(persona)` → provisions browser, returns immediately with live_url
- `_finish_bot_setup(...)` → background task: SDK login → CDP connect → auto-pilot
- `_run_auto_pilot(worker)` → fast CDP loop: read chat → LLM respond → send
- `stop_bot(username)` → stops session + saves cookies to profile
- `stop_all()` → graceful shutdown
- `start_session(persona)` → legacy alias for main.py compatibility

### Decoda Proxies

```python
DECODA_PROXIES = [
    {"host": "gate.decodo.com", "port": p,
     "username": "sp2ihy1g3e", "password": "8tjpKDcFwLem7j5v+2"}
    for p in range(10001, 10011)
]
```
Port-rotated per session. Sent as `customProxy` (camelCase — REST API field) via SDK `**extra` kwargs. Requires a paid Browser Use Cloud plan (any tier with credits).

### Singleton

```python
browser_manager = BotOrchestrator()
```
Imported by both `main.py` and `autopilot.py`.

### Flow Detail

1. `start_bot()`:
   - Creates/get SDK profile (persistent cookies survive restarts)
   - `client.browsers.create(customProxy=decoda, ...)` — provisions Chromium with Decoda baked into launch flags
   - Returns `BotWorker` with `live_url` immediately (~3s)
   - Background task: SDK agent navigates FCN, handles ad gateways, fills login form, clicks "Chat As Guest"

2. `_finish_bot_setup()` (background):
   - `client.run("Go to freechatnow.com, log in as guest...")` — SDK agent handles login autonomously
   - Playwright CDP `connect_over_cdp()` for fast JS evaluate
   - Start auto-pilot loop

3. `_run_auto_pilot()` (per-bot loop, 3-second ticks):
   - `page.evaluate()` to read chat messages (zero cost)
   - LLM generates response via configured provider
   - `page.evaluate()` to type + send message

4. `stop_bot()`:
   - Cancel auto-pilot task
   - Disconnect CDP
   - `client.sessions.stop(session_id)` — saves cookies to profile

## File: `app/autopilot.py`

Located at: `~/FCNAssistant/app/autopilot.py`

Minimal wrapper — delegates browser work to `BotOrchestrator`. Handles:
- `start(session_id, persona)` → calls `browser_manager.start_bot(persona)`
- `stop()` → calls `browser_manager.stop_bot(username)`
- `generate_suggestions(context, count)` → LLM response suggestions for manual mode

Singleton: `auto_pilot = AutoPilotEngine()`

## Environment Variables

Set in Railway dashboard → Variables:

| Variable | Required | Notes |
|----------|----------|-------|
| `BROWSER_USE_API_KEY` | ✅ | Your Browser Use Cloud API key |
| `OPENROUTER_API_KEY` | ✅ | For LLM chat responses |
| `NEON_DATABASE_URL` | Optional | Neon PostgreSQL for persistence |
| `DATABASE_PATH` | Optional | Defaults to `/data/fcn.db` (Railway volume) |
| `SESSION_SECRET` | Optional | For session cookies |
| `LOG_LEVEL` | Optional | Defaults to `INFO` |

## Deployment

- **Platform:** Railway (auto-deploys from GitHub `main` branch)
- **Config:** `railway.json` + `Dockerfile`
- **Healthcheck:** `GET /health` (every 30s, 15s start period)
- **Port:** `8000` (configurable via `PORT` env var)

### API Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Healthcheck |
| `POST /api/session/start` | Start a bot session (body: `{"persona_id": "..."}`) |
| `POST /api/session/stop` | Stop current session |
| `POST /api/session/toggle-autopilot` | Enable/disable auto-pilot |
| `GET /api/session/state` | Current session status |
| `POST /api/session/send` | Send a manual message |
| `GET /debug/browser-status` | Orchestrator status + live URLs |
| `GET /debug/browser-test` | Provision a test browser |

## What Needs Doing

### High Priority
- [ ] **Test the browser provisioning** with `customProxy` on paid plan — the `client.browsers.create(customProxy=...)` should work now that credits are loaded
- [ ] **Test SDK agent login** — run `client.run("Go to freechatnow.com, log in as guest...")` and verify it completes
- [ ] **Test CDP auto-pilot** — after login, verify `read_chat()` and `send_message()` via CDP JS evaluate

### Medium Priority
- [ ] **Error recovery** — if SDK login fails, retry or alert
- [ ] **Profile reuse** — existing profiles should skip login on subsequent sessions
- [ ] **Dashboard improvements** — show per-bot status (connected, logging in, running, error)

### Future / 50-Bot Scaling
- [ ] **Task pool** — use `asyncio.gather()` with semaphore to start 50 bots
- [ ] **Health monitoring** — periodic check on each bot's CDP connection
- [ ] **Auto-restart** — if a bot errors, restart it with same profile
- [ ] **Per-bot rate limiting** — cooldown between messages per persona

## SDK Reference

Package: `browser-use-sdk>=3.8.0`
Async client: `from browser_use_sdk.v3 import AsyncBrowserUse`

```python
client = AsyncBrowserUse(api_key="...")

# Profiles (persistent cookies)
profile = await client.profiles.create(name="fcn-Flirtyalexa9")
profiles = await client.profiles.list(query="fcn-")

# Browsers (standalone, with custom proxy)
browser = await client.browsers.create(
    profile_id=profile.id,
    customProxy={"host": "...", "port": 10001, "username": "...", "password": "..."},
    # customProxy goes through **extra to REST API body
)

# Sessions (agent-driven tasks)
result = await client.run(
    "Go to freechatnow.com, log in as guest...",
    profile_id=profile.id,
    session_id=session.id,  # resume existing session
    keep_alive=True,
)
output = await result  # SessionResult with .output and .session

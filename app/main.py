"""FCN Auto-Pilot — FastAPI application."""
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import httpx

from app.config import settings
from app.database import (
    get_db, get_personas, get_persona, create_persona, update_persona, delete_persona,
    get_providers, create_provider, delete_provider,
    create_session, update_session, get_active_session, get_session,
    log_chat, get_chat_log, get_ban_events, get_rules
)
from app.models import PersonaCreate, PersonaUpdate, LLMProviderCreate, new_id
from app.providers import provider_registry
from app.browser import browser_manager
from app.autopilot import auto_pilot
from app.supervisor import supervisor_engine

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
logger = logging.getLogger(__name__)

# ─── Lifecycle ───
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("FCN Auto-Pilot starting up...")
    
    # Check if old DB exists at default location and migrate to persistent volume
    import os, shutil
    default_db = "fcn.db"
    persistent_db = settings.database_path
    if default_db != persistent_db and os.path.exists(default_db):
        if not os.path.exists(persistent_db):
            shutil.copy2(default_db, persistent_db)
            logger.info(f"Migrated DB from {default_db} to {persistent_db}")
        else:
            logger.info(f"Persistent DB exists at {persistent_db}, skipping migration")
    
    db = await get_db()
    await db.close()
    providers = await get_providers()
    provider_registry.load_from_db(providers)
    logger.info(f"Loaded {len(providers)} LLM providers from database")
    yield
    logger.info("Shutting down...")
    await auto_pilot.stop()
    await browser_manager.stop_session()

app = FastAPI(title="FCN Auto-Pilot", version="0.1.0", lifespan=lifespan)

# Static files + templates
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Create Jinja2 environment with cache disabled to fix Jinja2>=3.1.6 hash error
from jinja2 import Environment, FileSystemLoader
_cache_free_env = Environment(loader=FileSystemLoader("app/templates"), cache_size=0)
templates = Jinja2Templates(env=_cache_free_env)

# ─── WebSocket connections ───
connected_websockets: set = set()

async def broadcast(msg: dict):
    dead = set()
    for ws in connected_websockets:
        try:
            await ws.send_json(msg)
        except Exception:
            dead.add(ws)
    connected_websockets -= dead

# ─── Health ───
@app.get("/health")
async def health():
    return {"status": "ok", "auto_pilot": auto_pilot.enabled}

# ─── DB Debug ───
@app.get("/debug/persona-update-model")
async def debug_persona_model():
    """Check which fields PersonaUpdate accepts."""
    import inspect
    from app.models import PersonaUpdate
    fields = list(PersonaUpdate.model_fields.keys())
    return {"fields": fields, "count": len(fields)}

@app.get("/debug/browser-test")
async def debug_browser():
    """Test Browser Use Cloud v3 API."""
    import traceback
    import httpx
    
    results = {}
    
    # Test 1: Check API key
    results["api_key_set"] = bool(settings.browser_use_api_key)
    results["api_key_prefix"] = settings.browser_use_api_key[:10] + "..." if settings.browser_use_api_key else ""
    
    # Test 2: Try creating a browser via v3 API
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.browser-use.com/api/v3/browsers",
                headers={
                    "X-Browser-Use-API-Key": settings.browser_use_api_key,
                    "Content-Type": "application/json"
                },
                json={"timeout": 10, "browserScreenWidth": 1280, "browserScreenHeight": 720}
            )
            results["api_status"] = resp.status_code
            results["api_response"] = resp.text[:500]
    except Exception as e:
        results["api_error"] = str(e)
        results["api_traceback"] = traceback.format_exc()
    
    # Test 3: Try importing playwright and connecting via CDP
    try:
        import playwright
        results["playwright_installed"] = True
        # Can't easily check version without __version__
    except Exception as e:
        results["playwright_installed"] = False
        results["playwright_error"] = str(e)
    
    # Test 4: If we got a browser created, try CDP connection
    if results.get("api_status") == 201:
        try:
            import json
            data = json.loads(results.get("api_response", "{}"))
            cdp_url = data.get("cdpUrl", "")
            box_id = data.get("id", "")
            results["cdp_url_raw"] = cdp_url
            
            # CDP URL is HTTPS — Playwright needs wss://
            wss_url = cdp_url.replace("https://", "wss://")
            results["cdp_wss_url"] = wss_url
            
            from playwright.async_api import async_playwright
            p = await async_playwright().start()
            try:
                browser = await p.chromium.connect_over_cdp(wss_url, timeout=15000)
                results["cdp_connected"] = True
                results["cdp_version"] = browser.version
                contexts = browser.contexts
                if contexts:
                    pages = contexts[0].pages
                    results["cdp_pages"] = len(pages)
                await browser.close()
            except Exception as e:
                results["cdp_connected"] = False
                results["cdp_error"] = str(e)[:200]
            await p.stop()
            
            # Clean up test browser
            async with httpx.AsyncClient(timeout=10) as client:
                await client.delete(
                    f"https://api.browser-use.com/api/v3/browsers/{box_id}",
                    headers={"X-Browser-Use-API-Key": settings.browser_use_api_key}
                )
        except Exception as e:
            results["cdp_test_error"] = str(e)[:300]
    
    return results

@app.get("/debug/cleanup-browsers")
async def cleanup_browsers():
    """List and optionally delete stale Browser Use Cloud sessions."""
    import httpx
    results = {"stale": [], "deleted": 0, "errors": []}
    
    async with httpx.AsyncClient(timeout=30) as client:
        # List all active browsers
        try:
            resp = await client.get(
                "https://api.browser-use.com/api/v3/browsers?page=1&page_size=10",
                headers={"X-Browser-Use-API-Key": settings.browser_use_api_key}
            )
            if resp.status_code != 200:
                return {"error": f"List failed: {resp.status_code}", "detail": resp.text[:200]}
            data = resp.json()
            browsers = data.get("browsers", [])
            results["total_browsers"] = len(browsers)
            
            for b in browsers:
                info = {
                    "id": b["id"],
                    "status": b.get("status", "?"),
                    "started": b.get("startedAt", "?")[:19],
                }
                results["stale"].append(info)
                
                # Delete it
                try:
                    del_resp = await client.delete(
                        f"https://api.browser-use.com/api/v3/browsers/{b['id']}",
                        headers={"X-Browser-Use-API-Key": settings.browser_use_api_key}
                    )
                    if del_resp.status_code in (200, 204):
                        results["deleted"] += 1
                    else:
                        results["errors"].append(f"{b['id']}: {del_resp.status_code}")
                except Exception as e:
                    results["errors"].append(f"{b['id']}: {e}")
        except Exception as e:
            return {"error": str(e)}
    
    # Also stop any local session
    if browser_manager.current_session:
        await browser_manager.stop_session()
    
    return results

@app.get("/debug/browser-status")
async def debug_browser_status():
    """Check the in-memory browser session status."""
    if browser_manager.current_session:
        info = {
            "status": browser_manager.current_session.status,
            "box_id": browser_manager.current_session.box_id,
            "live_url": browser_manager.current_session.live_url[:80] if browser_manager.current_session.live_url else "",
            "connected": browser_manager.current_session._connected,
            "has_page": browser_manager.current_session._page is not None,
        }
        # Try to get the current URL from the page
        if browser_manager.current_session._page:
            try:
                info["current_url"] = browser_manager.current_session._page.url[:120]
            except Exception:
                info["current_url"] = "error getting url"
        return info
    return {"status": "no_session"}

@app.get("/debug/db")
async def debug_db():
    import os
    db_path = settings.database_path
    exists = os.path.exists(db_path)
    size = os.path.getsize(db_path) if exists else 0
    providers = await get_providers()
    personas = await get_personas()
    # Check if /data is a real volume or ephemeral
    data_is_mount = os.path.ismount("/data") if os.path.exists("/data") else False
    return {
        "db_path": db_path,
        "exists": exists,
        "size_bytes": size,
        "data_is_mount": data_is_mount,
        "providers_count": len(providers),
        "personas_count": len(personas),
        "providers": [{"name": p["name"], "model": p["model"], "role": p["role"]} for p in providers],
        "personas": [{"name": p["name"], "username": p["username"]} for p in personas],
        "env": {
            "browser_use_key_set": bool(settings.browser_use_api_key),
            "browser_use_key_prefix": settings.browser_use_api_key[:7] + "..." if settings.browser_use_api_key else "",
            "neon_set": bool(settings.neon_database_url),
            "openrouter_set": bool(settings.openrouter_api_key),
        }
    }

# ─── WebSocket ───
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected_websockets.add(ws)
    try:
        while True:
            data = await ws.receive_json()
            cmd = data.get("command", "")
            if cmd == "toggle_autopilot":
                enabled = data.get("enabled", False)
                if enabled and not auto_pilot.enabled:
                    session = await get_active_session()
                    if session and session.get("persona_id"):
                        persona = await get_persona(session["persona_id"])
                        if persona:
                            await auto_pilot.start(session["id"], persona)
                            await broadcast({"type": "status", "data": {"auto_pilot": True}})
                elif not enabled and auto_pilot.enabled:
                    await auto_pilot.stop()
                    await broadcast({"type": "status", "data": {"auto_pilot": False}})
            elif cmd == "send_message":
                msg = data.get("message", "")
                if msg and browser_manager.current_session:
                    await browser_manager.current_session.send_message(msg)
            elif cmd == "refresh_state":
                if browser_manager.current_session:
                    msgs = await browser_manager.current_session.read_chat()
                    await ws.send_json({"type": "chat_update", "data": {"messages": msgs}})
    except WebSocketDisconnect:
        connected_websockets.discard(ws)

# ─── Pages ───
# ─── Pages ───
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    try:
        personas = await get_personas()
        providers = await get_providers()
        session = await get_active_session()
        rules = await get_rules()
        ban_events = await get_ban_events()
        return templates.TemplateResponse(request, "dashboard.html", context={
            "personas": personas,
            "providers": providers,
            "session": session,
            "rules": rules,
            "ban_events": ban_events,
            "auto_pilot_on": auto_pilot.enabled,
            "browser_live_url": browser_manager.current_session.live_url if browser_manager.current_session else ""
        })
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"DASHBOARD ERROR: {e}\n{tb}")
        return HTMLResponse(f"<pre>{tb}</pre>", status_code=500)

@app.get("/personas", response_class=HTMLResponse)
async def personas_page(request: Request):
    personas = await get_personas()
    return templates.TemplateResponse(request, "personas.html", context={
        "personas": personas
    })

@app.get("/providers", response_class=HTMLResponse)
async def providers_page(request: Request):
    providers = await get_providers()
    return templates.TemplateResponse(request, "providers.html", context={
        "providers": providers
    })

@app.get("/supervisor", response_class=HTMLResponse)
async def supervisor_page(request: Request):
    rules = await get_rules()
    ban_events = await get_ban_events()
    return templates.TemplateResponse(request, "supervisor.html", context={
        "rules": rules,
        "ban_events": ban_events
    })

@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    session = await get_active_session()
    logs = []
    if session:
        logs = await get_chat_log(session["id"], 100)
    return templates.TemplateResponse(request, "history.html", context={
        "logs": logs
    })

# ─── API: Session ───
@app.post("/api/session/start")
async def start_session(data: dict):
    import traceback
    persona_id = data.get("persona_id", "")
    if not persona_id:
        raise HTTPException(400, "persona_id required")
    persona = await get_persona(persona_id)
    if not persona:
        raise HTTPException(404, "Persona not found")
    for field in ["selected_rooms", "dm_gender_filter", "dm_blocklist"]:
        if isinstance(persona.get(field), str):
            try:
                persona[field] = json.loads(persona[field])
            except (json.JSONDecodeError, TypeError):
                persona[field] = []
    sess = await create_session({
        "id": new_id(),
        "persona_id": persona_id,
        "username": persona.get("username", ""),
        "room_ids": persona.get("selected_rooms", ["SextChat"]),
        "status": "connecting"
    })
    try:
        browser_sess = await browser_manager.start_session(persona)
        if not browser_sess:
            await update_session(sess["id"], {"status": "error"})
            raise HTTPException(500, detail="Browser session failed — check Railway logs for details.")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"BROWSER START ERROR: {e}\n{traceback.format_exc()}")
        await update_session(sess["id"], {"status": "error"})
        raise HTTPException(500, detail=f"Browser session failed: {e}")
    
    await update_session(sess["id"], {
        "status": "active",
        "browser_session_id": browser_sess.box_id,
        "browser_live_url": browser_sess.live_url
    })
    
    # Auto-enable auto-pilot
    await auto_pilot.start(sess["id"], persona)
    await update_session(sess["id"], {"auto_pilot": True})
    
    return {"session_id": sess["id"], "status": "active", "live_url": browser_sess.live_url, "auto_pilot": True}

@app.post("/api/session/stop")
async def stop_session():
    await auto_pilot.stop()
    await browser_manager.stop_session()
    session = await get_active_session()
    if session:
        await update_session(session["id"], {"status": "idle", "auto_pilot": False})
    return {"status": "stopped"}

@app.post("/api/session/toggle-autopilot")
async def toggle_autopilot(data: dict):
    enabled = data.get("enabled", False)
    if enabled and not auto_pilot.enabled:
        session = await get_active_session()
        if not session or not session.get("persona_id"):
            raise HTTPException(400, "No active session or persona")
        persona = await get_persona(session["persona_id"])
        if not persona:
            raise HTTPException(404, "Persona not found")
        await auto_pilot.start(session["id"], persona)
        await update_session(session["id"], {"auto_pilot": True})
    elif not enabled and auto_pilot.enabled:
        await auto_pilot.stop()
        session = await get_active_session()
        if session:
            await update_session(session["id"], {"auto_pilot": False})
    return {"auto_pilot": auto_pilot.enabled}

@app.get("/api/session/state")
async def session_state():
    session = await get_active_session()
    msgs = []
    live_url = ""
    if browser_manager.current_session:
        msgs = await browser_manager.current_session.read_chat()
        live_url = browser_manager.current_session.live_url
    return {"session": session, "messages": msgs, "live_url": live_url, "auto_pilot": auto_pilot.enabled}

@app.post("/api/session/send")
async def send_message(data: dict):
    msg = data.get("message", "")
    if not msg:
        raise HTTPException(400, "message required")
    if not browser_manager.current_session:
        raise HTTPException(400, "No active session")
    sent = await browser_manager.current_session.send_message(msg)
    if sent:
        session = await get_active_session()
        if session:
            await log_chat({"session_id": session["id"], "chat_type": "group", "source": "user", "message": msg})
    return {"sent": sent}

# ─── API: Personas ───
@app.get("/api/personas")
async def api_personas():
    return await get_personas()

@app.post("/api/personas")
async def api_create_persona(data: PersonaCreate):
    import traceback
    try:
        return await create_persona(data.model_dump())
    except Exception as e:
        logger.error(f"PERSONA CREATE ERROR: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, detail=f"Persona create failed: {e}")

@app.put("/api/personas/{persona_id}")
async def api_update_persona(persona_id: str, data: PersonaUpdate):
    import traceback
    try:
        updates = {k: v for k, v in data.model_dump().items() if v is not None}
        if updates:
            await update_persona(persona_id, updates)
        return {"updated": True}
    except Exception as e:
        logger.error(f"PERSONA UPDATE ERROR: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, detail=f"Persona update failed: {e}")

@app.delete("/api/personas/{persona_id}")
async def api_delete_persona(persona_id: str):
    await delete_persona(persona_id)
    return {"deleted": True}

# ─── API: LLM Providers ───
@app.get("/api/providers")
async def api_providers():
    return await get_providers()

# Get available models from provider API
@app.post("/api/providers/models")
async def api_provider_models(data: dict):
    provider_type = data.get("provider_type", "")
    api_key = data.get("api_key", "")
    if not api_key:
        return {"models": []}
    try:
        if provider_type == "openrouter":
            import httpx
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get("https://openrouter.ai/api/v1/models", headers={"Authorization": f"Bearer {api_key}"})
                if r.status_code == 200:
                    models = r.json().get("data", [])
                    return {"models": [m["id"] for m in models[:200]]}
        elif provider_type == "openai":
            return {"models": ["gpt-4o-mini", "gpt-4o", "gpt-4-turbo", "gpt-3.5-turbo"]}
        elif provider_type == "anthropic":
            return {"models": ["claude-sonnet-4", "claude-3-haiku", "claude-3-opus"]}
    except Exception as e:
        logger.error(f"Failed to fetch models: {e}")
    return {"models": []}

@app.post("/api/providers")
async def api_create_provider(data: LLMProviderCreate):
    # If api_key is __use_env__, resolve from environment
    if data.api_key == "__use_env__":
        env_map = {
            "openrouter": settings.openrouter_api_key,
            "openai": settings.openai_api_key,
            "anthropic": settings.anthropic_api_key,
        }
        resolved = env_map.get(data.provider_type, "")
        if not resolved:
            raise HTTPException(400, f"No API key found in environment for {data.provider_type}. "
                                      f"Set {data.provider_type.upper()}_API_KEY in Railway Variables.")
        data.api_key = resolved
    
    provider = await create_provider(data.model_dump())
    providers = await get_providers()
    provider_registry.load_from_db(providers)
    return provider

@app.delete("/api/providers/{provider_id}")
async def api_delete_provider(provider_id: str):
    await delete_provider(provider_id)
    providers = await get_providers()
    provider_registry.load_from_db(providers)
    return {"deleted": True}

# ─── API: Suggestions ───
@app.post("/api/suggest")
async def api_suggest(data: dict):
    context = data.get("context", "")
    count = data.get("count", 5)
    suggestions = await auto_pilot.generate_suggestions(context, count)
    return {"suggestions": suggestions}

# ─── API: Generate Username ───
@app.post("/api/generate-username")
async def api_generate_username(data: dict):
    vibe = data.get("vibe", "flirty")
    provider = provider_registry.get_chat_provider()
    if not provider:
        # Fallback: generate a simple username
        import random
        names = ["Babe", "Cutie", "Hunny", "Princess", "Angel", "Sweetie", "Doll", "Missy", "Kitten", "Vixen", "Siren", "Bombshell"]
        nums = str(random.randint(10, 9999))
        return {"username": random.choice(names) + nums}
    
    system = "Generate a single sexy, fun username for an adult chat room. Only letters and numbers, no spaces or special chars. 6-15 characters. Female vibe. Examples: SweetVixen88, BabeNextDoor, HoneyDrip42"
    result = await provider.chat(system, f"Vibe: {vibe}. Generate one username, nothing else.", max_tokens=50)
    
    if result:
        # Clean the result - only allow letters and numbers
        import re
        clean = re.sub(r'[^a-zA-Z0-9]', '', result.strip())[:15]
        if len(clean) >= 4:
            return {"username": clean}
    
    import random
    fallbacks = ["SugarSpice", "VelvetAngel", "SweetTempt", "BlushingBabe", "CherryBlossom", "SilkDreams", "GoldenMuse", "LunaFlirt"]
    return {"username": random.choice(fallbacks) + str(random.randint(10, 999))}

# ─── API: Supervisor ───
@app.get("/api/supervisor/rules")
async def api_rules():
    return await get_rules()

@app.get("/api/supervisor/ban-events")
async def api_ban_events():
    return await get_ban_events()

# ─── Entry point ───
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True)
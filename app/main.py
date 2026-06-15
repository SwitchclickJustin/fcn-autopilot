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
        "personas": [{"name": p["name"], "username": p["username"]} for p in personas]
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
    except Exception as e:
        logger.error(f"BROWSER START ERROR: {e}\n{traceback.format_exc()}")
        await update_session(sess["id"], {"status": "error"})
        raise HTTPException(500, detail=f"Browser session failed: {e}")
    if not browser_sess:
        await update_session(sess["id"], {"status": "error"})
        raise HTTPException(500, detail="Failed to start browser session — check BROWSER_USE_API_KEY is set in Railway")
    await update_session(sess["id"], {
        "status": "active",
        "browser_session_id": browser_sess.box_id,
        "browser_live_url": browser_sess.live_url
    })
    return {"session_id": sess["id"], "status": "active", "live_url": browser_sess.live_url}

@app.post("/api/session/stop")
async def stop_session():
    await auto_pilot.stop()
    await browser_manager.stop_session()
    session = await get_active_session()
    if session:
        await update_session(session["id"], {"status": "idle"})
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
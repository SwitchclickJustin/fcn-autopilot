"""FCN Auto-Pilot — FastAPI application."""
import json
import logging
import os
import time
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
from app.models import PersonaCreate, PersonaUpdate, LLMProviderCreate
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
    # Ensure DB tables exist
    db = await get_db()
    await db.close()
    # Load providers from DB
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
templates = Jinja2Templates(directory="app/templates")

# ─── WebSocket connections ───
connected_websockets: set = set()

async def broadcast(msg: dict):
    """Send a message to all connected WebSocket clients."""
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

# ─── WebSocket ───
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected_websockets.add(ws)
    try:
        while True:
            data = await ws.receive_json()
            # Handle incoming commands from UI
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
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    personas = await get_personas()
    providers = await get_providers()
    session = await get_active_session()
    rules = await get_rules()
    ban_events = await get_ban_events()
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "personas": personas,
        "providers": providers,
        "session": session,
        "rules": rules,
        "ban_events": ban_events,
        "auto_pilot_on": auto_pilot.enabled,
        "browser_live_url": browser_manager.current_session.live_url if browser_manager.current_session else ""
    })

@app.get("/personas", response_class=HTMLResponse)
async def personas_page(request: Request):
    personas = await get_personas()
    return templates.TemplateResponse("personas.html", {
        "request": request,
        "personas": personas
    })

@app.get("/providers", response_class=HTMLResponse)
async def providers_page(request: Request):
    providers = await get_providers()
    return templates.TemplateResponse("providers.html", {
        "request": request,
        "providers": providers
    })

@app.get("/supervisor", response_class=HTMLResponse)
async def supervisor_page(request: Request):
    rules = await get_rules()
    ban_events = await get_ban_events()
    return templates.TemplateResponse("supervisor.html", {
        "request": request,
        "rules": rules,
        "ban_events": ban_events
    })

@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    session = await get_active_session()
    logs = []
    if session:
        logs = await get_chat_log(session["id"], 100)
    return templates.TemplateResponse("history.html", {
        "request": request,
        "logs": logs
    })

# ─── API: Session ───
@app.post("/api/session/start")
async def start_session(data: dict):
    persona_id = data.get("persona_id", "")
    if not persona_id:
        raise HTTPException(400, "persona_id required")
    persona = await get_persona(persona_id)
    if not persona:
        raise HTTPException(404, "Persona not found")

    # Parse JSON fields
    for field in ["selected_rooms", "dm_gender_filter", "dm_blocklist"]:
        if isinstance(persona.get(field), str):
            try:
                persona[field] = json.loads(persona[field])
            except (json.JSONDecodeError, TypeError):
                persona[field] = []

    # Create session record
    sess = {
        "persona_id": persona_id,
        "username": persona.get("username", ""),
        "room_ids": persona.get("selected_rooms", ["SextChat"]),
        "status": "connecting"
    }
    sess = await create_session(sess)

    # Start browser session
    browser_sess = await browser_manager.start_session(persona)
    if not browser_sess:
        await update_session(sess["id"], {"status": "error"})
        raise HTTPException(500, "Failed to start browser session")

    await update_session(sess["id"], {
        "status": "active",
        "browser_session_id": browser_sess.session_id,
        "browser_live_url": browser_sess.live_url
    })

    return {
        "session_id": sess["id"],
        "status": "active",
        "live_url": browser_sess.live_url
    }

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
    return {
        "session": session,
        "messages": msgs,
        "live_url": live_url,
        "auto_pilot": auto_pilot.enabled
    }

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
            await log_chat({
                "session_id": session["id"],
                "chat_type": "group",
                "source": "user",
                "message": msg
            })
    return {"sent": sent}

# ─── API: Personas ───
@app.get("/api/personas")
async def api_personas():
    return await get_personas()

@app.post("/api/personas")
async def api_create_persona(data: PersonaCreate):
    persona = await create_persona(data.model_dump())
    return persona

@app.put("/api/personas/{persona_id}")
async def api_update_persona(persona_id: str, data: PersonaUpdate):
    updates = {k: v for k, v in data.model_dump().items() if v is not None}
    if updates:
        await update_persona(persona_id, updates)
    return {"updated": True}

@app.delete("/api/personas/{persona_id}")
async def api_delete_persona(persona_id: str):
    await delete_persona(persona_id)
    return {"deleted": True}

# ─── API: LLM Providers ───
@app.get("/api/providers")
async def api_providers():
    return await get_providers()

@app.post("/api/providers")
async def api_create_provider(data: LLMProviderCreate):
    provider = await create_provider(data.model_dump())
    # Reload providers
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
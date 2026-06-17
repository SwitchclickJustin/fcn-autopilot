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

@app.get("/debug/test-decoda")
async def debug_test_decoda():
    """Test creating a cloud browser with Decoda proxy."""
    import httpx, random
    
    decoda_proxies = [
        {"host": "gate.decodo.com", "port": 10001, "username": "sp2ihy1g3e", "password": "8tjpKDcFwLem7j5v+2"},
    ]
    proxy = random.choice(decoda_proxies)
    
    results = {}
    
    # Test 1: Create browser WITHOUT proxy
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.browser-use.com/api/v3/browsers",
                headers={"X-Browser-Use-API-Key": settings.browser_use_api_key, "Content-Type": "application/json"},
                json={"timeout": 5, "browserScreenWidth": 1280, "browserScreenHeight": 720}
            )
            results["no_proxy_status"] = resp.status_code
            results["no_proxy_body"] = resp.text[:300]
            if resp.status_code == 201:
                data = resp.json()
                bid = data["id"]
                # Clean up
                await client.delete(f"https://api.browser-use.com/api/v3/browsers/{bid}",
                    headers={"X-Browser-Use-API-Key": settings.browser_use_api_key})
    except Exception as e:
        results["no_proxy_error"] = str(e)
    
    # Test 2: Create browser WITH Decoda proxy
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.browser-use.com/api/v3/browsers",
                headers={"X-Browser-Use-API-Key": settings.browser_use_api_key, "Content-Type": "application/json"},
                json={
                    "timeout": 5,
                    "browserScreenWidth": 1280,
                    "browserScreenHeight": 720,
                    "customProxy": proxy
                }
            )
            results["with_proxy_status"] = resp.status_code
            results["with_proxy_body"] = resp.text[:500]
            if resp.status_code == 201:
                data = resp.json()
                bid = data["id"]
                # Clean up
                await client.delete(f"https://api.browser-use.com/api/v3/browsers/{bid}",
                    headers={"X-Browser-Use-API-Key": settings.browser_use_api_key})
    except Exception as e:
        results["with_proxy_error"] = str(e)
    
    results["used_proxy"] = proxy
    return results

# JS that snapshots the DOM (forms, inputs, selects, buttons, iframes, body text).
_SNAP_JS = """
(() => {
  const q = (sel) => Array.from(document.querySelectorAll(sel));
  const inputs = q('input').slice(0,50).map(e => ({
    type: e.type, name: e.name, id: e.id, placeholder: e.placeholder,
    value: (e.type === 'password' ? '' : (e.value || '').slice(0,40))
  }));
  const selects = q('select').slice(0,15).map(e => ({
    name: e.name, id: e.id,
    options: Array.from(e.options).slice(0,40).map(o => ({value: o.value, text: (o.textContent||'').trim()}))
  }));
  const buttons = q('button, input[type=submit], input[type=button], a[role=button], [onclick]')
    .slice(0,50).map(e => ({
      tag: e.tagName.toLowerCase(), type: e.getAttribute('type'),
      id: e.id, cls: (e.className && e.className.toString ? e.className.toString() : ''),
      text: (e.textContent || e.value || '').trim().slice(0,80)
    })).filter(b => b.text || b.id);
  const forms = q('form').slice(0,10).map(e => ({
    id: e.id, name: e.name, action: e.getAttribute('action'), method: e.getAttribute('method')
  }));
  const iframes = q('iframe').slice(0,10).map(e => ({id: e.id, src: e.src, name: e.name}));
  const links = q('a[href]').slice(0,80).map(e => ({
    href: e.href, text: (e.textContent || '').trim().slice(0,40)
  })).filter(l => l.text);
  const textareas = q('textarea').slice(0,15).map(e => ({
    name: e.name, id: e.id, placeholder: e.placeholder,
    cls: (e.className && e.className.toString ? e.className.toString() : '')
  }));
  const editables = q('[contenteditable]').slice(0,15).map(e => ({
    id: e.id, role: e.getAttribute('role'),
    cls: (e.className && e.className.toString ? e.className.toString() : ''),
    placeholder: e.getAttribute('data-placeholder') || e.getAttribute('placeholder')
  }));
  const closeButtons = (() => {
    const out = [];
    document.querySelectorAll('*').forEach(e => {
      if (e.children.length > 0) return;               // leaf nodes only
      const t = (e.textContent || '').trim();
      const c = (e.className && e.className.toString) ? e.className.toString() : '';
      const aria = (e.getAttribute('aria-label') || '') + ' ' + (e.getAttribute('title') || '');
      if (/^(\\[?x\\]?|×|✕|✖|✗|⨯|╳)$/i.test(t)
          || /close|dismiss|cross|exit/i.test(c)
          || /close|dismiss/i.test(aria)) {
        out.push({tag: e.tagName.toLowerCase(), cls: c, id: e.id,
                  text: t.slice(0,15), aria: aria.trim().slice(0,30)});
      }
    });
    return out.slice(0, 30);
  })();
  const msgCandidates = (() => {
    const hits = {};
    document.querySelectorAll('[class]').forEach(e => {
      const c = (e.className && e.className.toString) ? e.className.toString() : '';
      if (/mess|msg|chat-?line|chatmsg|bubble|post|nick|user-?msg/i.test(c)) {
        const key = e.tagName.toLowerCase() + '.' + c.split(/\\s+/).slice(0,3).join('.');
        if (!hits[key]) hits[key] = {count:0, sample:''};
        hits[key].count++;
        if (!hits[key].sample) hits[key].sample = (e.textContent||'').trim().slice(0,70);
      }
    });
    return Object.entries(hits).map(([k,v]) => ({sel:k, count:v.count, sample:v.sample}))
      .sort((a,b)=>b.count-a.count).slice(0,20);
  })();
  return {inputs, selects, buttons, forms, iframes, links, textareas, editables,
          msgCandidates, closeButtons,
          bodyText: (document.body ? document.body.innerText : '').slice(0,1800)};
})()
"""

@app.get("/debug/inspect-fcn")
async def debug_inspect_fcn(url: str = "https://freechatnow.com", login: int = 0,
                            username: str = "TestAlexa99", room: str = "SextChat",
                            gender: str = "f", block: int = 1, wait: int = 5):
    """Provision a Decoda-proxied browser, navigate to the target, and dump the DOM.

    Diagnostic for building the CDP-driven guest-login flow. Captures the main
    page + same-origin iframes, then cleans up the cloud browser. Pass ?url= to
    inspect a different page. Pass ?login=1 to run the REAL _cdp_guest_login()
    (fill + submit the guest form) and report the post-login page — entering a
    room posts nothing, so this validates login mechanics without chatting.
    """
    import httpx, random, traceback
    from app.browser import DECODA_PROXIES

    results = {"target": url}
    proxy = random.choice(DECODA_PROXIES)
    results["proxy_port"] = proxy["port"]
    bid = pw = browser = None
    try:
        # 1. Provision browser with Decoda proxy
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.post(
                "https://api.browser-use.com/api/v3/browsers",
                headers={"X-Browser-Use-API-Key": settings.browser_use_api_key, "Content-Type": "application/json"},
                json={"timeout": 5, "browserScreenWidth": 1280, "browserScreenHeight": 720, "customProxy": proxy},
            )
            results["provision_status"] = resp.status_code
            if resp.status_code != 201:
                results["provision_body"] = resp.text[:300]
                return results
            data = resp.json()
            bid = data["id"]
            cdp_url = data["cdpUrl"]
            results["live_url"] = data.get("liveUrl", "")

        # 2. CDP connect
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(cdp_url.replace("https://", "wss://"), timeout=30000)
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # Ad guard (block=0 disables): allow only top-level nav (login redirect),
        # block ad documents in child iframes (the "I AM 18+" age-gate) + ad
        # sub-resources. Mirrors the production _connect_cdp guard.
        if block:
            async def _ad_guard(route):
                req = route.request
                try:
                    f = req.frame
                    if req.is_navigation_request() and (f is None or f.parent_frame is None):
                        await route.continue_()
                    else:
                        await route.abort()
                except Exception:
                    try:
                        await route.abort()
                    except Exception:
                        pass
            for host in ("12chats.com", "exoclick.com", "popads.net", "doubleclick.net",
                         "popunder", "propellerads", "adsterra", "trafficjunky", "traffic"):
                try:
                    await page.route(f"**{host}**", _ad_guard)
                except Exception:
                    pass

        # 3. Navigate + let JS settle
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            results["goto_error"] = str(e)[:200]
        await page.wait_for_timeout(4500)
        results["landed_url"] = page.url
        try:
            results["title"] = await page.title()
        except Exception:
            results["title"] = ""

        # 3c. Discovery: fill + direct fetch-POST to /api/chat/login (bypass the
        # button's ad onclick), report where it redirects = the real room URL.
        if login == 2:
            slug = room.lower().replace("chat", "").strip() or "sext"
            try:
                await page.goto(f"https://www.freechatnow.com/chat/{slug}/",
                                wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(2000)
                js = """
                async (u) => {
                  const form = document.querySelector("form[action*='chat/login']");
                  if (!form) return {error: 'no form'};
                  const uIn = form.querySelector("input[name=username]"); if(uIn) uIn.value = u;
                  const g = form.querySelector("select[name=gender]"); if(g) g.value = "female";
                  const b = form.querySelector("input[name=birthdate]"); if(b) b.value = "2000-06-15";
                  const c = form.querySelector("input[type=checkbox]"); if(c) c.checked = true;
                  const fd = new FormData(form);
                  const entries = [...fd.entries()].map(e => e[0] + '=' + e[1]);
                  try {
                    const resp = await fetch(form.action, {method: (form.method||'POST'),
                        body: fd, credentials: 'include', redirect: 'follow'});
                    const text = await resp.text();
                    return {action: form.action, entries, status: resp.status,
                            final_url: resp.url, redirected: resp.redirected,
                            looks_like_chat: /textarea|contenteditable|send|room/i.test(text),
                            body: text.slice(0, 900)};
                  } catch(e) { return {action: form.action, entries, fetch_error: String(e)}; }
                }
                """
                results["fetch_login"] = await page.evaluate(js, username)
                results["fetch_login_page_url"] = page.url
            except Exception as e:
                results["fetch_login_error"] = str(e)[:250]

        # 3b. Optionally run login: 1 = real _cdp_guest_login (button click),
        #     3 = native form.submit(), 4 = submit + open Rooms panel/join probe.
        if login in (1, 3, 4):
            if login in (3, 4):
                slug = room.lower().replace("chat", "").strip() or "sext"
                try:
                    await page.goto(f"https://www.freechatnow.com/chat/{slug}/",
                                    wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(2500)
                    await page.evaluate("""(u)=>{
                        const f=document.querySelector("form[action*='chat/login']"); if(!f)return;
                        const x=f.querySelector("input[name=username]"); if(x)x.value=u;
                        const g=f.querySelector("select[name=gender]"); if(g)g.value="female";
                        const b=f.querySelector("input[name=birthdate]"); if(b)b.value="2000-06-15";
                        const c=f.querySelector("input[type=checkbox]"); if(c)c.checked=true;
                        f.submit();
                    }""", username)
                    results["login_ok"] = True
                except Exception as e:
                    results["login_error"] = str(e)[:250]
            else:
                from app.browser import browser_manager as _bm, BotWorker as _BW
                w = _BW({"username": username, "gender": gender, "selected_rooms": [room]})
                w._page = page
                try:
                    results["login_ok"] = await _bm._cdp_guest_login(w)
                except Exception as e:
                    results["login_error"] = str(e)[:250]
            await page.wait_for_timeout(max(1, wait) * 1000)
            # Dismiss tips using the REAL production method (DOM-click based)
            from app.browser import browser_manager as _bm2
            results["tips_dismissed"] = await _bm2._dismiss_overlays(page)
            results["post_login_url"] = page.url
            try:
                results["post_login_title"] = await page.title()
            except Exception:
                pass

            # Enumerate ALL pages/popups in the browser — the real FCN room may
            # have opened in a popup while the main tab got hijacked to an ad.
            all_pages = []
            try:
                for ctx2 in browser.contexts:
                    for pg in ctx2.pages:
                        info = {"url": pg.url}
                        try:
                            info["title"] = await pg.title()
                        except Exception:
                            pass
                        if "freechatnow" in (pg.url or ""):
                            try:
                                info["dom"] = await pg.evaluate(_SNAP_JS)
                            except Exception as e:
                                info["dom_error"] = str(e)[:100]
                        all_pages.append(info)
            except Exception as e:
                results["all_pages_error"] = str(e)[:150]
            results["all_pages"] = all_pages

            # Probe the conversation tab bar (rooms + DMs) for selectors
            try:
                results["tab_probe"] = await page.evaluate("""
                    (() => {
                        const out = {tabClassEls: [], headerHTML: ''};
                        document.querySelectorAll('[class*=tab i], [class*=conversation i], [class*=channel i], [class*=dm i], [class*=pm i]').forEach(e => {
                            const t = (e.textContent || '').trim();
                            if (t && t.length < 30)
                                out.tabClassEls.push({tag: e.tagName.toLowerCase(), cls: (e.className + '').slice(0,75), text: t.slice(0,25)});
                        });
                        for (const el of document.querySelectorAll('button, a, div, span')) {
                            const t = (el.textContent || '').trim();
                            if (t === 'Rooms' || t === 'Leave') {
                                let box = el;
                                for (let i = 0; i < 4 && box.parentElement; i++) box = box.parentElement;
                                out.headerHTML = box.outerHTML.slice(0, 2800);
                                break;
                            }
                        }
                        return out;
                    })()
                """)
            except Exception as e:
                results["tab_probe_error"] = str(e)[:150]

            # Dump the message structure inside .room-messages-container (to refine read_chat)
            try:
                results["msg_structure"] = await page.evaluate("""
                    (() => {
                        const box = document.querySelector('.room-messages-container');
                        if (!box) return {found: false};
                        return {found: true, childCount: box.children.length,
                                sampleHTML: box.innerHTML.slice(-3800)};
                    })()
                """)
            except Exception as e:
                results["msg_structure_error"] = str(e)[:150]

            # login=4: open the "Rooms" panel, dump the room list, join a 2nd room,
            # then dump nav.roomlist (the multi-room/DM tab structure).
            if login == 4:
                try:
                    await page.click("button.join", timeout=8000)
                    await page.wait_for_timeout(2500)
                    results["roomlist_probe"] = await page.evaluate("""
                        (() => {
                            const out = {rooms: [], panelHTML: ''};
                            const panel = document.querySelector('[class*=roomlist i], [class*=room-list i], [class*=rooms-panel i], [class*=roomselect i]');
                            if (panel) out.panelHTML = panel.outerHTML.slice(0, 2600);
                            document.querySelectorAll('a, li, [data-room], [class*=room-item i], [class*=roomlink i]').forEach(e => {
                                const t = (e.textContent || '').trim();
                                const dr = e.getAttribute('data-room') || '';
                                if ((dr || (t && t.length < 26)) && e.children.length < 3)
                                    out.rooms.push({tag: e.tagName.toLowerCase(), cls: (e.className + '').slice(0,50), text: t.slice(0,24), data_room: dr, href: (e.getAttribute('href') || '').slice(0,60)});
                            });
                            return out;
                        })()
                    """)
                    # try to join a different room, then re-dump the tab bar
                    joined = await page.evaluate("""
                        (() => {
                            const cur = location.pathname;
                            const cands = Array.from(document.querySelectorAll('a[href*="/room/"], [data-room]'));
                            for (const el of cands) {
                                const href = el.getAttribute('href') || '';
                                if (href.includes('/room/') && !cur.endsWith(href.split('/room/')[1])) { el.click(); return href; }
                            }
                            return null;
                        })()
                    """)
                    results["join_clicked"] = joined
                    await page.wait_for_timeout(4000)
                    results["tabs_after_join"] = await page.evaluate("""
                        (() => {
                            const navs = document.querySelectorAll('nav.roomlist');
                            return Array.from(navs).map(n => n.outerHTML.slice(0,1500));
                        })()
                    """)
                except Exception as e:
                    results["roomlist_error"] = str(e)[:200]

        # 4. Snapshot main page + same-origin iframes
        results["dom"] = await page.evaluate(_SNAP_JS)
        frames = []
        for fr in page.frames:
            if fr == page.main_frame:
                continue
            try:
                frames.append({"url": fr.url, "dom": await fr.evaluate(_SNAP_JS)})
            except Exception as e:
                frames.append({"url": fr.url, "error": str(e)[:120]})
        results["iframes_content"] = frames

    except Exception as e:
        results["error"] = str(e)[:300]
        results["traceback"] = traceback.format_exc()[-900:]
    finally:
        if browser:
            try: await browser.close()
            except Exception: pass
        if pw:
            try: await pw.stop()
            except Exception: pass
        if bid:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    await client.delete(
                        f"https://api.browser-use.com/api/v3/browsers/{bid}",
                        headers={"X-Browser-Use-API-Key": settings.browser_use_api_key},
                    )
            except Exception:
                pass
    return results

@app.get("/debug/start-trace")
async def debug_start_trace(persona_id: str = ""):
    """Run the real start path (auto_pilot.start) and return the full traceback.

    Diagnoses the 500 on /api/session/start. Stops the bot immediately after so it
    does not chat. Pass ?persona_id= or it uses the first persona.
    """
    import traceback
    out = {}
    try:
        personas = await get_personas()
        if not persona_id:
            persona_id = personas[0]["id"] if personas else ""
        persona = await get_persona(persona_id)
        if not persona:
            return {"error": "persona not found", "persona_id": persona_id}
        for field in ["selected_rooms", "dm_gender_filter", "dm_blocklist"]:
            if isinstance(persona.get(field), str):
                try:
                    persona[field] = json.loads(persona[field])
                except (json.JSONDecodeError, TypeError):
                    persona[field] = []
        out["persona_ok"] = True

        sess = await create_session({
            "id": new_id(), "persona_id": persona_id,
            "username": persona.get("username", ""),
            "room_ids": persona.get("selected_rooms", ["SextChat"]),
            "status": "connecting",
        })
        out["session_created"] = sess["id"]

        worker = await auto_pilot.start(sess["id"], persona)
        out["worker"] = worker.to_dict() if worker else None
        out["start_returned_worker"] = worker is not None

        # Replicate the EXACT real-endpoint post-start steps (the suspected 500)
        if worker:
            await update_session(sess["id"], {
                "status": "active",
                "browser_session_id": worker.browser_id,
                "browser_live_url": worker.live_url,
                "auto_pilot": True,
            })
            out["update_session_ok"] = True
            out["would_return"] = {"session_id": sess["id"], "status": "active",
                                   "live_url": worker.live_url, "auto_pilot": True}

        # Immediately stop so it doesn't chat
        await auto_pilot.stop()
        await browser_manager.stop_session()
        out["stopped"] = True
    except Exception as e:
        out["EXCEPTION"] = f"{type(e).__name__}: {e}"
        out["traceback"] = traceback.format_exc()
        try:
            await auto_pilot.stop()
            await browser_manager.stop_session()
        except Exception:
            pass
    return out

@app.get("/debug/proxy-check")
async def debug_proxy_check(country: str = "", port: int = 0):
    """Provision a Decoda-proxied browser and report the exit IP + geolocation.

    Confirms the proxy works and shows country/ISP/proxy-flag. Pass ?country=us to
    test country-targeted residential (appends '-country-<cc>' to the proxy user,
    the Decodo/Smartproxy format). Pass ?port= to pin a specific gateway port.
    """
    import httpx, random
    from app.browser import DECODA_PROXIES

    proxy = dict(random.choice(DECODA_PROXIES))
    if port:
        proxy["port"] = port
    if country:
        proxy["username"] = f"{proxy['username']}-country-{country.lower()}"
    out = {"proxy_host": proxy["host"], "proxy_port": proxy["port"], "proxy_user": proxy["username"]}
    bid = pw = browser = None
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.post(
                "https://api.browser-use.com/api/v3/browsers",
                headers={"X-Browser-Use-API-Key": settings.browser_use_api_key, "Content-Type": "application/json"},
                json={"timeout": 5, "customProxy": proxy},
            )
            out["provision_status"] = resp.status_code
            if resp.status_code != 201:
                out["provision_body"] = resp.text[:300]
                return out
            data = resp.json()
            bid = data["id"]
            cdp_url = data["cdpUrl"]

        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(cdp_url.replace("https://", "wss://"), timeout=30000)
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto(
                "http://ip-api.com/json/?fields=status,country,countryCode,city,isp,as,mobile,proxy,hosting,query",
                wait_until="domcontentloaded", timeout=30000)
            body = await page.evaluate("() => document.body.innerText")
            try:
                out["ip_info"] = json.loads(body)
            except Exception:
                out["ip_info_raw"] = (body or "")[:400]
            # Bot-detection check: what UA + automation flags does the browser show?
            out["user_agent"] = await page.evaluate("() => navigator.userAgent")
            out["webdriver"] = await page.evaluate("() => navigator.webdriver")
            out["headless_in_ua"] = "Headless" in (out.get("user_agent") or "")
        except Exception as e:
            out["lookup_error"] = str(e)[:200]
    except Exception as e:
        out["error"] = str(e)[:250]
    finally:
        if browser:
            try: await browser.close()
            except Exception: pass
        if pw:
            try: await pw.stop()
            except Exception: pass
        if bid:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    await client.delete(
                        f"https://api.browser-use.com/api/v3/browsers/{bid}",
                        headers={"X-Browser-Use-API-Key": settings.browser_use_api_key})
            except Exception:
                pass
    return out

@app.get("/debug/check-plan")
async def debug_check_plan():
    """Check Browser Use Cloud account plan info."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://api.browser-use.com/api/v3/billing/account",
                headers={"X-Browser-Use-API-Key": settings.browser_use_api_key}
            )
            return {"status": resp.status_code, "body": resp.text[:500]}
    except Exception as e:
        return {"error": str(e)}

@app.get("/debug/check-ip")
async def debug_check_ip():
    """Get the current cloud browser's public IP (for debugging FCN redirects)."""
    if not browser_manager.current_session or not browser_manager.current_session._page:
        return {"error": "no session"}
    try:
        page = browser_manager.current_session._page
        # Fetch IP from a simple service
        ip = await page.evaluate("""fetch('https://api.ipify.org?format=json')
            .then(r => r.json())
            .then(d => d.ip)
            .catch(() => 'fetch_failed')""")
        # Also get the country
        country = await page.evaluate("""fetch('https://ipapi.co/json/')
            .then(r => r.json())
            .then(d => d.country_name + ' / ' + d.city)
            .catch(() => 'unknown')""")
        return {"ip": ip, "location": country, "browser_url": page.url}
    except Exception as e:
        return {"error": str(e)}

@app.get("/debug/page-content")
async def debug_page_content():
    """Get the current page HTML content (for debugging redirect issues)."""
    if browser_manager.current_session and browser_manager.current_session._page:
        try:
            html = await browser_manager.current_session._page.content()
            url = browser_manager.current_session._page.url
            title = await browser_manager.current_session._page.title()
            # Snapshot the page
            snapshot = await browser_manager.current_session._page.evaluate("""(() => {
                const links = Array.from(document.querySelectorAll('a[href]')).slice(0,20).map(a => a.href + ' [' + (a.textContent||'').trim() + ']');
                const buttons = Array.from(document.querySelectorAll('button, input[type=submit], input[type=button]')).slice(0,20).map(b => (b.textContent||b.value||'').trim());
                const meta = Array.from(document.querySelectorAll('meta')).map(m => (m.getAttribute('http-equiv')||'') + '=' + (m.getAttribute('content')||'')).filter(Boolean);
                const scripts = Array.from(document.querySelectorAll('script')).slice(0,5).map(s => (s.textContent||'').slice(0,200)).filter(Boolean);
                return {links, buttons, meta, scripts, bodyText: document.body.innerText.slice(0,1000)};
            })()""")
            return {"url": url, "title": title, "snapshot": snapshot, "html_length": len(html)}
        except Exception as e:
            return {"error": str(e)}
    return {"status": "no_session"}

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
            "browser_id": browser_manager.current_session.browser_id,
            "proxy_ip": browser_manager.current_session.proxy_ip,
            "proxy_location": browser_manager.current_session.proxy_location,
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
    # Single start path: auto_pilot.start() provisions the browser via the
    # orchestrator AND begins the auto-pilot loop. Calling start_bot() a second
    # time here would provision a duplicate cloud browser and orphan the first.
    try:
        worker = await auto_pilot.start(sess["id"], persona)
        if not worker:
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
        "browser_session_id": worker.browser_id,
        "browser_live_url": worker.live_url,
        "auto_pilot": True,
    })

    return {"session_id": sess["id"], "status": "active", "live_url": worker.live_url, "auto_pilot": True}

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
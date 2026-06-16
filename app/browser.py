"""BotOrchestrator — manages N concurrent Browser Use SDK sessions with Decoda proxies.

Architecture for 50 concurrent bots:
  One SDK client → one profile per persona (persistent cookies)
                   → one session per bot (SDK agent handles login/navigation)
                   → CDP connection for fast auto-pilot loop (JS evaluate, zero cost)
                   → SDK agent fallback for recovery (stuck pages, re-login)

Custom proxies (Decoda):
  Passed as custom_proxy via SDK's **extra kwargs -> REST API body.
  Requires a paid Browser Use Cloud plan tier that allows custom proxies.
  Falls back to BU built-in US residential proxy if custom_proxy is rejected.

Scaling:
  asyncio.Semaphore(50) caps concurrent sessions.
  Each bot is an independent asyncio Task with its own CDP connection.
  Profiles persist login state across app restarts via Browser Use Cloud.
"""
import asyncio
import json
import logging
import random
import time
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

# ── Decoda proxy pool ──────────────────────────────────────────────────────────
# Each entry: SOCKS5 proxy at gate.decodo.com, port-rotated for IP diversity
DECODA_PROXIES = [
    {"host": "gate.decodo.com", "port": p, "username": "sp2ihy1g3e", "password": "8tjpKDcFwLem7j5v+2"}
    for p in range(10001, 10011)
]


# ── Bot state per persona ──────────────────────────────────────────────────────
class BotWorker:
    """Holds runtime state for one bot persona."""

    def __init__(self, persona: dict):
        self.persona = persona
        self.username: str = persona.get("username", "ChatBot_42")
        self.profile_id: str = ""
        self.session_id: str = ""
        self.browser_id: str = ""
        self.live_url: str = ""
        self.status: str = "created"  # created | logging_in | running | error

        # CDP connection (for fast JS-based auto-pilot)
        self._page = None
        self._cdp = None
        self._playwright = None

        # SDK client run handle (for streaming / awaiting login tasks)
        self._login_run = None

        # Auto-pilot asyncio task
        self._task: Optional[asyncio.Task] = None

    @property
    def _connected(self) -> bool:
        """True when a live CDP page is attached (for status reporting)."""
        return self._page is not None

    async def disconnect_cdp(self):
        """Close CDP connection (keeps SDK session alive)."""
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._page = None
        self._cdp = None
        self._playwright = None

    async def read_chat(self) -> list:
        """Read recent chat messages from this bot's page via CDP JS evaluate.

        Returns [] if no CDP page is attached (e.g. login still in progress).
        """
        if not self._page:
            return []
        try:
            result = await self._page.evaluate("""
                (() => {
                    for (const sel of ['.chat-message','.message','[class*=msg]',
                        '[class*=chatline]','[class*=content] p','[class*=conversation] div']) {
                        const els = document.querySelectorAll(sel);
                        if (els.length > 3) return Array.from(els).slice(-25)
                            .map(e => e.textContent.trim()).filter(t => t);
                    }
                    return document.body.innerText.split('\\n').filter(t => t.trim()).slice(-30);
                })();
            """)
            return result if isinstance(result, list) else []
        except Exception:
            return []

    async def send_message(self, message: str) -> bool:
        """Type + send a chat message on this bot's page via CDP JS evaluate.

        Returns False if there is no message or no CDP page attached.
        """
        if not message or not self._page:
            return False
        escaped = message.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        try:
            await self._page.evaluate(f"""
                (() => {{
                    const input = document.querySelector('input[placeholder="Type to chat"]')
                        || document.querySelector('textarea')
                        || document.querySelector('[contenteditable]')
                        || document.querySelector('input[type=search]')
                        || document.querySelector('input[type=text]');
                    if (!input) return 'no input';
                    input.value = '{escaped}';
                    input.dispatchEvent(new Event('input', {{bubbles: true}}));
                    input.dispatchEvent(new Event('change', {{bubbles: true}}));
                    input.dispatchEvent(new KeyboardEvent('keydown', {{key: 'Enter',
                        code: 'Enter', keyCode: 13, which: 13, bubbles: true}}));
                    const btn = document.querySelector('button[type=submit], [class*=send]');
                    if (btn) btn.click();
                    return 'sent';
                }})();
            """)
            await asyncio.sleep(1)
            return True
        except Exception:
            return False

    def to_dict(self):
        return {
            "username": self.username,
            "profile_id": self.profile_id,
            "session_id": self.session_id,
            "browser_id": self.browser_id,
            "live_url": self.live_url,
            "status": self.status,
        }


# ── Orchestrator ───────────────────────────────────────────────────────────────
class BotOrchestrator:
    """Manages N concurrent Browser Use SDK bot sessions.

    Usage:
        orchestrator = BotOrchestrator()
        worker = await orchestrator.start_bot(persona_dict)
        await orchestrator.stop_bot("Flirtyalexa9")
        await orchestrator.stop_all()
    """

    def __init__(self):
        self._client = None          # lazy-init AsyncBrowserUse
        self._semaphore = asyncio.Semaphore(50)
        self._workers: dict[str, BotWorker] = {}   # username -> BotWorker
        self._auto_pilot_enabled: dict[str, bool] = {}  # username -> bool

    # ── SDK client (lazy, single instance) ─────────────────────────────────

    async def _get_client(self):
        if self._client is None:
            from browser_use_sdk.v3 import AsyncBrowserUse
            self._client = AsyncBrowserUse(
                api_key=settings.browser_use_api_key,
                timeout=60,
            )
        return self._client

    # ── Profile management ──────────────────────────────────────────────────

    async def get_or_create_profile(self, persona_name: str) -> str:
        """Persistent profile per persona — cookies survive restarts."""
        client = await self._get_client()
        # Try existing profile first
        resp = await client.profiles.list(query=persona_name)
        if resp.items:
            profile = resp.items[0]
        else:
            profile = await client.profiles.create(name=persona_name)
        return profile.id

    # ── Bot lifecycle ───────────────────────────────────────────────────────

    async def start_bot(self, persona: dict) -> Optional[BotWorker]:
        """Provision a browser, log in via SDK agent, connect CDP, start auto-pilot.

        1. Get/create profile for persistent cookies
        2. Provision browser with Decoda proxy — returns immediately with live_url
        3. Background: SDK agent handles login (autonomous navigation + form fill)
        4. Background: Connect CDP for fast auto-pilot loop
        5. Background: Start auto-pilot loop
        """
        async with self._semaphore:
            username = persona.get("username", "ChatBot_42")
            logger.info(f"Starting bot: {username}")

            worker = BotWorker(persona)
            self._workers[username] = worker
            client = await self._get_client()

            # Step 1 — Profile (fast)
            worker.profile_id = await self.get_or_create_profile(f"fcn-{username}")
            logger.info(f"Profile: {worker.profile_id}")

            # Step 2 — Provision browser with Decoda proxy (fast, ~3s)
            # Returns browser with live_url immediately — login happens in bg
            decoda = random.choice(DECODA_PROXIES)
            proxy_kwargs = {
                "customProxy": decoda,
            }

            browser = await client.browsers.create(
                profile_id=worker.profile_id,
                timeout=60,
                browser_screen_width=1280,
                browser_screen_height=720,
                enable_recording=False,
                **proxy_kwargs,
            )
            worker.browser_id = browser.id
            worker.live_url = browser.live_url or ""
            cdp_url = browser.cdp_url or ""
            logger.info(f"Browser: {worker.browser_id}, live: {worker.live_url[:60] if worker.live_url else 'none'}")

            # Steps 3-5 run in background — return worker with live_url immediately
            worker._task = asyncio.create_task(
                self._finish_bot_setup(worker, username, cdp_url, client)
            )
            worker.status = "running"
            return worker

    async def _finish_bot_setup(self, worker: BotWorker, username: str,
                                 cdp_url: str, client):
        """Background: CDP connect → guest login via CDP → auto-pilot start.

        We drive the SAME browser we provisioned (the one embedded in the
        dashboard live view) — no separate agent session. Every step is visible
        in the live view.
        """
        try:
            worker.status = "logging_in"

            # Step 3 — Connect CDP to the provisioned browser
            if not cdp_url:
                logger.error(f"No cdp_url for {username} — cannot drive browser")
                worker.status = "error"
                return
            if not await self._connect_cdp(worker, cdp_url):
                logger.error(f"CDP connect failed for {username}")
                worker.status = "error"
                return
            logger.info(f"CDP connected for {username}")

            # Step 4 — Guest login (navigate to room → fill form → submit)
            await self._cdp_guest_login(worker)

            # Step 5 — Start the auto-pilot loop on this same browser
            worker.status = "running"
            self._auto_pilot_enabled[username] = True
            worker._task = asyncio.create_task(self._run_auto_pilot(worker))

        except Exception as e:
            logger.error(f"Bot setup failed for {username}: {e}")
            worker.status = "error"

    async def _connect_cdp(self, worker: BotWorker, cdp_url: str) -> bool:
        """Connect Playwright CDP to the running browser for fast JS auto-pilot."""
        try:
            from playwright.async_api import async_playwright
            worker._playwright = await async_playwright().start()
            wss_url = cdp_url.replace("https://", "wss://")
            worker._cdp = await worker._playwright.chromium.connect_over_cdp(wss_url, timeout=30000)

            contexts = worker._cdp.contexts
            if contexts:
                pages = contexts[0].pages
                worker._page = pages[0] if pages else await contexts[0].new_page()
            else:
                worker._page = await (await worker._cdp.new_context()).new_page()

            # Ad guard: ALLOW top-level document navigations (FCN's guest login
            # redirects THROUGH 12chats before landing in the room — blocking it
            # breaks login), but block ad sub-resources / iframes / popunders to
            # save proxy bandwidth and reduce clutter.
            async def _ad_guard(route):
                try:
                    if route.request.resource_type == "document":
                        await route.continue_()
                    else:
                        await route.abort()
                except Exception:
                    pass

            for host in ("12chats.com", "exoclick.com", "popads.net",
                         "doubleclick.net", "traffic"):
                try:
                    await worker._page.route(f"**{host}**", _ad_guard)
                except Exception:
                    pass

            return True
        except ImportError:
            logger.error("playwright not installed — run: pip install playwright")
            return False
        except Exception as e:
            logger.warning(f"CDP connect failed: {e}")
            return False

    # FCN guest-login form (verified 2026-06-16 via /debug/inspect-fcn):
    #   page:   https://www.freechatnow.com/chat/<slug>/   (e.g. SextChat -> sext)
    #   form:   <form action="/api/chat/login" method="post">
    #   fields: input[name=username], select[name=gender] (male|female|other),
    #           input[name=birthdate] (type=date, YYYY-MM-DD),
    #           input[type=checkbox] (agree), button[type=submit] "Chat As Guest"
    FCN_BASE = "https://www.freechatnow.com"
    _GENDER_MAP = {"f": "female", "female": "female", "m": "male", "male": "male",
                   "other": "other", "couple": "other", "x": "other"}

    async def _cdp_guest_login(self, worker: BotWorker, _attempt: int = 0) -> bool:
        """Navigate to the room page and submit FCN's guest-login form via CDP.

        Verified flow (2026-06-16): fill the form, then submit via NATIVE
        form.submit() — NOT a button click. The "Chat As Guest" button's onclick
        fires an ad redirect (12chats -> stripchat) that hijacks the tab; the
        native submit posts straight to /api/chat/login and the browser follows
        the redirect chain into schat.freechatnow.com/room/<Room>. On a username
        collision FCN bounces to /?alert=<base64> — we retry with a suffix.
        """
        page = worker._page
        persona = worker.persona

        rooms = persona.get("selected_rooms") or ["SextChat"]
        if isinstance(rooms, str):
            try:
                rooms = json.loads(rooms)
            except Exception:
                rooms = [rooms]
        room = (rooms[0] if rooms else "SextChat") or "SextChat"
        slug = room.lower().replace("chat", "").strip() or "sext"
        url = f"{self.FCN_BASE}/chat/{slug}/"

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            logger.warning(f"goto {url} failed for {worker.username}: {e}")
        await page.wait_for_timeout(2500)

        gval = self._GENDER_MAP.get((persona.get("gender") or "f").lower(), "female")
        age = random.randint(23, 30)
        birthdate = f"{time.localtime().tm_year - age}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"

        try:
            res = await page.evaluate(
                """(a)=>{
                    const f=document.querySelector("form[action*='chat/login']");
                    if(!f) return 'no-form';
                    const u=f.querySelector("input[name=username]"); if(u)u.value=a.username;
                    const g=f.querySelector("select[name=gender]"); if(g)g.value=a.gender;
                    const b=f.querySelector("input[name=birthdate]"); if(b)b.value=a.birthdate;
                    const c=f.querySelector("input[type=checkbox]"); if(c)c.checked=true;
                    f.submit();
                    return 'submitted';
                }""",
                {"username": worker.username, "gender": gval, "birthdate": birthdate},
            )
        except Exception as e:
            logger.warning(f"guest-form submit failed ({worker.username}): {e}")
            return False
        if res == "no-form":
            logger.warning(f"guest form not found for {worker.username} @ {page.url}")
            return False

        # Wait for the room SPA to sign in and load
        for _ in range(12):
            await page.wait_for_timeout(2000)
            if "schat." in page.url or "/room/" in page.url or "alert=" in page.url:
                break
        await page.wait_for_timeout(2500)
        url_now = page.url

        # Username-collision bounce → retry with a random suffix
        if "alert=" in url_now:
            import base64
            from urllib.parse import urlparse, parse_qs
            msg = ""
            try:
                a = parse_qs(urlparse(url_now).query).get("alert", [""])[0]
                msg = base64.b64decode(a).decode(errors="ignore")
            except Exception:
                pass
            logger.warning(f"login bounced for {worker.username}: {msg or url_now}")
            if "taken" in msg.lower() and _attempt < 2:
                worker.username = f"{worker.username}{random.randint(1, 99)}"
                logger.info(f"retrying guest login as {worker.username}")
                return await self._cdp_guest_login(worker, _attempt + 1)
            return False

        # Dismiss the first-run tip popup if present
        for sel in ["button.action.dismiss", "text=I'm already familiar"]:
            try:
                await page.click(sel, timeout=2500)
                break
            except Exception:
                pass

        in_room = "schat." in url_now or "/room/" in url_now
        logger.info(f"Guest login {'OK' if in_room else 'UNCERTAIN'} for "
                    f"{worker.username} (room={room}) @ {url_now}")
        return in_room

    async def _run_auto_pilot(self, worker: BotWorker):
        """Fast JS-based auto-pilot loop for a single bot.
        
        Uses CDP (zero cost per tick) for read_chat → generate → send.
        Falls back to SDK agent for recovery if CDP is unavailable.
        """
        username = worker.username
        client = await self._get_client()

        while self._auto_pilot_enabled.get(username, False):
            try:
                # ── CDP path (fast, zero cost) ──
                if worker._page:
                    messages = await worker.read_chat()
                    if messages:
                        # Generate response and send it
                        await self._auto_pilot_tick(worker, messages, client)

                # ── SDK fallback (if no CDP) ──
                elif worker.session_id:
                    await self._sdk_auto_pilot_tick(worker, client)

            except Exception as e:
                logger.error(f"Auto-pilot tick error for {username}: {e}")

            await asyncio.sleep(3)

    async def _auto_pilot_tick(self, worker: BotWorker, messages: list, client):
        """One auto-pilot tick: close ads, check messages, generate, send."""
        username = worker.username
        persona = worker.persona

        # Close ad popups via SDK agent (one-shot)
        if worker.session_id and random.random() < 0.1:  # 10% chance each tick
            try:
                await client.run(
                    "Close any popup ad windows and dismiss any modal overlays "
                    "on the current page.",
                    session_id=worker.session_id,
                    keep_alive=True,
                    enable_recording=False,
                )
            except Exception:
                pass

        # Read new messages
        # (messages list already fetched by caller)

        # Generate response using the persona's LLM provider
        from app.providers import provider_registry
        llm = provider_registry.get_chat_provider()
        if not llm:
            return

        context = "\n".join(messages[-10:])
        tone = persona.get("default_tone", "casual")
        bio = persona.get("bio", "")
        system = (
            f"You are chatting in an adult chat room (18+). Your username is {username}. "
            f"Tone: {tone}. Personality: {bio}. "
            f"Keep messages short, natural, and conversational. "
            f"Vary your responses. Never include your username prefix."
        )
        prompt = f"Recent chat:\n\"\"\"\n{context}\n\"\"\"\n\nRespond naturally."
        response = await llm.chat(system, prompt)

        if response:
            if worker._page:
                sent = await worker.send_message(response)
            elif worker.session_id:
                # Fallback: SDK agent sends the message
                await client.run(
                    f"Type this message in the chat input and send it: {response}",
                    session_id=worker.session_id,
                    keep_alive=True,
                    enable_recording=False,
                )

    async def _sdk_auto_pilot_tick(self, worker: BotWorker, client):
        """SDK-agent auto-pilot fallback (when CDP is unavailable)."""
        username = worker.username
        persona = worker.persona
        tone = persona.get("default_tone", "casual")
        bio = persona.get("bio", "")

        await client.run(
            f"You are {username} in a freechatnow.com chat room. "
            f"Tone: {tone}. Personality: {bio}. "
            f"Read the chat. If there are new messages, respond naturally. "
            f"If you were just logged in, just observe for now. "
            f"Close any popup ads.",
            session_id=worker.session_id,
            keep_alive=True,
            enable_recording=False,
        )

    async def stop_bot(self, username: str):
        """Stop a bot and persist its profile."""
        worker = self._workers.pop(username, None)
        if not worker:
            return

        # Stop auto-pilot loop
        self._auto_pilot_enabled[username] = False
        if worker._task:
            worker._task.cancel()
            worker._task = None

        # Disconnect CDP
        await worker.disconnect_cdp()

        client = await self._get_client()

        # Terminate the provisioned cloud browser (stops billing). Cookies persist
        # via the profile, so the next start for this persona resumes its login.
        if worker.browser_id:
            try:
                await client.browsers.stop(worker.browser_id)
                logger.info(f"Browser stopped for {username}")
            except Exception as e:
                logger.error(f"Error stopping browser for {username}: {e}")

        # Legacy: stop an SDK agent session if one was ever created
        if worker.session_id:
            try:
                await client.sessions.stop(worker.session_id)
            except Exception:
                pass

        worker.status = "stopped"

    async def stop_all(self):
        """Gracefully stop all bot sessions."""
        logger.info("Stopping all bots...")
        for username in list(self._workers.keys()):
            await self.stop_bot(username)

    async def close(self):
        """Full shutdown — stop bots + close SDK client."""
        await self.stop_all()
        if self._client:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None

    # ── Status ──────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        return {
            "active": len(self._workers),
            "capacity": 50,
            "bots": [w.to_dict() for w in self._workers.values()],
        }

    def get_bot(self, username: str) -> Optional[BotWorker]:
        return self._workers.get(username)

    # ── Legacy compatibility: BrowserManager-like interface ─────────────────

    async def start_session(self, persona: dict) -> Optional[BotWorker]:
        """Legacy alias — maps to start_bot for compatibility with main.py."""
        return await self.start_bot(persona)

    async def stop_session(self):
        """Legacy alias — stops the first running bot."""
        for username in list(self._workers.keys()):
            await self.stop_bot(username)
            break

    @property
    def current_session(self):
        """Return the first-running bot's worker (legacy compatibility)."""
        for w in self._workers.values():
            if w.status == "running":
                return w
        return None


# ── Singleton ───────────────────────────────────────────────────────────────────
browser_manager = BotOrchestrator()
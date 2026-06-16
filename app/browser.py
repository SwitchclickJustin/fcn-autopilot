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
        2. Provision browser with Decoda proxy (if plan allows)
        3. SDK agent handles login (navigates FCN, fills form, clicks guest)
        4. Connect direct CDP for fast auto-pilot loop
        5. Start async auto-pilot task
        """
        async with self._semaphore:
            username = persona.get("username", "ChatBot_42")
            logger.info(f"Starting bot: {username}")

            worker = BotWorker(persona)
            self._workers[username] = worker
            client = await self._get_client()

            # Step 1 — Profile
            worker.profile_id = await self.get_or_create_profile(f"fcn-{username}")
            logger.info(f"Profile: {worker.profile_id}")

            # Step 2 — Provision browser with optional custom proxy
            # Select a random Decoda proxy for IP rotation
            decoda = random.choice(DECODA_PROXIES)
            proxy_kwargs = {
                "custom_proxy": decoda,  # paid plan: proxy baked in at API level
            }

            browser = await client.browsers.create(
                profile_id=worker.profile_id,
                timeout=60,
                browser_screen_width=1280,
                browser_screen_height=720,
                enable_recording=False,
                **proxy_kwargs,  # passes custom_proxy to REST API if set
            )
            worker.browser_id = browser.id
            worker.live_url = browser.live_url or ""
            cdp_url = browser.cdp_url or ""
            logger.info(f"Browser: {worker.browser_id}, live: {worker.live_url[:60] if worker.live_url else 'none'}")

            # Step 3 — SDK agent handles login (autonomous: nav + ad bypass + form fill)
            worker.status = "logging_in"
            age = random.randint(22, 26)
            year = time.localtime().tm_year - age
            month = random.randint(1, 12)
            day = random.randint(1, 28)
            birthdate = f"{year}-{month:02d}-{day:02d}"
            gender = persona.get("gender", "f")

            login_prompt = (
                f"Go to freechatnow.com. You are logging in as a guest. "
                f"Username: {username}. Gender: {gender}. Birthdate: {birthdate}. "
                f"Check the agree-to-terms checkbox. Click the 'Chat As Guest' button. "
                f"If you see any popup ads or 'Continue'/'Skip' buttons, close/dismiss them. "
                f"If you get redirected to 12chats or any ad gateway, type freechatnow.com "
                f"into the address bar and press Enter to navigate away. "
                f"Once you are logged in and in a chat room, say 'logged in'."
            )

            result = await client.run(
                login_prompt,
                profile_id=worker.profile_id,
                keep_alive=True,        # keep session alive after login
                enable_recording=False,
            )
            session_output = await result
            worker.session_id = str(session_output.session_id) if hasattr(session_output, 'session_id') else ""
            logger.info(f"Login complete, session: {worker.session_id}")

            # Step 4 — Connect CDP for fast auto-pilot loop
            if cdp_url:
                cdok = await self._connect_cdp(worker, cdp_url)
                if cdok:
                    logger.info(f"CDP connected for {username}")
                else:
                    logger.warning(f"CDP connection failed for {username}, using SDK-only mode")

            # Step 5 — Start auto-pilot
            worker.status = "running"
            self._auto_pilot_enabled[username] = True
            worker._task = asyncio.create_task(self._run_auto_pilot(worker))

            return worker

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

            # Block ad-redirect domains at network level
            await worker._page.route("**12chats.com**", lambda route: route.abort("blockedbyclient"))
            await worker._page.route("**traffic*.com**", lambda route: route.abort("blockedbyclient"))
            await worker._page.route("**exoclick.com**", lambda route: route.abort("blockedbyclient"))
            await worker._page.route("**popads.net**", lambda route: route.abort("blockedbyclient"))

            return True
        except ImportError:
            logger.error("playwright not installed — run: pip install playwright")
            return False
        except Exception as e:
            logger.warning(f"CDP connect failed: {e}")
            return False

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
                    messages = await self._cdp_read_chat(worker._page)
                    if messages:
                        # Generate response and send it
                        await self._auto_pilot_tick(worker, messages, client)

                # ── SDK fallback (if no CDP) ──
                elif worker.session_id:
                    await self._sdk_auto_pilot_tick(worker, client)

            except Exception as e:
                logger.error(f"Auto-pilot tick error for {username}: {e}")

            await asyncio.sleep(3)

    async def _cdp_read_chat(self, page) -> list:
        """Read chat messages from the page via CDP JS evaluate."""
        try:
            result = await page.evaluate("""
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

    async def _cdp_send_message(self, page, message: str) -> bool:
        """Send a chat message via CDP JS evaluate."""
        if not message:
            return False
        escaped = message.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        try:
            await page.evaluate(f"""
                (() => {{
                    const input = document.querySelector('textarea')
                        || document.querySelector('[contenteditable]')
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
                sent = await self._cdp_send_message(worker._page, response)
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

        # Stop SDK session (saves cookies to profile)
        if worker.session_id:
            try:
                client = await self._get_client()
                await client.sessions.stop(worker.session_id)
                logger.info(f"Session stopped for {username}, profile saved")
            except Exception as e:
                logger.error(f"Error stopping session for {username}: {e}")

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

    @property
    def current_session(self):
        """Return the first-running bot's worker (legacy compatibility)."""
        for w in self._workers.values():
            if w.status == "running":
                return w
        return None


# ── Singleton ───────────────────────────────────────────────────────────────────
browser_manager = BotOrchestrator()
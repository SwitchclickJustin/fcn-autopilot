"""Browser Use Cloud v3 driver — provisions remote Chromium via REST API + CDP."""
import asyncio
import json
import logging
import random
import time
from typing import Optional

import httpx
from app.config import settings

logger = logging.getLogger(__name__)

API_BASE = "https://api.browser-use.com/api/v3"

# Decoda residential proxy pool — each entry: host:port:username:password
# Picks a random one each session for IP rotation
DECODA_PROXIES = [
    {"host": "gate.decodo.com", "port": 10001, "username": "sp2ihy1g3e", "password": "8tjpKDcFwLem7j5v+2"},
    {"host": "gate.decodo.com", "port": 10002, "username": "sp2ihy1g3e", "password": "8tjpKDcFwLem7j5v+2"},
    {"host": "gate.decodo.com", "port": 10003, "username": "sp2ihy1g3e", "password": "8tjpKDcFwLem7j5v+2"},
    {"host": "gate.decodo.com", "port": 10004, "username": "sp2ihy1g3e", "password": "8tjpKDcFwLem7j5v+2"},
    {"host": "gate.decodo.com", "port": 10005, "username": "sp2ihy1g3e", "password": "8tjpKDcFwLem7j5v+2"},
    {"host": "gate.decodo.com", "port": 10006, "username": "sp2ihy1g3e", "password": "8tjpKDcFwLem7j5v+2"},
    {"host": "gate.decodo.com", "port": 10007, "username": "sp2ihy1g3e", "password": "8tjpKDcFwLem7j5v+2"},
    {"host": "gate.decodo.com", "port": 10008, "username": "sp2ihy1g3e", "password": "8tjpKDcFwLem7j5v+2"},
    {"host": "gate.decodo.com", "port": 10009, "username": "sp2ihy1g3e", "password": "8tjpKDcFwLem7j5v+2"},
    {"host": "gate.decodo.com", "port": 10010, "username": "sp2ihy1g3e", "password": "8tjpKDcFwLem7j5v+2"},
]


class BrowserSession:
    """Represents a single Browser Use Cloud v3 browser session."""

    def __init__(self, persona: dict):
        self.persona = persona
        self.box_id: str = ""
        self.live_url: str = ""
        self.status: str = "created"
        self._connected: bool = False
        self._page = None
        self._cdp = None
        self._playwright = None

    async def _api(self, method: str, path: str, json_data: dict = None) -> Optional[dict]:
        """Make an API call to Browser Use Cloud v3."""
        api_key = settings.browser_use_api_key
        if not api_key:
            logger.error("BROWSER_USE_API_KEY not set")
            return None

        headers = {"X-Browser-Use-API-Key": api_key}
        if json_data:
            headers["Content-Type"] = "application/json"

        url = f"{API_BASE}/{path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.request(method, url, json=json_data, headers=headers)

                if resp.status_code == 402:
                    logger.error("Browser Use Cloud: insufficient credits")
                    return None

                resp.raise_for_status()
                return resp.json() if resp.text else {}
        except httpx.HTTPStatusError as e:
            logger.error(f"API error {method} {path}: {e.response.status_code} {e.response.text[:200]}")
            return None
        except Exception as e:
            logger.error(f"API call failed {method} {path}: {e}")
            return None

    async def connect(self) -> bool:
        """Provision a cloud browser with Decoda proxy, connect via CDP."""
        logger.info(f"Provisioning cloud browser for {self.persona.get('username')}")

        # Pick a random Decoda proxy from the pool for IP rotation
        decoda = random.choice(DECODA_PROXIES)

        # Create browser — try with Decoda proxy first, fall back to BU residential
        browser_config = {
            "timeout": 60,
            "browserScreenWidth": 1280,
            "browserScreenHeight": 720,
            "enableRecording": False,
        }
        # Try with Decoda proxy
        browser_config["customProxy"] = decoda
        result = await self._api("POST", "browsers", browser_config)
        if not result:
            logger.warning("Decoda proxy rejected — falling back to BU residential proxy")
            browser_config.pop("customProxy")
            browser_config["proxyCountryCode"] = "us"
            result = await self._api("POST", "browsers", browser_config)
        if not result:
            logger.error("Browser API returned None — SDK/proxy setup failed")
            self.status = "error"
            return False
        if not result.get("cdpUrl"):
            logger.error(f"Browser API response missing cdpUrl: {json.dumps(result)[:200]}")
            self.status = "error"
            return False

        self.box_id = result.get("id", "")
        self.live_url = result.get("liveUrl", "")
        cdp_url = result.get("cdpUrl", "")
        logger.info(f"Cloud browser created: {self.box_id}, proxy port {decoda['port']}")

        # Connect via CDP WebSocket using Playwright
        try:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            wss_url = cdp_url.replace("https://", "wss://")
            self._cdp = await self._playwright.chromium.connect_over_cdp(wss_url, timeout=30000)

            # Use the first default context/page or create one
            contexts = self._cdp.contexts
            if contexts:
                pages = contexts[0].pages
                self._page = pages[0] if pages else await contexts[0].new_page()
            else:
                ctx = await self._cdp.new_context()
                self._page = await ctx.new_page()

            # Auto-dismiss dialogs & close popup windows
            self._page.on("dialog", lambda dialog: asyncio.ensure_future(self._handle_dialog(dialog)))
            self._page.on("popup", lambda popup: asyncio.ensure_future(self._close_popup(popup)))

            # Block popups at the JS source — override window.open before any page scripts run
            await self._page.add_init_script("""
                (() => {
                    const origOpen = window.open;
                    window.open = function(url, ...args) {
                        if (url && (url.includes('freechatnow') || url.includes('fcnchat'))) {
                            return origOpen.call(window, url, ...args);
                        }
                        return null;
                    };
                    // Kill any ad layer that appears
                    setInterval(() => {
                        document.querySelectorAll('iframe').forEach(f => {
                            if (f.src && !f.src.includes('freechatnow') && !f.src.includes('fcnchat')) {
                                f.remove();
                            }
                        });
                    }, 2000);
                })();
            """)

            self._connected = True
            self.status = "connected"
            logger.info("CDP connection established")
            return True

        except ImportError:
            logger.error("playwright not installed — run: pip install playwright")
            self.status = "error"
            return False
        except Exception as e:
            logger.error(f"CDP connection failed: {e}")
            self.status = "error"
            return False

    async def _handle_dialog(self, dialog):
        """Auto-dismiss any dialog (alert/confirm/prompt)."""
        try:
            await dialog.dismiss()
        except Exception:
            pass

    async def _close_popup(self, popup):
        """Close any popup window immediately."""
        try:
            await popup.close()
        except Exception:
            pass

    async def _close_ad_windows(self):
        """Close all pages except the FCN chat page."""
        if not self._cdp:
            return
        try:
            for ctx in self._cdp.contexts:
                for page in ctx.pages:
                    url = page.url.lower()
                    if "freechatnow.com" not in url and "chat" not in url:
                        try:
                            await page.close()
                            logger.info(f"Closed ad window: {url[:60]}")
                        except Exception:
                            pass
        except Exception:
            pass

    async def _close_overlays(self):
        """Close any modal overlays via JS."""
        if not self._page:
            return
        try:
            await self._page.evaluate("""
                (() => {
                    const closeSelectors = ['.close','.modal-close','button:has-text("X")',
                        'button:has-text("Close")','button:has-text("No thanks")',
                        'button:has-text("Continue")','a:has-text("Skip")','.popup-close','.ad-close'];
                    closeSelectors.forEach(s => { const el = document.querySelector(s); if(el) el.click(); });
                    document.querySelectorAll('.modal,.overlay,.popup').forEach(el => { el.style.display='none'; });
                })();
            """)
        except Exception:
            pass

    async def login(self) -> bool:
        """Navigate to FCN, fill login form, and click Chat As Guest."""
        if not self._page:
            logger.error("Cannot login: no page")
            return False
        username = self.persona.get("username", "ChatBot_42")
        gender = self.persona.get("gender", "f")
        age = random.randint(22, 26)
        year = time.localtime().tm_year - age
        month = random.randint(1, 12)
        day = random.randint(1, 28)
        birthdate = f"{year}-{month:02d}-{day:02d}"
        try:
            logger.info(f"Navigating to FCN as {username}...")
            await self._page.goto("https://www.freechatnow.com/chat/sextchat", wait_until="domcontentloaded")
            await asyncio.sleep(3)
            await self._close_overlays()
            await self._page.evaluate(f"""document.querySelector('input[name="username"]').value='{username}';
                document.querySelector('input[name="username"]').dispatchEvent(new Event('input',{{bubbles:true}}));""")
            await asyncio.sleep(0.5)
            await self._page.evaluate(f"""document.querySelector('select[name="gender"]').value='{gender}';
                document.querySelector('select[name="gender"]').dispatchEvent(new Event('change',{{bubbles:true}}));""")
            await asyncio.sleep(0.5)
            await self._page.evaluate(f"""document.querySelector('input[name="birthdate"]').value='{birthdate}';
                document.querySelector('input[name="birthdate"]').dispatchEvent(new Event('input',{{bubbles:true}}));""")
            await asyncio.sleep(0.5)
            await self._page.evaluate("""document.querySelector('input[type="checkbox"]').checked=true;
                document.querySelector('input[type="checkbox"]').dispatchEvent(new Event('change',{bubbles:true}));""")
            await asyncio.sleep(0.5)
            await self._page.evaluate("""document.querySelector('button[type="submit"][value="guest"]').click();""")
            await asyncio.sleep(3)
            await self._close_overlays()
            await asyncio.sleep(2)
            self.status = "logged_in"
            logger.info(f"Logged in as {username}")
            return True
        except Exception as e:
            logger.error(f"Login failed: {e}")
            self.status = "error"
            return False

    async def read_chat(self) -> list:
        if not self._page:
            return []
        try:
            result = await self._page.evaluate("""
                (() => {
                    for (const sel of ['.chat-message','.message','[class*=msg]','[class*=chatline]','[class*=content] p','[class*=conversation] div']) {
                        const els = document.querySelectorAll(sel);
                        if (els.length > 3) return Array.from(els).slice(-25).map(e => e.textContent.trim()).filter(t => t);
                    }
                    return document.body.innerText.split('\\n').filter(t => t.trim()).slice(-30);
                })();
            """)
            return result if isinstance(result, list) else []
        except Exception as e:
            logger.error(f"Read chat failed: {e}")
            return []

    async def send_message(self, message: str) -> bool:
        if not self._page or not message:
            return False
        escaped = message.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        try:
            await self._page.evaluate(f"""
                (() => {{
                    const input = document.querySelector('textarea') || document.querySelector('[contenteditable]') || document.querySelector('input[type=text]');
                    if (!input) return 'no input';
                    input.value = '{escaped}';
                    input.dispatchEvent(new Event('input', {{bubbles: true}}));
                    input.dispatchEvent(new Event('change', {{bubbles: true}}));
                    input.dispatchEvent(new KeyboardEvent('keydown', {{key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}}));
                    const btn = document.querySelector('button[type=submit], [class*=send]');
                    if (btn) btn.click();
                    return 'sent';
                }})();
            """)
            await asyncio.sleep(1)
            return True
        except Exception as e:
            logger.error(f"Send message failed: {e}")
            return False

    async def disconnect(self):
        """Destroy the cloud browser."""
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        if self.box_id:
            await self._api("DELETE", f"browsers/{self.box_id}")
        self._connected = False
        self.status = "disconnected"


class BrowserManager:
    """Manages the lifecycle of Browser Use Cloud sessions."""

    def __init__(self):
        self.current_session: Optional[BrowserSession] = None

    async def start_session(self, persona: dict) -> Optional[BrowserSession]:
        if self.current_session:
            await self.current_session.disconnect()
        session = BrowserSession(persona)
        if not await session.connect():
            return None
        if not await session.login():
            await session.disconnect()
            return None
        self.current_session = session
        return session

    async def stop_session(self):
        if self.current_session:
            await self.current_session.disconnect()
            self.current_session = None


browser_manager = BrowserManager()
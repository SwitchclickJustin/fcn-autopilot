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
        """Provision a cloud browser (POST /api/v3/browsers) and connect via CDP."""
        logger.info(f"Provisioning cloud browser for {self.persona.get('username')}")

        # Build browser config
        browser_config = {
            "timeout": 60,                                # 60 min session
            "browserScreenWidth": 1280,
            "browserScreenHeight": 720,
            "enableRecording": False,
        }

        # Always use a random Decoda residential proxy for IP rotation
        decoda = random.choice(DECODA_PROXIES)
        # Use SDK format: custom_proxy (snake_case)
        browser_config["custom_proxy"] = decoda

        # Create the cloud browser
        logger.info("POST browsers with US residential proxy")
        result = await self._api("POST", "browsers", browser_config)
        if not result:
            logger.error(f"Browser API returned None — check BROWSER_USE_API_KEY or proxy config")
            self.status = "error"
            return False
        if not result.get("cdpUrl"):
            logger.error(f"Browser API response missing cdpUrl: {json.dumps(result)[:200]}")
            self.status = "error"
            return False

        self.box_id = result.get("id", "")
        self.live_url = result.get("liveUrl", "")
        cdp_url = result.get("cdpUrl", "")
        logger.info(f"Cloud browser created: {self.box_id}, liveUrl={self.live_url[:80] if self.live_url else 'none'}")

        # Connect via CDP WebSocket using Playwright
        try:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            # CDP URL from API is HTTPS — Playwright needs WebSocket URL
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
        """Auto-dismiss any dialog."""
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
        """Close all tabs/windows except the FCN chat page, using CDP protocol directly."""
        if not self._page:
            return
        try:
            # Use CDP protocol to list ALL targets in the remote browser
            cdp = await self._page.context.new_cdp_session(self._page)
            result = await cdp.send("Target.getTargets")
            targets = result.get("targetInfos", [])
            closed = 0
            for t in targets:
                url = t.get("url", "").lower()
                target_id = t.get("targetId", "")
                if not url or target_id == self._page.context._target_id:
                    continue  # skip our own page
                # Close anything that isn't freechatnow
                if url and "freechatnow.com" not in url and "about:blank" not in url:
                    try:
                        await cdp.send("Target.closeTarget", {"targetId": target_id})
                        closed += 1
                        logger.info(f"CDP closed target: {url[:60]}")
                    except Exception:
                        pass
            if closed:
                logger.info(f"Closed {closed} popup/ad windows via CDP")
        except Exception:
            pass

    async def _close_overlays(self):
        """Close any modal overlays or popup ads via JS on the remote page."""
        if not self._page:
            return
        try:
            await self._page.evaluate("""(() => {
                let closed = 0;
                const closeSelectors = [
                    '.close', '.modal-close', '[class*=close]', '[class*=dismiss]',
                    'button[class*=close]', 'a[class*=close]', '[aria-label*=close]',
                    '[aria-label*=Close]', '.popup-close', '.ad-close',
                    '.overlay-close', '.modal .close',
                ];
                for (const sel of closeSelectors) {
                    for (const el of document.querySelectorAll(sel)) {
                        try { el.click(); closed++; } catch(e) {}
                    }
                }
                const blockers = document.querySelectorAll(
                    '.modal, .overlay, .popup, [class*=modal], [class*=overlay], [class*=popup]'
                );
                blockers.forEach(el => { if (el.style) { el.style.display = 'none'; closed++; } });
                return closed;
            })()""")
        except Exception:
            pass

    async def login(self) -> bool:
        """Navigate to FCN, handle popups, fill login form, join room."""
        if not self._connected or not self._page:
            logger.error("Cannot login: browser not connected")
            return False

        username = self.persona.get("username", "ChatBot_42")
        gender = self.persona.get("gender", "f")
        age = random.randint(22, 26)
        year = time.localtime().tm_year - age
        month = random.randint(1, 12)
        day = random.randint(1, 28)
        birthdate = f"{year}-{month:02d}-{day:02d}"
        room = (self.persona.get("selected_rooms") or ["SextChat"])[0]

        try:
            room_lower = room.lower()
            logger.info(f"Navigating to FCN/{room} as {username}...")
            entry_urls = [
                f"https://www.freechatnow.com/chat/{room_lower}",  # direct (may redirect)
                f"https://m.freechatnow.com/chat/{room_lower}",    # mobile
                "https://www.freechatnow.com/",                    # homepage
            ]

            landed = False

            for url in entry_urls:
                logger.info(f"Trying entry: {url}")
                try:
                    await self._page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    await asyncio.sleep(3)
                    current_url = self._page.url.lower()
                    if "freechatnow.com" in current_url or "fcnchat" in current_url:
                        if "chat" in current_url or url == "https://www.freechatnow.com/":
                            landed = True
                            logger.info(f"Landed on: {current_url}")
                            break
                    
                    # Not on FCN — probably hit a redirect ad (12chats, etc.)
                    logger.warning(f"Hit redirect to: {current_url[:80]}")
                    
                    # Look for a way to proceed: click "Continue", "Skip", or follow redirect
                    proceed = await self._page.evaluate("""(() => {
                        // Find any clickable element that leads to freechatnow
                        const buttons = document.querySelectorAll('a, button, [onclick], [href]');
                        for (const el of buttons) {
                            const txt = (el.textContent || '').toLowerCase().trim();
                            const href = (el.getAttribute('href') || '').toLowerCase();
                            const onclick = (el.getAttribute('onclick') || '').toLowerCase();
                            if (
                                txt.includes('continue') || txt.includes('skip') ||
                                txt.includes('proceed') || txt.includes('enter') ||
                                txt.includes('freechatnow') || txt.includes('fcn') ||
                                href.includes('freechatnow') || href.includes('fcn') ||
                                onclick.includes('freechatnow') || onclick.includes('fcn')
                            ) {
                                return href || 'click';
                            }
                        }
                        // Check if there's a meta refresh or redirect
                        const meta = document.querySelector('meta[http-equiv=refresh]');
                        if (meta) return meta.getAttribute('content') || '';
                        // Check for countdown-then-redirect script
                        const scripts = document.querySelectorAll('script');
                        for (const s of scripts) {
                            const text = (s.textContent || '').toLowerCase();
                            if (text.includes('freechatnow') || text.includes('location')) {
                                return 'js_redirect';
                            }
                        }
                        return '';
                    })()""")
                    
                    if proceed == 'click':
                        logger.info("Found proceed button — clicking it")
                        await self._page.evaluate("""(() => {
                            const buttons = document.querySelectorAll('a, button, [onclick]');
                            for (const el of buttons) {
                                const txt = (el.textContent || '').toLowerCase().trim();
                                if (txt.includes('continue') || txt.includes('skip') ||
                                    txt.includes('proceed') || txt.includes('enter')) {
                                    el.click();
                                    return;
                                }
                            }
                        })()""")
                        await asyncio.sleep(4)
                        current_url = self._page.url.lower()
                        if "freechatnow.com" in current_url:
                            landed = True
                            logger.info(f"Proceed button took us to: {current_url}")
                            break
                    elif proceed:
                        logger.info(f"Found redirect target: {proceed[:60]}")
                        if proceed.startswith('http'):
                            await self._page.goto(proceed, wait_until="domcontentloaded")
                        else:
                            await self._page.evaluate(f"window.location.href = '{proceed}'")
                        await asyncio.sleep(4)
                        current_url = self._page.url.lower()
                        if "freechatnow.com" in current_url:
                            landed = True
                            logger.info(f"Redirect link took us to: {current_url}")
                            break
                    elif not self._page.is_closed():
                        # Try clicking any prominent link/button on the page
                        await self._page.evaluate("""(() => {
                            const els = document.querySelectorAll('a, button');
                            for (const el of els) {
                                const txt = (el.textContent || '').trim();
                                if (txt && txt.length < 30) {
                                    el.click();
                                    return;
                                }
                            }
                        })()""")
                        await asyncio.sleep(4)
                        current_url = self._page.url.lower()
                        if "freechatnow.com" in current_url:
                            landed = True
                            break
                except Exception as e:
                    logger.warning(f"Entry {url} failed: {e}")

            # If still not on FCN, try JS navigation from homepage
            if not landed:
                await self._page.goto("https://www.freechatnow.com/", wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(2)
                current_url = self._page.url.lower()
                if "freechatnow.com" in current_url:
                    # We're on FCN homepage — navigate to room via click or JS
                    await self._page.evaluate(f"window.location.href = '/chat/{room_lower}'")
                    await asyncio.sleep(4)
                    current_url = self._page.url.lower()
                    if "freechatnow.com" in current_url and ("chat" in current_url or room_lower in current_url):
                        landed = True

            if not landed:
                logger.error(f"Could not reach FCN. Current URL: {self._page.url}")
                self.status = "error"
                return False

            # Fill username
            await self._page.evaluate(f"""document.querySelector('input[name="username"]').value = '{username}';
                document.querySelector('input[name="username"]').dispatchEvent(new Event('input', {{bubbles:true}}));""")
            await asyncio.sleep(0.5)

            # Select gender
            await self._page.evaluate(f"""document.querySelector('select[name="gender"]').value = '{gender}';
                document.querySelector('select[name="gender"]').dispatchEvent(new Event('change', {{bubbles:true}}));""")
            await asyncio.sleep(0.5)

            # Set birthdate
            await self._page.evaluate(f"""document.querySelector('input[name="birthdate"]').value = '{birthdate}';
                document.querySelector('input[name="birthdate"]').dispatchEvent(new Event('input', {{bubbles:true}}));""")
            await asyncio.sleep(0.5)

            # Check age checkbox
            await self._page.evaluate("""document.querySelector('input[type="checkbox"]').checked = true;
                document.querySelector('input[type="checkbox"]').dispatchEvent(new Event('change', {bubbles:true}));""")
            await asyncio.sleep(0.5)

            # Close overlays before clicking submit
            await self._close_overlays()
            await asyncio.sleep(0.5)

            # Click Chat As Guest
            await self._page.evaluate("""document.querySelector('button[type="submit"][value="guest"]').click();""")
            await asyncio.sleep(3)
            await self._close_overlays()
            await asyncio.sleep(2)

            self.status = "logged_in"
            logger.info(f"Logged in as {username} in {room}")
            return True

        except Exception as e:
            logger.error(f"Login failed: {e}")
            self.status = "error"
            return False

    async def read_chat(self) -> list:
        """Extract visible chat messages from the remote page."""
        if not self._connected or not self._page:
            return []
        try:
            result = await self._page.evaluate("""(() => {
                const selectors = [
                    '.chat-message', '.message', '[class*=msg]', '[class*=chatline]',
                    '[class*=line]', '[class*=content] p', '.chat-content div',
                    '#chat-body div', '[class*=conversation] div'
                ];
                for (const sel of selectors) {
                    const els = document.querySelectorAll(sel);
                    if (els.length > 3) {
                        return JSON.stringify(Array.from(els).slice(-25).map(el => el.textContent.trim()).filter(t => t));
                    }
                }
                return JSON.stringify(document.body.innerText.split('\\n').filter(t => t.trim()).slice(-30));
            })()""")
            return json.loads(result) if result else []
        except Exception as e:
            logger.error(f"read_chat failed: {e}")
            return []

    async def send_message(self, message: str) -> bool:
        """Type a message into the chat input and send it."""
        if not self._connected or not self._page or not message:
            return False
        try:
            escaped = message.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
            result = await self._page.evaluate(f"""(() => {{
                const input = document.querySelector('textarea') ||
                    document.querySelector('[contenteditable]') ||
                    document.querySelector('input[type=text]');
                if (!input) return 'no input';
                input.value = '{escaped}';
                input.dispatchEvent(new Event('input', {{bubbles: true}}));
                input.dispatchEvent(new Event('change', {{bubbles: true}}));
                input.dispatchEvent(new KeyboardEvent('keydown', {{key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}}));
                const btn = document.querySelector('button[type=submit], [class*=send]');
                if (btn) btn.click();
                return 'sent';
            }})()""")
            await asyncio.sleep(1)
            return result is not None
        except Exception as e:
            logger.error(f"send_message failed: {e}")
            return False

    async def disconnect(self):
        """Destroy the cloud browser and close CDP connection."""
        try:
            if self._page:
                try:
                    await self._page.close()
                except Exception:
                    pass
        except Exception:
            pass

        try:
            if self._cdp:
                await self._cdp.close()
        except Exception:
            pass

        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass

        # Destroy the cloud browser box
        if self.box_id:
            await self._api("DELETE", f"browsers/{self.box_id}")

        self._connected = False
        self._page = None
        self._cdp = None
        self._playwright = None
        self.status = "disconnected"


class BrowserManager:
    """Manages the lifecycle of Browser Use Cloud v3 sessions."""

    def __init__(self):
        self.current_session: Optional[BrowserSession] = None

    async def start_session(self, persona: dict) -> Optional[BrowserSession]:
        """Provision + login in one call."""
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